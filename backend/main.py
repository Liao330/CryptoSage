"""
FastAPI 入口 —— REST + WebSocket 过程流。

WebSocket 采用「事件缓冲 + 游标补发」模型解决竞态：
分析任务在 /api/analyze 立即后台启动，而前端拿到 task_id 后才连接 WS，
早期事件（agent_start 等）会先于连接产生。所有事件先写入 task_events 缓冲，
WS 连接后从游标 0 开始补发，之后由事件通知驱动增量发送，保证不丢事件。
"""

import asyncio
import json
import logging
import logging.handlers
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from backend.config import config
from backend.calibration.performance import (
    calculate_shadow_metrics,
    get_agent_drift_report,
    get_agent_performance,
)
from backend.data.db import (
    init_db,
    save_analysis_history,
    get_analysis_history,
    get_analysis_by_task_id,
    get_connection,
)
from backend.agents.graph import get_analysis_graph
from backend.data.kline_repository import kline_repo
from backend.llm.hy3_client import close_client as close_llm_client
from backend.utils.constants import DefaultConfig, EventBuffer

# ── 日志配置：支持文件持久化 ──
_log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
if config.LOG_FILE:
    _fh = logging.handlers.RotatingFileHandler(
        config.LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    _log_handlers.append(_fh)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger(__name__)

app = FastAPI(title="CryptoSage API", version="0.1.0")

# CORS 白名单 —— 从环境变量读取，逗号分隔；默认本地开发端口。
# 注意：本应用无 cookie 认证（纯 REST + WS），allow_credentials 必须为 False，
# 否则与通配来源组合非法且引入 CSRF 面。
_default_origins = "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173,http://127.0.0.1:3000"
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", _default_origins).split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
)

# ── WebSocket 事件缓冲基础设施 ──
# 每个 task_id 的事件按产生顺序追加到 task_events，WS 连接后按游标补发。
# 事件缓冲带上限（防止内存无限膨胀），超出时丢弃最早的事件。
task_events: dict[str, list[dict]] = {}
task_signals: dict[str, asyncio.Event] = {}
task_done: dict[str, bool] = {}
task_event_seq: dict[str, int] = {}
task_handles: dict[str, asyncio.Task] = {}
cleanup_handles: set[asyncio.Task] = set()
shadow_monitor_handle: asyncio.Task | None = None


def _ensure_task(task_id: str) -> None:
    """注册任务的事件缓冲；并发准入由 REST 入口负责。"""
    task_events.setdefault(task_id, [])
    task_signals.setdefault(task_id, asyncio.Event())
    task_done.setdefault(task_id, False)
    task_event_seq.setdefault(task_id, 0)


def _cleanup_task(task_id: str) -> None:
    task_events.pop(task_id, None)
    task_signals.pop(task_id, None)
    task_done.pop(task_id, None)
    task_event_seq.pop(task_id, None)


def _execution_metrics(
    task_id: str,
    state: dict,
    started_at: datetime,
    started_perf: float,
    final_event: bool = False,
) -> dict:
    """Summarize server-side execution separately from frontend log entries."""
    trace = state.get("trace", []) if isinstance(state, dict) else []
    timings = state.get("_execution_timings", []) if isinstance(state, dict) else []
    tool_call_count = sum(
        len(entry.get("tool_calls", []) or [])
        for entry in trace if isinstance(entry, dict)
    )
    direction = str((state.get("synthesis_result") or {}).get("direction") or "neutral")
    confidence = float((state.get("synthesis_result") or {}).get("confidence") or 0.0)
    observe_shadow = direction in {"bullish", "bearish"} and confidence < 0.60
    metrics = {
        "task_id": task_id,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "duration_ms": round((asyncio.get_running_loop().time() - started_perf) * 1000, 1),
        "backend_trace_steps": len(trace),
        "backend_event_count": len(task_events.get(task_id, [])) + (1 if final_event else 0),
        "tool_call_count": tool_call_count,
        "timings": timings,
        "observe_shadow": observe_shadow,
        "shadow_track_status": "scheduled" if observe_shadow else "pending_or_not_directional",
    }
    macro = next(
        (item for item in state.get("evidence_pool", [])
         if isinstance(item, dict) and item.get("agent") == "macro"),
        None,
    )
    fetched_at = ((macro or {}).get("raw_metrics") or {}).get("fetched_at")
    if fetched_at:
        try:
            fetched = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
            completed = datetime.now(timezone.utc)
            lag_ms = max(0.0, (completed - fetched).total_seconds() * 1000)
            metrics["latest_news_fetched_at"] = str(fetched_at)
            metrics["news_fetch_lag_ms"] = round(lag_ms, 1)
            metrics["news_freshness_status"] = "stale_at_completion" if lag_ms > 300_000 else "fresh_at_completion"
        except (TypeError, ValueError):
            metrics["news_freshness_status"] = "unknown"
    return metrics


