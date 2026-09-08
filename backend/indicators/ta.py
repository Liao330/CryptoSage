"""
技术指标计算 —— MA / MACD / RSI / BOLL。
基于 pandas + pandas-ta，输入为 K线 DataFrame。
"""

import logging
from typing import Any

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


def _klines_to_dataframe(klines: list[dict]) -> pd.DataFrame:
    """将 K线 dict 列表转为 DataFrame，含必要列。"""
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


def calc_all_indicators(klines: list[dict]) -> dict[str, Any]:
    """计算全部技术指标。

    Returns:
        {
            "ma": {...},
            "macd": {...},
            "rsi": {...},
            "boll": {...},
            "latest_close": float,
            "trend_summary": str,
            "signals": [...],
        }
    """
    try:
        df = _klines_to_dataframe(klines)
        if df.empty:
            return {"error": "K线数据为空"}

        latest_close = float(df["Close"].iloc[-1])

        # MA
        ma = calc_ma(df)

        # MACD
        macd = calc_macd(df)

        # RSI
        rsi = calc_rsi(df)

        # BOLL
        boll = calc_boll(df)

        # 汇总信号
        signals = _summarize_signals(ma, macd, rsi, boll, latest_close)

        return {
            "ma": ma,
            "macd": macd,
            "rsi": rsi,
            "boll": boll,
            "latest_close": latest_close,
            "signals": signals,
            "trend_summary": _build_trend_summary(signals),
        }
    except Exception as e:
        logger.warning("技术指标计算失败: %s", e)
        return {"error": str(e)}


def calc_ma(df: pd.DataFrame) -> dict:
    """计算 EMA 均线。"""
    result = {}
    for period in [7, 25, 50, 200]:
        col_name = f"EMA{period}"
        if len(df) >= period:
            df[col_name] = df["Close"].ewm(span=period, adjust=False).mean()
        else:
            df[col_name] = df["Close"].rolling(window=period).mean()
        latest = float(df[col_name].iloc[-1]) if not pd.isna(df[col_name].iloc[-1]) else None
        result[f"ema_{period}"] = latest
    return result


def calc_macd(df: pd.DataFrame) -> dict:
    """计算 MACD 指标。"""
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line

    latest_macd = float(macd_line.iloc[-1])
    latest_signal = float(signal_line.iloc[-1])
    latest_hist = float(histogram.iloc[-1])

    # 金叉/死叉判断
    prev_hist = float(histogram.iloc[-2]) if len(histogram) > 1 else 0
    crossover = None
    if prev_hist <= 0 and latest_hist > 0:
        crossover = "金叉（看涨信号）"
    elif prev_hist >= 0 and latest_hist < 0:
        crossover = "死叉（看跌信号）"

    return {
        "macd_line": round(latest_macd, 4),
        "signal_line": round(latest_signal, 4),
        "histogram": round(latest_hist, 4),
        "crossover": crossover,
        "trend": "多头排列" if latest_macd > latest_signal else "空头排列",
    }


