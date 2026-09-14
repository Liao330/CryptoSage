# -*- coding: utf-8 -*-
"""
CodeBuddy LLM Proxy —— OpenAI 兼容本地代理。

把 OpenAI 协议的 `POST /v1/chat/completions` 请求翻译为 CodeBuddy Agent SDK
调用（使用 CodeBuddy AI Key 认证），让 CryptoSage 这类 OpenAI 兼容应用
可以直接使用 CodeBuddy 账号下的模型能力（含 Function Calling 与
reasoning_effort 的近似映射）。

用法：
  1. 设置环境变量（AI Key 从 https://copilot.tencent.com/profile/ 获取）：
       set CODEBUDDY_API_KEY=xxx
       set CODEBUDDY_INTERNET_ENVIRONMENT=internal
  2. 启动代理：
       python codebuddy_llm_proxy.py            # 默认监听 127.0.0.1:8360
  3. 项目 .env 配置：
       HY3_BASE_URL=http://127.0.0.1:8360/v1
       HY3_API_KEY=codebuddy-local-proxy
       HY3_FAST_MODEL=hy3-fast
       HY3_SLOW_MODEL=hy3-slow

可选环境变量：
  CB_PROXY_PORT      监听端口（默认 8360）
  CB_FAST_MODEL      快速档使用的 CodeBuddy 模型 id（留空 = CLI 默认模型）
  CB_SLOW_MODEL      慢思考档使用的 CodeBuddy 模型 id（留空 = CLI 默认模型）
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def _load_dotenv():
    """从项目 .env 读取代理相关配置（不覆盖已存在的环境变量）。"""
    from pathlib import Path

    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.exists():
        return
    wanted = (
        "CODEBUDDY_API_KEY",
        "CODEBUDDY_INTERNET_ENVIRONMENT",
        "CB_PROXY_PORT",
        "CB_FAST_MODEL",
        "CB_SLOW_MODEL",
    )
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in wanted and value:
            os.environ.setdefault(key, value)


_load_dotenv()
os.environ.setdefault("CODEBUDDY_INTERNET_ENVIRONMENT", "internal")

from codebuddy_agent_sdk import (  # noqa: E402
    AssistantMessage,
    CodeBuddyAgentOptions,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool as sdk_tool,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [cb-proxy] %(levelname)s %(message)s",
)
logger = logging.getLogger("cb-llm-proxy")

PORT = int(os.getenv("CB_PROXY_PORT", "8360"))
FAST_MODEL = os.getenv("CB_FAST_MODEL", "").strip()
SLOW_MODEL = os.getenv("CB_SLOW_MODEL", "").strip()
NEUTRAL_CWD = os.path.join(tempfile.gettempdir(), "cb_proxy_ws")
os.makedirs(NEUTRAL_CWD, exist_ok=True)

# 说明：工具调用通过 CLI 原生 MCP 注册（_DeferredToolSession）转发给调用方执行。
# 历史上曾用文本协议提示（<tool_calls> 标签），但 CLI 会给 XML 标签注入 ID 破坏解析，
# 且模型可能改用 CLI 内置 ToolSearch 造成混淆，故已废弃；_parse_tool_calls 仅留作兜底。


def _extract_system(messages: list) -> str:
    parts = [m for m in messages if m.get("role") == "system"]
    return "\n\n".join(str(m.get("content") or "") for m in parts)


def _flatten_messages(messages: list) -> str:
    """把 OpenAI 多轮消息（含 tool / tool_calls）拍平成一段对话文本。"""
    lines = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            continue
        if role == "user":
            lines.append(f"[用户]\n{content}")
        elif role == "assistant":
            tool_calls = m.get("tool_calls") or []
            if tool_calls:
                calls = []
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = fn.get("arguments")
                    calls.append({"name": fn.get("name"), "arguments": args})
                lines.append(
                    "[助手-工具调用]\n" + json.dumps(calls, ensure_ascii=False)
                )
            if content:
                lines.append(f"[助手]\n{content}")
        elif role == "tool":
            name = m.get("name") or "tool"
            lines.append(f"[工具结果 {name}]\n{content}")
    return "\n\n".join(lines)


def _parse_tool_calls(text: str) -> list | None:
    """从模型输出中解析工具调用标记（或裸 JSON 数组）。"""
    m = re.search(
        r"\[\[TOOL_CALLS_BEGIN\]\]\s*(.*?)\s*\[\[TOOL_CALLS_END\]\]", text, re.DOTALL
    )
    candidates = []
    if m:
        candidates.append(m.group(1))
    # 兜底：整段就是一个 JSON 数组
    stripped = text.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        candidates.append(stripped)
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, list):
            data = [data]
        calls = []
        ok = True
        for item in data:
            if not isinstance(item, dict) or "name" not in item:
                ok = False
                break
            calls.append(
                {
                    "name": item.get("name"),
                    "arguments": item.get("arguments") or {},
                }
            )
        if ok and calls:
            return calls
    return None


def _pick_model(model: str | None, reasoning_effort: str | None) -> str | None:
    slow = (reasoning_effort == "high") or (
        model and "slow" in model.lower()
    )
    chosen = SLOW_MODEL if slow else FAST_MODEL
    return chosen or None


class _DeferredToolSession:
    """把 OpenAI 请求中的工具注册为 CLI 原生 MCP 工具（非破坏性转发）。

    模型发起原生工具调用时，handler 记录调用并立即返回"已转发"标记，
    查询自然结束；代理把捕获到的调用以 OpenAI tool_calls 格式返回给
    调用方（后端）执行，下一轮携带工具结果重新发起。
    （不采用挂起+取消查询的方式：会破坏 SDK 内部 anyio cancel scope。）
    """

    _MARKER = (
        "TOOL_FORWARDED: 该工具调用已被转发给外部执行器，结果将在下一轮对话中以"
        "「工具结果」形式提供。请不要再调用任何工具，只回复四个字：等待结果"
    )

    def __init__(self, openai_tools: list):
        self.calls: list[dict] = []
        self.server = self._build(openai_tools)

    def _build(self, openai_tools: list):
        if not openai_tools:
            return None
        funcs = []
        for t in openai_tools:
            fn = t.get("function", {})
            name = fn.get("name")
            if not name:
                continue
            desc = fn.get("description") or name
            params = fn.get("parameters") or {"type": "object", "properties": {}}

            def _make(tool_name: str):
                async def handler(args: dict) -> dict:
                    self.calls.append({
                        "id": f"call_{uuid.uuid4().hex[:12]}",
                        "name": tool_name,
                        "input": args or {},
                    })
                    return {"content": [{"type": "text", "text": self._MARKER}]}
                return handler

            decorated = _make(name)
            funcs.append(sdk_tool(name, desc, params)(decorated))
        if not funcs:
            return None
        return {"cb-openai-tools": create_sdk_mcp_server(
            name="cb-openai-tools", tools=funcs
        )}


async def _run_codebuddy_query(
    system_prompt: str,
    prompt: str,
    model: str | None,
    effort: str | None,
    openai_tools: list | None = None,
):
    """调用 CodeBuddy Agent SDK，返回 (text, reasoning, tool_uses)。

    工具调用通过 CLI 原生 MCP 机制捕获（_DeferredToolSession），转交调用方执行。
    """
    deferred = _DeferredToolSession(openai_tools or [])
    options = CodeBuddyAgentOptions(
        permission_mode="bypassPermissions",
        allowed_tools=[],          # 纯文本 LLM：禁用 Agent 自带工具
        disallowed_tools=["*"],
        max_turns=4,
        cwd=NEUTRAL_CWD,
        system_prompt=system_prompt,
        request_timeout_ms=int(os.getenv("CB_QUERY_TIMEOUT_MS", "280000")),
    )
    if deferred.server:
        options.mcp_servers = deferred.server
    if model:
        options.model = model
    if effort:
        options.effort = effort

    texts: list[str] = []
    reasoning_parts: list[str] = []
    last_error = None
    msg_stats: dict[str, int] = {}

    async for message in query(prompt=prompt, options=options):
        msg_stats[type(message).__name__] = msg_stats.get(type(message).__name__, 0) + 1
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    texts.append(block.text)
                elif isinstance(block, ThinkingBlock):
                    reasoning_parts.append(getattr(block, "thinking", "") or "")
            if message.error:
                last_error = message.error
        elif isinstance(message, ResultMessage):
            if getattr(message, "is_error", False):
                last_error = str(getattr(message, "result", ""))[:500]

    tool_uses = deferred.calls
    # 已捕获原生工具调用时，查询末尾的 "Max turns" 等错误可忽略
    if not texts and not tool_uses and last_error:
        raise RuntimeError(f"CodeBuddy query failed: {last_error}")
    if not texts and not tool_uses:
        # 诊断：查询正常结束但既无正文也无工具调用（模型只思考未作答的典型特征）
        logger.warning(
            "empty response: msgs=%s thinking_chars=%d error=%s",
            msg_stats, len("".join(reasoning_parts)), last_error,
        )
    return "".join(texts), "\n".join(reasoning_parts), tool_uses


app = FastAPI(title="CodeBuddy LLM Proxy", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models():
    now = int(time.time())
    data = []
    seen = set()
    for mid in ("hy3-fast", "hy3-slow", FAST_MODEL, SLOW_MODEL):
        if mid and mid not in seen:
            seen.add(mid)
            data.append({"id": mid, "object": "model", "created": now})
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        req = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid json"})

    messages = req.get("messages") or []
    tools = req.get("tools") or []
    req_model = req.get("model") or "hy3"
    reasoning_effort = req.get("reasoning_effort")
    effort = "high" if reasoning_effort == "high" else ("low" if reasoning_effort else None)

    # 构造 system prompt（工具通过 CLI 原生 MCP 注册，不再注入文本协议）
    system = _extract_system(messages) or "You are a helpful AI assistant."

    prompt = _flatten_messages(messages) or "Hello"
    model = _pick_model(req_model, reasoning_effort)

    logger.info(
        "→ model=%s effort=%s tools=%d msgs=%d chars=%d",
        model or "(default)", effort, len(tools), len(messages), len(prompt),
    )
    t0 = time.time()
    try:
        text, reasoning, native_tool_uses = await _run_codebuddy_query(
            system, prompt, model, effort, openai_tools=tools
        )
    except Exception as e:
        logger.error("query failed: %s", e)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(e), "type": "upstream_error"}},
        )
    elapsed = time.time() - t0

    # 工具调用来源：CLI 原生捕获（主） / 文本协议解析（兜底）
    tool_calls = None
    if tools and native_tool_uses:
        tool_calls = [
            {
                "id": u["id"] or f"call_{uuid.uuid4().hex[:12]}",
                "name": u["name"],
                "arguments": u["input"],
            }
            for u in native_tool_uses
        ]
        logger.info(
            "← native tool_calls x%d in %.1fs: %s",
            len(tool_calls), elapsed, [c["name"] for c in tool_calls],
        )
    elif tools:
        tool_calls = _parse_tool_calls(text)
        logger.info(
            "← %d chars in %.1fs (text-protocol tool_calls=%s)",
            len(text), elapsed, bool(tool_calls),
        )
    else:
        logger.info("← %d chars in %.1fs", len(text), elapsed)

    message: dict = {"role": "assistant", "content": text}
    if reasoning:
        message["reasoning_content"] = reasoning
    finish = "stop"
    if tool_calls:
        message["content"] = text if text else None
        message["tool_calls"] = [
            {
                "id": c["id"] if c.get("id") else f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": c["name"],
                    "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                },
            }
            for c in tool_calls
        ]
        finish = "tool_calls"

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req_model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish}
        ],
        "usage": {
            "prompt_tokens": len(prompt) // 4,
            "completion_tokens": len(text) // 4,
            "total_tokens": (len(prompt) + len(text)) // 4,
        },
    }


if __name__ == "__main__":
    logger.info(
        "CodeBuddy LLM Proxy 启动: 127.0.0.1:%d (fast=%s slow=%s)",
        PORT, FAST_MODEL or "(default)", SLOW_MODEL or "(default)",
    )
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
