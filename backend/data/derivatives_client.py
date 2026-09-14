"""
衍生品数据 Client —— Gate/Binance/OKX 永续合约 + Deribit 期权公开 API。
获取资金费率、未平仓合约、爆仓数据，以及近月期权交割/最大痛点信号。
"""

import asyncio
import logging
import math
import os
import time
from collections import defaultdict

from backend.config import config
from backend.data.ssl_utils import make_client
from backend.data.gate_client import gate_client

logger = logging.getLogger(__name__)

BINANCE_FAPI = "https://fapi.binance.com"
DERIBIT_PUBLIC = "https://www.deribit.com/api/v2/public"
DERIBIT_FALLBACK_ENDPOINTS = (
    "https://api.deribit.com/api/v2/public",
)


def _funding_interpretation(rate: float) -> str:
    if rate > 0:
        return "多头支付空头（多头拥挤，反向偏空）"
    if rate < 0:
        return "空头支付多头（空头拥挤，反向偏多）"
    return "资金费率为零（中性）"


def _normalize_contract_symbol(symbol: str) -> str:
    return symbol.upper().replace("-", "").replace("_", "").strip()


def summarize_options_market(
    instruments: list[dict],
    summaries: list[dict],
    now_ms: int | None = None,
) -> dict:
    """把 Deribit 期权链压缩为可解释的交割/做市压力信号。

    OI 只能说明仓位规模，不能直接证明多空方向（不知道谁是买方、谁是卖方），
    因此最大痛点只在临近交割时用于判断“钉住/均值回归”风险，Call/Put 墙只作为
    支撑阻力佐证。方向性信号被限制在弱强度和低置信度，避免把“期权占优方”误当
    成必然的护盘方向。
    """
    now_ms = now_ms or int(time.time() * 1000)
    metadata: dict[str, dict] = {}
    for item in instruments:
        if not isinstance(item, dict):
            continue
        name = str(item.get("instrument_name") or "")
        if not name:
            continue
        try:
            expiration = int(item.get("expiration_timestamp"))
            strike = float(item.get("strike"))
        except (TypeError, ValueError):
            continue
        option_type = str(item.get("option_type") or "").lower()
        if option_type not in {"call", "put"}:
            continue
        if expiration <= now_ms:
            continue
        metadata[name] = {
            "expiration_timestamp": expiration,
            "strike": strike,
            "option_type": option_type,
        }

    expiry_groups: dict[int, list[dict]] = defaultdict(list)
    for item in summaries:
        if not isinstance(item, dict):
            continue
        name = str(item.get("instrument_name") or "")
        meta = metadata.get(name)
        if not meta:
            continue
        try:
            open_interest = max(0.0, float(item.get("open_interest") or 0.0))
            volume = max(0.0, float(item.get("volume") or 0.0))
        except (TypeError, ValueError):
            continue
        expiry_groups[meta["expiration_timestamp"]].append({
            **meta,
            "open_interest": open_interest,
            "volume": volume,
            "underlying_price": item.get("underlying_price"),
            "mark_iv": item.get("mark_iv"),
        })

    if not expiry_groups:
        return {
            "data_quality": "degraded",
            "error": "Deribit 没有返回可用的未到期期权链",
            "bias": "neutral",
            "score": 50,
            "confidence": 0.0,
        }

    # 选择最近的活跃到期日；若最近一档没有 OI，优先跳到下一档有仓位的到期日，
    # 避免把“零 OI 的日历合约”误当成市场最大痛点。
    expiry = min(
        expiry_groups,
        key=lambda timestamp: (
            sum(item["open_interest"] for item in expiry_groups[timestamp]) <= 0,
            timestamp,
        ),
    )
    contracts = expiry_groups[expiry]
    spot_values = [
        float(item["underlying_price"])
        for item in contracts
        if item.get("underlying_price") is not None
        and _is_finite_number(item.get("underlying_price"))
        and float(item["underlying_price"]) > 0
    ]
    spot = sum(spot_values) / len(spot_values) if spot_values else 0.0
    call_oi = sum(item["open_interest"] for item in contracts if item["option_type"] == "call")
    put_oi = sum(item["open_interest"] for item in contracts if item["option_type"] == "put")
    call_volume = sum(item["volume"] for item in contracts if item["option_type"] == "call")
    put_volume = sum(item["volume"] for item in contracts if item["option_type"] == "put")
    strikes = sorted({item["strike"] for item in contracts})

    max_pain = None
    if strikes and (call_oi + put_oi) > 0:
        pain_by_strike = {
            settlement: sum(
                item["open_interest"] * (
                    max(0.0, settlement - item["strike"])
                    if item["option_type"] == "call"
                    else max(0.0, item["strike"] - settlement)
                )
                for item in contracts
            )
            for settlement in strikes
        }
        max_pain = min(pain_by_strike, key=pain_by_strike.get)

    call_wall_item = max(
        (item for item in contracts if item["option_type"] == "call"),
        key=lambda item: item["open_interest"],
        default=None,
    )
    put_wall_item = max(
        (item for item in contracts if item["option_type"] == "put"),
        key=lambda item: item["open_interest"],
        default=None,
    )
    hours_to_expiry = max(0.0, (expiry - now_ms) / 3_600_000)
    call_put_ratio = call_oi / put_oi if put_oi > 0 else (math.inf if call_oi > 0 else 1.0)
    max_pain_distance_pct = (
        (max_pain - spot) / spot * 100
        if max_pain is not None and spot > 0
        else None
    )

    # 方向仅作为弱证据：临近交割更强调钉住，远离最大痛点才允许给出轻微偏向。
    bias = "neutral"
    score = 50
    if max_pain_distance_pct is not None and hours_to_expiry <= 72:
        if abs(max_pain_distance_pct) <= 1.5:
            mode = "pinning"
        elif max_pain_distance_pct > 1.5 and call_oi > put_oi * 1.20:
            bias, score, mode = "bullish", 56, "call_wall_support"
        elif max_pain_distance_pct < -1.5 and put_oi > call_oi * 1.20:
            bias, score, mode = "bearish", 44, "put_wall_pressure"
        else:
            mode = "uncertain_positioning"
    elif call_oi > put_oi * 1.35 and max_pain_distance_pct is not None and max_pain_distance_pct > 1.5:
        bias, score, mode = "bullish", 55, "call_positioning"
    elif put_oi > call_oi * 1.35 and max_pain_distance_pct is not None and max_pain_distance_pct < -1.5:
        bias, score, mode = "bearish", 45, "put_positioning"
    else:
        mode = "uncertain_positioning"

    if mode == "pinning":
        interpretation = "临近交割且现价接近最大痛点，做市商对冲可能带来价格钉住/均值回归"
    elif mode == "call_wall_support":
        interpretation = "Call OI 与最大痛点位于现价上方，提供轻微上行牵引，但不代表必然护盘"
    elif mode == "put_wall_pressure":
        interpretation = "Put OI 与最大痛点位于现价下方，提供轻微下行压力，但可能存在收割大头仓位"
    else:
        interpretation = "期权 OI 方向无法区分买方与卖方，护盘/收割路径不确定，仅作辅助证据"

    return {
        "data_quality": "real",
        "currency": contracts[0].get("currency", ""),
        "expiry_timestamp": expiry,
        "hours_to_expiry": round(hours_to_expiry, 2),
        "delivery_risk": "high" if hours_to_expiry <= 72 else "normal",
        "spot": round(spot, 2) if spot else None,
        "call_oi": round(call_oi, 4),
        "put_oi": round(put_oi, 4),
        "call_put_oi_ratio": round(call_put_ratio, 3) if math.isfinite(call_put_ratio) else None,
        "call_volume": round(call_volume, 4),
        "put_volume": round(put_volume, 4),
        "max_pain": round(max_pain, 2) if max_pain is not None else None,
        "max_pain_distance_pct": round(max_pain_distance_pct, 3) if max_pain_distance_pct is not None else None,
        "call_wall": round(call_wall_item["strike"], 2) if call_wall_item else None,
        "put_wall": round(put_wall_item["strike"], 2) if put_wall_item else None,
        "contract_count": len(contracts),
        "positioning_mode": mode,
        "interpretation": interpretation,
        "bias": bias,
        "score": score,
        "confidence": 0.35 if bias != "neutral" else 0.25,
        "caveat": "Deribit 期权 OI 未披露买卖方与做市商 gamma；最大痛点不是必然价格目标，须与现货、资金费率和 OI 共振",
    }


