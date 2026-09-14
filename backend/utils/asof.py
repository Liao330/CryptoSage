"""
as-of 历史回测支持 —— 把分析"时间旅行"到指定时刻。

核心原则：**杜绝未来数据泄漏**。
- 能按时间取历史的数据源（K线 / 恐惧贪婪 / 资金费率 / OI）→ 只返回 ts <= as_of 的数据；
- 不能回溯的数据源（链上大额转账实时扫描 / 爆仓 / 期权 / 社交实时情绪）→ 返回
  "历史数据不可用"错误，绝不返回"当前"数据冒充历史（那会把未来泄漏进分析）。
"""

from datetime import datetime, timezone


def parse_as_of_ms(as_of: str | None) -> int | None:
    """把 as_of 字符串解析为毫秒时间戳；非法输入返回 None（视为实时模式）。

    支持格式：ISO 8601（含/不含时区，无时区按 UTC）。
    """
    if not as_of:
        return None
    text = str(as_of).strip()
    if not text:
        return None
    try:
        # Z 结尾 → +00:00
        normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


UNAVAILABLE_HINT = "历史模式（as-of）下该数据源不可回溯，本次不提供数据"
