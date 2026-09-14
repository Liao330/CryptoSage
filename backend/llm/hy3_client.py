"""
统一 LLM 客户端 —— Hy3 + DeepSeek 自动 failover。
通过 LLM_PROVIDER 指定主 provider，不可用时自动切到备选。
"""

import json
import logging
import os
import time
from typing import Any

from openai import AsyncOpenAI, OpenAI
from backend.config import config

logger = logging.getLogger(__name__)

# 触发 failover 的错误类型（状态码或异常类）
_FAILOVER_STATUSES = {401, 402, 403, 429, 500, 502, 503, 504}
_FAILOVER_COOLDOWN = 120  # 秒，冷却期过后重试主 provider

# ── Provider 工厂 ──

# 单次请求超时：high 档 reasoning 实测常需 20-60s，给足余量，
# 避免网络抖动时过早超时触发 failover/降级。
# 走本地代理（如 CodeBuddy LLM Proxy）时慢思考调用可能超过 120s，
# 可通过环境变量 LLM_REQUEST_TIMEOUT 放宽。
_REQUEST_TIMEOUT = float(os.getenv("LLM_REQUEST_TIMEOUT", "120"))


def _make_hy3():
    return (
        OpenAI(api_key=config.HY3_API_KEY, base_url=config.HY3_BASE_URL, timeout=_REQUEST_TIMEOUT),
        AsyncOpenAI(api_key=config.HY3_API_KEY, base_url=config.HY3_BASE_URL, timeout=_REQUEST_TIMEOUT),
        config.HY3_FAST_MODEL,
        config.HY3_SLOW_MODEL,
        True,  # supports reasoning_effort
    )


def _make_deepseek():
    return (
        OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL, timeout=_REQUEST_TIMEOUT),
        AsyncOpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL, timeout=_REQUEST_TIMEOUT),
        config.DEEPSEEK_FAST_MODEL,
        config.DEEPSEEK_SLOW_MODEL,
        False,  # no reasoning_effort param; use model name to distinguish
    )


# ── 统一客户端 ──