def _apply_execution_freshness_guard(result: dict, metrics: dict) -> dict:
    """Cap confidence when news was already stale by the time synthesis ended."""
    if metrics.get("news_freshness_status") != "stale_at_completion":
        return result
    result["confidence"] = min(float(result.get("confidence", 0.0) or 0.0), 0.45)
    calibration = result.setdefault("confidence_calibration", {})
    calibration["freshness_penalty"] = "stale_news_at_completion"
    caveats = result.setdefault("caveats", [])
    message = "新闻在最终研判完成时已滞后超过 5 分钟，方向置信度已封顶为 45%"
    if message not in caveats:
        caveats.append(message)
    advice = result.get("position_advice")
    if not isinstance(advice, dict):
        advice = {}
        result["position_advice"] = advice
    advice["action"] = "观望（自动）"
    advice["reason"] = message
    return result


async def _delayed_cleanup(task_id: str, delay: float = 120.0) -> None:
    """任务结束后延迟清理缓冲，给断线重连留出补发窗口。"""
    await asyncio.sleep(delay)
    _cleanup_task(task_id)


def _schedule_cleanup(task_id: str) -> None:
    task = asyncio.create_task(
        _delayed_cleanup(task_id, delay=DefaultConfig.TASK_CLEANUP_DELAY)
    )
    cleanup_handles.add(task)
    task.add_done_callback(cleanup_handles.discard)


@app.on_event("startup")
async def startup():
    global shadow_monitor_handle
    missing = config.validate()
    if missing:
        logger.error("缺少必填配置项: %s，服务将拒绝启动", missing)
        raise RuntimeError(f"缺少必填配置项: {missing}，请在 .env 中配置后重启")
    logger.info("配置校验通过 (provider=%s)", config.LLM_PROVIDER)
    await init_db()
    logger.info("数据库初始化完成")
    shadow_monitor_handle = asyncio.create_task(_shadow_settlement_loop())


@app.on_event("shutdown")
async def shutdown():
    global shadow_monitor_handle
    running = [task for task in task_handles.values() if not task.done()]
    for task in running:
        task.cancel()
    if running:
        await asyncio.gather(*running, return_exceptions=True)
    for task in list(cleanup_handles):
        task.cancel()
    if cleanup_handles:
        await asyncio.gather(*cleanup_handles, return_exceptions=True)
    if shadow_monitor_handle is not None:
        shadow_monitor_handle.cancel()
        await asyncio.gather(shadow_monitor_handle, return_exceptions=True)
        shadow_monitor_handle = None
    await close_llm_client()
    await kline_repo.close()


# ── 健康检查 ──

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    """就绪探针：检查关键依赖。"""
    return {"status": "ready", "provider": config.LLM_PROVIDER}


# ── Models ──

SupportedSymbol = Literal["BTC-USDT", "ETH-USDT"]
SupportedBar = Literal["1m", "5m", "15m", "30m", "1H", "4H", "1D", "1W"]


class AnalyzeRequest(BaseModel):
    symbol: SupportedSymbol = "BTC-USDT"
    query: str = Field(default="分析当前市场", min_length=1, max_length=2000)
    # as-of 回测模式：把分析"时间旅行"到该历史时刻（ISO 8601）。
    # 所有数据源只取该时刻之前的数据，不可回溯的源诚实降级，杜绝未来泄漏。
    as_of: str | None = Field(default=None, max_length=40)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query 不能为空")
        return value

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value: str | None) -> str | None:
        from backend.utils.asof import parse_as_of_ms

        if value in (None, ""):
            return None
        if parse_as_of_ms(value) is None:
            raise ValueError("as_of 必须是合法的 ISO 8601 时间字符串，如 2026-09-01T00:00:00Z")
        return value


