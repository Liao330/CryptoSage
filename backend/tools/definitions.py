"""
Function Calling 工具 schema 定义。
每个工具定义用于 OpenAI-compatible API 的 tools 参数。
"""

# ── K线与行情工具 ──

GET_KLINES_TOOL = {
    "type": "function",
    "function": {
        "name": "get_klines",
        "description": "获取指定交易对的K线历史数据",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "交易对，如 BTC-USDT, ETH-USDT",
                },
                "bar": {
                    "type": "string",
                    "enum": ["1m", "5m", "15m", "30m", "1H", "4H", "1D", "1W"],
                    "description": "K线周期",
                },
                "limit": {
                    "type": "integer",
                    "description": "获取数量，默认200",
                    "default": 200,
                },
            },
            "required": ["symbol", "bar"],
        },
    },
}

CALC_INDICATORS_TOOL = {
    "type": "function",
    "function": {
        "name": "calc_indicators",
        "description": "计算技术指标（MA/MACD/RSI/BOLL），需要先获取K线数据",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "交易对",
                },
                "bar": {
                    "type": "string",
                    "enum": ["1m", "5m", "15m", "30m", "1H", "4H", "1D", "1W"],
                    "description": "K线周期",
                },
            },
            "required": ["symbol", "bar"],
        },
    },
}

CALC_KEY_LEVELS_TOOL = {
    "type": "function",
    "function": {
        "name": "calc_key_levels",
        "description": "从多来源算法推导关键价位（前高前低/成交量密集区/斐波那契/布林带/EMA），产出可追溯的关键支撑阻力位",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "交易对",
                },
                "bar": {
                    "type": "string",
                    "enum": ["1m", "5m", "15m", "30m", "1H", "4H", "1D", "1W"],
                    "description": "K线周期，默认4H",
                    "default": "4H",
                },
            },
            "required": ["symbol"],
        },
    },
}

# ── 链上数据工具 ──

GET_WHALE_FLOWS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_whale_flows",
        "description": "获取鲸鱼大额转账记录（ETH 链）",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "币种，BTC 或 ETH",
                },
                "limit": {
                    "type": "integer",
                    "description": "获取条数，默认20",
                    "default": 20,
                },
            },
            "required": ["symbol"],
        },
    },
}

GET_EXCHANGE_NETFLOW_TOOL = {
    "type": "function",
    "function": {
        "name": "get_exchange_netflow",
        "description": "获取交易所热钱包净流入/流出（ETH 链）",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "币种，BTC 或 ETH",
                },
            },
            "required": ["symbol"],
        },
    },
}

GET_GOVERNMENT_EXCHANGE_DEPOSITS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_government_exchange_deposits",
        "description": "验证已配置政府钱包在最近窗口内是否向交易所热钱包入金；未配置经过核验的钱包地址时必须返回未确认，不得根据新闻标题猜测。",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "币种，如 BTC 或 ETH"},
                "lookback_hours": {"type": "integer", "description": "回看小时数，默认24", "default": 24},
            },
            "required": ["symbol"],
        },
    },
}

# ── 衍生品数据工具 ──

GET_FUNDING_RATE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_funding_rate",
        "description": "获取永续合约当前资金费率",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "交易对，如 BTCUSDT, ETHUSDT",
                },
            },
            "required": ["symbol"],
        },
    },
}

GET_OPEN_INTEREST_TOOL = {
    "type": "function",
    "function": {
        "name": "get_open_interest",
        "description": "获取合约未平仓合约量（OI）及历史变化",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "交易对，如 BTCUSDT, ETHUSDT",
                },
                "period": {
                    "type": "string",
                    "enum": ["5m", "15m", "30m", "1H", "4H", "1D"],
                    "description": "数据粒度（与K线周期命名一致）",
                    "default": "1H",
                },
                "limit": {
                    "type": "integer",
                    "description": "获取条数",
                    "default": 30,
                },
            },
            "required": ["symbol"],
        },
    },
}

GET_LIQUIDATIONS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_liquidations",
        "description": "获取近期爆仓订单数据，分析多空爆仓密集区（可作为关键价位佐证）",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "交易对，如 BTCUSDT, ETHUSDT",
                },
                "limit": {
                    "type": "integer",
                    "description": "获取条数",
                    "default": 100,
                },
            },
            "required": ["symbol"],
        },
    },
}