class UnifiedClient:
    """Hy3 / DeepSeek 双 provider，支持自动 failover。

    使用方式与之前完全一致，所有 Agent 无需改动。
    """

    def __init__(self):
        provider = config.LLM_PROVIDER
        if provider == "deepseek":
            self._primary_label = "deepseek"
            self._fallback_label = "hy3"
            self._make_primary = _make_deepseek
            self._make_fallback = _make_hy3 if config.HY3_API_KEY else None
        else:
            self._primary_label = "hy3"
            self._fallback_label = "deepseek"
            self._make_primary = _make_hy3
            self._make_fallback = _make_deepseek if config.DEEPSEEK_API_KEY else None

        # 初始化主 provider
        (
            self._client,
            self._async_client,
            self._fast,
            self._slow,
            self._supports_effort,
        ) = self._make_primary()
        self._active_label = self._primary_label
        self._failover_until: float = 0.0
        self._fallback_ready = False

        # 预初始化 fallback（延迟到首次 failover 时）
        if self._make_fallback:
            try:
                (
                    self._fb_client,
                    self._fb_async_client,
                    self._fb_fast,
                    self._fb_slow,
                    self._fb_effort,
                ) = self._make_fallback()
                self._fallback_ready = True
                logger.info(
                    "LLM: 主=%s 备=%s (fast=%s/%s slow=%s/%s)",
                    self._primary_label, self._fallback_label,
                    self._fast, self._fb_fast, self._slow, self._fb_slow,
                )
            except Exception as e:
                logger.warning("备 provider 初始化失败: %s", e)
        else:
            logger.info("LLM: 主=%s (无备选)", self._primary_label)

    # ── active client shortcuts ──
    @property
    def _active_client(self):
        return self._fb_client if self._is_failed_over() else self._client

    @property
    def _active_async_client(self):
        return self._fb_async_client if self._is_failed_over() else self._async_client

    @property
    def _active_fast(self):
        return self._fb_fast if self._is_failed_over() else self._fast

    @property
    def _active_slow(self):
        return self._fb_slow if self._is_failed_over() else self._slow

    @property
    def _active_effort_support(self):
        return self._fb_effort if self._is_failed_over() else self._supports_effort

    def _is_failed_over(self) -> bool:
        return self._fallback_ready and time.time() < self._failover_until

    def _activate_failover(self):
        if not self._fallback_ready:
            logger.error("备 provider 不可用，无法 failover")
            return False
        self._failover_until = time.time() + _FAILOVER_COOLDOWN
        logger.warning(
            "▸ 主 provider (%s) 触发 failover → 切入 %s (冷却 %ds)",
            self._primary_label, self._fallback_label, _FAILOVER_COOLDOWN,
        )
        self._active_label = self._fallback_label
        return True

    def _try_recover(self):
        """冷却期过后尝试切回主 provider。"""
        if (
            self._fallback_ready
            and self._active_label == self._fallback_label
            and time.time() >= self._failover_until
        ):
            logger.info("冷却结束，切回主 provider (%s)", self._primary_label)
            self._active_label = self._primary_label
            self._failover_until = 0.0

    def _should_failover(self, error: Exception) -> bool:
        """判断异常是否应触发 failover。"""
        status = getattr(error, "status_code", None) or getattr(error, "code", None)
        if status and int(status) in _FAILOVER_STATUSES:
            return True
        etype = type(error).__name__
        # 连接/超时类错误也触发
        return etype in ("ConnectError", "ConnectTimeout", "ReadTimeout",
                         "ConnectionError", "APITimeoutError", "RemoteDisconnected")

    # ── 模型解析 ──
    def _resolve_model(self, model: str | None, reasoning_effort: str | None) -> str:
        """根据 reasoning_effort 选择 fast/slow 模型。

        优先级：1) 显式传入 model → 2) 根据 reasoning_effort 从 active provider 选择
        注意：Hy3 虽通过 reasoning_effort 参数控制思考档位，但仍需使用正确的模型名
        （FAST_MODEL/SLOW_MODEL），因为模型 token 限额和 context 窗口不同。
        """
        if model:
            return model
        # 慢思考 → 慢模型；快思考 / 未指定 → 快模型
        return self._active_slow if reasoning_effort == "high" else self._active_fast

    # ── 公共 API ──

    def chat(self, messages, model=None, reasoning_effort=None, tools=None,
             tool_choice="auto", temperature=0.3, max_tokens=None):
        return self._call("chat", messages=messages, model=model,
                          reasoning_effort=reasoning_effort, tools=tools,
                          tool_choice=tool_choice, temperature=temperature,
                          max_tokens=max_tokens)

    async def achat(self, messages, model=None, reasoning_effort=None, tools=None,
                    tool_choice="auto", temperature=0.3, max_tokens=None):
        return await self._acall(
            "chat",
            messages=messages,
            model=model,
            reasoning_effort=reasoning_effort,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def chat_stream(self, messages, model=None, reasoning_effort=None, tools=None,
                    temperature=0.3, max_tokens=None):
        return self._call("stream", messages=messages, model=model,
                          reasoning_effort=reasoning_effort, tools=tools,
                          temperature=temperature, max_tokens=max_tokens)

    def function_calling_loop(self, messages, tools, tool_map, model=None,
                               reasoning_effort=None, max_rounds=10, max_tokens=None):
        return self._call("fc_loop", messages=messages, tools=tools,
                          tool_map=tool_map, model=model,
                          reasoning_effort=reasoning_effort,
                          max_rounds=max_rounds, max_tokens=max_tokens)

    async def afunction_calling_loop(
        self,
        messages,
        tools,
        tool_map,
        model=None,
        reasoning_effort=None,
        max_rounds=10,
        max_tokens=None,
    ):
        return await self._acall(
            "fc_loop",
            messages=messages,
            tools=tools,
            tool_map=tool_map,
            model=model,
            reasoning_effort=reasoning_effort,
            max_rounds=max_rounds,
            max_tokens=max_tokens,
        )

    async def aclose(self) -> None:
        await self._async_client.close()
        self._client.close()
        if self._fallback_ready:
            await self._fb_async_client.close()
            self._fb_client.close()

    def _call(self, method: str, **kwargs):
        """统一调用入口，带自动 failover 与恢复。"""
        self._try_recover()
        label_before = self._active_label

        try:
            result = self._do_call(method, **kwargs)
            # 成功后：若之前在 failover 状态但冷却未到期（手动恢复），不作处理；
            # 由 _try_recover 在下次调用时判断
            return result
        except Exception as e:
            if self._should_failover(e) and self._activate_failover():
                logger.info("Failover 后重试请求...")
                return self._do_call(method, **kwargs)
            raise

    async def _acall(self, method: str, **kwargs):
        """Async call path; cancellation propagates into the provider HTTP request."""
        self._try_recover()
        try:
            return await self._ado_call(method, **kwargs)
        except Exception as error:
            if self._should_failover(error) and self._activate_failover():
                logger.info("Failover 后异步重试请求...")
                return await self._ado_call(method, **kwargs)
            raise

    def _do_call(self, method: str, **kwargs):
        model = self._resolve_model(kwargs.get("model"), kwargs.get("reasoning_effort"))
        messages: list = kwargs.get("messages", [])
        tools: list | None = kwargs.get("tools")
        temperature: float = kwargs.get("temperature", 0.3)
        max_tokens: int | None = kwargs.get("max_tokens", None)
        tool_choice: str = kwargs.get("tool_choice", "auto")
        reasoning_effort: str | None = kwargs.get("reasoning_effort")
        client = self._active_client

        build = {
            "messages": messages,
            "model": model,
            "temperature": temperature,
        }
        # 仅当显式指定时传 max_tokens；None 时走 API 默认最大值（≈∞）
        if max_tokens is not None:
            build["max_tokens"] = max_tokens
        if self._active_effort_support and reasoning_effort:
            build["reasoning_effort"] = reasoning_effort
        if tools:
            build["tools"] = tools
            build["tool_choice"] = tool_choice

        if method in ("chat", "fc_loop"):
            response = client.chat.completions.create(**build)
            if method == "chat":
                return response
            return self._run_fc_loop(client, messages, tools, kwargs.get("tool_map", {}),
                                     model, reasoning_effort, max_tokens,
                                     kwargs.get("max_rounds", 10), response)

        # stream
        build["stream"] = True
        return client.chat.completions.create(**build)

    async def _ado_call(self, method: str, **kwargs):
        model = self._resolve_model(kwargs.get("model"), kwargs.get("reasoning_effort"))
        messages: list = kwargs.get("messages", [])
        tools: list | None = kwargs.get("tools")
        max_tokens: int | None = kwargs.get("max_tokens")
        reasoning_effort: str | None = kwargs.get("reasoning_effort")
        build = {
            "messages": messages,
            "model": model,
            "temperature": kwargs.get("temperature", 0.3),
        }
        if max_tokens is not None:
            build["max_tokens"] = max_tokens
        if self._active_effort_support and reasoning_effort:
            build["reasoning_effort"] = reasoning_effort
        if tools:
            build["tools"] = tools
            build["tool_choice"] = kwargs.get("tool_choice", "auto")

        client = self._active_async_client
        response = await client.chat.completions.create(**build)
        if method == "chat":
            return response
        return await self._run_fc_loop_async(
            client,
            messages,
            tools,
            kwargs.get("tool_map", {}),
            model,
            reasoning_effort,
            max_tokens,
            kwargs.get("max_rounds", 10),
            response,
        )

    def _run_fc_loop(self, client, messages, tools, tool_map, model,
                     reasoning_effort, max_tokens, max_rounds, first_response):
        tool_calls_log: list = []
        reasoning_buf: list = []
        response = first_response

        for _round in range(max_rounds):
            choice = response.choices[0]
            msg = choice.message
            rc = getattr(choice, "reasoning_content", None) or getattr(msg, "reasoning_content", None)
            if rc:
                reasoning_buf.append(rc)

            if not msg.tool_calls:
                return {
                    "final_message": msg.content or "",
                    "reasoning_content": "\n".join(reasoning_buf),
                    "tool_calls_log": tool_calls_log,
                }

            messages.append({
                "role": "assistant", "content": msg.content,
                "tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name,
                                             "arguments": tc.function.arguments}}
                               for tc in msg.tool_calls],
            })

            for tc in msg.tool_calls:
                func_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_result = {"error": f"未知工具: {func_name}"}
                if func_name in tool_map:
                    try:
                        tool_result = tool_map[func_name](**args)
                    except Exception as e:
                        tool_result = {"error": str(e)}
                        logger.warning("工具 %s 失败: %s", func_name, e)
                messages.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": json.dumps(tool_result, ensure_ascii=False),
                })
                tool_calls_log.append({
                    "function": func_name, "arguments": args, "result": tool_result,
                })

            # 下一轮
            build: dict = {"messages": messages, "model": model,
                     "temperature": 0.3, "tools": tools, "tool_choice": "auto"}
            if max_tokens is not None:
                build["max_tokens"] = max_tokens
            if self._active_effort_support and reasoning_effort:
                build["reasoning_effort"] = reasoning_effort
            response = client.chat.completions.create(**build)

        # 超轮数：最后一次调用保留 reasoning_effort（确保慢思考不被截断）
        logger.warning("FC 循环达到最大轮数 (%d)，强制结案", max_rounds)
        final_build: dict = {
            "messages": messages,
            "model": model,
            "temperature": 0.3,
        }
        if max_tokens is not None:
            final_build["max_tokens"] = max_tokens
        if self._active_effort_support and reasoning_effort:
            final_build["reasoning_effort"] = reasoning_effort
        final = client.chat.completions.create(**final_build)
        return {
            "final_message": final.choices[0].message.content or "",
            "reasoning_content": "\n".join(reasoning_buf),
            "tool_calls_log": tool_calls_log,
        }

    async def _run_fc_loop_async(
        self,
        client,
        messages,
        tools,
        tool_map,
        model,
        reasoning_effort,
        max_tokens,
        max_rounds,
        first_response,
    ):
        tool_calls_log: list = []
        reasoning_buf: list = []
        response = first_response

        for _round in range(max_rounds):
            choice = response.choices[0]
            msg = choice.message
            reasoning = getattr(choice, "reasoning_content", None) or getattr(
                msg, "reasoning_content", None
            )
            if reasoning:
                reasoning_buf.append(reasoning)

            if not msg.tool_calls:
                return {
                    "final_message": msg.content or "",
                    "reasoning_content": "\n".join(reasoning_buf),
                    "tool_calls_log": tool_calls_log,
                }

            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments,
                        },
                    }
                    for tool_call in msg.tool_calls
                ],
            })

            for tool_call in msg.tool_calls:
                function_name = tool_call.function.name
                try:
                    arguments = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    arguments = {}
                tool_result = {"error": f"未知工具: {function_name}"}
                if function_name in tool_map:
                    try:
                        tool_result = await tool_map[function_name](**arguments)
                    except Exception as error:
                        tool_result = {"error": str(error)}
                        logger.warning("工具 %s 失败: %s", function_name, error)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(tool_result, ensure_ascii=False),
                })
                tool_calls_log.append({
                    "function": function_name,
                    "arguments": arguments,
                    "result": tool_result,
                })

            build: dict = {
                "messages": messages,
                "model": model,
                "temperature": 0.3,
                "tools": tools,
                "tool_choice": "auto",
            }
            if max_tokens is not None:
                build["max_tokens"] = max_tokens
            if self._active_effort_support and reasoning_effort:
                build["reasoning_effort"] = reasoning_effort
            response = await client.chat.completions.create(**build)

        logger.warning("FC 循环达到最大轮数 (%d)，强制结案", max_rounds)
        final_build: dict = {
            "messages": messages,
            "model": model,
            "temperature": 0.3,
        }
        if max_tokens is not None:
            final_build["max_tokens"] = max_tokens
        if self._active_effort_support and reasoning_effort:
            final_build["reasoning_effort"] = reasoning_effort
        final = await client.chat.completions.create(**final_build)
        return {
            "final_message": final.choices[0].message.content or "",
            "reasoning_content": "\n".join(reasoning_buf),
            "tool_calls_log": tool_calls_log,
        }


# ── 懒加载单例（线程安全）──
import threading

_instance: UnifiedClient | None = None
_lock = threading.Lock()


def _get_client() -> UnifiedClient:
    global _instance
    if _instance is None:
        with _lock:
            # 双重检查：锁内再确认一次，防止竞态创建多个实例
            if _instance is None:
                _instance = UnifiedClient()
    return _instance


class _LazyClient:
    def __getattr__(self, name):
        return getattr(_get_client(), name)


hy3_client = _LazyClient()


async def close_client() -> None:
    if _instance is not None:
        await _instance.aclose()
