"""运行日志（Run Log）：把思考过程 / 工具调用 / 结果完整落盘。

两套文件、一个目录（默认 ``chat_memory/logs/``，因此对 Agent 是可读目录）：

* ``session_<时间戳>.jsonl``：机器可读事件流，供后续 Agent 复用（复盘、画像提炼、回归）；
* ``session_<时间戳>.md``：人类可读归档，包含推理过程、工具入参/结果、Goal 轮次与耗时。

CLI 的「简略模式」只把状态与耗时打到终端，但**所有细节都会进入日志**——
"终端安静、档案完整"。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

MAX_RESULT_CHARS = 8000
MAX_CONTENT_CHARS = 20000


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class RunLogger:
    """把 Agent 事件同时写成 JSONL + Markdown。"""

    def __init__(self, log_dir: str | Path, *, session_id: str | None = None):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = session_id or time.strftime("%Y%m%d_%H%M%S")
        base = f"session_{stamp}"
        candidate = self.log_dir / base
        index = 1
        while (candidate.with_suffix(".jsonl")).exists():
            index += 1
            candidate = self.log_dir / f"{base}_{index}"
        self.jsonl_path = candidate.with_suffix(".jsonl")
        self.md_path = candidate.with_suffix(".md")
        self.event_count = 0
        self._start = time.perf_counter()
        self.md_path.write_text(
            f"# XCPC-Claw 运行日志\n\n- 开始时间：{_now()}\n- JSONL：`{self.jsonl_path.name}`\n\n",
            encoding="utf-8",
        )

    # ---------------------------- 写入 ---------------------------- #
    def log(self, kind: str, **fields: Any) -> None:
        self.event_count += 1
        event: dict[str, Any] = {
            "ts": _now(),
            "elapsed": round(time.perf_counter() - self._start, 3),
            "kind": kind,
        }
        event.update(fields)
        with self.jsonl_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        section = self._render_markdown(kind, event)
        if section:
            with self.md_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(section.rstrip() + "\n\n")

    # ---------------------------- 渲染 ---------------------------- #
    def _render_markdown(self, kind: str, event: dict[str, Any]) -> str:
        ts = event["ts"]
        if kind == "user_message":
            return f"## 👤 用户 · {ts}\n\n{_clip(event.get('content'), MAX_CONTENT_CHARS)}"
        if kind == "llm_end":
            usage = event.get("usage") or {}
            lines = [
                f"### 🤖 模型输出 · {ts} · 耗时 {event.get('elapsed', 0):.2f}s · "
                f"tokens {usage.get('total_tokens', '-')}"
            ]
            reasoning = str(event.get("reasoning") or "").strip()
            if reasoning:
                lines += ["", "> 🧠 **思考过程**", "", _quote(_clip(reasoning, MAX_CONTENT_CHARS))]
            content = str(event.get("content") or "").strip()
            if content:
                lines += ["", _clip(content, MAX_CONTENT_CHARS)]
            calls = event.get("tool_calls") or []
            if calls:
                lines += ["", f"🔧 调用工具：{', '.join(calls)}"]
            return "\n".join(lines)
        if kind == "tool_end":
            status = "✅" if event.get("ok") else "❌"
            lines = [f"#### {status} `{event.get('name')}` · 耗时 {event.get('elapsed', 0):.2f}s"]
            args = event.get("args")
            if args:
                lines += ["", "```json", _clip(json.dumps(args, ensure_ascii=False), 2000), "```"]
            result = str(event.get("result") or "").strip()
            if result:
                lines += ["", "```text", _clip(result, MAX_RESULT_CHARS), "```"]
            return "\n".join(lines)
        if kind == "goal_round":
            return f"---\n\n### 🎯 Goal 第 {event.get('n')}/{event.get('max')} 轮 · {ts}"
        if kind == "goal_end":
            return f"### 🏁 Goal {event.get('outcome')} · {ts}\n\n{_clip(event.get('summary') or '', 4000)}"
        if kind == "turn_end":
            return f"> ⏱ 本轮结束 · {ts} · 耗时 {event.get('elapsed', 0):.2f}s"
        if kind == "session_end":
            return f"---\n\n## ✅ 会话结束 · {ts}\n\n- 事件数：{self.event_count}\n- 用时：{event.get('elapsed', 0):.1f}s"
        return ""

    # ---------------------------- 查询 ---------------------------- #
    def markdown_tail(self, lines: int = 40) -> str:
        try:
            content = self.md_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(content[-max(1, lines) :])

    def summary(self) -> str:
        return (
            f"日志(JSONL): {self.jsonl_path}\n"
            f"日志(Markdown): {self.md_path}\n"
            f"事件数: {self.event_count}"
        )


def _clip(text: Any, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + f"\n...（已截断，原始 {len(text)} 字符）"


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())
