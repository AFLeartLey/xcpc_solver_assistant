"""LLM 调用封装：OpenAI 兼容接口（DeepSeek / Kimi / 智谱 / OpenAI / Anthropic 兼容网关）。

对外只暴露 :func:`call_llm`，返回**纯字典**，便于 Memory 直接落盘::

    {
      "role": "assistant",
      "content": "...",
      "tool_calls": [{"id": "...", "type": "function", "function": {...}}] | None,
      "reasoning_content": "...",              # deepseek-reasoner 时保留
      "usage": {"prompt_tokens": .., "completion_tokens": .., "total_tokens": ..},
    }
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Callable

import openai
from openai import OpenAI

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_MAX_RETRIES = 3
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}

# 允许 CLI / 测试覆盖的运行时配置（None 表示回落到环境变量）
_SETTINGS: dict[str, Any] = {"api_key": None, "base_url": None, "model": None, "stream": None}
_client: OpenAI | None = None
_env_loaded = False


# --------------------------------------------------------------------------- #
# 环境变量
# --------------------------------------------------------------------------- #
def load_env_file(path: str | Path | None = None) -> None:
    """极简 .env 解析（不引入 python-dotenv，保持 4 个核心依赖）。"""
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    candidates = [Path(path)] if path else [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            for raw in candidate.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
            return
        except OSError:
            continue


def _env(name: str, default: str | None = None) -> str | None:
    load_env_file()
    return _SETTINGS.get(_lower_key(name)) or os.environ.get(name) or default


def _lower_key(name: str) -> str:
    return {"OPENAI_API_KEY": "api_key", "OPENAI_BASE_URL": "base_url", "OPENAI_MODEL": "model"}.get(name, name.lower())


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def configure(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    stream: bool | None = None,
) -> None:
    """覆盖客户端配置（CLI 参数 / 单元测试用），并重置缓存的 client。"""
    global _client
    for key, value in (("api_key", api_key), ("base_url", base_url), ("model", model), ("stream", stream)):
        if value is not None:
            _SETTINGS[key] = value
    _client = None


def reset_settings() -> None:
    global _client
    _SETTINGS.update({"api_key": None, "base_url": None, "model": None, "stream": None})
    _client = None


def current_model() -> str:
    return str(_env("OPENAI_MODEL", DEFAULT_MODEL))


def has_api_key() -> bool:
    return bool(_env("OPENAI_API_KEY"))


def get_client() -> OpenAI:
    """惰性构造 OpenAI 客户端（兼容任意 OpenAI 协议端点）。"""
    global _client
    if _client is not None:
        return _client
    api_key = _env("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未配置 OPENAI_API_KEY。请在 .env 或环境变量中设置后重试（参见 .env.example）。"
        )
    base_url = _env("OPENAI_BASE_URL", DEFAULT_BASE_URL)
    timeout = float(_env("XCPC_LLM_TIMEOUT_SEC", "180") or 180)
    _client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
    return _client


# --------------------------------------------------------------------------- #
# token 估算（provider 未返回 usage 时的兜底）
# --------------------------------------------------------------------------- #
def estimate_tokens(text: str) -> int:
    """粗略估算 token：CJK 约 1 token/字，其余约 1 token/3.5 字符。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - cjk
    return cjk + int(other / 3.5) + 1


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for msg in messages:
        for key in ("content", "reasoning_content"):
            value = msg.get(key)
            if isinstance(value, str):
                total += estimate_tokens(value)
        for call in msg.get("tool_calls") or []:
            total += estimate_tokens(json.dumps(call, ensure_ascii=False))
        total += 4
    return total


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def call_llm(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    system_prompt: str = "",
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict[str, Any] | str | None = None,
    on_delta: Callable[[str], None] | None = None,
    on_reasoning: Callable[[str], None] | None = None,
    stream: bool | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict[str, Any]:
    """调用 LLM 并返回标准化字典；429/网络抖动自动指数退避重试。"""
    msgs = _with_system(messages, system_prompt)
    if stream is None:
        stream = bool(_SETTINGS.get("stream")) if _SETTINGS.get("stream") is not None else _env_flag("XCPC_STREAM", True)
    use_stream = bool(stream) and (on_delta is not None or on_reasoning is not None)
    kwargs: dict[str, Any] = {
        "model": model or current_model(),
        "messages": msgs,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if response_format is not None:
        kwargs["response_format"] = response_format

    last_exc: Exception | None = None
    for attempt in range(1, max(1, max_retries) + 1):
        try:
            if use_stream:
                return _stream_once(kwargs, on_delta, on_reasoning)
            return _complete_once(kwargs)
        except Exception as exc:  # noqa: BLE001 - 统一判定是否可重试
            last_exc = exc
            if attempt >= max_retries or not _is_retryable(exc):
                break
            delay = min(8.0, 2.0 ** (attempt - 1)) + random.uniform(0.0, 0.3)
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _with_system(messages: list[dict[str, Any]], system_prompt: str) -> list[dict[str, Any]]:
    """system_prompt 非空且首条不是 system 时自动前置（不修改入参）。"""
    msgs = [dict(m) for m in messages]
    if system_prompt and (not msgs or msgs[0].get("role") != "system"):
        msgs.insert(0, {"role": "system", "content": system_prompt})
    return msgs


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError, openai.InternalServerError)):
        return True
    if isinstance(exc, openai.APIStatusError):
        return getattr(exc, "status_code", None) in _RETRYABLE_STATUS
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    return False


