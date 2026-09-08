"""
链上数据 Client —— Etherscan API（ETH）+ Blockchain.info API（BTC）。
查询大额转账（鲸鱼动向）和交易所净流入/流出。

BTC 使用 blockchain.info 免费 API（无需 Key），提供大额 UTXO 监控。
ETH 使用 Etherscan API（需 API Key）。

支持 USDC/USDT/BUSD 等合约后缀的衍生品交易对符号。
"""

import asyncio
import logging
import time
from backend.data.ssl_utils import make_client

from backend.config import config

logger = logging.getLogger(__name__)

# Etherscan 免费档限制: 3 req/s，使用令牌桶做节流
_rate_limit_lock = asyncio.Lock()
_last_call_ts: float = 0.0
_MIN_INTERVAL = 0.34  # 约 3 req/s

# Blockchain.info 公开 API（无需 Key，但有限速）
BLOCKCHAIN_BASE = "https://blockchain.info"
BTC_WHALE_THRESHOLD_BTC = 100  # BTC 大额阈值


async def _rate_limit() -> None:
    """Etherscan 免费档速率控制。"""
    global _last_call_ts
    async with _rate_limit_lock:
        elapsed = time.time() - _last_call_ts
        if elapsed < _MIN_INTERVAL:
            await asyncio.sleep(_MIN_INTERVAL - elapsed)
        _last_call_ts = time.time()

ETHERSCAN_BASE = "https://api.etherscan.io/api"

# 主流交易所热钱包地址（ETH）
EXCHANGE_WALLETS = {
    "Binance": ["0x28C6c06298d514Db089934071355E5743bf21d60"],
    "Coinbase": ["0x71660c4005ba85c37ccec55d0c4493e66fe775d3"],
    "Kraken": ["0x2910543af39aba0cd09dbb2d50200b3e800a63d2"],
}

# 大额阈值 (ETH)
WHALE_THRESHOLD_ETH = 500

# WETH 合约 (视为 ETH)
WETH_ADDRESS = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"