GET_OPTIONS_SNAPSHOT_TOOL = {
    "type": "function",
    "function": {
        "name": "get_options_snapshot",
        "description": "获取 Deribit BTC/ETH 近月期权链：Call/Put 未平仓量、最大痛点、期权墙与距离交割时间。OI 不能单独证明方向，结果只作为辅助证据。",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "币种，如 BTC 或 ETH",
                },
            },
            "required": ["symbol"],
        },
    },
}

# ── 舆情情绪工具 ──

GET_FEAR_GREED_TOOL = {
    "type": "function",
    "function": {
        "name": "get_fear_greed",
        "description": "获取加密货币恐惧贪婪指数（0-100），及近期趋势。极度恐惧(≤25)=潜在底部，极度贪婪(≥75)=潜在顶部（反身性反向指标）",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "获取天数，默认30",
                    "default": 30,
                },
            },
            "required": [],
        },
    },
}

GET_SOCIAL_SENTIMENT_TOOL = {
    "type": "function",
    "function": {
        "name": "get_social_sentiment",
        "description": "获取社交媒体情绪评分（基于恐惧贪婪指数等公开数据源）",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "币种，BTC 或 ETH",
                },
            },
            "required": ["symbol"],
        },
    },
}

# ── 宏观/地缘工具 ──

SEARCH_MACRO_NEWS_TOOL = {
    "type": "function",
    "function": {
        "name": "search_macro_news",
        "description": (
            "联网搜索币圈动态、美国政治/经济/货币政策及直接影响加密资产的全球风险（如美伊冲突、FOMC会议、OPEC决议、稳定币脱锚、美国政府/US Marshals出售或转移BTC/ETH等），评估对加密货币市场的潜在影响。中国国内政策、中文财经媒体和中国市场新闻不属于默认方向性范围。\n"
            "⚠ query 参数关键约束：底层为 Google News RSS，采用全词精确匹配（AND逻辑），"
            "关键词越多越难命中同一篇文章标题/摘要，堆叠 5 个以上词几乎必然返回 0 结果。\n"
            "正确用法：query 必须精简为 **2-3 个最核心的名词/术语**（如 '稳定币 脱锚'、'FOMC 利率'、'美伊冲突'），"
            "**禁止**在 query 中额外拼接年份/月份等时间文字（如 '2026'、'July'、'when'），"
            "时间范围只能通过 time_window 参数控制，重复写时间词只会降低命中率、不会提升相关性。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "2-3 个核心关键词，不含年份/月份等时间文字，如 '美伊冲突'、'FOMC 利率'、'稳定币 脱锚'",
                },
                "time_window": {
                    "type": "string",
                    "enum": ["24h", "7d", "30d"],
                    "description": "时间范围，如 '24h', '7d', '30d'",
                    "default": "24h",
                },
            },
            "required": ["query"],
        },
    },
}

# ── 聚合所有工具列表 ──

ALL_TOOLS = [
    GET_KLINES_TOOL,
    CALC_INDICATORS_TOOL,
    CALC_KEY_LEVELS_TOOL,
    GET_WHALE_FLOWS_TOOL,
    GET_EXCHANGE_NETFLOW_TOOL,
    GET_GOVERNMENT_EXCHANGE_DEPOSITS_TOOL,
    GET_FUNDING_RATE_TOOL,
    GET_OPEN_INTEREST_TOOL,
    GET_LIQUIDATIONS_TOOL,
    GET_OPTIONS_SNAPSHOT_TOOL,
    GET_FEAR_GREED_TOOL,
    GET_SOCIAL_SENTIMENT_TOOL,
    SEARCH_MACRO_NEWS_TOOL,
]

# ── 按 Agent 分组的工具映射 ──

TECHNICAL_TOOLS = [GET_KLINES_TOOL, CALC_INDICATORS_TOOL, CALC_KEY_LEVELS_TOOL]
ONCHAIN_TOOLS = [GET_WHALE_FLOWS_TOOL, GET_EXCHANGE_NETFLOW_TOOL, GET_GOVERNMENT_EXCHANGE_DEPOSITS_TOOL]
DERIVATIVES_TOOLS = [GET_FUNDING_RATE_TOOL, GET_OPEN_INTEREST_TOOL, GET_LIQUIDATIONS_TOOL, GET_OPTIONS_SNAPSHOT_TOOL]
SENTIMENT_TOOLS = [GET_FEAR_GREED_TOOL, GET_SOCIAL_SENTIMENT_TOOL]
MACRO_TOOLS = [SEARCH_MACRO_NEWS_TOOL]