class AnalyzeResponse(BaseModel):
    task_id: str
    status: str = "started"


# ── REST API ──

@app.post("/api/analyze", response_model=AnalyzeResponse)
async def start_analysis(req: AnalyzeRequest):
    """启动分析任务，返回 task_id。"""
    running_count = sum(not task.done() for task in task_handles.values())
    if running_count >= config.MAX_CONCURRENT_ANALYSES:
        raise HTTPException(
            status_code=429,
            detail=f"并发分析任务已达上限 ({config.MAX_CONCURRENT_ANALYSES})，请稍后重试",
        )

    task_id = uuid.uuid4().hex
    _ensure_task(task_id)

    task = asyncio.create_task(_run_analysis(task_id, req.symbol, req.query, as_of=req.as_of))
    task_handles[task_id] = task

    def _release_handle(done_task: asyncio.Task) -> None:
        if task_handles.get(task_id) is done_task:
            task_handles.pop(task_id, None)

    task.add_done_callback(_release_handle)

    return AnalyzeResponse(task_id=task_id)


@app.delete("/api/analyze/{task_id}")
async def cancel_analysis(task_id: str):
    """取消分析任务，并向已连接的客户端发布明确终态。"""
    task = task_handles.get(task_id)
    if task is None:
        if task_id in task_done and task_done[task_id]:
            return {"task_id": task_id, "status": "finished"}
        raise HTTPException(status_code=404, detail="任务不存在或已过期")

    if task.done():
        return {"task_id": task_id, "status": "finished"}

    await _push_to_frontend(task_id, "final", {
        "error": "分析已由用户取消",
        "direction": "neutral",
        "confidence": 0.0,
        "key_findings": ["分析已取消，未生成完整研判"],
    })
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return {"task_id": task_id, "status": "cancelled"}


@app.get("/api/klines")
async def get_klines(
    symbol: SupportedSymbol = "BTC-USDT",
    bar: SupportedBar = "4H",
    limit: int = Query(default=200, ge=1, le=1000),
):
    """行情代理。"""
    klines = await kline_repo.get_klines(symbol, bar, limit)
    return {"symbol": symbol, "bar": bar, "count": len(klines), "data": klines}


@app.get("/api/report/{task_id}")
async def get_report(task_id: str):
    """获取最终报告（从事件缓冲中取 final 事件；未完成则提示走 WS）。"""
    events = task_events.get(task_id)
    if events is None:
        durable = await get_analysis_by_task_id(task_id)
        if durable:
            return {
                "task_id": task_id,
                "status": "done",
                "report": durable.get("report", {}),
                "execution_trace": durable.get("execution_trace", []),
                "execution_metrics": durable.get("execution_metrics", {}),
            }
        return {
            "task_id": task_id,
            "status": "expired",
            "message": "任务不存在或未持久化，请重新发起分析请求",
        }
    for ev in reversed(events):
        if ev.get("type") == "final":
            return {"task_id": task_id, "status": "done", "report": ev.get("data")}
    return {"task_id": task_id, "status": "running", "message": "分析进行中，请通过 WebSocket 获取实时进度"}


@app.get("/api/history")
async def get_history(
    symbol: SupportedSymbol | None = None,
    limit: int = Query(default=20, ge=1, le=100),
):
    """查询历史分析记录。"""
    records = await get_analysis_history(symbol=symbol or "", limit=limit)
    return {"count": len(records), "records": records}


@app.get("/api/backtest")
async def get_backtest(
    symbol: SupportedSymbol | None = None,
    limit: int = Query(default=20, ge=1, le=100),
):
    """查询回测追踪记录。"""
    try:
        db = await get_connection()
        try:
            query = "SELECT * FROM backtest_tracks"
            params = []
            if symbol is not None:
                query += " WHERE symbol=?"
                params.append(symbol)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            cursor = await db.execute(query, tuple(params))
            rows = await cursor.fetchall()
            stats = {}
            resolved = [
                dict(r) for r in rows
                if r["status"] == "resolved" and r["direction"] in ("bullish", "bearish")
            ]
            if resolved:
                wins = [r for r in resolved if (r["pnl_pct"] or 0) > 0]
                stats = {
                    "total_resolved": len(resolved),
                    "win_count": len(wins),
                    "win_rate": round(len(wins) / len(resolved) * 100, 1),
                    "avg_pnl_pct": round(
                        sum(r["pnl_pct"] or 0 for r in resolved) / len(resolved), 2
                    ),
                }
            return {"count": len(rows), "records": [dict(r) for r in rows], "stats": stats}
        finally:
            await db.close()
    except Exception as e:
        logger.warning("回测查询失败: %s", e)
        return {"error": str(e)}