class OnchainClient:
    """Etherscan 链上数据客户端（无状态：每次请求新建 AsyncClient，可在任意事件循环/子线程中安全调用）。"""

    def _has_key(self) -> bool:
        return bool(config.ETHERSCAN_API_KEY)

    async def _call(self, params: dict) -> dict:
        """带 API key 的通用调用（含速率控制）。"""
        if not self._has_key():
            return {"error": "ETHERSCAN_API_KEY 未配置"}
        params["apikey"] = config.ETHERSCAN_API_KEY
        await _rate_limit()
        try:
            async with make_client(timeout=15.0) as http:
                resp = await http.get(ETHERSCAN_BASE, params=params)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.warning("Etherscan API 调用失败: %s", e)
            return {"error": str(e)}

    async def get_whale_flows(self, symbol: str, limit: int = 20) -> dict:
        """查询大额转账（鲸鱼动向）。ETH 走 Etherscan，BTC 走 Blockchain.com。"""
        sym = symbol.upper()
        if sym == "BTC":
            return await self._btc_whale_flows(limit)
        if sym != "ETH":
            return await self._btc_whale_flows(limit) if sym == "BTC" else {
                "symbol": symbol, "error": f"暂不支持的币种: {symbol}", "data_quality": "degraded"}

        if not self._has_key():
            return {"symbol": symbol, "error": "ETHERSCAN_API_KEY 未配置", "data_quality": "degraded"}

        try:
            # 查询 WETH 合约的大额 Transfer 事件
            params = {
                "module": "account",
                "action": "tokentx",
                "contractaddress": WETH_ADDRESS,
                "page": 1,
                "offset": str(min(limit, 50)),
                "sort": "desc",
            }
            result = await self._call(params)

            transactions = []
            if result.get("status") == "1":
                for tx in result.get("result", []):
                    value = float(tx.get("value", 0)) / 1e18
                    if value >= WHALE_THRESHOLD_ETH:
                        transactions.append({
                            "hash": tx.get("hash"),
                            "from": tx.get("from"),
                            "to": tx.get("to"),
                            "value_eth": round(value, 2),
                            "timestamp": tx.get("timeStamp"),
                        })

            # 标记是否流入/流出交易所
            exchange_addrs = set()
            for wallets in EXCHANGE_WALLETS.values():
                exchange_addrs.update(a.lower() for a in wallets)

            for tx in transactions:
                tx_to = tx["to"].lower()
                tx_from = tx["from"].lower()
                if tx_to in exchange_addrs:
                    tx["flow_type"] = "流入交易所（潜在抛压）"
                elif tx_from in exchange_addrs:
                    tx["flow_type"] = "流出交易所（潜在吸筹）"
                else:
                    tx["flow_type"] = "未知钱包间转账"

            # 补充交易所地址的原生 ETH 转账，避免只看 WETH 而漏掉主链 ETH 流。
            native_transactions = await self._eth_exchange_transfers(limit=limit)
            known_hashes = {str(tx.get("hash")) for tx in transactions}
            for tx in native_transactions:
                if str(tx.get("hash")) not in known_hashes:
                    transactions.append(tx)
                    known_hashes.add(str(tx.get("hash")))
            transactions.sort(key=lambda tx: str(tx.get("timestamp") or ""), reverse=True)

            return {
                "symbol": symbol,
                "total_whale_txs": len(transactions),
                "whale_transactions": transactions[:limit],
                "source": "etherscan_weth+native",
                "data_quality": "real",
                "exchange_wallet_count": sum(len(wallets) for wallets in EXCHANGE_WALLETS.values()),
                "window_hours": 24,
            }
        except Exception as e:
            logger.warning("获取鲸鱼动向失败: %s", e)
            return {"symbol": symbol, "error": str(e)}

    async def get_exchange_netflow(self, symbol: str) -> dict:
        """估算交易所净流入/流出（基于大额转账分析）。BTC 走 Blockchain.com。"""
        if symbol.upper() == "BTC":
            return await self._btc_netflow()

        if not self._has_key():
            return {"symbol": symbol, "error": "ETHERSCAN_API_KEY 未配置", "data_quality": "degraded"}

        whale_data = await self.get_whale_flows(symbol, limit=50)
        if "error" in whale_data:
            return whale_data

        txns = whale_data.get("whale_transactions", [])
        inflow_value = sum(
            t["value_eth"] for t in txns if "流入交易所" in t.get("flow_type", "")
        )
        outflow_value = sum(
            t["value_eth"] for t in txns if "流出交易所" in t.get("flow_type", "")
        )
        netflow = inflow_value - outflow_value

        return {
            "symbol": symbol,
            "inflow_eth": round(inflow_value, 2),
            "outflow_eth": round(outflow_value, 2),
            "netflow_eth": round(netflow, 2),
            "signal": (
                "净流入（偏空：大户向交易所转移资产）" if netflow > 100
                else ("净流出（偏多：大户从交易所提币）" if netflow < -100
                else "净流持平（中性）")
            ),
            "data_quality": "real",
            "source": whale_data.get("source", "etherscan"),
            "window_hours": whale_data.get("window_hours", 24),
            "exchange_wallet_count": whale_data.get("exchange_wallet_count", 0),
        }

    async def get_government_exchange_deposits(self, symbol: str, lookback_hours: int = 24) -> dict:
        """验证已配置政府钱包是否在窗口内向交易所地址入金。

        地址名单必须由运营方从 Arkham/官方公告等来源核验后配置；空名单时返回
        unavailable，不使用新闻标题或地址猜测冒充链上确认。
        """
        base = str(symbol).upper().split("-")[0]
        if base != "ETH":
            if base == "BTC":
                return await self._btc_government_exchange_deposits(lookback_hours)
            return {
                "symbol": base,
                "data_quality": "unavailable",
                "chain_confirmed": False,
                "reason": f"暂不支持 {base} 的政府钱包链上核验",
                "government_exchange_deposit_count": 0,
            }
        addresses = tuple(config.GOVERNMENT_ETH_ADDRESSES)
        if not addresses:
            return {
                "symbol": base,
                "data_quality": "unavailable",
                "chain_confirmed": False,
                "reason": "未配置 GOVERNMENT_ETH_ADDRESSES，无法将新闻转账与链上地址绑定",
                "government_exchange_deposit_count": 0,
            }

        exchange_addrs = {
            address.lower(): exchange
            for exchange, wallets in EXCHANGE_WALLETS.items()
            for address in wallets
        }
        cutoff = int(time.time()) - max(1, int(lookback_hours)) * 3600
        deposits: list[dict] = []
        query_failures = 0
        for government_address in addresses:
            result = await self._call({
                "module": "account",
                "action": "txlist",
                "address": government_address,
                "startblock": 0,
                "endblock": 99999999,
                "page": 1,
                "offset": 100,
                "sort": "desc",
            })
            if result.get("status") != "1" or not isinstance(result.get("result"), list):
                query_failures += 1
                continue
            for tx in result["result"]:
                tx_to = str(tx.get("to") or "").lower()
                tx_from = str(tx.get("from") or "").lower()
                if tx_from != government_address.lower() or tx_to not in exchange_addrs:
                    continue
                try:
                    timestamp = int(tx.get("timeStamp") or 0)
                    value_eth = float(tx.get("value") or 0) / 1e18
                except (TypeError, ValueError):
                    continue
                if timestamp < cutoff:
                    continue
                deposits.append({
                    "tx_hash": tx.get("hash"),
                    "government_address": government_address,
                    "exchange": exchange_addrs[tx_to],
                    "to": tx.get("to"),
                    "value_eth": round(value_eth, 4),
                    "timestamp": timestamp,
                })
        response = {
            "symbol": base,
            "data_quality": "degraded" if query_failures == len(addresses) else "real",
            "chain_confirmed": bool(deposits),
            "government_exchange_deposit_count": len(deposits),
            "government_exchange_deposit_value_eth": round(sum(item["value_eth"] for item in deposits), 4),
            "lookback_hours": lookback_hours,
            "deposits": deposits[:20],
            "source": "etherscan",
        }
        if query_failures:
            response["reason"] = (
                f"{query_failures}/{len(addresses)} 个政府地址的 Etherscan 查询失败，"
                "未将未返回的数据解释为无转账"
            )
        return response

    async def _btc_government_exchange_deposits(self, lookback_hours: int = 24) -> dict:
        """用 mempool.space 的地址交易记录核验 BTC 政府地址到交易所地址的转账。

        BTC 交易所地址没有统一、稳定的公共标签，目的地址必须通过
        ``EXCHANGE_BTC_ADDRESSES`` 显式配置；仅看到政府地址有转出不会被视为入金。
        """
        addresses = tuple(config.GOVERNMENT_BTC_ADDRESSES)
        exchange_addresses = {
            address.lower(): exchange
            for exchange, wallets in config.EXCHANGE_BTC_ADDRESSES.items()
            for address in wallets
        }
        if not addresses:
            return {
                "symbol": "BTC",
                "data_quality": "unavailable",
                "chain_confirmed": False,
                "reason": "未配置 GOVERNMENT_BTC_ADDRESSES，无法将新闻转账与链上地址绑定",
                "government_exchange_deposit_count": 0,
            }
        if not exchange_addresses:
            return {
                "symbol": "BTC",
                "data_quality": "unavailable",
                "chain_confirmed": False,
                "reason": "未配置 EXCHANGE_BTC_ADDRESSES，无法确认 BTC 目的地址属于交易所",
                "government_exchange_deposit_count": 0,
            }

        cutoff = int(time.time()) - max(1, int(lookback_hours)) * 3600
        deposits: list[dict] = []
        try:
            async with make_client(timeout=15.0) as http:
                for government_address in addresses:
                    transactions: list[dict] = []
                    next_url = f"{self.MEMPOOL_BASE}/address/{government_address}/txs"
                    # 地址历史接口分页返回 25 笔；继续向旧记录翻页，直到越过窗口，
                    # 避免高频政府钱包的最新交易把窗口内入金挤出第一页。
                    for _ in range(12):
                        response = await http.get(next_url)
                        response.raise_for_status()
                        page = response.json()
                        if not isinstance(page, list) or not page:
                            break
                        transactions.extend(item for item in page if isinstance(item, dict))
                        oldest = min(
                            int((item.get("status") or {}).get("block_time") or 0)
                            for item in page
                        )
                        if oldest and oldest < cutoff:
                            break
                        if len(page) < 25:
                            break
                        last_txid = page[-1].get("txid")
                        if not last_txid:
                            break
                        next_url = f"{self.MEMPOOL_BASE}/address/{government_address}/txs/chain/{last_txid}"
                    for tx in transactions:
                        status = tx.get("status") or {}
                        timestamp = int(status.get("block_time") or 0)
                        if timestamp < cutoff:
                            continue
                        inputs = tx.get("vin") or []
                        input_addresses = {
                            str((item.get("prevout") or {}).get("scriptpubkey_address") or "").lower()
                            for item in inputs
                        }
                        if government_address.lower() not in input_addresses:
                            continue
                        for output in tx.get("vout") or []:
                            destination = str(output.get("scriptpubkey_address") or "").lower()
                            exchange = exchange_addresses.get(destination)
                            if not exchange:
                                continue
                            value_btc = float(output.get("value") or 0) / 1e8
                            deposits.append({
                                "tx_hash": tx.get("txid"),
                                "government_address": government_address,
                                "exchange": exchange,
                                "to": destination,
                                "value_btc": round(value_btc, 8),
                                "timestamp": timestamp,
                            })
        except Exception as exc:
            logger.warning("BTC 政府钱包入金核验失败: %s", exc)
            return {
                "symbol": "BTC",
                "data_quality": "degraded",
                "chain_confirmed": False,
                "reason": f"mempool.space 查询失败：{exc}",
                "government_exchange_deposit_count": 0,
                "source": "mempool.space",
            }
        return {
            "symbol": "BTC",
            "data_quality": "real",
            "chain_confirmed": bool(deposits),
            "government_exchange_deposit_count": len(deposits),
            "government_exchange_deposit_value_btc": round(
                sum(item["value_btc"] for item in deposits), 8
            ),
            "lookback_hours": lookback_hours,
            "deposits": deposits[:20],
            "source": "mempool.space",
        }

    async def _eth_exchange_transfers(self, limit: int = 20) -> list[dict]:
        """扫描主流交易所地址的原生 ETH 大额转账（最近约 24 小时）。"""
        exchange_addrs = {
            address.lower(): exchange
            for exchange, wallets in EXCHANGE_WALLETS.items()
            for address in wallets
        }
        transfers: list[dict] = []
        now = int(time.time())
        for address, exchange in exchange_addrs.items():
            result = await self._call({
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": 0,
                "endblock": 99999999,
                "page": 1,
                "offset": 100,
                "sort": "desc",
            })
            if result.get("status") != "1" or not isinstance(result.get("result"), list):
                continue
            for tx in result["result"]:
                try:
                    timestamp = int(tx.get("timeStamp") or 0)
                    value_eth = float(tx.get("value") or 0) / 1e18
                except (TypeError, ValueError):
                    continue
                if timestamp <= 0 or now - timestamp > 24 * 3600 or value_eth < WHALE_THRESHOLD_ETH:
                    continue
                tx_from = str(tx.get("from") or "").lower()
                tx_to = str(tx.get("to") or "").lower()
                if tx_to == address:
                    flow_type = "原生ETH流入交易所（潜在抛压）"
                elif tx_from == address:
                    flow_type = "原生ETH流出交易所（潜在吸筹）"
                else:
                    continue
                transfers.append({
                    "hash": tx.get("hash"),
                    "from": tx.get("from"),
                    "to": tx.get("to"),
                    "value_eth": round(value_eth, 2),
                    "timestamp": timestamp,
                    "flow_type": flow_type,
                    "exchange": exchange,
                    "asset_type": "native_eth",
                })
        return transfers

    # ── BTC 链上（mempool.space，免费无 Key） ──
    #
    # 关键修复：blockchain.info / api.blockchain.info 在当前网络环境下经实测
    # 完全不可达（TLS record layer failure / 连接超时，非临时抖动，而是域名级封锁），
    # 无论怎么调整超时或 SSL 参数都无法解决。改用 mempool.space（同为免费开源、
    # 无需 Key，国内实测可正常访问）替代：
    # - 鲸鱼动向：用最新已确认区块的 vout 大额输出替代原来的"未确认交易"近似
    #   （更稳定，不受 mempool 拥堵/费率影响）。
    # - 交易所净流：mempool.space 没有现成的"净流图表"接口，改为用最近几个区块的
    #   大额转账方向粗略估算活跃度，如实标注为近似信号（不冒充精确净流数据）。
    BTC_WHALE_THRESHOLD_SATS = 50 * 1e8  # 50 BTC（沿用原阈值）
    MEMPOOL_BASE = "https://mempool.space/api"

    async def _btc_whale_flows(self, limit: int = 20) -> dict:
        """BTC 鲸鱼动向：扫描最新已确认区块的大额输出（≥50 BTC）。"""
        try:
            async with make_client(timeout=15.0) as http:
                tip_resp = await http.get(f"{self.MEMPOOL_BASE}/blocks/tip/hash")
                tip_resp.raise_for_status()
                block_hash = tip_resp.text.strip().strip('"')

                whales: list[dict] = []
                start_index = 0
                # 单区块最多几千笔交易，分页扫描直到拿够阈值样本或翻完全部
                for _ in range(6):  # 最多扫 6 页（每页 25 笔），足够覆盖绝大多数大额交易
                    resp = await http.get(f"{self.MEMPOOL_BASE}/block/{block_hash}/txs/{start_index}")
                    resp.raise_for_status()
                    txs = resp.json()
                    if not txs:
                        break
                    for tx in txs:
                        total_out_sats = sum(o.get("value", 0) for o in tx.get("vout", []))
                        if total_out_sats >= self.BTC_WHALE_THRESHOLD_SATS:
                            whales.append({
                                "hash": tx.get("txid"),
                                "value_btc": round(total_out_sats / 1e8, 2),
                                "timestamp": tx.get("status", {}).get("block_time"),
                            })
                        if len(whales) >= limit:
                            break
                    if len(whales) >= limit:
                        break
                    start_index += len(txs)

            return {
                "symbol": "BTC",
                "total_whale_txs": len(whales),
                "whale_transactions": whales[:limit],
                "source": "mempool.space",
                "data_quality": "real",
                "note": f"基于最新已确认区块({block_hash[:12]}...)大额输出（≥50 BTC）扫描",
            }
        except Exception as e:
            logger.warning("BTC 鲸鱼动向获取失败: %s", e)
            return {"symbol": "BTC", "error": str(e), "data_quality": "degraded",
                    "whale_transactions": []}

    async def _btc_netflow(self) -> dict:
        """BTC 交易所净流：mempool.space 无现成净流图表接口，
        用鲸鱼动向活跃度做近似信号，如实标注为近似估算（非精确净流）。
        """
        whale = await self._btc_whale_flows(limit=50)
        whale_count = whale.get("total_whale_txs", 0)
        return {
            "symbol": "BTC",
            "signal": f"链上活跃度参考：最新区块检测到 {whale_count} 笔大额转账(≥50BTC)，" +
                      ("活跃度偏高" if whale_count >= 5 else "活跃度正常"),
            "whale_activity": whale_count,
            "source": "mempool.space",
            "data_quality": whale.get("data_quality", "degraded"),
            "note": "mempool.space 无精确交易所净流接口，本信号为链上大额转账活跃度近似，非精确净流入/流出金额",
        }


onchain_client = OnchainClient()
