"""
SQLite 数据库初始化 —— K线缓存 + 分析历史 + 回测信号。
"""

import os
import aiosqlite
from backend.config import config

SCHEMA = """
-- K线缓存
CREATE TABLE IF NOT EXISTS klines (
    symbol TEXT NOT NULL,
    bar    TEXT NOT NULL,
    ts     INTEGER NOT NULL,
    open   REAL,
    high   REAL,
    low    REAL,
    close  REAL,
    volume REAL,
    PRIMARY KEY (symbol, bar, ts)
);

CREATE INDEX IF NOT EXISTS idx_klines_lookup ON klines(symbol, bar, ts);

-- 分析历史持久化
CREATE TABLE IF NOT EXISTS analysis_history (
    task_id       TEXT PRIMARY KEY,
    symbol        TEXT NOT NULL,
    direction     TEXT NOT NULL,          -- bullish | bearish | neutral
    confidence    REAL NOT NULL,
    market_state  TEXT,
    risk_assessment TEXT,
    key_findings  TEXT,                   -- JSON array
    key_levels    TEXT,                   -- JSON
    evidence_pool TEXT,                   -- JSON array (各维度信号)
    position_advice TEXT,                 -- JSON
    timeframe_outlook TEXT,               -- JSON: 4H / 24H / 7D
    counterfactuals TEXT,                 -- JSON: 方向翻转条件
    confidence_calibration TEXT,          -- JSON: 校准组成
    final_report   TEXT,                   -- JSON: 完整最终报告（支持服务端回放）
    execution_trace TEXT,                  -- JSON: LangGraph 节点审计
    execution_metrics TEXT,                -- JSON: 时延/工具/事件统计
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    entry_price   REAL                    -- 分析时的价格（用于回测算盈亏）
);

CREATE INDEX IF NOT EXISTS idx_history_symbol ON analysis_history(symbol);
CREATE INDEX IF NOT EXISTS idx_history_created ON analysis_history(created_at);

-- 回测追踪：下游实际走势 vs 分析预测
CREATE TABLE IF NOT EXISTS backtest_tracks (
    task_id       TEXT PRIMARY KEY,
    symbol        TEXT NOT NULL,
    direction     TEXT NOT NULL,
    confidence    REAL,
    entry_price   REAL,
    exit_price    REAL,                  -- 实际退出价
    pnl_pct       REAL,                  -- 盈亏百分比
    status        TEXT DEFAULT 'tracking', -- tracking | resolved | ignored
    created_at    TEXT DEFAULT (datetime('now')),
    target_at     TEXT,                  -- 固定结算时间
    direction_score REAL,                -- 0-100 净方向信号
    action        TEXT,                  -- 轻仓/标准仓等影子执行动作
    calibration   TEXT,                  -- JSON: 创建时校准快照
    track_type    TEXT DEFAULT 'execute', -- execute | observe
    resolved_at   TEXT,
    FOREIGN KEY (task_id) REFERENCES analysis_history(task_id)
);
"""


def _resolve_db_path() -> str:
    """解析数据库路径，确保目录存在。"""
    db_path = config.DB_PATH
    if not os.path.isabs(db_path):
        backend_dir = os.path.dirname(os.path.abspath(__file__))
        db_path = os.path.join(os.path.dirname(backend_dir), db_path)
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    return db_path


