"""
关键价位算法推导 —— 拒绝 LLM 臆造，所有价位来自真实数据计算。
来源：前高前低 (Swing) / 成交量密集区 (POC) / 斐波那契 / 布林带 / EMA / 爆仓密集区。
"""

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def calc_key_levels(
    klines: list[dict],
    liquidation_clusters: list[dict] | None = None,
    bar: str = "4H",
) -> dict[str, Any]:
    """从多来源推导候选关键价位，每个价位附带可验证理由。

    Args:
        klines: K线数据
        liquidation_clusters: 来自衍生品 Agent 的爆仓密集区数据
        bar: K线周期
    Returns:
        {
            "current_price": float,
            "support_levels": [...],
            "resistance_levels": [...],
            "confluence_zones": [...],
        }
    """
    try:
        df = _klines_to_dataframe(klines)
        if df.empty:
            return {"error": "K线数据为空"}

        current_price = float(df["Close"].iloc[-1])
        levels: list[dict] = []

        # 1. 前高前低 (Swing) — 默认 20 根 K线周期
        swing_highs, swing_lows = _find_swing_points(df, window=20)
        for h in swing_highs[-5:]:  # 最近 5 个
            levels.append({
                "level": round(float(h), 2),
                "type": "resistance",
                "source": "前高 (Swing High)",
                "strength": "medium",
            })
        for l in swing_lows[-5:]:
            levels.append({
                "level": round(float(l), 2),
                "type": "support",
                "source": "前低 (Swing Low)",
                "strength": "medium",
            })

        # 2. 成交量密集区 POC (简化版)
        poc = _find_volume_poc(df, bins=20)
        if poc:
            levels.append({
                "level": round(poc, 2),
                "type": "support" if poc < current_price else "resistance",
                "source": "成交量密集区 POC",
                "strength": "strong",
            })

        # 3. 斐波那契回撤
        fib_levels = _calc_fibonacci(df)
        for fib in fib_levels:
            levels.append({
                "level": round(fib["price"], 2),
                "type": fib["type"],
                "source": f"斐波那契 {fib['ratio']}",
                "strength": "medium" if fib["ratio"] in ("0.382", "0.618") else "weak",
            })

        # 4. 布林带
        boll = _calc_boll_levels(df)
        if boll["upper"] and boll["upper"] != current_price:
            levels.append({
                "level": round(boll["upper"], 2),
                "type": "resistance",
                "source": "布林带上轨",
                "strength": "medium",
            })
        if boll["lower"] and boll["lower"] != current_price:
            levels.append({
                "level": round(boll["lower"], 2),
                "type": "support",
                "source": "布林带下轨",
                "strength": "medium",
            })

        # 5. EMA50/EMA200
        for period, name in [(50, "EMA50"), (200, "EMA200")]:
            ema = float(df["Close"].ewm(span=period, adjust=False).mean().iloc[-1])
            if not np.isnan(ema) and ema != current_price:
                levels.append({
                    "level": round(ema, 2),
                    "type": "support" if ema < current_price else "resistance",
                    "source": name,
                    "strength": "strong" if period == 200 else "medium",
                })

        # 6. 爆仓密集区（来自衍生品 Agent）
        if liquidation_clusters:
            for cluster in liquidation_clusters:
                price_range = cluster.get("price_range", "")
                if "-" in price_range:
                    mid_price = (int(price_range.split("-")[0]) + int(price_range.split("-")[1])) / 2
                    levels.append({
                        "level": mid_price,
                        "type": "support" if mid_price < current_price else "resistance",
                        "source": f"爆仓密集区 ({cluster.get('long_count', 0)}多/{cluster.get('short_count', 0)}空)",
                        "strength": "strong" if (cluster.get("long_count", 0) + cluster.get("short_count", 0)) > 10 else "medium",
                    })

        # 合并相近价位 + 计算汇聚度
        merged = _merge_and_calc_confluence(levels, threshold_pct=2.0)
        supports = sorted(
            [m for m in merged if m["type"] == "support" and m["level"] < current_price],
            key=lambda x: x["level"], reverse=True,
        )[:5]
        resistances = sorted(
            [m for m in merged if m["type"] == "resistance" and m["level"] > current_price],
            key=lambda x: x["level"],
        )[:5]

        # 高汇聚区
        confluence = sorted(
            [m for m in merged if m.get("confluence_count", 0) >= 2],
            key=lambda x: x["confluence_count"], reverse=True,
        )[:3]

        return {
            "current_price": current_price,
            "support_levels": supports,
            "resistance_levels": resistances,
            "confluence_zones": confluence,
        }
    except Exception as e:
        logger.warning("关键价位计算失败: %s", e)
        return {"error": str(e)}


