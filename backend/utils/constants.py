"""
CryptoSage 常量定义 —— 消除各文件中分散的硬编码字符串。
"""

from enum import Enum


class Bias(str, Enum):
    """信号偏向。"""
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class AgentName(str, Enum):
    """Agent 标识。"""
    TECHNICAL = "technical"
    ONCHAIN = "onchain"
    DERIVATIVES = "derivatives"
    SENTIMENT = "sentiment"
    MACRO = "macro"
    ORCHESTRATOR = "orchestrator"
    SYNTHESIS = "synthesis"
    CRITIC = "critic"


class Bar(str, Enum):
    """K线周期。"""
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1H"
    H4 = "4H"
    D1 = "1D"
    W1 = "1W"


class DefaultSymbol(str, Enum):
    """默认交易对。"""
    BTC_USDT = "BTC-USDT"
    ETH_USDT = "ETH-USDT"


class SignalScore:
    """信号分数常量。"""
    NEUTRAL = 50        # 中性基准分
    MIN = 0             # 最低分（极端看空）
    MAX = 100           # 最高分（极端看多）
    BULLISH_THRESHOLD = 60   # 高于此值为看多
    BEARISH_THRESHOLD = 40   # 低于此值为看空


class ConfidenceThreshold:
    """置信度阈值。"""
    POSITION_START = 0.6        # 开仓最低置信度
    LIGHT_POSITION = 0.75       # 轻仓上限
    STANDARD_POSITION = 0.75    # 标准仓最低
    STOP_LOSS_PCT = 8.0         # 止损距离 > 8% 下调仓位


class DefaultConfig:
    """默认配置值。"""
    MAX_ORCHESTRATOR_STEPS = 10
    MIN_CONFIDENCE_THRESHOLD = 0.7
    MAX_CRITIC_ROUNDS = 4
    MAX_FC_ROUNDS = 5
    KLINE_LIMIT = 200
    TASK_CLEANUP_DELAY = 120.0
    WS_HEARTBEAT_TIMEOUT = 30


class EventBuffer:
    """事件缓冲限制。"""
    MAX_EVENTS_PER_TASK = 500       # 单任务最大事件数