@app.get("/api/calibration/agents")
async def get_calibration_agents(symbol: SupportedSymbol | None = None):
    """返回已结算影子交易驱动的 Agent 动态可靠度。"""
    return await get_agent_performance(symbol)


@app.get("/api/monitor/drift")
async def get_drift_monitor(symbol: SupportedSymbol | None = None):
    """比较近期与历史基线，识别 Agent 表现漂移。"""
    return await get_agent_drift_report(symbol)


@app.get("/api/shadow/stats")
async def get_shadow_stats(symbol: SupportedSymbol | None = None):
    """影子交易组合统计；同时保留 executable 与 observe-only 方向预测。"""
    db = await get_connection()
    try:
        query = "SELECT * FROM backtest_tracks"
        params: list[object] = []
        if symbol:
            query += " WHERE symbol=?"
            params.append(symbol)
        query += " ORDER BY datetime(created_at) DESC LIMIT 500"
        cursor = await db.execute(query, tuple(params))
        records = [dict(row) for row in await cursor.fetchall()]
    finally:
        await db.close()
    metrics = calculate_shadow_metrics(records)
    drift = await get_agent_drift_report(symbol)
    performance = await get_agent_performance(symbol)
    return {
        "symbol": symbol or "ALL",
        "tracking_count": sum(1 for record in records if record.get("status") == "tracking"),
        "observe_count": sum(1 for record in records if record.get("track_type") == "observe"),
        "execute_count": sum(1 for record in records if record.get("track_type") != "observe"),
        "metrics": metrics,
        "drift_alerts": drift["alerts"],
        "dynamic_weight_multipliers": {
            agent: metric.get("weight_multiplier", 1.0)
            for agent, metric in performance.get("agents", {}).items()
        },
        "calibration_method": performance.get("method", "resolved_shadow_brier_v1"),
        "settlement_horizon_hours": config.BACKTEST_HORIZON_HOURS,
        "eligibility_policy": "方向性报告均进入影子追踪；置信度 >= 60% 标记 execute，低置信度标记 observe，不产生真实仓位",
        "drift_method": drift.get("method", "recent_vs_baseline_v1"),
        "records": records[:50],
    }


# ── WebSocket ──

@app.websocket("/ws/{task_id}")
async def websocket_endpoint(ws: WebSocket, task_id: str):
    await ws.accept()
    if task_id not in task_events:
        await ws.close(code=4404, reason="任务不存在或已过期")
        return

    try:
        last_seq = max(0, int(ws.query_params.get("after_seq", "0")))
    except ValueError:
        await ws.close(code=4400, reason="after_seq 必须为非负整数")
        return

    normal_close = False
    try:
        while True:
            signal = task_signals[task_id]
            signal.clear()

            events = task_events.get(task_id, [])
            for event in events:
                if event.get("seq", 0) > last_seq:
                    await ws.send_text(json.dumps(event, ensure_ascii=False, default=str))
                    last_seq = event["seq"]

            if task_done.get(task_id) and last_seq >= task_event_seq.get(task_id, 0):
                normal_close = True
                break

            try:
                await asyncio.wait_for(signal.wait(), timeout=DefaultConfig.WS_HEARTBEAT_TIMEOUT)
            except asyncio.TimeoutError:
                # 心跳保活，同时探测连接是否断开
                # 再次检查缓冲区，防止在 wait 期间有最终事件到达
                events_now = task_events.get(task_id, [])
                for event in events_now:
                    if event.get("seq", 0) > last_seq:
                        await ws.send_text(json.dumps(event, ensure_ascii=False, default=str))
                        last_seq = event["seq"]
                if task_done.get(task_id) and last_seq >= task_event_seq.get(task_id, 0):
                    normal_close = True
                    break
                try:
                    await ws.send_text(json.dumps({"type": "ping", "task_id": task_id, "seq": last_seq}))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("[%s] WebSocket 异常: %s", task_id, e)
    finally:
        # 任务正常完成时，显式发送 1000 正常关闭码，避免前端误判为异常断线而触发重连风暴
        if normal_close:
            try:
                await ws.close(code=1000)
            except Exception:
                pass


