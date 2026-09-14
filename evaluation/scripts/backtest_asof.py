# -*- coding: utf-8 -*-
"""CryptoSage as-of 历史回测评测。

把分析系统"时间旅行"到指定历史时刻运行完整多 Agent 分析（as_of 模式，
所有数据源只取该时刻之前的数据，杜绝未来泄漏），然后用之后的真实价格
走势作为"标准答案"判定方向是否正确，并汇总准确率指标。

用法：
  # 自动生成过去 6 周、每 7 天一个回测点（BTC，24h 结算窗口）
  python evaluation/scripts/backtest_asof.py --symbol BTC-USDT \
      --days-back 42 --interval-days 7 --horizon-hours 24

  # 指定回测点
  python evaluation/scripts/backtest_asof.py --symbol BTC-USDT \
      --points 2026-08-20T00:00:00Z 2026-08-27T00:00:00Z

判定规则：
  - 方向性预测（bullish/bearish）与 horizon 后价格涨跌符号一致 → 命中
  - neutral / 低置信观望 → 弃权（不计入命中率，单独统计）
  - Brier：P(涨) = confidence（bullish）或 1-confidence（bearish），按实际涨跌结算
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.data.kline_repository import kline_repo  # noqa: E402

DEFAULT_QUERY = "分析当前是否适合进场，给出方向判断与关键价位"


async def price_at(symbol: str, ts_ms: int, bar: str = "1H") -> float | None:
    """该时刻的最新成交价（取 ts 之前最后一根 K 线的收盘）。"""
    rows = await kline_repo.get_klines(symbol, bar, 300, before_ts=ts_ms)
    if not rows:
        return None
    return float(rows[-1]["close"])


async def run_one_analysis(
    api: str, symbol: str, as_of: str, query: str, timeout_s: int
) -> dict:
    """提交一次 as-of 分析并轮询至完成。"""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{api}/api/analyze",
            json={"symbol": symbol, "query": query, "as_of": as_of},
        )
        resp.raise_for_status()
        task_id = resp.json()["task_id"]

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            await asyncio.sleep(20)
            r = await client.get(f"{api}/api/report/{task_id}")
            data = r.json()
            if data.get("status") == "done":
                return {"task_id": task_id, "report": data.get("report", {})}
            if data.get("status") not in ("running",):
                return {"task_id": task_id, "report": {}, "error": json.dumps(data, ensure_ascii=False)[:200]}
        return {"task_id": task_id, "report": {}, "error": "timeout"}


async def main() -> None:
    parser = argparse.ArgumentParser(description="CryptoSage as-of 历史回测评测")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--symbol", default="BTC-USDT")
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--points", nargs="*", help="ISO 8601 回测点列表（与 --days-back 二选一）")
    parser.add_argument("--days-back", type=int, default=42, help="自动回测跨度（天）")
    parser.add_argument("--interval-days", type=int, default=7, help="自动回测间隔（天）")
    parser.add_argument("--horizon-hours", type=int, default=24, help="结算窗口（小时）")
    parser.add_argument("--timeout-min", type=int, default=30, help="单次分析超时（分钟）")
    parser.add_argument("--out", default="evaluation/results/asof_backtest.json")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    if args.points:
        as_of_points = args.points
    else:
        as_of_points = []
        d = args.interval_days
        while d <= args.days_back:
            ts = now - timedelta(days=d)
            as_of_points.append(ts.strftime("%Y-%m-%dT%H:%M:%SZ"))
            d += args.interval_days
        as_of_points.reverse()  # 时间正序

    print(f"as-of 回测: {args.symbol} | {len(as_of_points)} 个回测点 | 结算窗口 {args.horizon_hours}h")
    print(f"回测点: {', '.join(as_of_points)}\n")

    results = []
    for idx, as_of in enumerate(as_of_points, 1):
        as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        as_of_ms = int(as_of_dt.timestamp() * 1000)
        resolve_ms = as_of_ms + args.horizon_hours * 3_600_000

        print(f"[{idx}/{len(as_of_points)}] as_of={as_of} 分析中（约 12-20 分钟）...", flush=True)
        started = time.time()
        outcome = await run_one_analysis(args.api, args.symbol, as_of, args.query, args.timeout_min * 60)
        elapsed = time.time() - started
        report = outcome.get("report", {})
        direction = str(report.get("direction") or "neutral")
        confidence = float(report.get("confidence", 0.0) or 0.0)

        # 结算：as_of 与 as_of+horizon 两时刻价格
        p0 = await price_at(args.symbol, as_of_ms)
        p1 = await price_at(args.symbol, resolve_ms)
        row = {
            "as_of": as_of,
            "direction": direction,
            "confidence": confidence,
            "market_state": str(report.get("market_state") or "")[:80],
            "task_id": outcome.get("task_id"),
            "elapsed_s": round(elapsed, 1),
            "error": outcome.get("error"),
        }
        if p0 is not None and p1 is not None:
            pct = (p1 - p0) / p0 * 100
            actual = "up" if pct > 0 else "down"
            row.update({
                "price_at_asof": p0,
                "price_at_resolve": p1,
                "pct_change": round(pct, 2),
                "actual": actual,
                "hit": (direction == "bullish" and actual == "up")
                       or (direction == "bearish" and actual == "down"),
                "abstain": direction not in ("bullish", "bearish"),
            })
            # Brier（方向性预测）：P(涨) = conf（bullish）或 1-conf（bearish）
            if direction in ("bullish", "bearish"):
                p_up = confidence if direction == "bullish" else 1.0 - confidence
                row["brier"] = round((p_up - (1.0 if actual == "up" else 0.0)) ** 2, 4)
            else:
                row["brier"] = None
        else:
            row["hit"] = None
            row["abstain"] = None
            row["brier"] = None
            row["note"] = "价格结算数据不可得"
        results.append(row)

        mark = "?" if row.get("hit") is None else ("✓" if row["hit"] else "✗")
        print(
            f"      {direction}(conf={confidence:.2f}) vs 实际 {row.get('actual', '?')}"
            f"({row.get('pct_change', '?')}%) → {mark}  [{elapsed/60:.1f}min]",
            flush=True,
        )

    # ── 汇总 ──
    valid = [r for r in results if r.get("hit") is not None]
    directional = [r for r in valid if not r.get("abstain")]
    abstains = [r for r in valid if r.get("abstain")]
    hits = [r for r in directional if r["hit"]]
    briers = [r["brier"] for r in directional if r.get("brier") is not None]

    summary = {
        "symbol": args.symbol,
        "horizon_hours": args.horizon_hours,
        "total_points": len(results),
        "resolved_points": len(valid),
        "directional_calls": len(directional),
        "abstain_calls": len(abstains),
        "directional_coverage": round(len(directional) / len(valid), 3) if valid else None,
        "hit_count": len(hits),
        "hit_rate": round(len(hits) / len(directional), 3) if directional else None,
        "avg_confidence": round(sum(r["confidence"] for r in directional) / len(directional), 3) if directional else None,
        "brier_score": round(sum(briers) / len(briers), 4) if briers else None,
        "generated_at": now.isoformat(),
    }

    print("\n========== 回测汇总 ==========")
    print(f"回测点: {summary['total_points']} | 可结算: {summary['resolved_points']}")
    print(f"方向性预测: {summary['directional_calls']} | 弃权(neutral): {summary['abstain_calls']}"
          f" | 方向覆盖率: {summary['directional_coverage']}")
    print(f"命中: {summary['hit_count']}/{summary['directional_calls']}"
          f" | 命中率: {summary['hit_rate']}")
    print(f"平均置信度: {summary['avg_confidence']} | Brier 分: {summary['brier_score']}"
          f"（越低越好，随机瞎猜 ≈ 0.25）")

    print("\n---------- 明细 ----------")
    for r in results:
        mark = "?" if r.get("hit") is None else ("✓" if r["hit"] else "✗")
        print(f"{r['as_of']}  {r['direction']:8s} conf={r['confidence']:.2f}"
              f"  实际 {str(r.get('actual')):4s} {str(r.get('pct_change')):>6s}%  {mark}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
