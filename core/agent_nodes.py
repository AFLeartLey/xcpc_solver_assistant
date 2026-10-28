"""内层 Agent Loop：ChatNode ⇄ ToolCallNode。

单轮 Flow 的走向::

    ChatNode --(有 tool_calls)--> ToolCallNode --(回填 tool 结果)--> ChatNode
        └──────(无 tool_calls)──────> 结束（返回最终 assistant 消息）

所有消息统一由 ``Memory`` 追加落盘，保证崩溃后可自愈。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .goal import GoalState
from .llm import call_llm
from .memory import Memory, build_context
from .node import Flow, Node
from .profile import Profile

BASE_SYSTEM_PROMPT = """你是 XCPC-Claw，一名 AI-Native 竞争性编程（ICPC / CCPC / Codeforces）金牌教练与自动化对拍 Hack 专家。

工作准则：
- 当需要执行某项专业任务（自动对拍找反例、图论造数据、苏格拉底点拨、静态边界审计、更新选手画像）时，先调用 `load_skill` 加载对应技能规范，再动手。
- 对拍属于高噪声长链路任务：优先调用 `spawn_subagent`，让 Hacker Subagent 在独立 `workspace/<problem_slug>/` 目录里编写 brute.cpp / gen.py（必要时 spj.cpp）并调用 `run_stress_test`，主对话只接收结构化最小反例战报。
- 拿到反例或定位出选手 Bug 后，必须调用 `update_profile` 记录 Bug 指纹，并在回答末尾简述本次训练结论。
- 所有临时文件必须写在 `workspace/<problem_slug>/` 下，严禁污染选手源码目录；写入前优先使用绝对路径。
- 文件访问受安全策略限制：**只能读** `workspace/`、`tools/skills/`、`chat_memory/` 以及用户在本次会话中明确给出的文件；
  **只能写** `workspace/<problem_slug>/`。若工具返回“被安全策略拒绝”，不要反复重试，
  应提示用户用 `/allow <path>` 授权或把文件复制进工作区。
