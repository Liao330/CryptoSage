"""
Critic / Risk Agent —— 对抗审查（慢思考）。
对 Synthesis 结论提出反方质疑，消除确认偏误。
"""

import json
import logging
from typing import Any

from backend.agents.state import AnalysisState
from backend.agents.synthesis import _apply_trade_action_guard
from backend.config import config
from backend.llm.hy3_client import hy3_client
from backend.utils.json_utils import parse_json

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个严格的风险审查员（Critic），负责对加密货币分析结论进行对抗式审查。

你的职责是找出分析中的漏洞、偏见和被忽略的风险，而不是简单地赞同。

审查要点：
1. **矛盾信号**：证据中是否存在被忽略的反向信号？
2. **因果陷阱**：相关性 ≠ 因果性。例如：
   - "巨鲸转入交易所"未必是抛售，可能是做市/maker 策略
   - "OI 增长"可能只是套利盘，而非方向性押注
3. **反身性**：市场情绪是否走向极端？
   - 极度贪婪往往是顶部信号（群众疯狂时退出）
   - 极度恐惧往往是底部信号（血流成河时买入）
4. **黑天鹅风险**：对照以下常备尾部风险清单，逐项检查研判是否覆盖（缺失即视为有效质疑）：
   - 交易所安全事件（被盗/宕机/冻结提现）
   - 监管政策突变（SEC诉讼/国家禁令/合规风暴）
   - 稳定币脱锚风险（USDT/USDC脱钩）
   - 大额代币解锁（基金会/团队抛压）
   - 关联市场传导（美股暴跌/美元流动性危机）
   - 极端行情清算链（连环爆仓→市场闪崩）
5. **置信度校准**：Synthesis 的 confidence 是否过高？
   - 如果只有 2 个维度同向且信号模糊，不应给出 >0.7 的置信度
   - 如果数据源存在 degraded，confidence 必须下调
6. **数据质量审查**：检查证据中各维度的 data_quality，若存在 degraded 维度，
   必须在 critiques 中指出"X 维度数据不可靠，结论可能因此偏差"

输出要求：
- 列出具体的有数据支撑的质疑
- 判断是否有"有效质疑"（需要 Synthesis 修正）
- 给出风险边界：在什么条件下当前判断会失效
- black_swan_risks 必须覆盖上述常备清单中研判遗漏的条目