def _complete_once(kwargs: dict[str, Any]) -> dict[str, Any]:
    client = get_client()
    resp = client.chat.completions.create(**kwargs)
    return _normalize(resp, kwargs.get("messages") or [])


def _stream_once(
    kwargs: dict[str, Any],
    on_delta: Callable[[str], None] | None,
    on_reasoning: Callable[[str], None] | None,
) -> dict[str, Any]:
    client = get_client()
    params = dict(kwargs)
    params["stream"] = True
    params["stream_options"] = {"include_usage": True}
    try:
        stream = client.chat.completions.create(**params)
    except openai.BadRequestError:
        # 部分兼容端点不支持 stream_options，降级重试一次（usage 用估算兜底）
        params.pop("stream_options", None)
        stream = client.chat.completions.create(**params)

    content: list[str] = []
    reasoning: list[str] = []
    tool_slots: dict[int, dict[str, Any]] = {}
    usage: Any = None
    finish_reason: str | None = None

    for chunk in stream:
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage:
            usage = chunk_usage
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is None:
            continue
        text = getattr(delta, "content", None)
        if text:
            content.append(text)
            if on_delta:
                on_delta(text)
        extra = getattr(delta, "model_extra", None) or {}
        think = getattr(delta, "reasoning_content", None) or extra.get("reasoning_content")
        if think:
            reasoning.append(think)
            if on_reasoning:
                on_reasoning(think)
        for call in getattr(delta, "tool_calls", None) or []:
            index = getattr(call, "index", 0) or 0
            slot = tool_slots.setdefault(
                index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
            if getattr(call, "id", None):
                slot["id"] = call.id
            fn = getattr(call, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    slot["function"]["name"] += fn.name
                if getattr(fn, "arguments", None):
                    slot["function"]["arguments"] += fn.arguments
        if getattr(choice, "finish_reason", None):
            finish_reason = choice.finish_reason

    result: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content),
        "tool_calls": [tool_slots[i] for i in sorted(tool_slots)] or None,
    }
    if reasoning:
        result["reasoning_content"] = "".join(reasoning)
    if finish_reason:
        result["finish_reason"] = finish_reason
    result["usage"] = _usage_dict(usage, kwargs.get("messages") or [], result)
    return result


def _normalize(resp: Any, messages: list[dict[str, Any]]) -> dict[str, Any]:
    choice = (getattr(resp, "choices", None) or [None])[0]
    message = getattr(choice, "message", None)
    content = getattr(message, "content", "") if message is not None else ""
    if content is None:
        content = ""
    extra = getattr(message, "model_extra", None) or {}
    reasoning = getattr(message, "reasoning_content", None) or extra.get("reasoning_content")

    tool_calls: list[dict[str, Any]] = []
    for call in getattr(message, "tool_calls", None) or []:
        fn = getattr(call, "function", None)
        tool_calls.append(
            {
                "id": getattr(call, "id", "") or "",
                "type": "function",
                "function": {
                    "name": getattr(fn, "name", "") if fn else "",
                    "arguments": getattr(fn, "arguments", "") if fn else "",
                },
            }
        )

    result: dict[str, Any] = {"role": "assistant", "content": content, "tool_calls": tool_calls or None}
    if reasoning:
        result["reasoning_content"] = reasoning
    finish_reason = getattr(choice, "finish_reason", None) if choice is not None else None
    if finish_reason:
        result["finish_reason"] = finish_reason
    result["usage"] = _usage_dict(getattr(resp, "usage", None), messages, result)
    return result


def _usage_dict(usage: Any, messages: list[dict[str, Any]], result: dict[str, Any]) -> dict[str, int]:
    prompt = getattr(usage, "prompt_tokens", None) if usage is not None else None
    completion = getattr(usage, "completion_tokens", None) if usage is not None else None
    total = getattr(usage, "total_tokens", None) if usage is not None else None
    if not prompt:
        prompt = estimate_messages_tokens(messages)
    if not completion:
        completion = estimate_tokens(str(result.get("content") or "")) + estimate_tokens(
            json.dumps(result.get("tool_calls") or [], ensure_ascii=False)
        )
    if not total:
        total = int(prompt) + int(completion)
    return {"prompt_tokens": int(prompt), "completion_tokens": int(completion), "total_tokens": int(total)}


# --------------------------------------------------------------------------- #
# 宽松 JSON 解析（摘要 / 子代理战报共用）
# --------------------------------------------------------------------------- #
def parse_json_lenient(text: str) -> dict[str, Any] | None:
    """从可能包裹 ```json 或前后带解释文字的模型输出中抽出第一个 JSON 对象。"""
    if not text:
        return None
    candidates: list[str] = []
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)
    for candidate in candidates:
        candidate = candidate.strip()
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
            if isinstance(value, list):
                return {"items": value}
        except json.JSONDecodeError:
            pass
        start, depth, in_str, escape = None, 0, False, False
        for index, ch in enumerate(candidate):
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                if start is None:
                    start = index
                depth += 1
            elif ch == "}" and start is not None:
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(candidate[start : index + 1])
                        if isinstance(value, dict):
                            return value
                    except json.JSONDecodeError:
                        pass
                    start = None
    return None