async def _push_to_frontend(task_id: str, msg_type: str, data: Any):
    """将事件写入缓冲并通知 WS 协程发送（WS 未连接时仅缓冲，待连接后补发）。"""
    _ensure_task(task_id)
    events = task_events[task_id]
    # 事件数上限保护：超出时丢弃最早的非 final 事件
    if len(events) >= EventBuffer.MAX_EVENTS_PER_TASK:
        for i, ev in enumerate(events):
            if ev.get("type") != "final":
                events.pop(i)
                break
    next_seq = task_event_seq[task_id] + 1
    task_event_seq[task_id] = next_seq
    events.append({
        "type": msg_type,
        "data": data,
        "task_id": task_id,
        "seq": next_seq,
    })
    task_signals[task_id].set()


# ── 分析执行 ──

async def _run_analysis(task_id: str, symbol: str, query: str, as_of: str | None = None):
    """后台执行完整的分析流程。as_of 非 None 时为历史回测模式。"""
    logger.info("[%s] 开始分析 %s: %s%s", task_id, symbol, query,
                f"（as-of 回测: {as_of}）" if as_of else "")
    await _push_to_frontend(task_id, "agent_start", {"symbol": symbol, "query": query})

    started_at = datetime.now(timezone.utc)
    started_perf = asyncio.get_running_loop().time()
    initial_state: dict = {
        "query": query,
        "symbol": symbol,
        "as_of": as_of,
        "evidence_pool": [],
        "step": 0,
        "critic_round": 0,
        "orchestrator_decision": {},
        "synthesis_result": None,
        "critic_feedback": [],
        "final_report": None,
        "trace": [],
        "_execution_timings": [],
        "_started_at": started_at.isoformat().replace("+00:00", "Z"),
    }

    last_state: dict = {}
    prev_trace_len = 0

    async def _emit_state(event: Any) -> None:
        """将 LangGraph 的单次状态增量立即转换为前端事件。"""
        nonlocal last_state, prev_trace_len
        current_state = event if isinstance(event, dict) else {}
        last_state = current_state
        trace = current_state.get("trace", [])
        evidence_pool = current_state.get("evidence_pool", [])

        for trace_entry in trace[prev_trace_len:]:
            trace_entry.setdefault(
                "observed_at",
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            node = trace_entry.get("node", "unknown")
            if node == "orchestrator":
                await _push_to_frontend(task_id, "agent_start", {
                    "agent": "orchestrator",
                    "decision": trace_entry.get("decision", {}),
                })
            elif node in ("technical", "onchain", "derivatives", "sentiment", "macro"):
                full_signal = next(
                    (
                        item for item in reversed(evidence_pool)
                        if item.get("agent") == node
                    ),
                    {},
                )
                await _push_to_frontend(task_id, "signal", {
                    "agent": node,
                    "bias": full_signal.get("bias", trace_entry.get("signal_bias")),
                    "score": full_signal.get("score", trace_entry.get("score")),
                    "confidence": full_signal.get("confidence", 0.5),
                    "evidence": full_signal.get("evidence", []) or [],
                    "caveats": full_signal.get("caveats", []) or [],
                    "thinking": trace_entry.get("reasoning", "") or "",
                    "tools": trace_entry.get("tool_calls", []) or [],
                    "data_source": full_signal.get("data_source", ""),
                    "data_quality": full_signal.get("data_quality", ""),
                    "raw_metrics": full_signal.get("raw_metrics", {}),
                    "options_snapshot": full_signal.get("options_snapshot", {}),
                    "news_event_graph": full_signal.get("news_event_graph", {}),
                    "government_event_confirmation": full_signal.get("government_event_confirmation", {}),
                })
            elif node == "synthesis":
                synthesis = dict(current_state.get("synthesis_result") or {})
                synthesis["thinking"] = trace_entry.get("reasoning", "") or ""
                await _push_to_frontend(task_id, "synthesis", synthesis)
            elif node == "critic":
                await _push_to_frontend(task_id, "critic", {
                    "round": trace_entry.get("round"),
                    "has_valid_critique": trace_entry.get("has_valid_critique"),
                    "critiques": trace_entry.get("critiques", []) or [],
                    "thinking": trace_entry.get("reasoning", "") or "",
                })

        prev_trace_len = len(trace)

    try:
        # 整体分析耗时保护：不设常规硬性上限（用户明确要求分析质量优先于速度，
        # 不应因为追求速度而缩短超时导致数据源失败/证据不足）。
        # 仅保留一个非常宽松的兜底上限（30 分钟），防止真正的死循环/挂死无限占用资源；
        # 正常分析（含多轮 Orchestrator + Critic 对抗审查）耗时更长也应放行。
        # 每次 astream 产出的状态都立即推送；last_state 同时用于超时兜底。
        async def _run_stream():
            graph = get_analysis_graph()
            async for event in graph.astream(initial_state, stream_mode="values"):
                await _emit_state(event)

        # 兜底超时，仅防止真正挂死，不用于常规限速。走本地代理（如 CodeBuddy
        # LLM Proxy）时慢思考调用显著变慢，可通过环境变量放宽。
        import os as _os
        _OVERALL_SAFETY_TIMEOUT = float(_os.getenv("OVERALL_SAFETY_TIMEOUT", "1800"))

        try:
            await asyncio.wait_for(_run_stream(), timeout=_OVERALL_SAFETY_TIMEOUT)
        except asyncio.TimeoutError:
            fallback_evidence = last_state.get("evidence_pool", []) if last_state else []
            logger.error(
                "[%s] 分析超过兜底上限（>%ds），使用已采集的 %d 个维度做兜底融合",
                task_id, int(_OVERALL_SAFETY_TIMEOUT), len(fallback_evidence),
            )
            if fallback_evidence:
                from backend.agents.synthesis import _fallback_synthesis
                result = _fallback_synthesis(fallback_evidence, symbol)
                execution_metrics = _execution_metrics(
                    task_id, last_state, started_at, started_perf, final_event=False
                )
                result["execution_metrics"] = execution_metrics
                result = _apply_execution_freshness_guard(result, execution_metrics)
                result["key_findings"] = [
                    f"⚠ 分析异常挂死（>{int(_OVERALL_SAFETY_TIMEOUT/60)}min），基于已采集的 {len(fallback_evidence)} 个维度自动融合（非完整审查结果）",
                ] + result.get("key_findings", [])
                await _push_to_frontend(task_id, "final", result)
                await save_analysis_history(
                    task_id, symbol, result, fallback_evidence,
                    execution_trace=last_state.get("trace", []),
                    execution_metrics=execution_metrics,
                )
            else:
                await _push_to_frontend(task_id, "final", {
                    "error": f"分析异常挂死（>{int(_OVERALL_SAFETY_TIMEOUT/60)}min）且未采集到任何证据，请检查网络环境或数据源可达性",
                    "direction": "neutral",
                    "confidence": 0.0,
                })
            return

        # 图执行完毕后，推送最终研判结果（仅一次）
        result = last_state.get("synthesis_result") if isinstance(last_state, dict) else None
        evidence_pool = last_state.get("evidence_pool", []) if isinstance(last_state, dict) else []
        if result:
            execution_metrics = _execution_metrics(
                task_id, last_state, started_at, started_perf, final_event=True
            )
            result["execution_metrics"] = execution_metrics
            if as_of:
                # as-of 回测模式：标注分析基准时刻；新闻时效守卫与影子回测追踪
                # 均基于"当前时间"结算，对历史回测无意义，跳过（由回测脚本自行判定结果）
                result["as_of"] = as_of
            else:
                result = _apply_execution_freshness_guard(result, execution_metrics)
            persist_tasks = [
                save_analysis_history(
                    task_id, symbol, result, evidence_pool,
                    execution_trace=last_state.get("trace", []),
                    execution_metrics=execution_metrics,
                ),
            ]
            if not as_of:
                persist_tasks.append(
                    _create_backtest_track(task_id, symbol, result, evidence_pool)
                )
            await asyncio.gather(*persist_tasks)
            # 先完成历史/影子记录，再广播 final，保证前端完成态刷新监控时能读到本轮结果。
            await _push_to_frontend(task_id, "final", result)
            logger.info(
                "[%s] 分析完成: %s confidence=%.2f",
                task_id, result.get("direction"), result.get("confidence", 0.0) or 0.0,
            )
        else:
            await _push_to_frontend(task_id, "final", {"error": "分析未产出结果"})
    except asyncio.CancelledError:
        logger.info("[%s] 分析任务已取消", task_id)
        raise
    except Exception as e:
        # exc_info=True 打印完整堆栈，避免下次排查再靠猜测（此前 critic.py 的
        # unhashable dict 异常曾因缺失堆栈而不易定位）。
        logger.error("[%s] 分析失败: %s", task_id, e, exc_info=True)
        # 即使发生未预料的异常，也给出明确的错误态字段（direction/confidence），
        # 避免前端因缺字段而显示裸的 "undefined·置信度0%"，用户无法判断是真失败还是显示问题。
        await _push_to_frontend(task_id, "final", {
            "error": str(e),
            "direction": "error",
            "confidence": 0.0,
            "key_findings": [f"⚠ 分析过程发生异常：{e}"],
        })
    finally:
        if task_id in task_done:
            task_done[task_id] = True
        if task_id in task_signals:
            task_signals[task_id].set()
        _schedule_cleanup(task_id)


def _is_executable_signal(result: dict) -> bool:
    """Return whether a report represents an executable directional decision."""
    direction = str(result.get("direction") or "neutral")
    try:
        confidence = float(result.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    advice = result.get("position_advice")
    action = str(advice.get("action") or "") if isinstance(advice, dict) else ""
    return (
        direction in {"bullish", "bearish"}
        and confidence >= 0.60
        and "观望" not in action
    )


def _is_directional_forecast(result: dict) -> bool:
    """Return true for executable or observe-only directional forecasts."""
    return str(result.get("direction") or "neutral") in {"bullish", "bearish"}


async def _create_backtest_track(
    task_id: str, symbol: str, result: dict, evidence_pool: list[dict]
) -> None:
    """创建回测追踪记录 —— 记录分析时的价格，供后续准确率结算。"""
    if not _is_directional_forecast(result):
        logger.info(
            "[%s] 方向=%s 非方向性结果，不创建影子追踪",
            task_id,
            result.get("direction", "neutral"),
        )
        return
    try:
        rows = await kline_repo.get_klines(symbol, "4H", 1)
        if not rows or rows[-1].get("data_quality") != "real":
            logger.warning("[%s] 缺少真实实时价格，跳过回测追踪", task_id)
            return
        entry_price = float(rows[-1]["close"])
        target_at = (
            datetime.now(timezone.utc) + timedelta(hours=config.BACKTEST_HORIZON_HOURS)
        ).strftime("%Y-%m-%d %H:%M:%S")
        action = str((result.get("position_advice") or {}).get("action", ""))
        confidence = float(result.get("confidence", 0.0) or 0.0)
        track_type = "execute" if _is_executable_signal(result) else "observe"

        db = await get_connection()
        try:
            await db.execute(
                """INSERT OR IGNORE INTO backtest_tracks
                   (task_id, symbol, direction, confidence, entry_price, target_at,
                   direction_score, action, calibration, track_type, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'tracking')""",
                (task_id, symbol, result.get("direction", "neutral"),
                 confidence, entry_price, target_at,
                 result.get("direction_score"),
                 action,
                 json.dumps(result.get("confidence_calibration", {}), ensure_ascii=False),
                 track_type),
            )
            await db.commit()
        finally:
            await db.close()
    except Exception as e:
        logger.warning("[%s] 回测追踪创建失败: %s", task_id, e)


@app.get("/api/backtest/stats")
async def get_backtest_stats(symbol: SupportedSymbol | None = None):
    """获取准确率统计：已结算的追踪记录胜率。"""
    try:
        db_path = config.DB_PATH
        if not os.path.isabs(db_path):
            db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), db_path)
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            q = "SELECT * FROM backtest_tracks WHERE status='resolved'"
            params: tuple = ()
            if symbol is not None:
                q += " AND symbol=?"
                params = (symbol,)
            q += " ORDER BY resolved_at DESC LIMIT 100"
            cursor = await db.execute(q, params)
            rows = await cursor.fetchall()
            records = [
                dict(r) for r in rows
                if r["direction"] in ("bullish", "bearish")
            ]
            # 统计
            wins = [r for r in records if (r["pnl_pct"] or 0) > 0]
            total = len(records)
            return {
                "count": total,
                "observe_count": sum(1 for r in records if r.get("track_type") == "observe"),
                "execute_count": sum(1 for r in records if r.get("track_type") != "observe"),
                "wins": len(wins),
                "win_rate": round(len(wins) / total * 100, 1) if total else 0,
                "avg_pnl_pct": round(sum(r["pnl_pct"] or 0 for r in records) / total, 2) if total else 0,
                "records": records[:20],
            }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/backtest/resolve")
