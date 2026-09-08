"""Backtest-driven Agent reliability calibration.

The calibration is intentionally conservative: small samples are shrunk toward
the neutral multiplier 1.0, so a few lucky outcomes cannot dominate synthesis.
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from statistics import pstdev

from backend.data.db import get_connection

logger = logging.getLogger(__name__)

AGENTS = ("technical", "onchain", "derivatives", "sentiment", "macro")


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _prediction_probability(signal: dict) -> float:
    """Return probability of an upward move from an Agent signal."""
    score = max(0.0, min(100.0, _safe_float(signal.get("score"), 50.0)))
    bias = str(signal.get("bias") or "neutral").lower()
    probability = score / 100.0
    if bias == "bullish" and probability < 0.5:
        probability = 1.0 - probability
    elif bias == "bearish" and probability > 0.5:
        probability = 1.0 - probability
    return probability


def _score_samples(samples: list[dict]) -> dict:
    if not samples:
        return {
            "sample_count": 0,
            "accuracy": None,
            "brier_score": None,
            "weight_multiplier": 1.0,
        }
    correct = sum(1 for sample in samples if sample["correct"])
    accuracy = correct / len(samples)
    brier = sum((sample["probability"] - sample["outcome_up"]) ** 2 for sample in samples) / len(samples)

    # Accuracy and probability calibration contribute equally. The result is
    # then shrunk toward 1.0 until enough resolved samples exist.
    probability_skill = 1.0 - min(1.0, brier / 0.50)
    skill = 0.50 * accuracy + 0.50 * probability_skill
    raw_multiplier = 0.70 + 0.60 * skill
    shrinkage = len(samples) / (len(samples) + 12.0)
    multiplier = 1.0 + (raw_multiplier - 1.0) * shrinkage
    return {
        "sample_count": len(samples),
        "accuracy": round(accuracy, 3),
        "brier_score": round(brier, 3),
        "weight_multiplier": round(max(0.70, min(1.30, multiplier)), 3),
    }


async def _load_resolved_agent_samples(symbol: str | None = None, limit: int = 300) -> dict[str, list[dict]]:
    db = await get_connection()
    try:
        query = """
            SELECT b.task_id, b.symbol, b.entry_price, b.exit_price, b.resolved_at,
                   h.evidence_pool
            FROM backtest_tracks b
            JOIN analysis_history h ON h.task_id = b.task_id
            WHERE b.status='resolved'
              AND b.entry_price IS NOT NULL
              AND b.exit_price IS NOT NULL
        """
        params: list[object] = []
        if symbol:
            query += " AND b.symbol=?"
            params.append(symbol)
        query += " ORDER BY datetime(b.resolved_at) DESC LIMIT ?"
        params.append(limit)
        cursor = await db.execute(query, tuple(params))
        rows = await cursor.fetchall()
    finally:
        await db.close()

    samples: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        try:
            evidence = json.loads(row["evidence_pool"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        outcome_up = 1.0 if float(row["exit_price"]) > float(row["entry_price"]) else 0.0
        for item in evidence:
            if not isinstance(item, dict):
                continue
            agent = str(item.get("agent") or "")
            bias = str(item.get("bias") or "neutral").lower()
            if agent not in AGENTS or bias not in {"bullish", "bearish"}:
                continue
            probability = _prediction_probability(item)
            predicted_up = probability >= 0.5
            samples[agent].append({
                "task_id": row["task_id"],
                "resolved_at": row["resolved_at"],
                "probability": probability,
                "outcome_up": outcome_up,
                "correct": predicted_up == bool(outcome_up),
            })
    return samples


async def get_agent_performance(symbol: str | None = None, limit: int = 300) -> dict:
    try:
        samples = await _load_resolved_agent_samples(symbol, limit=limit)
        metrics = {agent: _score_samples(samples.get(agent, [])) for agent in AGENTS}
        return {
            "symbol": symbol or "ALL",
            "method": "resolved_shadow_brier_v1",
            "agents": metrics,
            "total_samples": sum(metric["sample_count"] for metric in metrics.values()),
        }
    except Exception as exc:
        logger.warning("Agent 绩效校准读取失败: %s", exc)
        return {
            "symbol": symbol or "ALL",
            "method": "resolved_shadow_brier_v1",
            "agents": {agent: _score_samples([]) for agent in AGENTS},
            "total_samples": 0,
            "error": str(exc),
        }


async def get_dynamic_weight_multipliers(symbol: str | None = None) -> tuple[dict[str, float], dict]:
    report = await get_agent_performance(symbol)
    multipliers = {
        agent: _safe_float(report["agents"].get(agent, {}).get("weight_multiplier"), 1.0)
        for agent in AGENTS
    }
    return multipliers, report


async def get_agent_drift_report(
    symbol: str | None = None,
    recent_window: int = 10,
    baseline_window: int = 30,
    min_recent: int = 3,
    min_baseline: int = 5,
) -> dict:
    try:
        samples = await _load_resolved_agent_samples(
            symbol,
            limit=max(100, (recent_window + baseline_window) * 5),
        )
    except Exception as exc:
        logger.warning("Agent 漂移读取失败: %s", exc)
        samples = {}

    agents: dict[str, dict] = {}
    alerts: list[dict] = []
    for agent in AGENTS:
        agent_samples = samples.get(agent, [])
        recent = agent_samples[:recent_window]
        baseline = agent_samples[recent_window:recent_window + baseline_window]
        recent_metrics = _score_samples(recent)
        baseline_metrics = _score_samples(baseline)
        status = "insufficient_data"
        accuracy_delta = None
        brier_delta = None
        if len(recent) >= min_recent and len(baseline) >= min_baseline:
            accuracy_delta = round(recent_metrics["accuracy"] - baseline_metrics["accuracy"], 3)
            brier_delta = round(recent_metrics["brier_score"] - baseline_metrics["brier_score"], 3)
            if accuracy_delta <= -0.20 or brier_delta >= 0.12:
                status = "degrading"
            elif accuracy_delta >= 0.15 and brier_delta <= 0:
                status = "improving"
            else:
                status = "stable"
        item = {
            "status": status,
            "recent": recent_metrics,
            "baseline": baseline_metrics,
            "accuracy_delta": accuracy_delta,
            "brier_delta": brier_delta,
        }
        agents[agent] = item
        if status == "degrading":
            alerts.append({
                "agent": agent,
                "severity": "high" if (accuracy_delta or 0) <= -0.30 else "medium",
                "message": f"{agent} 近期准确率相对基线下降 {abs(accuracy_delta or 0):.0%}",
            })
    return {
        "symbol": symbol or "ALL",
        "method": "recent_vs_baseline_v1",
        "recent_window": recent_window,
        "baseline_window": baseline_window,
        "agents": agents,
        "alerts": alerts,
    }


def calculate_shadow_metrics(records: list[dict]) -> dict:
    resolved = [record for record in records if record.get("status") == "resolved" and record.get("pnl_pct") is not None]
    pnls = [_safe_float(record.get("pnl_pct")) for record in resolved]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in reversed(pnls):
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    profit_factor = sum(wins) / abs(sum(losses)) if losses else (math.inf if wins else 0.0)
    volatility = pstdev(pnls) if len(pnls) > 1 else 0.0
    risk_adjusted = (sum(pnls) / len(pnls)) / volatility * math.sqrt(len(pnls)) if volatility else 0.0
    return {
        "resolved_count": len(resolved),
        "win_count": len(wins),
        "win_rate": round(len(wins) / len(resolved) * 100, 1) if resolved else 0.0,
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3) if pnls else 0.0,
        "cumulative_pnl_pct": round(sum(pnls), 3),
        "profit_factor": None if math.isinf(profit_factor) else round(profit_factor, 3),
        "max_drawdown_pct": round(max_drawdown, 3),
        "risk_adjusted_score": round(risk_adjusted, 3),
    }
