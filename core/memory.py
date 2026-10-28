"""双层记忆：`session.jsonl`（短期、追加写）+ `MEMORY.md`（长期语义记忆）。

关键工程点（对齐 PRD 3.4.1）：

1. **追加写**保护 Prefix KV Cache：只有压缩/自愈时才整体重写。
2. **崩溃残尾自愈**：`assistant(tool_calls)` 与 `tool` 消息必须成对，否则整组丢弃。
3. **90% 阈值安全压缩**：切分点绝不落在 `assistant(tool_calls)` 与 `tool` 之间。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from .llm import call_llm, estimate_messages_tokens, load_env_file, parse_json_lenient

MESSAGE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "reasoning_content")
VALID_ROLES = {"system", "user", "assistant", "tool"}

COMPRESS_THRESHOLD = 0.9
KEEP_MESSAGES_ON_COMPRESS = 6

MEMORY_HEADER = """# XCPC-Claw 长期记忆

> 由 `core/memory.py` 在上下文压缩、画像更新时自动追加；也可人工编辑补充。

## 训练记录
"""

SUMMARY_PREFIX = "对话历史摘要：\n"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_memory_dir() -> Path:
    load_env_file()
    raw = os.environ.get("XCPC_MEMORY_DIR")
    return Path(raw).expanduser() if raw else project_root() / "chat_memory"


# --------------------------------------------------------------------------- #
# 消息规范化
# --------------------------------------------------------------------------- #
def normalize_message(msg: dict[str, Any]) -> dict[str, Any]:
    """只保留 PRD 规定的 5 个字段，保证 jsonl 干净且兼容 OpenAI 协议。"""
    role = str(msg.get("role") or "").strip()
    if role not in VALID_ROLES:
        role = "user"
    out: dict[str, Any] = {"role": role, "content": msg.get("content") or ""}
    calls = msg.get("tool_calls")
    if calls:
        out["tool_calls"] = [
            {
                "id": str(call.get("id") or f"call_{index}"),
                "type": "function",
                "function": {
                    "name": str((call.get("function") or {}).get("name") or ""),
                    "arguments": (call.get("function") or {}).get("arguments") or "{}",
                },
            }
            for index, call in enumerate(calls)
            if (call.get("function") or {}).get("name")
        ]
        if not out["tool_calls"]:
            out.pop("tool_calls")
    if msg.get("tool_call_id"):
        out["tool_call_id"] = str(msg["tool_call_id"])
    reasoning = msg.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        out["reasoning_content"] = reasoning
    return out


def normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_message(m) for m in messages if isinstance(m, dict)]


# --------------------------------------------------------------------------- #
# System Prompt 组装（固定顺序，前缀稳定）
# --------------------------------------------------------------------------- #
def build_system_prompt(
    base_prompt: str,
    skill_catalog: str = "",
    profile_snippet: str = "",
    long_term_memory: str = "",
    max_memory_chars: int = 4000,
) -> str:
    parts: list[str] = [base_prompt.strip()]
    if skill_catalog.strip():
        parts.append(skill_catalog.strip())
    if profile_snippet.strip():
        parts.append(profile_snippet.strip())
    if long_term_memory.strip():
        memory = long_term_memory.strip()
        if len(memory) > max_memory_chars:
            memory = "……（更早记忆已省略）\n" + memory[-max_memory_chars:]
        parts.append(f"<long_term_memory>\n{memory}\n</long_term_memory>")
    return "\n\n".join(parts)


def build_context(
    history: list[dict[str, Any]],
    *,
    base_prompt: str,
    skill_catalog: str = "",
    profile_snippet: str = "",
    long_term_memory: str = "",
) -> list[dict[str, Any]]:
    """[1 静态角色] + [2 Skill Catalog] + [3 画像/长期记忆] 后接历史消息。"""
    system = build_system_prompt(base_prompt, skill_catalog, profile_snippet, long_term_memory)
    return [{"role": "system", "content": system}] + normalize_messages(history)


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #
class Memory:
    def __init__(
        self,
        memory_dir: str | Path | None = None,
        *,
        max_context_length: int | None = None,
        keep_messages: int = KEEP_MESSAGES_ON_COMPRESS,
        llm_fn: Callable[..., dict[str, Any]] | None = None,
    ):
        load_env_file()
        self.memory_dir = Path(memory_dir) if memory_dir else default_memory_dir()
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.session_path = self.memory_dir / "session.jsonl"
        self.memory_path = self.memory_dir / "MEMORY.md"
        self.max_context_length = int(
            max_context_length or os.environ.get("XCPC_MAX_CONTEXT", 128_000) or 128_000
        )
        self.keep_messages = max(2, int(keep_messages))
        self.llm_fn = llm_fn or call_llm

        self.messages: list[dict[str, Any]] = []
        self.recovery_note: str = ""
        self.last_usage: dict[str, int] = {}
        self.usage_totals: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.compress_events: list[dict[str, Any]] = []

        self.messages = self._load()
        self._repair()
        self._ensure_long_term_file()

    # ---------------------------- 读写 ---------------------------- #
    def _load(self) -> list[dict[str, Any]]:
        if not self.session_path.is_file():
            return []
        loaded: list[dict[str, Any]] = []
        for line_no, raw in enumerate(self.session_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                self.recovery_note = f"session.jsonl 第 {line_no} 行不是合法 JSON，已跳过。"
                continue
            if isinstance(obj, dict) and obj.get("role"):
                loaded.append(normalize_message(obj))
        return loaded

    def _rewrite_disk(self) -> None:
        tmp = self.session_path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            for msg in self.messages:
                fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
        tmp.replace(self.session_path)

    def _append_disk(self, msg: dict[str, Any]) -> None:
        with self.session_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(msg, ensure_ascii=False) + "\n")

    def _ensure_long_term_file(self) -> None:
        if not self.memory_path.is_file():
            self.memory_path.write_text(MEMORY_HEADER, encoding="utf-8")

    # ---------------------------- 崩溃自愈 ---------------------------- #
    def _repair(self) -> None:
        """丢弃未完成/孤儿 tool 调用组，防止 API 报 `tool_calls must be followed by tool messages`。"""
        msgs = self.messages
        cut: int | None = None
        index = 0
        while index < len(msgs):
            msg = msgs[index]
            role = msg["role"]
            if role == "tool":  # 孤儿 tool 结果
                cut = index
                break
            if role == "assistant" and msg.get("tool_calls"):
                expected = [call.get("id") for call in msg["tool_calls"]]
                cursor, answered = index + 1, []
                while cursor < len(msgs) and msgs[cursor]["role"] == "tool":
                    answered.append(msgs[cursor].get("tool_call_id"))
                    cursor += 1
                if any(call_id not in answered for call_id in expected):
                    cut = index
                    break
                index = cursor
                continue
            index += 1

        if cut is None:
            return
        # 连同该轮的 user 提问一起回滚，避免重放时产生上下文歧义
        if cut > 0 and msgs[cut - 1]["role"] == "user":
            cut -= 1
        dropped = len(msgs) - cut
        self.messages = msgs[:cut]
        self._rewrite_disk()
        self.recovery_note = (
            f"检测到上次会话在工具执行中途中断，已丢弃 {dropped} 条残尾消息并修复 session.jsonl。"
        )

    # ---------------------------- 写入 ---------------------------- #
    def append(self, msg: dict[str, Any], persist: bool = True) -> dict[str, Any]:
        clean = normalize_message(msg)
        self.messages.append(clean)
        if persist:
            self._append_disk(clean)
        return clean

    def extend(self, messages: list[dict[str, Any]], persist: bool = True) -> None:
        for msg in messages:
            self.append(msg, persist=persist)

    def clear(self) -> None:
        """清空短期会话，保留 MEMORY.md 与 PROFILE.json。"""
        self.messages = []
        self.last_usage = {}
        self.session_path.write_text("", encoding="utf-8")

    # ---------------------------- token ---------------------------- #
    def record_usage(self, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        self.last_usage = {key: int(usage.get(key) or 0) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        for key, value in self.last_usage.items():
            self.usage_totals[key] = self.usage_totals.get(key, 0) + value

    def context_tokens(self) -> int:
        if self.last_usage.get("total_tokens"):
            return int(self.last_usage["total_tokens"])
        return estimate_messages_tokens(self.messages)

    # ---------------------------- 长期记忆 ---------------------------- #
    def long_term_memory(self, max_chars: int = 4000) -> str:
        if not self.memory_path.is_file():
            return ""
        text = self.memory_path.read_text(encoding="utf-8", errors="replace").strip()
        return text[-max_chars:] if len(text) > max_chars else text

    def append_long_term(self, note: str) -> None:
        note = note.strip()
        if not note:
            return
        self._ensure_long_term_file()
        stamp = time.strftime("%Y-%m-%d %H:%M")
        with self.memory_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"\n- [{stamp}] {note}\n")

    # ---------------------------- 压缩 ---------------------------- #
    def _unsafe_split(self, msgs: list[dict[str, Any]], index: int) -> bool:
        msg = msgs[index]
        if msg["role"] == "tool":
            return True
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            return True
        previous = msgs[index - 1] if index > 0 else None
        return bool(previous and previous["role"] == "assistant" and previous.get("tool_calls"))

    def maybe_compress(self, force: bool = False) -> bool:
        """超过 90% 上下文窗口且消息数足够时，安全压缩旧消息。"""
        if len(self.messages) <= self.keep_messages:
            return False
        if not force and self.context_tokens() <= self.max_context_length * COMPRESS_THRESHOLD:
            return False

        split = max(1, len(self.messages) - self.keep_messages)
        while split > 1 and self._unsafe_split(self.messages, split):
            split -= 1
        if split <= 0:
            return False

        old, recent = self.messages[:split], self.messages[split:]
        if not old:
            return False

        used_fallback = False
        payload = self._summarize(old)
        if payload is None:
            used_fallback = True
            payload = {"summary": self._mechanical_summary(old), "memory_update": ""}

        summary = str(payload.get("summary") or "").strip() or self._mechanical_summary(old)
        self.messages = [{"role": "system", "content": SUMMARY_PREFIX + summary}] + recent
        self._rewrite_disk()

        update = str(payload.get("memory_update") or "").strip()
        if update:
            self.append_long_term(f"[自动压缩摘要] {update}")
        self.compress_events.append(
            {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "old_messages": len(old),
                "kept_messages": len(recent),
                "fallback": used_fallback,
            }
        )
        self.last_usage = {}
        return True

    def _summarize(self, old: list[dict[str, Any]]) -> dict[str, Any] | None:
        transcript = []
        for msg in old:
            role = msg["role"]
            content = str(msg.get("content") or "")
            if msg.get("tool_calls"):
                names = ", ".join((call.get("function") or {}).get("name", "?") for call in msg["tool_calls"])
                content += f" [tool_calls: {names}]"
            transcript.append(f"{role.upper()}: {content[:1200]}")
        prompt = (
            "你是上下文压缩器。把下面的 XCPC 训练对话压缩为 JSON，"
            '字段：{"summary": "保留题目要求、已尝试的反例/结论、待办事项（中文，<=400字）", '
            '"memory_update": "可写入长期记忆的选手训练结论（<=120字，可为空字符串）"}。\n'
            "只输出 JSON，不要输出其他文字。\n\n" + "\n\n".join(transcript)
        )
        try:
            resp = self.llm_fn(
                [{"role": "user", "content": prompt}],
                None,
                "你是严谨的对话摘要器，只输出合法 JSON。",
                temperature=0.1,
                stream=False,
            )
        except Exception:  # noqa: BLE001 - 压缩失败必须降级而不是中断会话
            return None
        return parse_json_lenient(str(resp.get("content") or ""))

    @staticmethod
    def _mechanical_summary(old: list[dict[str, Any]]) -> str:
        lines = []
        for msg in old:
            content = " ".join(str(msg.get("content") or "").split())
            if not content:
                if msg.get("tool_calls"):
                    names = ", ".join((call.get("function") or {}).get("name", "?") for call in msg["tool_calls"])
                    content = f"(调用工具: {names})"
                else:
                    continue
            lines.append(f"- {msg['role']}: {content[:160]}")
        return "以下为机械摘要（压缩模型不可用）：\n" + "\n".join(lines[-40:])
