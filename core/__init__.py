"""XCPC-Claw 极简 Agent 运行时内核。

模块划分（严格对齐 prd_implementation.md 第二节）::

    core/llm.py           LLM 兼容调用（重试 / tool_calls 序列化 / token 统计）
    core/node.py          Node 与 Flow 状态机（< 60 行）
    core/agent_nodes.py   ChatNode / ToolCallNode / AgentSession
    core/goal.py          外层 /goal 自治循环
    core/memory.py        session.jsonl + MEMORY.md + 90% 阈值压缩
    core/profile.py       PROFILE.json 选手画像
"""

from .memory import Memory, build_context, normalize_messages
from .node import Flow, Node
from .profile import Profile

__all__ = ["Flow", "Node", "Memory", "Profile", "build_context", "normalize_messages"]