def calc_rsi(df: pd.DataFrame, period: int = 14) -> dict:
    """计算 RSI 指标。"""
    delta = df["Close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    latest_gain = float(avg_gain.iloc[-1])
    latest_loss = float(avg_loss.iloc[-1])
    if latest_loss == 0:
        latest_rsi = 100.0 if latest_gain > 0 else 50.0
    else:
        rs = latest_gain / latest_loss
        latest_rsi = 100 - (100 / (1 + rs))

    level = "中性"
    if latest_rsi >= 70:
        level = "超买（偏空）"
    elif latest_rsi <= 30:
        level = "超卖（偏多）"
    elif latest_rsi > 50:
        level = "偏强（偏多）"
    elif latest_rsi < 50:
        level = "偏弱（偏空）"

    return {"value": round(latest_rsi, 2), "level": level}


def calc_boll(df: pd.DataFrame, period: int = 20, std_mult: float = 2.0) -> dict:
    """计算布林带。"""
    sma = df["Close"].rolling(window=period).mean()
    std = df["Close"].rolling(window=period).std()
    upper = sma + std_mult * std
    lower = sma - std_mult * std

    latest_upper = float(upper.iloc[-1]) if not pd.isna(upper.iloc[-1]) else None
    latest_mid = float(sma.iloc[-1]) if not pd.isna(sma.iloc[-1]) else None
    latest_lower = float(lower.iloc[-1]) if not pd.isna(lower.iloc[-1]) else None
    latest_close = float(df["Close"].iloc[-1])

    position = "轨道内"
    if latest_upper and latest_close > latest_upper:
        position = "突破上轨（超强/潜在回调）"
    elif latest_lower and latest_close < latest_lower:
        position = "跌破下轨（超弱/潜在反弹）"

    bandwidth = round(((latest_upper - latest_lower) / latest_mid * 100), 2) if latest_upper and latest_lower and latest_mid else None

    return {
        "upper": round(latest_upper, 2) if latest_upper else None,
        "middle": round(latest_mid, 2) if latest_mid else None,
        "lower": round(latest_lower, 2) if latest_lower else None,
        "position": position,
        "bandwidth_pct": bandwidth,
    }


def _summarize_signals(
    ma: dict, macd: dict, rsi: dict, boll: dict, close: float
) -> list[dict]:
    """汇总各指标信号。"""
    signals = []

    # EMA 排列
    ema50 = ma.get("ema_50")
    ema200 = ma.get("ema_200")
    if ema50 and ema200:
        if ema50 > ema200:
            signals.append({"indicator": "EMA 排列", "signal": "bullish", "detail": f"EMA50({ema50:.2f}) > EMA200({ema200:.2f})，多头排列"})
        else:
            signals.append({"indicator": "EMA 排列", "signal": "bearish", "detail": f"EMA50({ema50:.2f}) < EMA200({ema200:.2f})，空头排列"})

    # 价格 vs EMA200
    if ema200:
        if close > ema200:
            signals.append({"indicator": "价格 vs EMA200", "signal": "bullish", "detail": f"价格({close:.2f})站在EMA200({ema200:.2f})上方"})
        else:
            signals.append({"indicator": "价格 vs EMA200", "signal": "bearish", "detail": f"价格({close:.2f})跌破EMA200({ema200:.2f})"})

    # MACD
    if macd.get("crossover"):
        signals.append({"indicator": "MACD", "signal": "bullish" if "金叉" in macd["crossover"] else "bearish", "detail": macd["crossover"]})

    # RSI
    rsi_val = rsi.get("value", 50)
    if rsi_val >= 70:
        signals.append({"indicator": "RSI", "signal": "bearish", "detail": f"RSI={rsi_val}，{rsi['level']}"})
    elif rsi_val <= 30:
        signals.append({"indicator": "RSI", "signal": "bullish", "detail": f"RSI={rsi_val}，{rsi['level']}"})
    else:
        sig = "bullish" if rsi_val > 50 else "bearish"
        signals.append({"indicator": "RSI", "signal": sig, "detail": f"RSI={rsi_val}，{rsi['level']}"})

    # BOLL
    if "上轨" in boll.get("position", ""):
        signals.append({"indicator": "BOLL", "signal": "bearish", "detail": boll["position"]})
    elif "下轨" in boll.get("position", ""):
        signals.append({"indicator": "BOLL", "signal": "bullish", "detail": boll["position"]})
    else:
        signals.append({"indicator": "BOLL", "signal": "neutral", "detail": boll["position"]})

    # Volume: 成交量配合判断（OBV 趋势 + Volume RSI）
    return signals


def calc_volume_indicators(klines: list[dict]) -> dict:
    """计算成交量相关指标：OBV 趋势 + 量价关系。

    需要完整的 K线 DataFrame，建议从 calc_all_indicators 结果中获取。
    """
    try:
        df = _klines_to_dataframe(klines)
        if df.empty or len(df) < 14:
            return {"error": "数据不足"}

        # OBV (On-Balance Volume)
        df["OBV"] = (df["Volume"] * ((df["Close"] > df["Close"].shift(1)).astype(int) * 2 - 1)).cumsum()
        obv_current = float(df["OBV"].iloc[-1])
        obv_ma = float(df["OBV"].rolling(window=20).mean().iloc[-1])
        obv_trend = "上升 (量价配合)" if obv_current > obv_ma else "下降 (量价背离)"

        # Volume RSI (14-period on volume)
        vol_delta = df["Volume"].diff()
        vol_gain = vol_delta.where(vol_delta > 0, 0.0)
        vol_loss = (-vol_delta).where(vol_delta < 0, 0.0)
        avg_vol_gain = vol_gain.ewm(alpha=1/14, adjust=False).mean()
        avg_vol_loss = vol_loss.ewm(alpha=1/14, adjust=False).mean()
        vol_rs = avg_vol_gain / avg_vol_loss.replace(0, float("nan"))
        vol_rsi = float(100 - (100 / (1 + vol_rs.iloc[-1]))) if not pd.isna(vol_rs.iloc[-1]) else 50

        # 近5根K线成交量 vs 近20根均值
        recent_vol = float(df["Volume"].iloc[-5:].mean())
        total_vol = float(df["Volume"].iloc[-20:].mean()) if len(df) >= 20 else float(df["Volume"].mean())
        vol_ratio = round(recent_vol / total_vol, 2) if total_vol > 0 else 1.0

        vol_conclusion = "放量" if vol_ratio > 1.5 else ("缩量" if vol_ratio < 0.5 else "正常")

        return {
            "obv_current": round(obv_current, 2),
            "obv_ma": round(obv_ma, 2),
            "obv_trend": obv_trend,
            "vol_rsi": round(vol_rsi, 2),
            "vol_ratio": vol_ratio,
            "vol_conclusion": vol_conclusion,
        }
    except Exception as e:
        logger.warning("成交量指标计算失败: %s", e)
        return {"error": str(e)}


def _build_trend_summary(signals: list[dict]) -> str:
    """基于信号构建趋势摘要。"""
    bullish_count = sum(1 for s in signals if s["signal"] == "bullish")
    bearish_count = sum(1 for s in signals if s["signal"] == "bearish")
    total = len(signals) or 1
    bull_pct = bullish_count / total

    if bull_pct >= 0.67:
        return "多项指标偏多，短期趋势看涨"
    elif bull_pct <= 0.33:
        return "多项指标偏空，短期趋势看跌"
    else:
        return "指标多空交织，趋势尚不明朗，建议观望等待确认"