async def init_db() -> None:
    """初始化 SQLite 数据库及表结构。"""
    db_path = _resolve_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        cursor = await db.execute("PRAGMA table_info(backtest_tracks)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "target_at" not in columns:
            await db.execute("ALTER TABLE backtest_tracks ADD COLUMN target_at TEXT")
        for column, column_type in {
            "direction_score": "REAL",
            "action": "TEXT",
            "calibration": "TEXT",
            "track_type": "TEXT DEFAULT 'execute'",
        }.items():
            if column not in columns:
                await db.execute(f"ALTER TABLE backtest_tracks ADD COLUMN {column} {column_type}")

        cursor = await db.execute("PRAGMA table_info(analysis_history)")
        history_columns = {row[1] for row in await cursor.fetchall()}
        for column in ("timeframe_outlook", "counterfactuals", "confidence_calibration",
                       "final_report", "execution_trace", "execution_metrics"):
            if column not in history_columns:
                await db.execute(f"ALTER TABLE analysis_history ADD COLUMN {column} TEXT")
        modifier = f"+{config.BACKTEST_HORIZON_HOURS} hours"
        await db.execute(
            """UPDATE backtest_tracks
               SET target_at=datetime(created_at, ?)
               WHERE target_at IS NULL""",
            (modifier,),
        )
        # Backfill a minimal durable report for rows written before final_report
        # existed.  This keeps historical task IDs replayable after the memory
        # event buffer is cleaned, while leaving the original evidence untouched.
        cursor = await db.execute(
            """SELECT task_id, direction, confidence, market_state,
                      risk_assessment, key_findings, key_levels, position_advice,
                      timeframe_outlook, counterfactuals, confidence_calibration
               FROM analysis_history
               WHERE final_report IS NULL"""
        )
        import json
        legacy_rows = await cursor.fetchall()
        for row in legacy_rows:
            def _json(value, fallback):
                try:
                    return json.loads(value) if value else fallback
                except (TypeError, json.JSONDecodeError):
                    return fallback
            report = {
                "direction": row[1],
                "confidence": row[2],
                "market_state": row[3] or "",
                "risk_assessment": row[4] or "",
                "key_findings": _json(row[5], []),
                "key_levels": _json(row[6], {}),
                "position_advice": _json(row[7], {}),
                "timeframe_outlook": _json(row[8], {}),
                "counterfactuals": _json(row[9], []),
                "confidence_calibration": _json(row[10], {}),
                "execution_metrics": {"legacy_record": True},
            }
            await db.execute(
                "UPDATE analysis_history SET final_report=?, execution_metrics=? WHERE task_id=?",
                (json.dumps(report, ensure_ascii=False), json.dumps(report["execution_metrics"]), row[0]),
            )
        # 旧版本曾把中性/低置信度结果也写入交易回测。保留记录用于审计，
        # 但标记 ignored，避免它们污染影子收益和 Agent 动态权重。
        await db.execute(
            """UPDATE backtest_tracks
               SET status='ignored'
               WHERE status='tracking'
                 AND COALESCE(track_type, 'execute')='execute'
                 AND (direction NOT IN ('bullish', 'bearish')
                      OR confidence IS NULL OR confidence < 0.60)"""
        )
        await db.commit()


async def get_connection() -> aiosqlite.Connection:
    """获取数据库连接。"""
    db_path = _resolve_db_path()
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    return db


async def save_analysis_history(
    task_id: str,
    symbol: str,
    result: dict,
    evidence_pool: list[dict],
    entry_price: float | None = None,
    execution_trace: list[dict] | None = None,
    execution_metrics: dict | None = None,
) -> None:
    """持久化分析结果到 SQLite。"""
    import json
    try:
        db_path = _resolve_db_path()
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO analysis_history
                   (task_id, symbol, direction, confidence, market_state,
                   risk_assessment, key_findings, key_levels,
                   evidence_pool, position_advice, timeframe_outlook,
                    counterfactuals, confidence_calibration, final_report,
                    execution_trace, execution_metrics, entry_price)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    symbol,
                    result.get("direction", "neutral"),
                    result.get("confidence", 0.0),
                    result.get("market_state", ""),
                    result.get("risk_assessment", ""),
                    json.dumps(result.get("key_findings", []), ensure_ascii=False),
                    json.dumps(result.get("key_levels", {}), ensure_ascii=False),
                    json.dumps(evidence_pool, ensure_ascii=False, default=str),
                    json.dumps(result.get("position_advice", {}), ensure_ascii=False),
                    json.dumps(result.get("timeframe_outlook", {}), ensure_ascii=False),
                    json.dumps(result.get("counterfactuals", []), ensure_ascii=False),
                    json.dumps(result.get("confidence_calibration", {}), ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False, default=str),
                    json.dumps(execution_trace or [], ensure_ascii=False, default=str),
                    json.dumps(execution_metrics or result.get("execution_metrics", {}), ensure_ascii=False, default=str),
                    entry_price,
                ),
            )
            await db.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("分析历史保存失败: %s", e)


async def get_analysis_history(
    symbol: str = "", limit: int = 20
) -> list[dict]:
    """查询历史分析记录。"""
    try:
        db_path = _resolve_db_path()
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            if symbol:
                cursor = await db.execute(
                    "SELECT * FROM analysis_history WHERE symbol=? ORDER BY created_at DESC LIMIT ?",
                    (symbol, limit),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM analysis_history ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("历史查询失败: %s", e)
        return []


async def get_analysis_by_task_id(task_id: str) -> dict | None:
    """Load a durable final report after the in-memory WS buffer is cleaned up."""
    try:
        db_path = _resolve_db_path()
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM analysis_history WHERE task_id=? LIMIT 1", (task_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            record = dict(row)
            import json
            try:
                record["report"] = json.loads(record.get("final_report") or "{}")
            except (TypeError, json.JSONDecodeError):
                record["report"] = {}
            for field in ("execution_trace", "execution_metrics"):
                try:
                    record[field] = json.loads(record.get(field) or "[]")
                except (TypeError, json.JSONDecodeError):
                    record[field] = [] if field == "execution_trace" else {}
            return record if record["report"] else None
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("分析任务回放读取失败: %s", e)
        return None
