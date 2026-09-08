"""
数据 Agent 的 Function Calling 通用执行器。

LLM 与工具调用均原生异步执行，因此任务取消会传播到当前 HTTP 请求，且多个数据 Agent
可在同一个事件循环中并行运行。
"""

from typing import Any, Awaitable, Callable

from backend.llm.hy3_client import hy3_client


async def run_function_calling(
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    async_tool_map: dict[str, Callable[..., Awaitable[Any]]],
    model: str | None = None,
    max_rounds: int = 5,
    reasoning_effort: str | None = "low",
) -> dict:
    """执行可取消的异步 Function Calling 循环。

    Args:
        reasoning_effort: 思考档位。**必须为非 None**（"low"/"high"）Hy3 端点才会回传
            `reasoning_content`；数据 Agent 默认走快思考 "low"，以便前端能展示思维链。

    Returns:
        {"final_message": str, "reasoning_content": str, "tool_calls_log": [...]}
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    result = await hy3_client.afunction_calling_loop(
        messages=messages,
        tools=tools,
        tool_map=async_tool_map,
        # 不传 config.HY3_FAST_MODEL 作硬编码兜底：由 UnifiedClient._resolve_model
        # 根据当前激活的 provider (hy3/deepseek) 自动选择对应的 fast 模型。
        # 若调用方显式传了 model 则仍尊重该值。
        model=model,
        reasoning_effort=reasoning_effort,
        max_rounds=max_rounds,
    )
    return result
