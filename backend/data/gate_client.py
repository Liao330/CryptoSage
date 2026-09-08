"""
Gate.io 数据客户端 —— 替代被拦截的 OKX + Binance。
所有接口均从 api.gateio.ws 获取，无状态设计。
"""

import logging
from backend.data.ssl_utils import make_client

logger = logging.getLogger(__name__)

GATE_BASE = "https://api.gateio.ws"


def _funding_interpretation(rate: float) -> str:
    if rate > 0:
        return "多头支付空头（多头拥挤，反向偏空）"
    if rate < 0:
        return "空头支付多头（空头拥挤，反向偏多）"
    return "资金费率为零（中性）"

# 币种映射: 归一化为 Gate 要求的 "BASE_QUOTE" 格式（如 BTC_USDT）。
# 关键修复：此前只做 symbol.replace("-", "_")，但上游 derivatives_client/
# derivatives.py 传入的往往是 Binance 格式（连字符已被去掉，如 "BTCUSDT"），
# 此时 replace("-", "_") 是空操作，最终把 "BTCUSDT" 原样传给 Gate API，
# 触发 400 Bad Request（Gate 要求 "BTC_USDT" 带下划线）。
# 这里同时处理三种输入形态：BTC-USDT（连字符）、BTC_USDT（已是目标格式）、
# BTCUSDT（无分隔符，需按常见计价币后缀智能插入下划线）。
_QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "BUSD", "BTC", "ETH")


def _pair(symbol: str) -> str:
    s = symbol.upper().strip()
    if "_" in s:
        return s
    if "-" in s:
        return s.replace("-", "_")
    for suf in _QUOTE_SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf):
            return f"{s[:-len(suf)]}_{suf}"
    return s


class GateClient:
    """Gate.io 行情 + 衍生品数据客户端。"""

    # ── K线 ──

    async def get_klines(self, symbol: str, bar: str = "4h", limit: int = 200) -> list[dict]:
        """获取 K 线数据。
        Gate 返回: [ts, vol_quote, close, high, low, open, vol_base, ...]
        归一化为: {ts, open, high, low, close, volume}
        """
        pair = _pair(symbol)
        try:
            async with make_client(timeout=15.0) as http:
                resp = await http.get(
                    f"{GATE_BASE}/api/v4/spot/candlesticks",
                    params={"currency_pair": pair, "interval": bar, "limit": min(limit, 1000)},
                )
                resp.raise_for_status()
                candles = resp.json()
        except Exception as e:
            logger.warning("Gate K线拉取失败: %s", e)
            return []

        result = []
        for c in candles:
            ts = int(c[0])
            result.append({
                "symbol": symbol,
                "bar": bar,
                "ts": ts,
                "open": float(c[5]),
                "high": float(c[3]),
                "low": float(c[4]),
                "close": float(c[2]),
                "volume": float(c[1]) if len(c) > 1 else 0.0,  # quote volume
            })
        return result[-limit:] if len(result) > limit else result

    # ── 资金费率 ──

    async def get_funding_rate(self, symbol: str) -> dict:
        """获取当前资金费率。"""
        pair = _pair(symbol)
        try:
            async with make_client(timeout=10.0) as http:
                resp = await http.get(
                    f"{GATE_BASE}/api/v4/futures/usdt/contract_stats",
                    params={"contract": pair, "limit": 1},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning("Gate 资金费率失败: %s", e)
            return {
                "symbol": symbol,
                "error": str(e),
                "source": "gate",
                "data_quality": "degraded",
            }

        if data and len(data) > 0:
            item = data[0]
            rate = float(item.get("funding_rate", 0))
            return {
                "symbol": symbol,
                "funding_rate": rate,
                "funding_rate_pct": round(rate * 100, 4),
                "funding_time": item.get("funding_time"),
                "interpretation": _funding_interpretation(rate),
                "source": "gate",
                "data_quality": "real",
            }
        return {
            "symbol": symbol,
            "error": "无数据",
            "source": "gate",
            "data_quality": "degraded",
        }

    # ── 未平仓合约 (OI) ──

    async def get_open_interest(self, symbol: str, limit: int = 30) -> dict:
        """从 contract_stats 历史数据提取未平仓合约量（OI）变化。

        实测 contract_stats 每条记录都带 open_interest / open_interest_usd 字段
        （币本位张数 + USD 名义价值），且该接口国内网络可正常访问，
        可作为 Binance openInterestHist 在国内不可达时的更精确备源
        （优于 CoinGecko 全市场聚合估算）。
        """
        pair = _pair(symbol)
        try:
            async with make_client(timeout=10.0) as http:
                resp = await http.get(
                    f"{GATE_BASE}/api/v4/futures/usdt/contract_stats",
                    params={"contract": pair, "interval": "1h", "limit": min(limit, 100)},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning("Gate OI 获取失败: %s", e)
            return {"symbol": symbol, "error": str(e), "source": "gate"}

        if not data:
            return {"symbol": symbol, "error": "无数据", "source": "gate"}

        current = float(data[-1].get("open_interest_usd", 0))
        prev = float(data[0].get("open_interest_usd", 0))
        change_pct = round(((current - prev) / prev * 100) if prev else 0, 2)
        return {
            "symbol": symbol,
            "current_oi": current,
            "oi_change_pct": change_pct,
            "oi_trend": "增长" if change_pct > 2 else ("下降" if change_pct < -2 else "持平"),
            "oi_history": [
                {"time": d.get("time"), "oi": float(d.get("open_interest_usd", 0))}
                for d in data
            ],
            "source": "gate",
            "data_quality": "real",
        }

    # ── 爆仓数据 ──

    async def get_liquidations(self, symbol: str, limit: int = 100) -> dict:
        """获取近期强平订单。"""
        pair = _pair(symbol)
        try:
            async with make_client(timeout=15.0) as http:
                resp = await http.get(
                    f"{GATE_BASE}/api/v4/futures/usdt/liq_orders",
                    params={"contract": pair, "limit": min(limit, 100)},
                )
                resp.raise_for_status()
                orders = resp.json()
        except Exception as e:
            logger.warning("Gate 爆仓数据失败: %s", e)
            return {"symbol": symbol, "error": str(e), "source": "gate"}

        long_liq = sum(float(o.get("size", 0)) for o in orders if o.get("side") == "long")
        short_liq = sum(float(o.get("size", 0)) for o in orders if o.get("side") == "short")

        # 爆仓价格聚类
        buckets: dict[int, dict] = {}
        for o in orders:
            price = float(o.get("price", 0))
            if price <= 0:
                continue
            bucket = int(price / 100) * 100
            side = o.get("side", "unknown")
            if bucket not in buckets:
                buckets[bucket] = {"price_range": f"{bucket}-{bucket+100}", "long_count": 0, "short_count": 0}
            if side == "long":
                buckets[bucket]["long_count"] += 1
            elif side == "short":
                buckets[bucket]["short_count"] += 1

        clusters = sorted(buckets.values(), key=lambda x: x["long_count"] + x["short_count"], reverse=True)[:5]

        return {
            "symbol": symbol,
            "total_orders": len(orders),
            "long_liquidation_value": round(long_liq, 2),
            "short_liquidation_value": round(short_liq, 2),
            "dominant_side": "多头爆仓为主（偏空压力区）" if long_liq > short_liq * 1.5
                             else ("空头爆仓为主（偏多磁吸区）" if short_liq > long_liq * 1.5
                             else "多空爆仓均衡"),
            "liquidation_clusters": clusters,
            "source": "gate",
        }


gate_client = GateClient()
