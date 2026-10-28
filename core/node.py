"""极简 Node / Flow 状态机（学习自 Learn-OpenClaw，核心 < 60 行）。

用法::

    chat = ChatNode()
    tool = ToolCallNode()
    chat - "tool_call" >> tool
    tool - "chat" >> chat
    Flow(start=chat).run(None)
"""

from __future__ import annotations

import time
from typing import Any


class Node:
    """一个可重试、可分支的执行节点。

    ``exec`` 返回 ``(action, payload)``；``Flow`` 依据 ``action`` 选择后继节点，
    无后继时整条流自然结束。
    """

    def __init__(self, max_retries: int = 1, wait: float = 0.0):
        self.successors: dict[str, "Node"] = {}
        self._action: str = "default"
        self.max_retries = max(1, int(max_retries))
        self.wait = float(wait)

    def exec(self, payload: Any) -> tuple[str, Any]:  # pragma: no cover - 抽象接口
        raise NotImplementedError

    def _exec(self, payload: Any) -> tuple[str, Any]:
        """带重试与节流的 exec 包装；重试耗尽后抛出最后一次异常。"""
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                if self.wait:
                    time.sleep(self.wait)
                return self.exec(payload)
            except Exception as exc:  # noqa: BLE001 - 节点级重试需要兜住所有异常
                last = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(8.0, 0.5 * 2 ** (attempt - 1)))
        assert last is not None
        raise last

    def __rshift__(self, other: "Node") -> "Node":
        """``a - "action" >> b``：把 a 在 action 分支上的后继设为 b。"""
        self.successors[self._action] = other
        self._action = "default"
        return other

    def __sub__(self, action: str) -> "Node":
        """设置下一条 ``>>`` 的跳转条件。"""
        self._action = action or "default"
        return self


class Flow:
    """从 start 出发，按节点返回的 action 依次推进，直到无后继。"""

    def __init__(self, start: Node | None = None):
        self.start = start

    def run(self, payload: Any = None) -> tuple[str | None, Any]:
        curr, last_action = self.start, "default"
        while curr:
            last_action, payload = curr._exec(payload)
            curr = curr.successors.get(last_action)
        return last_action, payload