async def resolve_backtest(symbol: SupportedSymbol = "BTC-USDT"):
    """结算已到固定目标时间的回测追踪。

    简单规则（基于 direction + 价格变化）：
    - 看多 + 价格上涨 → 盈；看空 + 价格下跌 → 盈；方向相反 → 亏
    - pnl_pct = 价格变化 %（看多）或 -价格变化 %（看空）
    """
    try:
        db_path = config.DB_PATH
        if not os.path.isabs(db_path):
            db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), db_path)

        # 取当前价
        current_price = None
        try:
            rows = await kline_repo.get_klines(symbol, "4H", 1)
            if rows:
                if rows[-1].get("data_quality") != "real":
                    raise HTTPException(status_code=503, detail="实时行情不可用，拒绝用降级数据结算")
                current_price = float(rows[-1]["close"])
        except Exception:
            pass

        if not current_price:
            return {"error": f"无法获取 {symbol} 当前价格"}

        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM backtest_tracks
                   WHERE status='tracking' AND symbol=?
                     AND target_at IS NOT NULL
                     AND datetime(target_at) <= datetime('now')""",
                (symbol,),
            )
            tracks = await cursor.fetchall()
            resolved = 0
            for t in tracks:
                t = dict(t)
                entry = t.get("entry_price")
                direction = t.get("direction")
                if not entry or not direction:
                    continue
                price_change = (current_price - float(entry)) / float(entry)
                round_trip_cost_pct = config.BACKTEST_ROUND_TRIP_COST_BPS / 100
                if direction == "bullish":
                    pnl = round(price_change * 100 - round_trip_cost_pct, 2)
                elif direction == "bearish":
                    pnl = round(-price_change * 100 - round_trip_cost_pct, 2)
                else:
                    pnl = 0.0
                await db.execute(
                    """UPDATE backtest_tracks
                       SET exit_price=?, pnl_pct=?, status='resolved',
                           resolved_at=datetime('now')
                       WHERE task_id=?""",
                    (current_price, pnl, t["task_id"]),
                )
                resolved += 1
            await db.commit()
        return {
            "symbol": symbol,
            "current_price": current_price,
            "resolved_count": resolved,
            "horizon_hours": config.BACKTEST_HORIZON_HOURS,
            "message": f"已结算 {resolved} 条到期回测记录",
        }
    except HTTPException:
        raise
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/shadow/resolve")
async def resolve_shadow_trades(symbol: SupportedSymbol = "BTC-USDT"):
    """结算到期影子交易；与旧 backtest API 共用同一真实价格结算逻辑。"""
    return await resolve_backtest(symbol)


async def _resolve_due_shadow_trades_once() -> dict[str, dict]:
    """Resolve only symbols that currently have matured tracking records."""
    db = await get_connection()
    try:
        cursor = await db.execute(
            """SELECT DISTINCT symbol FROM backtest_tracks
               WHERE status='tracking'
                 AND target_at IS NOT NULL
                 AND datetime(target_at) <= datetime('now')"""
        )
        symbols = [str(row["symbol"]) for row in await cursor.fetchall()]
    finally:
        await db.close()

    results: dict[str, dict] = {}
    for symbol in symbols:
        if symbol not in {"BTC-USDT", "ETH-USDT"}:
            logger.warning("跳过不受支持的影子交易标的: %s", symbol)
            continue
        results[symbol] = await resolve_backtest(symbol)
    return results


async def _shadow_settlement_loop() -> None:
    """Background lifecycle for matured shadow trades."""
    while True:
        try:
            results = await _resolve_due_shadow_trades_once()
            for symbol, result in results.items():
                logger.info(
                    "影子交易自动结算 %s: resolved=%s",
                    symbol,
                    result.get("resolved_count", 0),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("影子交易自动结算失败: %s", exc)
        await asyncio.sleep(max(30, config.SHADOW_SETTLEMENT_INTERVAL_SECONDS))


frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)