def _is_finite_number(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


class DerivativesClient:
    """Binance 衍生品数据客户端（无状态：每次请求新建 client（含 certifi SSL 修复），可在任意事件循环/子线程中安全调用）。"""

    async def get_funding_rate(self, symbol: str, before_ts: int | None = None) -> dict:
        """获取资金费率（Gate → Binance → CoinGecko fallback）。

        before_ts: as-of 回测毫秒时间戳。历史模式仅走可回溯的 Gate/Binance
        历史端点；不可回溯的 CoinGecko 实时 fallback 直接跳过（杜绝未来泄漏）。
        """
        symbol = _normalize_contract_symbol(symbol)
        # 1. Gate.io（支持 as-of 历史窗口）
        try:
            gate_result = await self._get_funding_rate_gate(symbol, before_ts=before_ts)
            if not gate_result.get("error") and gate_result.get("funding_rate") is not None:
                return gate_result
            logger.warning("Gate 资金费率无有效数据: %s", gate_result.get("error", "未知错误"))
        except Exception as e:
            logger.warning("Gate 资金费率失败: %s", e)

        # 2. Binance（国内网络偶发 TLS RECORD_LAYER_FAILURE，但给足超时优先尝试拿到真实数据，
        # 而非过早放弃切到精度更低的备源——分析质量优先于速度）
        try:
            params: dict = {"symbol": symbol, "limit": 1}
            if before_ts is not None:
                # 历史资金费率：取 as_of 前 48h 内最近一条
                params = {
                    "symbol": symbol,
                    "startTime": before_ts - 172_800_000,
                    "endTime": before_ts,
                    "limit": 10,
                }
            async with make_client(timeout=20.0) as http:
                resp = await http.get(
                    f"{BINANCE_FAPI}/fapi/v1/fundingRate",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
            if data and len(data) > 0:
                item = data[-1] if before_ts is not None else data[0]
                rate = float(item["fundingRate"])
                return {
                    "symbol": symbol,
                    "funding_rate": rate,
                    "funding_rate_pct": round(rate * 100, 4),
                    "funding_time": item["fundingTime"],
                    "interpretation": _funding_interpretation(rate),
                    "source": "binance",
                    "data_quality": "real",
                }
        except Exception as e:
            logger.warning("Binance 资金费率失败: %s", e)

        if before_ts is not None:
            # as-of 模式：CoinGecko 实时估算不可回溯，不提供数据（诚实降级）
            return {
                "symbol": symbol,
                "error": "as-of 模式下所有资金费率历史源不可用",
                "source": "none",
                "data_quality": "degraded",
            }

        # 2. CoinGecko derivatives fallback
        return await self._get_funding_rate_coingecko(symbol)

    async def get_options_snapshot(self, symbol: str, before_ts: int | None = None) -> dict:
        """获取 BTC/ETH 近月期权链，计算最大痛点与交割压力。Deribit 无需 API key。

        before_ts: as-of 回测模式 —— Deribit 实时期权链不可回溯，直接返回不可用
        （绝不用当前期权数据冒充历史）。
        """
        if before_ts is not None:
            return {
                "symbol": symbol,
                "data_quality": "degraded",
                "error": "as-of 模式下期权数据不可回溯",
                "bias": "neutral",
                "score": 50,
                "confidence": 0.0,
            }
        currency = str(symbol).upper().replace("-", "")
        currency = currency.removesuffix("USDT").removesuffix("USDC").removesuffix("USD")
        if currency not in {"BTC", "ETH"}:
            return {"symbol": symbol, "data_quality": "degraded", "error": "Deribit 仅支持 BTC/ETH 期权"}
        endpoints = [config.DERIBIT_API_BASE_URL, *DERIBIT_FALLBACK_ENDPOINTS]
        # 去重并保留用户配置的入口优先级。
        endpoints = list(dict.fromkeys(endpoint.rstrip("/") for endpoint in endpoints if endpoint))
        errors: list[str] = []
        error_types: list[str] = []
        for endpoint in endpoints:
            try:
                async with make_client(timeout=15.0, proxy=config.DERIBIT_PROXY_URL or None) as http:
                    instruments_resp, summaries_resp = await asyncio.gather(
                        http.get(
                            f"{endpoint}/get_instruments",
                            params={"currency": currency, "kind": "option", "expired": "false"},
                        ),
                        http.get(
                            f"{endpoint}/get_book_summary_by_currency",
                            params={"currency": currency, "kind": "option"},
                        ),
                    )
                    instruments_resp.raise_for_status()
                    summaries_resp.raise_for_status()
                    instruments_payload = instruments_resp.json()
                    summaries_payload = summaries_resp.json()
                instruments = instruments_payload.get("result", []) if isinstance(instruments_payload, dict) else []
                summaries = summaries_payload.get("result", []) if isinstance(summaries_payload, dict) else []
                result = summarize_options_market(instruments, summaries)
                result["symbol"] = currency
                result["source"] = "deribit"
                result["endpoint"] = endpoint
                return result
            except Exception as e:
                errors.append(f"{endpoint}: {type(e).__name__}: {e}")
                error_types.append(type(e).__name__)
                logger.warning("Deribit 期权入口失败(%s): %s", endpoint, e)
        effective_proxy = (
            config.DERIBIT_PROXY_URL
            or os.getenv("HTTPS_PROXY")
            or os.getenv("https_proxy")
            or os.getenv("HTTP_PROXY")
            or os.getenv("http_proxy")
            or os.getenv("ALL_PROXY")
            or os.getenv("all_proxy")
        )
        network_hint = (
            "Deribit 连接被本机网络/DNS拦截；请配置 DERIBIT_PROXY_URL（例如 "
            "http://127.0.0.1:7890），或把 DERIBIT_API_BASE_URL 指向可达的同源反向代理。"
            if not effective_proxy
            else "Deribit 专用代理也未能建立 TLS；请确认代理支持 HTTPS CONNECT，并检查代理日志/证书。"
        )
        return {
            "symbol": currency,
            "source": "deribit",
            "data_quality": "degraded",
            "error": "；".join(errors),
            "attempted_endpoints": endpoints,
            "error_types": sorted(set(error_types)),
            "proxy_configured": bool(effective_proxy),
            "network_hint": network_hint,
            "bias": "neutral",
            "score": 50,
            "confidence": 0.0,
        }

    async def _get_funding_rate_gate(self, symbol: str, before_ts: int | None = None) -> dict:
        """Gate.io 合约统计获取资金费率（支持 as-of 历史窗口）。"""
        pair = symbol.replace("USDT", "_USDT")
        params: dict = {"contract": pair, "limit": 1}
        if before_ts is not None:
            params = {
                "contract": pair,
                "interval": "1h",
                "from": before_ts // 1000 - 172800,
                "to": before_ts // 1000,
                "limit": 100,
            }
        async with make_client(timeout=10.0) as http:
            resp = await http.get(
                "https://api.gateio.ws/api/v4/futures/usdt/contract_stats",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
        if data and len(data) > 0:
            if before_ts is not None:
                valid = [d for d in data if int(d.get("time", 0)) * 1000 <= before_ts]
                if not valid:
                    return {"symbol": symbol, "error": "as-of 时刻之前无资金费率数据", "source": "gate"}
                item = max(valid, key=lambda d: int(d.get("time", 0)))
            else:
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

    async def _get_funding_rate_coingecko(self, symbol: str) -> dict:
        """CoinGecko 衍生品端点不提供逐合约精确资金费率。

        为杜绝假数据（此前此处硬编码 funding_rate=0.0、btc_price=62000），
        改为尝试 OKX 公共合约费率作为最终备源；仍失败则如实返回 error，
        绝不返回捏造的费率或价格。
        """
        # 最终备源：OKX 公共资金费率（无需 Key，国内多数可达）
        try:
            base = symbol.replace("USDT", "").upper()
            inst_id = f"{base}-USDT-SWAP"
            async with make_client(timeout=10.0) as http:
                resp = await http.get(
                    "https://www.okx.com/api/v5/public/funding-rate",
                    params={"instId": inst_id},
                )
                resp.raise_for_status()
                data = resp.json()
            items = data.get("data", []) if isinstance(data, dict) else []
            if items:
                rate = float(items[0].get("fundingRate", 0))
                return {
                    "symbol": symbol,
                    "funding_rate": rate,
                    "funding_rate_pct": round(rate * 100, 4),
                    "interpretation": _funding_interpretation(rate),
                    "source": "okx",
                    "data_quality": "real",
                }
        except Exception as e:
            logger.warning("OKX 资金费率备源失败: %s", e)

        # 全部失败：如实返回，不造假
        return {
            "symbol": symbol,
            "error": "资金费率数据源（Gate/Binance/OKX）均不可达",
            "source": "none",
            "data_quality": "degraded",
        }

    async def get_open_interest(
        self, symbol: str, period: str = "1H", limit: int = 30, before_ts: int | None = None
    ) -> dict:
        """获取 OI 历史数据（Gate → Binance → CoinGecko fallback）。

        关键修复：Binance fapi.binance.com 在当前网络环境下经实测完全不可达
        （连接超时/TLS 握手失败，非临时抖动），此前把它排在首位导致 OI 长期
        走向精度较低的 CoinGecko 聚合估算兜底。改为 Gate.io contract_stats
        优先（国内可正常访问，且是逐合约精确数据），Binance 降级为备源。

        before_ts: as-of 回测毫秒时间戳（仅可回溯源生效；CoinGecko 实时估算跳过）。
        """
        symbol = _normalize_contract_symbol(symbol)

        # 1. Gate.io（国内可达，逐合约精确数据，支持 as-of 窗口）
        try:
            gate_res = await gate_client.get_open_interest(symbol, limit=limit, before_ts=before_ts)
            if gate_res.get("current_oi") is not None and not gate_res.get("error"):
                return gate_res
        except Exception as e:
            logger.warning("Gate OI 失败: %s，回退 Binance", e)

        # 2. Binance 备源
        try:
            params: dict = {"symbol": symbol, "period": period, "limit": limit}
            if before_ts is not None:
                params["startTime"] = before_ts - (limit + 1) * 3_600_000
                params["endTime"] = before_ts
            async with make_client(timeout=20.0) as http:
                resp = await http.get(
                    f"{BINANCE_FAPI}/futures/data/openInterestHist",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
            if data:
                current = float(data[-1]["sumOpenInterestValue"])
                prev = float(data[0]["sumOpenInterestValue"])
                change_pct = round(
                    ((current - prev) / prev * 100) if prev else 0, 2
                )
                return {
                    "symbol": symbol,
                    "current_oi": current,
                    "oi_change_pct": change_pct,
                    "oi_trend": "增长" if change_pct > 2 else ("下降" if change_pct < -2 else "持平"),
                    "period": period,
                    "oi_history": [
                        {"time": d["timestamp"], "oi": float(d["sumOpenInterestValue"])}
                        for d in data
                    ],
                    "source": "binance",
                }
        except Exception as e:
            logger.warning("Binance OI 失败: %s，回退 CoinGecko", e)
        if before_ts is not None:
            # as-of 模式：CoinGecko 实时聚合估算不可回溯，诚实降级
            return {
                "symbol": symbol,
                "error": "as-of 模式下所有 OI 历史源不可用",
                "source": "none",
                "data_quality": "degraded",
            }
        return await self._get_oi_coingecko(symbol)

    async def get_liquidations(self, symbol: str, limit: int = 100, before_ts: int | None = None) -> dict:
        """获取近期强平订单数据，分析多空爆仓分布。

        数据源优先级（Binance allForceOrders 已被限制/国内不可达，故 Gate 优先）：
            1. Gate.io /futures/usdt/liq_orders （国内可达，主源）
            2. Binance /fapi/v1/allForceOrders （备源）
        全部失败时如实返回 data_quality=degraded（不造假）。

        before_ts: as-of 回测模式 —— 爆仓订单流为实时数据不可回溯，直接降级
        （绝不用当前爆仓数据冒充历史）。
        """
        symbol = _normalize_contract_symbol(symbol)
        if before_ts is not None:
            return {
                "symbol": symbol,
                "error": "as-of 模式下爆仓数据不可回溯",
                "source": "none",
                "data_quality": "degraded",
                "liquidation_clusters": [],
            }

        # 1. Gate.io 主源
        try:
            gate_res = await gate_client.get_liquidations(symbol, limit=limit)
            if gate_res.get("liquidation_clusters") or gate_res.get("total_orders"):
                return gate_res
            logger.info("Gate 爆仓无数据，回退 Binance")
        except Exception as e:
            logger.warning("Gate 爆仓失败: %s，回退 Binance", e)

        # 2. Binance 备源（给足超时优先尝试拿到真实数据）
        try:
            async with make_client(timeout=20.0) as http:
                resp = await http.get(
                    f"{BINANCE_FAPI}/fapi/v1/allForceOrders",
                    params={"symbol": symbol, "limit": limit},
                )
                resp.raise_for_status()
                orders = resp.json()

            long_liq = sum(
                o["price"] * o["origQty"]
                for o in orders
                if o.get("side") == "LONG"
            )
            short_liq = sum(
                o["price"] * o["origQty"]
                for o in orders
                if o.get("side") == "SHORT"
            )

            # 爆仓价格聚类
            price_buckets: dict[int, dict] = {}
            for o in orders:
                bucket = int(float(o["price"]) / 100) * 100
                side = o.get("side", "UNKNOWN")
                if bucket not in price_buckets:
                    price_buckets[bucket] = {"price_range": f"{bucket}-{bucket+100}", "long_count": 0, "short_count": 0}
                if side == "LONG":
                    price_buckets[bucket]["long_count"] += 1
                elif side == "SHORT":
                    price_buckets[bucket]["short_count"] += 1

            # 按总爆仓量排序，取 Top 5 密集区
            clusters = sorted(
                price_buckets.values(),
                key=lambda x: x["long_count"] + x["short_count"],
                reverse=True,
            )[:5]

            return {
                "symbol": symbol,
                "total_orders": len(orders),
                "long_liquidation_value": round(long_liq, 2),
                "short_liquidation_value": round(short_liq, 2),
                "dominant_side": "多头爆仓为主（偏空压力区）" if long_liq > short_liq * 1.5
                                 else ("空头爆仓为主（偏多磁吸区）" if short_liq > long_liq * 1.5
                                 else "多空爆仓均衡"),
                "liquidation_clusters": clusters,
            }
        except Exception as e:
            logger.warning("Binance 爆仓也失败: %s", e)
        # 所有源失败：如实返回，不给假 0 数据
        return {"symbol": symbol, "source": "none", "data_quality": "degraded",
                "error": "Gate 与 Binance 爆仓数据均不可达",
                "liquidation_clusters": [],
                "dominant_side": "无法判定（数据源不可用）"}


    async def _get_oi_coingecko(self, symbol: str) -> dict:
        """从 CoinGecko 衍生品端点提取 OI 数据。"""
        try:
            base = symbol.replace("USDT", "").upper()
            coin_id = {"BTC": "bitcoin", "ETH": "ethereum"}.get(base, "")
            if not coin_id:
                return {"symbol": symbol, "error": "不支持的币种", "source": "coingecko"}
            async with make_client(timeout=15.0) as http:
                resp = await http.get("https://api.coingecko.com/api/v3/derivatives/exchanges")
                resp.raise_for_status()
                exchanges = resp.json()
            total_oi = 0.0
            count = 0
            for ex in exchanges:
                oi = ex.get("open_interest_btc")
                if oi and float(oi) > 0:
                    total_oi += float(oi)
                    count += 1
            return {
                "symbol": symbol,
                "current_oi": round(total_oi, 2),
                "oi_change_pct": 0,
                "oi_trend": "持平",
                "oi_history": [],
                "source": "coingecko",
                "data_quality": f"CoinGecko 聚合 {count} 家交易所 OI 数据，精度有限",
            }
        except Exception as e:
            logger.warning("CoinGecko OI 获取失败: %s", e)
            return {"symbol": symbol, "error": str(e), "source": "coingecko"}


derivatives_client = DerivativesClient()
