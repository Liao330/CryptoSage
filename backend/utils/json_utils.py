"""
JSON 解析工具 —— 从 LLM 响应中提取 JSON，统一去除 markdown 代码块。
消除各 Agent 中重复的 _parse_json / _parse_signal 逻辑。
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def parse_json(text: str, default: dict | None = None) -> dict:
    """从文本中提取 JSON 对象。

    处理：
    1. 去除 ```json ... ``` markdown 代码块
    2. 查找第一个 { ... } 块
    3. 解析失败返回 default

    Args:
        text: LLM 原始响应文本
        default: 解析失败时的默认值（None 则返回 {"error": "JSON 解析失败"}）
    """
    if not text:
        return default or {"error": "空响应"}

    text = text.strip()

    # 去除 markdown 代码块标记
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 查找第一个 { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            logger.warning("JSON 提取失败，文本前 200 字符: %s", text[:200])

    if default is not None:
        return default
    return {"error": "JSON 解析失败", "raw": text[:500]}


def parse_signal(
    text: str,
    agent: str,
    symbol: str = "",
    defaults: dict[str, Any] | None = None,
) -> dict:
    """从 LLM 响应中解析标准化信号 JSON。

    所有数据 Agent 共用此函数，根据 agent 类型设置不同的默认值。
    data_quality 默认 "degraded"（即 LLM 端走 fallback 路径），
    让 Orchestrator 早退机制能正确识别"这条信号不可信"。

    Args:
        text: LLM 响应文本
        agent: Agent 名称 (technical/onchain/derivatives/sentiment/macro)
        symbol: 交易对/币种
        defaults: 额外的默认字段 (如 macro 的 key_events / derivatives 的 liquidation_clusters)

    Returns:
        标准化信号 dict
    """
    base_defaults = {
        "agent": agent,
        "symbol": symbol,
        "bias": "neutral",
        "score": 50,
        "confidence": 0.5,
        "evidence": [],
        "caveats": [],
        "data_quality": "degraded",
    }
    if defaults:
        base_defaults.update(defaults)

    signal = parse_json(text, default=base_defaults)
    # 确保必填字段
    for key, val in base_defaults.items():
        signal.setdefault(key, val)
    return signal
