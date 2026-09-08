"""
K线仓库 —— Gate.io → OKX → CoinGecko → SQLite 多级缓存。
"""

import logging
import math
import os

import aiosqlite

from backend.config import config
from backend.data.ssl_utils import make_client


def _db_path() -> str:
    """解析数据库路径。"""
    p = config.DB_PATH
    if not os.path.isabs(p):
        backend_dir = os.path.dirname(os.path.abspath(__file__))
        p = os.path.join(os.path.dirname(backend_dir), p)
    return p

logger = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"

# OKX bar 映射
BAR_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1H": "1H", "4H": "4H", "1D": "1D", "1W": "1W",
}
# Gate.io bar 映射 (小写)
GATE_BAR_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1H": "1h", "4H": "4h", "1D": "1d", "1W": "1w",
}
BAR_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1H": 3600, "4H": 14400, "1D": 86400, "1W": 604800,
}


def aggregate_price_samples(
    symbol: str,
    bar: str,
    prices: list[list[float]],
    volumes: list[list[float]],
) -> list[dict]:
    """Causally aggregate timestamped price samples into requested OHLC buckets."""
    bucket_ms = BAR_SECONDS.get(bar, BAR_SECONDS["4H"]) * 1000
    vol_by_ts = {int(item[0]): float(item[1]) for item in volumes}
    buckets: dict[int, list[tuple[int, float, float]]] = {}
    for timestamp, raw_price in prices:
        ts_ms = int(timestamp)
        bucket_start = ts_ms - (ts_ms % bucket_ms)
        buckets.setdefault(bucket_start, []).append(
            (ts_ms, float(raw_price), vol_by_ts.get(ts_ms, 0.0))
        )

    klines: list[dict] = []
    for bucket_start, samples in sorted(buckets.items()):
        samples.sort(key=lambda item: item[0])
        sample_prices = [item[1] for item in samples]
        klines.append({
            "symbol": symbol,
            "bar": bar,
            "ts": bucket_start,
            "open": sample_prices[0],
            "high": max(sample_prices),
            "low": min(sample_prices),
            "close": sample_prices[-1],
            "volume": samples[-1][2],
            "data_source": "coingecko_market_chart",
            "data_quality": "degraded",
        })
    return klines


