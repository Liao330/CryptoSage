"""
CryptoSage 配置模块 —— 全部从环境变量读取，绝不硬编码密钥。
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# 显式指定 .env 路径（crypto-analysis-agent 项目根），避免读取到父目录的 .env
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(dotenv_path=str(_ENV_PATH))
else:
    load_dotenv()


def _parse_exchange_btc_addresses(raw: str) -> dict[str, tuple[str, ...]]:
    """Parse ``Exchange:address|address`` entries while merging repeated exchanges."""
    parsed: dict[str, list[str]] = {}
    for item in raw.split(","):
        if ":" not in item:
            continue
        exchange, addresses = item.split(":", 1)
        exchange = exchange.strip()
        if not exchange:
            continue
        parsed.setdefault(exchange, [])
        parsed[exchange].extend(
            address.strip()
            for address in addresses.split("|")
            if address.strip()
        )
    return {exchange: tuple(addresses) for exchange, addresses in parsed.items()}


class Config:
    """全局配置，所有敏感信息从 .env 读取。"""

    # LLM Provider 选择: hy3 | deepseek
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "hy3").strip().lower()

    # Hy3 API
    HY3_API_KEY: str = os.getenv("HY3_API_KEY", "")
    HY3_BASE_URL: str = os.getenv("HY3_BASE_URL", "https://tokenhub.tencentmaas.com/v1")
    HY3_MODEL: str = os.getenv("HY3_MODEL", "hy3")
    HY3_FAST_MODEL: str = os.getenv("HY3_FAST_MODEL", os.getenv("HY3_MODEL", "hy3"))
    HY3_SLOW_MODEL: str = os.getenv("HY3_SLOW_MODEL", os.getenv("HY3_MODEL", "hy3"))

    # DeepSeek API (LLM_PROVIDER=deepseek 时使用)
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    DEEPSEEK_FAST_MODEL: str = os.getenv("DEEPSEEK_FAST_MODEL", "deepseek-v4-flash")
    DEEPSEEK_SLOW_MODEL: str = os.getenv("DEEPSEEK_SLOW_MODEL", "deepseek-v4-pro")

    # Etherscan
    ETHERSCAN_API_KEY: str = os.getenv("ETHERSCAN_API_KEY", "")

    # Deribit 期权网络：企业网络拦截时可单独指定代理/备用 API 入口。
    DERIBIT_API_BASE_URL: str = os.getenv(
        "DERIBIT_API_BASE_URL", "https://www.deribit.com/api/v2/public"
    ).rstrip("/")
    DERIBIT_PROXY_URL: str = os.getenv("DERIBIT_PROXY_URL", "").strip()

    # 可选的政府钱包观察名单。没有配置时不宣称链上已确认政府入金。
    GOVERNMENT_ETH_ADDRESSES: tuple[str, ...] = tuple(
        address.strip().lower()
        for address in os.getenv("GOVERNMENT_ETH_ADDRESSES", "").split(",")
        if address.strip()
    )
    GOVERNMENT_BTC_ADDRESSES: tuple[str, ...] = tuple(
        address.strip()
        for address in os.getenv("GOVERNMENT_BTC_ADDRESSES", "").split(",")
        if address.strip()
    )
    # BTC 交易所收款地址需要由运营方按链上标签核验后配置，格式为
    # `Exchange:address,Exchange:address`。不配置时不会把 BTC 转账升级为出售确认。
    EXCHANGE_BTC_ADDRESSES: dict[str, tuple[str, ...]] = _parse_exchange_btc_addresses(
        os.getenv("EXCHANGE_BTC_ADDRESSES", "")
    )

    # 仓位建议开关
    ENABLE_POSITION_ADVICE: bool = os.getenv("ENABLE_POSITION_ADVICE", "true").lower() == "true"

    # 数据源可用性开关：网络不可达的数据源直接禁用，让相关 Agent 完全无感知
    # （不进系统提示词、不进工具表、不做"获取失败"降级标注），而不是反复
    # 重试后带着"数据盲区"警告污染研判。
    # ENABLE_MACRO_AGENT=false  : 禁用宏观/地缘 Agent（联网新闻搜索整条链路）
    # ENABLE_OPTIONS_TOOL=false : 禁用 Deribit 期权工具（衍生品 Agent 保留费率/OI/爆仓）
    ENABLE_MACRO_AGENT: bool = os.getenv("ENABLE_MACRO_AGENT", "true").lower() in ("1", "true", "yes")
    ENABLE_OPTIONS_TOOL: bool = os.getenv("ENABLE_OPTIONS_TOOL", "true").lower() in ("1", "true", "yes")

    # Agent 控制参数（质量优先于速度：适当放宽轮次上限，仅作为防死循环兜底，
    # 不作为常规限速手段）
    MAX_ORCHESTRATOR_STEPS: int = int(os.getenv("MAX_ORCHESTRATOR_STEPS", "10"))
    MIN_CONFIDENCE_THRESHOLD: float = float(os.getenv("MIN_CONFIDENCE_THRESHOLD", "0.7"))
    MAX_CRITIC_ROUNDS: int = int(os.getenv("MAX_CRITIC_ROUNDS", "2"))
    MAX_CONCURRENT_ANALYSES: int = int(os.getenv("MAX_CONCURRENT_ANALYSES", "4"))

    # 回测采用固定结算周期，避免任意时点手工结算污染胜率。
    BACKTEST_HORIZON_HOURS: int = int(os.getenv("BACKTEST_HORIZON_HOURS", "24"))
    BACKTEST_ROUND_TRIP_COST_BPS: float = float(
        os.getenv("BACKTEST_ROUND_TRIP_COST_BPS", "10")
    )
    SHADOW_SETTLEMENT_INTERVAL_SECONDS: int = int(
        os.getenv("SHADOW_SETTLEMENT_INTERVAL_SECONDS", "300")
    )

    # 联网搜索 provider（自建 search_web function，默认走免费 Google News + DuckDuckGo）
    MACRO_SEARCH_PROVIDER: str = os.getenv("MACRO_SEARCH_PROVIDER", "web_search")
    # 币圈宏观新闻默认使用美国英文新闻区域；避免 Google News 默认的
    # 中文区域把中国本地财经报道排到前面。
    MACRO_NEWS_LANGUAGE: str = os.getenv("MACRO_NEWS_LANGUAGE", "en-US")
    MACRO_NEWS_REGION: str = os.getenv("MACRO_NEWS_REGION", "US")
    MACRO_NEWS_EDITION: str = os.getenv("MACRO_NEWS_EDITION", "US:en")
    MACRO_DDG_REGION: str = os.getenv("MACRO_DDG_REGION", "us-en")

    # SSRF 白名单域名 —— 仅允许请求以下外部 API
    ALLOWED_HOSTS: list[str] = [
        "www.okx.com",
        "api.binance.com",
        "fapi.binance.com",
        "fstream.binance.com",
        "api.etherscan.io",
        "api.alternative.me",
        "api.gateio.ws",
        "api.coingecko.com",
        "www.deribit.com",        # BTC/ETH 期权公开市场数据
        "api.deribit.com",        # Deribit 备用入口
        "mempool.space",           # BTC 链上：区块/大额交易扫描（blockchain.info 在当前网络环境不可达，已替换）
        "news.google.com",         # 宏观：Google News RSS 联网搜索
        "lite.duckduckgo.com",     # 宏观：DuckDuckGo 联网搜索
        "api.tavily.com",
        "serpapi.com",
    ]

    # 数据库路径
    DB_PATH: str = os.getenv(
        "SQLITE_DB_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cache.db"),
    )

    # SSL 验证开关
    SSL_VERIFY: bool = os.getenv("SSL_VERIFY", "true").strip().lower() not in ("false", "0", "no", "off")

    # 日志配置
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
    LOG_FILE: str = os.getenv("LOG_FILE", "")  # 留空则仅输出 stdout

    @classmethod
    def validate(cls) -> list[str]:
        """启动前校验必填配置，返回缺失项列表。"""
        missing = []
        if cls.LLM_PROVIDER == "deepseek":
            if not cls.DEEPSEEK_API_KEY:
                missing.append("DEEPSEEK_API_KEY")
        else:
            if not cls.HY3_API_KEY:
                missing.append("HY3_API_KEY")
        return missing


config = Config()