- 默认遵守"防剧透"原则：除非选手明确要求 L4，否则不要直接输出完整 AC 代码，而是给出反例 + 针对性提问 + 最小修改建议。
- 工具调用失败时先读错误信息再修正参数，不要重复提交完全相同的错误调用。
"""


@dataclass
class SessionState:
    """会话级可变状态：Goal、轮次与最终回复。"""

    goal: GoalState | None = None
    turns: int = 0
    last_reply: str = ""
    stop_reason: str = ""
    tool_calls_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class AgentSession:
    """把 Memory / Profile / ToolRegistry / 回调 组合成一个可运行的 Agent 会话。"""

    def __init__(
        self,
        *,
        memory: Memory,
        profile: Profile,
        registry: Any,
        skill_catalog: str = "",
        base_prompt: str = BASE_SYSTEM_PROMPT,
        state: SessionState | None = None,
        on_text: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        on_tool: Callable[[str, dict[str, Any], str], None] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        model: str | None = None,
        stream: bool = True,
        llm_fn: Callable[..., dict[str, Any]] | None = None,
    ):
        self.memory = memory
        self.profile = profile
        self.registry = registry
        self.skill_catalog = skill_catalog
        self.base_prompt = base_prompt
        self.state = state or SessionState()
        self.on_text = on_text
        self.on_reasoning = on_reasoning
        self.on_tool = on_tool
        self.on_event = on_event
        self.model = model
        self.stream = stream
        self.llm_fn = llm_fn or call_llm

    def emit(self, kind: str, **data: Any) -> None:
        """向 CLI / 日志层广播事件；UI 异常绝不影响 Agent 主流程。"""
        if self.on_event is None:
            return
        try:
            self.on_event(kind, data)
        except Exception:  # noqa: BLE001 - 展示层故障必须被隔离
            pass

    # ---------------------------- 上下文 ---------------------------- #
    def system_prompt(self) -> str:
        return build_context(
            [],
            base_prompt=self.base_prompt,
            skill_catalog=self.skill_catalog,
            profile_snippet=self.profile.get_profile_prompt_snippet(),
            long_term_memory=self.memory.long_term_memory(),
        )[0]["content"]

    def context_messages(self) -> list[dict[str, Any]]:
        return build_context(
            self.memory.messages,
            base_prompt=self.base_prompt,
            skill_catalog=self.skill_catalog,
            profile_snippet=self.profile.get_profile_prompt_snippet(),
            long_term_memory=self.memory.long_term_memory(),
        )

    # ---------------------------- 循环 ---------------------------- #
    def run_turn(self) -> dict[str, Any] | None:
        """跑一次完整 Flow（含任意轮工具调用），返回最终 assistant 消息。"""
        action, payload = build_agent_flow(self).run(None)
        self.state.turns += 1
        if isinstance(payload, dict) and payload.get("role") == "assistant":
            self.state.last_reply = str(payload.get("content") or "")
            return payload
        return None

    def chat(self, user_text: str) -> dict[str, Any] | None:
        self.memory.append({"role": "user", "content": user_text})
        return self.run_turn()


class ChatNode(Node):
    """单次 LLM 调用节点：装配上下文 -> 调用模型 -> 落盘 -> 决定分支。"""

    def __init__(self, session: AgentSession, max_retries: int = 2):
        super().__init__(max_retries=max_retries)
        self.session = session

    def exec(self, payload: Any) -> tuple[str, Any]:
        session = self.session
        # 只在"无待回填工具结果"的干净边界做压缩，绝不断开 tool_calls 配对
        try:
            if session.memory.maybe_compress():
                session.state.extra["compressed"] = session.state.extra.get("compressed", 0) + 1
        except Exception as exc:  # noqa: BLE001 - 压缩失败不应影响主流程
            session.state.extra["compress_error"] = repr(exc)

        messages = session.context_messages()
        last = messages[-1] if messages else {}
        if last.get("role") == "user":
            session.emit("user_message", content=str(last.get("content") or ""))

        streaming = bool(session.stream and (session.on_text is not None or session.on_reasoning is not None))
        delivered = {"content": False, "reasoning": False}

        def _on_delta(chunk: str) -> None:
            delivered["content"] = True
            if session.on_text is not None:
                session.on_text(chunk)

        def _on_reasoning(chunk: str) -> None:
            delivered["reasoning"] = True
            if session.on_reasoning is not None:
                session.on_reasoning(chunk)

        schemas = session.registry.schemas()
        session.emit("llm_start", message_count=len(messages), tool_count=len(schemas))
        started = time.perf_counter()
        resp = session.llm_fn(
            messages,
            schemas or None,
            "",
            model=session.model,
            stream=streaming,
            on_delta=_on_delta if streaming else None,
            on_reasoning=_on_reasoning if streaming else None,
        )
        elapsed = time.perf_counter() - started
        session.memory.record_usage(resp.get("usage"))
        stored = session.memory.append(resp)
        calls = stored.get("tool_calls") or []
        session.emit(
            "llm_end",
            elapsed=round(elapsed, 3),
            content=str(stored.get("content") or ""),
            reasoning=str(resp.get("reasoning_content") or stored.get("reasoning_content") or ""),
            reasoning_streamed=delivered["reasoning"],
            tool_calls=[str((call.get("function") or {}).get("name") or "?") for call in calls],
            usage=resp.get("usage") or {},
            final=not calls,
            streamed=delivered["content"],
        )
        if calls:
            return "tool_call", stored
        return "done", stored


class ToolCallNode(Node):
    """执行 assistant 发起的全部 tool_calls，并逐条回填成 tool 消息。"""

    def __init__(self, session: AgentSession, max_retries: int = 1):
        super().__init__(max_retries=max_retries)
        self.session = session

    def exec(self, payload: Any) -> tuple[str, Any]:
        from tools.executor import ToolExecutor  # 延迟导入，避免核心层依赖工具包初始化

        session = self.session
        calls = (payload or {}).get("tool_calls") or []
        executor = ToolExecutor(session.registry)
        for call in calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "?")
            try:
                args = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": function.get("arguments")}
            if not isinstance(args, dict):
                args = {"_value": args}

            session.emit("tool_start", name=name, args=args)
            started = time.perf_counter()
            result = executor.execute([call])[0]
            elapsed = time.perf_counter() - started
            content = str(result.get("content") or "")
            ok = not content.lstrip().startswith("ERROR")
            session.emit("tool_end", name=name, args=args, elapsed=round(elapsed, 3), ok=ok, result=content)

            session.memory.append(result)
            session.state.tool_calls_count += 1
            if session.on_tool:
                session.on_tool(name, args if isinstance(args, dict) else {}, content)
        return "chat", None


def build_agent_flow(session: AgentSession) -> Flow:
    chat_node = ChatNode(session)
    tool_node = ToolCallNode(session)
    chat_node - "tool_call" >> tool_node
    tool_node - "chat" >> chat_node
    return Flow(start=chat_node)