class KlineRepository:
    """K线数据仓库：从 OKX 拉取 + SQLite 本地缓存。

    无状态设计：每次网络请求新建 `httpx.AsyncClient`（不持有绑定某个事件循环的长连接），
    因此可在主事件循环、以及数据 Agent 的 Function Calling 子线程（`asyncio.run` 新建循环）中
    安全调用，不会踩「client 绑定到已关闭/不同事件循环」的错误。
    """

    async def close(self):
        """无状态实现下无需释放长连接，保留接口以兼容 main.py 的 shutdown 钩子。"""
        return None

    async def get_klines(
        self, symbol: str, bar: str, limit: int = 200
    ) -> list[dict]:
        """获取 K 线数据，多级 fallback：Gate.io → OKX → CoinGecko → 缓存。

        优先拉取实时数据，缓存仅作为最终兜底（不再因缓存命中而跳过实时拉取）。
        """
        okx_bar = BAR_MAP.get(bar, bar)

        # 1. Gate.io (主数据源，优先实时)
        try:
            fresh = await self._fetch_from_gate(symbol, bar, limit)
            if fresh:
                await self._save_to_cache(fresh)
                return sorted(fresh, key=lambda r: r["ts"])[-limit:]
        except Exception as e:
            logger.warning("Gate K线失败: %s", e)

        # 2. OKX
        try:
            fresh = await self._fetch_from_okx(symbol, okx_bar, limit)
            if fresh:
                await self._save_to_cache(fresh)
                return sorted(fresh, key=lambda r: r["ts"])[-limit:]
        except Exception as e:
            logger.warning("OKX K线失败: %s", e)

        # 3. CoinGecko
        try:
            fresh = await self._fetch_from_coingecko(symbol, bar, limit)
            if fresh:
                await self._save_to_cache(fresh)
                return sorted(fresh, key=lambda r: r["ts"])[-limit:]
        except Exception as e:
            logger.warning("CoinGecko 失败: %s", e)

        # 4. 缓存兜底
        cached = await self._load_from_cache(symbol, bar, limit)
        if cached:
            return sorted(cached, key=lambda r: r["ts"])[-limit:]

        return []

    async def _load_from_cache(
        self, symbol: str, bar: str, limit: int
    ) -> list[dict]:
        try:
            async with aiosqlite.connect(_db_path()) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT * FROM klines WHERE symbol=? AND bar=? ORDER BY ts DESC LIMIT ?",
                    (symbol, bar, limit),
                )
                rows = await cursor.fetchall()
                result = []
                for row in rows:
                    item = dict(row)
                    if item["ts"] < 1_000_000_000_000:
                        item["ts"] *= 1000
                    item["data_source"] = "sqlite_cache"
                    item["data_quality"] = "degraded"
                    result.append(item)
                return result
        except Exception as e:
            logger.warning("缓存读取失败: %s", e)
            return []

    async def _save_to_cache(self, klines: list[dict]) -> None:
        try:
            async with aiosqlite.connect(_db_path()) as db:
                await db.executemany(
                    """INSERT INTO klines (symbol, bar, ts, open, high, low, close, volume)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(symbol, bar, ts) DO UPDATE SET
                           open=excluded.open,
                           high=excluded.high,
                           low=excluded.low,
                           close=excluded.close,
                           volume=excluded.volume""",
                    [
                        (
                            k["symbol"], k["bar"], k["ts"],
                            k["open"], k["high"], k["low"], k["close"], k["volume"],
                        )
                        for k in klines
                    ],
                )
                await db.commit()
        except Exception as e:
            logger.warning("缓存写入失败: %s", e)

    async def _fetch_from_okx(
        self, symbol: str, bar: str, limit: int
    ) -> list[dict]:
        """从 OKX 分页拉取 K 线数据。"""
        all_klines = []
        after = ""
        remaining = limit

        # 无状态：每次拉取新建 client（含 certifi SSL 修复），用完即释放
        # 给足超时优先尝试拿到 OKX 真实数据，而非过早放弃转向精度更低的 CoinGecko 备源
        async with make_client(timeout=20.0) as http:
            while remaining > 0:
                batch_limit = min(remaining, 100)
                params = {"instId": symbol, "bar": bar, "limit": str(batch_limit)}
                if after:
                    params["after"] = after

                resp = await http.get(
                    f"{OKX_BASE}/api/v5/market/history-candles",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()

                if data.get("code") != "0":
                    logger.error("OKX API 错误: %s", data.get("msg"))
                    break

                candles = data.get("data", [])
                if not candles:
                    break

                for c in candles:
                    all_klines.append({
                        "symbol": symbol,
                        "bar": bar,
                        "ts": int(c[0]),
                        "open": float(c[1]),
                        "high": float(c[2]),
                        "low": float(c[3]),
                        "close": float(c[4]),
                        "volume": float(c[5]),
                        "data_source": "okx",
                        "data_quality": "real",
                    })

                remaining -= len(candles)
                # 用最早一根的时间戳作为 after
                after = str(candles[-1][0])

        return all_klines


    async def _fetch_from_coingecko(
        self, symbol: str, bar: str, limit: int
    ) -> list[dict]:
        """从 CoinGecko 价格采样按请求周期做因果 OHLC 聚合。

        market_chart 不是交易所原生 K 线，因此结果必须标记 degraded。每根 K 线只使用
        自己时间桶内的采样点，绝不借用后续采样构造当前高低价。
        """
        base = symbol.split("-")[0].upper()
        coin_id = {"BTC": "bitcoin", "ETH": "ethereum"}.get(base.upper(), "")
        if not coin_id:
            return []

        bucket_ms = BAR_SECONDS.get(bar, BAR_SECONDS["4H"]) * 1000
        days = max(1, min(365, math.ceil(limit * bucket_ms / 86_400_000) + 1))
        async with make_client(timeout=15.0) as http:
            # 使用 market_chart 获取价格+成交量（同一端点，时间戳一致）
            resp = await http.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
                params={"vs_currency": "usd", "days": days},
            )
            resp.raise_for_status()
            data = resp.json()

        prices = data.get("prices", [])
        volumes = data.get("total_volumes", [])

        klines = aggregate_price_samples(symbol, bar, prices, volumes)

        return klines[-limit:] if len(klines) > limit else klines


    async def _fetch_from_gate(
        self, symbol: str, bar: str, limit: int
    ) -> list[dict]:
        """从 Gate.io 获取 K 线（主数据源）。"""
        pair = symbol.replace("-", "_")
        gate_bar = GATE_BAR_MAP.get(bar, bar.lower())
        async with make_client(timeout=15.0) as http:
            resp = await http.get(
                "https://api.gateio.ws/api/v4/spot/candlesticks",
                params={"currency_pair": pair, "interval": gate_bar, "limit": min(limit, 1000)},
            )
            resp.raise_for_status()
            candles = resp.json()

        result = []
        for c in candles:
            ts = int(c[0])
            result.append({
                "symbol": symbol,
                "bar": bar,
                "ts": ts if ts > 1_000_000_000_000 else ts * 1000,
                "open": float(c[5]),
                "high": float(c[3]),
                "low": float(c[4]),
                "close": float(c[2]),
                "volume": float(c[1]) if len(c) > 1 else 0.0,
                "data_source": "gate",
                "data_quality": "real",
            })
        return result[-limit:] if len(result) > limit else result


# 全局单例
kline_repo = KlineRepository()
