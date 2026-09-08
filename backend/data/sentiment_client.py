"""
舆情情绪数据 Client —— Alternative.me 恐惧贪婪指数。
"""

import logging
from backend.data.ssl_utils import make_client

logger = logging.getLogger(__name__)

FNG_BASE = "https://api.alternative.me"


class SentimentClient:
    """恐惧贪婪指数数据客户端（无状态：每次请求新建 AsyncClient，可在任意事件循环/子线程中安全调用）。"""

    async def get_fear_greed(self, limit: int = 30) -> dict:
        """获取恐惧贪婪指数历史数据。

        Args:
            limit: 获取天数
        Returns:
            {
                "current": {"value": 45, "classification": "Fear"},
                "history": [...],
                "trend": "上升",
                "signal": "..."
            }
        """
        try:
            async with make_client(timeout=15.0) as http:
                resp = await http.get(
                    f"{FNG_BASE}/fng/",
                    params={"limit": limit},
                )
                resp.raise_for_status()
                data = resp.json()

            items = data.get("data", [])
            if not items:
                return {"error": "无数据"}

            history = []
            for item in items:
                val = int(item["value"])
                history.append({
                    "value": val,
                    "classification": item["value_classification"],
                    "timestamp": item["timestamp"],
                })

            current = history[0]
            prev = history[-1] if len(history) > 1 else current
            trend = "上升" if current["value"] > prev["value"] else ("下降" if current["value"] < prev["value"] else "持平")

            # 反向指标判断
            signal = "中性"
            if current["value"] >= 75:
                signal = "极度贪婪（反向指标：潜在顶部信号）"
            elif current["value"] >= 55:
                signal = "贪婪（偏谨慎）"
            elif current["value"] <= 25:
                signal = "极度恐惧（反向指标：潜在底部信号）"
            elif current["value"] <= 45:
                signal = "恐惧（偏积极）"

            return {
                "current": current,
                "history": history,
                "trend": trend,
                "signal": signal,
                "source": "Alternative.me Fear & Greed Index",
            }
        except Exception as e:
            logger.warning("获取恐惧贪婪指数失败: %s", e)
            return {"error": str(e)}

    async def get_social_sentiment(self, symbol: str) -> dict:
        """获取社交媒体情绪评分。

        BTC: Alternative.me 恐惧贪婪指数（全市场情绪）
        ETH: 恐惧贪婪指数 + CoinGecko 社区情绪分（双重交叉验证）
        """
        fg_data = await self.get_fear_greed(limit=7)
        if "error" in fg_data:
            return {"symbol": symbol, "error": fg_data["error"]}

        current_val = fg_data["current"]["value"]
        sentiment_score = current_val
        sentiment_label = (
            "极度恐惧" if current_val <= 25
            else "恐惧" if current_val <= 45
            else "中性" if current_val <= 55
            else "贪婪" if current_val <= 75
            else "极度贪婪"
        )

        base = {
            "symbol": symbol,
            "sentiment_score": sentiment_score,
            "sentiment_label": sentiment_label,
            "recent_trend": fg_data.get("trend", "未知"),
            "note": "基于恐惧贪婪指数",
            "source": "Alternative.me",
            "data_quality": "real",
        }

        # ETH 增补 CoinGecko 社区情绪（免费端点，无需 Key）
        if symbol.upper() == "ETH":
            cg = await self._get_coingecko_sentiment("ethereum")
            if cg and cg.get("sentiment_votes_up_pct") is not None:
                # CoinGecko 情绪分 0-100（正向百分）
                cg_pct = cg["sentiment_votes_up_pct"]
                cg_label = "乐观" if cg_pct > 60 else ("悲观" if cg_pct < 40 else "中性")
                base["note"] = f"FGI={current_val} + CoinGecko社区{cg_label}({cg_pct}%↑) 双重交叉验证"
                base["source"] = "Alternative.me + CoinGecko"
                # 加权融合：FGI 60% + CG 40%
                base["sentiment_score"] = round(current_val * 0.6 + cg_pct * 0.4)

        return base

    async def _get_coingecko_sentiment(self, coin_id: str) -> dict | None:
        """从 CoinGecko 获取社区情绪数据（免费，无需 Key）。"""
        try:
            async with make_client(timeout=10.0) as http:
                resp = await http.get(
                    f"https://api.coingecko.com/api/v3/coins/{coin_id}",
                    params={"localization": "false", "tickers": "false",
                            "community_data": "true", "developer_data": "false"},
                )
                resp.raise_for_status()
                data = resp.json()
            comm = data.get("community_data", {}) if isinstance(data, dict) else {}
            up = comm.get("sentiment_votes_up_percentage", 50)
            return {
                "sentiment_votes_up_pct": round(float(up), 1),
                "twitter_followers": comm.get("twitter_followers", 0),
            }
        except Exception as e:
            logger.debug("CoinGecko ETH 情绪数据获取失败: %s", e)
            return None


sentiment_client = SentimentClient()
