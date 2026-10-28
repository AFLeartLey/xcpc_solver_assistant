"""外层 `/goal` 持久自治循环。

内层 `ChatNode ⇄ ToolCallNode` 只负责"一次用户输入 -> 若干工具调用 -> 最终回答"，
而长链路对拍需要 5~15 步连续操作。若模型在中途输出阶段性汇报就停下，外层循环会
自动注入督促消息继续推进，直到：

* Agent 调用 ``goal_complete(summary)`` -> 正常完成；
* Agent 调用 ``goal_pause(question)`` -> 需要选手输入，循环挂起；
* 达到 ``max_rounds`` 上限 -> 熔断，避免死循环烧 token。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class GoalState:
    text: str | None = None
    active: bool = False
    paused: bool = False
    max_rounds: int = 15
    current_round: int = 0
    last_summary: str = ""
    pending_question: str = ""
    history: list[str] = field(default_factory=list)

    # ---------------------------- 生命周期 ---------------------------- #
    def start(self, text: str, max_rounds: int | None = None) -> None:
        self.text = text.strip()
        self.active = True
        self.paused = False
        self.current_round = 0
        self.last_summary = ""
        self.pending_question = ""
        self.history = []
        if max_rounds:
            self.max_rounds = max(1, int(max_rounds))

    def complete(self, summary: str = "") -> None:
        self.active = False
        self.paused = False
        self.last_summary = (summary or "").strip()

    def pause(self, question: str = "") -> None:
        self.paused = True
        self.pending_question = (question or "").strip()

    def resume(self) -> bool:
        if not self.active or not self.paused:
            return False
        self.paused = False
        self.pending_question = ""
        return True

    def status_text(self) -> str:
        if not self.active and not self.paused:
            state = "空闲" if not self.text else "已结束"
        elif self.paused:
            state = "已挂起（等待选手回答）"
        else:
            state = "运行中"
        return (
            f"Goal 状态: {state}\n"
            f"目标: {self.text or '(未设置)'}\n"
            f"轮次: {self.current_round}/{self.max_rounds}\n"
            f"待回答: {self.pending_question or '(无)'}\n"
            f"最近结论: {self.last_summary or '(无)'}"
        )


def goal_message(goal: GoalState) -> str:
    """每轮注入的督促消息（role=user），避免 Agent 半途停下等待人类催促。"""
    return (
        f"[GOAL 第 {goal.current_round}/{goal.max_rounds} 轮] 当前自治目标：\n{goal.text}\n\n"
        "继续推进该目标，本轮必须通过工具调用产生实际进展（读文件 / 写代码 / run_stress_test / spawn_subagent 等）。\n"
        "约束：\n"
        "1. 不要只输出“我准备开始…”之类的阶段性汇报后停下来，直接用工具执行。\n"
        "2. 只在对拍真正完成、反例已确认并完成点拨与画像更新后，调用 goal_complete(summary) 结束。\n"
        "3. 如果确实需要选手本人回答或确认（例如讲解后等待其改代码），调用 goal_pause(question) 挂起。\n"
    )


def run_goal(
    session: Any,
    goal: GoalState,
    *,
    on_round: Callable[[GoalState], None] | None = None,
    max_rounds: int | None = None,
) -> str:
    """驱动外层循环，返回 ``COMPLETED`` / ``PAUSED`` / ``ROUND_LIMIT`` / ``INACTIVE``。"""
    if not goal.active:
        return "INACTIVE"
    if max_rounds:
        goal.max_rounds = max(1, int(max_rounds))

    while goal.active and not goal.paused and goal.current_round < goal.max_rounds:
        goal.current_round += 1
        session.memory.append({"role": "user", "content": goal_message(goal)})
        if on_round:
            on_round(goal)
        session.run_turn()
        goal.history.append(f"round {goal.current_round}: ok")

    if goal.paused:
        return "PAUSED"
    if goal.active and goal.current_round >= goal.max_rounds:
        goal.active = False
        goal.last_summary = goal.last_summary or f"达到轮数上限 {goal.max_rounds}，自动结束。"
        return "ROUND_LIMIT"
    return "COMPLETED"