def _klines_to_dataframe(klines: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(klines)
    df = df.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    df = df.set_index("ts").sort_index()
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _find_swing_points(df: pd.DataFrame, window: int = 20) -> tuple[list[float], list[float]]:
    """识别摆动高低点。"""
    highs = []
    lows = []
    for i in range(window, len(df) - window):
        slice_high = df["High"].iloc[i - window : i + window + 1]
        slice_low = df["Low"].iloc[i - window : i + window + 1]
        if df["High"].iloc[i] == slice_high.max():
            highs.append(df["High"].iloc[i])
        if df["Low"].iloc[i] == slice_low.min():
            lows.append(df["Low"].iloc[i])
    return highs, lows


def _find_volume_poc(df: pd.DataFrame, bins: int = 20) -> float | None:
    """简化版 Volume Profile POC（成交量最大价格区间）。"""
    price_range = df["Close"].max() - df["Close"].min()
    if price_range == 0:
        return None
    hist, bin_edges = np.histogram(df["Close"], bins=bins, weights=df.get("Volume", np.ones(len(df))))
    poc_idx = np.argmax(hist)
    poc_price = (bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2
    return float(poc_price)


def _calc_fibonacci(df: pd.DataFrame) -> list[dict]:
    """计算斐波那契回撤位。"""
    high = float(df["High"].max())
    low = float(df["Low"].min())
    diff = high - low

    ratios = [("0.236", 0.236), ("0.382", 0.382), ("0.5", 0.5), ("0.618", 0.618), ("0.786", 0.786)]
    current = float(df["Close"].iloc[-1])
    is_uptrend = current > (high + low) / 2

    results = []
    for name, ratio in ratios:
        level = high - diff * ratio if is_uptrend else low + diff * ratio
        results.append({
            "ratio": name,
            "price": round(level, 2),
            "type": "support" if level < current else "resistance",
        })
    return results


def _calc_boll_levels(df: pd.DataFrame, period: int = 20) -> dict:
    """计算布林带上下轨。"""
    sma = df["Close"].rolling(window=period).mean()
    std = df["Close"].rolling(window=period).std()
    return {
        "upper": float(sma.iloc[-1] + 2 * std.iloc[-1]) if not np.isnan(std.iloc[-1]) else None,
        "lower": float(sma.iloc[-1] - 2 * std.iloc[-1]) if not np.isnan(std.iloc[-1]) else None,
    }


def _merge_and_calc_confluence(levels: list[dict], threshold_pct: float = 2.0) -> list[dict]:
    """合并相近的价位，计算多重汇聚度。"""
    if not levels:
        return []

    sorted_levels = sorted(levels, key=lambda x: x["level"])
    merged = []
    current_group = [sorted_levels[0]]
    current_avg = sorted_levels[0]["level"]

    for lv in sorted_levels[1:]:
        if current_avg > 0 and abs(lv["level"] - current_avg) / current_avg * 100 < threshold_pct:
            current_group.append(lv)
            current_avg = sum(l["level"] for l in current_group) / len(current_group)
        else:
            merged.append(_build_confluence_entry(current_group, current_avg))
            current_group = [lv]
            current_avg = lv["level"]

    if current_group:
        merged.append(_build_confluence_entry(current_group, current_avg))

    return merged


def _build_confluence_entry(group: list[dict], avg_price: float) -> dict:
    """构建汇聚条目。"""
    reasons = list(dict.fromkeys(l["source"] for l in group))  # 去重保持顺序
    # 选出现最多的 type
    types = [l["type"] for l in group]
    dominant_type = max(set(types), key=types.count)
    # 取最高强度
    strength_order = {"strong": 3, "medium": 2, "weak": 1}
    best_strength = max(group, key=lambda l: strength_order.get(l["strength"], 0))
    confluence_count = len(group)

    return {
        "level": round(avg_price, 2),
        "type": dominant_type,
        "strength": "strong" if confluence_count >= 3 else ("medium" if confluence_count >= 2 else best_strength["strength"]),
        "reasons": reasons,
        "confluence_count": confluence_count,
    }