输出格式（严格 JSON）：
```json
{
  "has_valid_critique": true,
  "critiques": [
    {"point": "成交量未放大但结论给出高置信度看多", "severity": "high"},
    {"point": "链上数据仅基于大额转账估算，可能遗漏 OTC 交易", "severity": "medium"}
  ],
  "confidence_adjustment": -0.1,
  "adjusted_confidence": 0.62,
  "risk_boundary": "若价格跌破 59800（前低+POC 汇聚），当前看多判断失效",
  "black_swan_risks": ["交易所安全事件", "监管政策突变"],
  "analysis": "详细审查文本..."
}
```
"""


async def run_critic(state: AnalysisState) -> AnalysisState:
    """Critic 节点：对抗审查 Synthesis 结论。"""
    synthesis = state.get("synthesis_result", {})
    # 守卫：若 synthesis 无 direction（LLM 完全失败），跳过审查直接结束
    if not synthesis.get("direction"):
        state["_critique"] = {"has_valid_critique": False, "critiques": []}
        state["synthesis_result"] = synthesis  # 保持原样
        logger.warning("Critic 跳过：synthesis_result 缺少 direction，无需审查")
        return state

    evidence = state.get("evidence_pool", [])
    critic_round = state.get("critic_round", 0)

    synthesis_str = json.dumps(synthesis, ensure_ascii=False, indent=2)
    evidence_str = json.dumps(evidence, ensure_ascii=False, indent=2)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"综合研判:\n{synthesis_str}\n\n原始证据:\n{evidence_str}\n\n当前审查轮次: {critic_round + 1}/{config.MAX_CRITIC_ROUNDS}\n\n请进行对抗审查，输出 JSON。"},
    ]

    reasoning = ""
    try:
        response = await hy3_client.achat(
            messages=messages,
            # 不传 model：由 UnifiedClient 根据当前 provider 自动选择 slow 模型
            reasoning_effort="high",
            temperature=0.3,
        )
        choice = response.choices[0]
        content = choice.message.content or "{}"
        # 慢思考回传的对抗审查思维链，供前端"审查推理"展示
        reasoning = getattr(choice, "reasoning_content", None) or getattr(choice.message, "reasoning_content", None) or ""
        critique = parse_json(content, default={
            "has_valid_critique": False,
            "critiques": [],
            "confidence_adjustment": 0,
            "risk_boundary": synthesis.get("risk_assessment", ""),
        })
    except Exception as e:
        logger.warning("Critic LLM 调用失败: %s", e)
        critique = {
            "has_valid_critique": False,
            "critiques": [{"point": f"审查失败: {e}", "severity": "low"}],
            "confidence_adjustment": 0,
            "risk_boundary": "无法评估",
        }

    # Critic 只允许在首次有效审查时施加一次风险折扣，最多下调 10 个百分点。
    # 后续修订轮继续提供文字反馈，但不得重复扣减置信度。
    if critique.get("has_valid_critique") and not state.get("_critic_confidence_calibrated", False):
        current_confidence = float(synthesis.get("confidence", 0.0) or 0.0)
        if critique.get("adjusted_confidence") is not None:
            proposed_penalty = float(critique["adjusted_confidence"]) - current_confidence
        else:
            proposed_penalty = float(critique.get("confidence_adjustment", 0.0) or 0.0)
        penalty = max(-0.10, min(0.0, proposed_penalty))
        state["_critic_confidence_calibrated"] = True
        state["_critic_confidence_penalty"] = round(penalty, 2)
        synthesis["confidence"] = round(max(0.0, current_confidence + penalty), 2)
        calibration = synthesis.setdefault("confidence_calibration", {})
        calibration["critic_penalty"] = round(penalty, 2)

    synthesis = _apply_trade_action_guard(synthesis)

    # 添加风险边界
    if critique.get("risk_boundary"):
        synthesis["risk_boundary"] = critique["risk_boundary"]
    if critique.get("black_swan_risks"):
        # 合并而非替换：Critic 的补充追加到 Synthesis 已有的尾部风险后。
        # 关键修复：LLM 有时会返回 dict 形式的风险项（如 {"risk": "...", "severity": "high"}）
        # 而非纯字符串，直接塞进 set() 会抛 "unhashable type: 'dict'" 导致整轮分析崩溃
        # （异常冒泡到最外层 main.py，最终吐出无 direction 的错误对象，前端显示 undefined）。
        # 这里统一归一化为字符串再去重。
        def _risk_to_str(r: Any) -> str:
            if isinstance(r, str):
                return r
            if isinstance(r, dict):
                return r.get("risk") or r.get("point") or r.get("name") or json.dumps(r, ensure_ascii=False)
            return str(r)

        existing_list = [_risk_to_str(r) for r in synthesis.get("black_swan_risks", [])]
        existing = set(existing_list)
        merged = list(existing_list)
        for risk in critique["black_swan_risks"]:
            risk_str = _risk_to_str(risk)
            if risk_str not in existing:
                existing.add(risk_str)
                merged.append(risk_str)
        synthesis["black_swan_risks"] = merged

    # 记录批评
    feedback = state.get("critic_feedback", [])
    for c in critique.get("critiques", []):
        feedback.append(c.get("point", ""))
    state["critic_feedback"] = feedback
    state["critic_round"] = critic_round + 1
    state["synthesis_result"] = synthesis
    state["_critique"] = critique

    trace = state.get("trace", [])
    trace.append({
        "node": "critic",
        "round": critic_round + 1,
        "has_valid_critique": critique.get("has_valid_critique"),
        "confidence_adjustment": critique.get("confidence_adjustment"),
        "critiques": [c.get("point", "") for c in critique.get("critiques", []) if c.get("point")],
        "reasoning": reasoning,
    })
    state["trace"] = trace

    return state


def _should_continue_critic_loop(state: AnalysisState) -> str:
    """判断是否继续 Critic 循环。

    扩宽修订触发条件：不仅有 high severity，以下 medium 级别也触发修订——
    - 尾部风险覆盖不足（含关键词 black_swan/尾部/监管/脱锚/安全事件）
    - 置信度校准偏差（含关键词 confidence/校准/过高/偏低）
    - 数据质量质疑（含关键词 数据/缺失/degraded/不可靠/偏差）
    """
    critique = state.get("_critique", {})
    round_num = state.get("critic_round", 0)
    max_rounds = config.MAX_CRITIC_ROUNDS

    if round_num >= max_rounds:
        return "finalize"

    if not critique.get("has_valid_critique"):
        return "finalize"

    critiques = critique.get("critiques", [])

    # high → 必触发
    high_sev = any(c.get("severity") == "high" for c in critiques)
    if high_sev:
        return "synthesis_revise"

    # medium 但涉及关键领域 → 也触发
    _revise_keywords = [
        "black_swan", "尾部", "黑天鹅", "监管", "脱锚", "安全事件",
        "confidence", "置信度", "校准", "过高", "偏低",
        "数据缺失", "数据不可靠", "degraded", "来源异常", "系统性偏差",
    ]
    for c in critiques:
        if c.get("severity") == "medium":
            point = c.get("point", "")
            if any(kw in point.lower() for kw in _revise_keywords):
                return "synthesis_revise"

    return "finalize"
