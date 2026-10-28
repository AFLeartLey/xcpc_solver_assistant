"""工具包出口：``get_tools`` 按需组装 9 个核心原子工具。

常规对话暴露 9 个工具；仅当传入 ``goal`` 且处于运行态时额外暴露 ``goal_pause``
（PRD 3.3 要求的能力，用于"讲解后等待选手动手"的人工确认关卡）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.security import SecurityPolicy

from .builtins.bash_tool import build_bash_tool
from .builtins.file_tools import build_file_tools
from .builtins.stress_runner import run_stress_test
from .builtins.tool_def import Tool, ToolRegistry, obj_schema
from .executor import ToolExecutor
from .skill_loader import (
    get_skill_catalog,
    list_skill_summaries,
    load_skill_content,
    scan_skills,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent / "skills"

__all__ = [
    "get_tools",
    "build_tool_registry",
    "Tool",
    "ToolRegistry",
    "ToolExecutor",
    "obj_schema",
    "SecurityPolicy",
    "run_stress_test",
    "get_skill_catalog",
    "load_skill_content",
    "list_skill_summaries",
    "scan_skills",
]


# --------------------------------------------------------------------------- #
# 各工具定义
# --------------------------------------------------------------------------- #
def _stress_tool(base_dir: Path, policy: SecurityPolicy) -> Tool:
    def _run(
        workspace_dir: str,
        target_cpp: str,
        brute_cpp: str,
        gen_py: str,
        spj_cpp: str = "",
        iterations: int = 200,
        time_limit: float | None = None,
    ) -> str:
        ws = Path(workspace_dir).expanduser()
        if not ws.is_absolute():
            ws = base_dir / ws
        return run_stress_test(
            str(ws), target_cpp, brute_cpp, gen_py, spj_cpp, iterations, time_limit, policy=policy
        )

    return Tool(
        "run_stress_test",
        "【核心对拍加速器】在 workspace_dir 内自动编译 target/brute（可选 spj）并循环 iterations 次："
        "运行 `python gen_py <seed>` 造数据 -> 跑 brute 得标准答案 -> 跑 target -> 规范化比对。"
        "一旦 WA/TLE/RE 立即停止，并把反例固化为 hack.in / hack.ans / hack.out 返回 JSON。"
        "整题编译只做一次，无需自己写 shell 循环。",
        obj_schema(
            {
                "workspace_dir": {"type": "string", "description": "对拍沙箱目录，如 workspace/cf1920c"},
                "target_cpp": {"type": "string", "description": "选手待测代码 .cpp（相对 workspace_dir 或绝对路径）"},
                "brute_cpp": {"type": "string", "description": "绝对正确的暴力标程 .cpp"},
                "gen_py": {"type": "string", "description": "数据生成器 .py，约定 `python gen.py <seed>` 把测试数据打印到 stdout"},
                "spj_cpp": {"type": "string", "description": "可选 Special Judge：`spj in out ans`，返回 0 表示选手输出合法"},
                "iterations": {"type": "integer", "description": "对拍组数，默认 200"},
                "time_limit": {"type": "number", "description": "单组数据 target 运行超时秒数，默认取环境变量 XCPC_TIMEOUT_SEC（2.0）"},
            },
            ["workspace_dir", "target_cpp", "brute_cpp", "gen_py"],
        ),
        _run,
    )


def _load_skill_tool(skills_dir: Path) -> Tool:
    def _run(skill_name: str) -> str:
        return load_skill_content(skill_name, skills_dir)

    return Tool(
        "load_skill",
        "按需加载某个 XCPC 专项技能的完整 SOP（SKILL.md 正文 + 本地脚本绝对路径）。"
        "当任务匹配 <available_skills> 中某个技能时先调用它，再动手。",
        obj_schema({"skill_name": {"type": "string", "description": "技能名，如 stress-test-hack"}}, ["skill_name"]),
        _run,
    )


def _spawn_tool(base_dir: Path, policy: SecurityPolicy) -> Tool:
    def _run(task_description: str, prompt: str, workspace_dir: str) -> str:
        from .subagent import spawn_subagent  # 延迟导入，避免循环依赖

        ws = Path(workspace_dir).expanduser()
        if not ws.is_absolute():
            ws = base_dir / ws
        ws.mkdir(parents=True, exist_ok=True)
        return spawn_subagent(task_description, prompt, ws, policy=policy)

    return Tool(
        "spawn_subagent",
        "派生一个拥有独立上下文（messages=[]）的 Hacker Subagent 执行高噪声子任务"
        "（写暴力/造数据/对拍/缩反例），主 Agent 阻塞等待其结构化 JSON 战报（约 200 tokens）。"
        "编译报错与上千次对拍日志不会污染主对话。",
        obj_schema(
            {
                "task_description": {"type": "string", "description": "一句话任务名，如“对拍 cf1920c 并最小化反例”"},
                "prompt": {
                    "type": "string",
                    "description": "交给 Subagent 的完整任务说明：题面约束、选手代码路径、已怀疑方向、输出要求",
                },
                "workspace_dir": {"type": "string", "description": "独立沙箱目录，如 workspace/cf1920c"},
            },
            ["task_description", "prompt", "workspace_dir"],
        ),
        _run,
    )


def _update_profile_tool(profile: Any, memory: Any) -> Tool:
    def _run(problem_id: str, bug_id: str, bug_summary: str, topic: str, mastery_delta: int = 0) -> str:
        entry = profile.record_bug_occurrence(
            bug_id, problem_id, bug_summary, topic, mastery_delta=mastery_delta, solution_status="hacked"
        )
        if memory is not None:
            memory.append_long_term(
                f"[画像更新] {problem_id} -> {bug_id}（{bug_summary}）；专题 {topic or '-'} 掌握度变化 {mastery_delta:+d}"
            )
        mastery = (profile.data.get("topic_mastery") or {}).get(topic or "", {})
        return json.dumps(
            {
                "status": "ok",
                "problem_id": problem_id,
                "bug_id": entry.get("id"),
                "bug_name": entry.get("name"),
                "total_count": entry.get("count"),
                "topic": topic,
                "topic_score": mastery.get("score"),
                "topic_level": mastery.get("level"),
            },
            ensure_ascii=False,
        )

    return Tool(
        "update_profile",
        "对拍定位出选手 Bug 后调用：累加该 Bug 指纹计数、追加案例、微调专题掌握度分数，"
        "并往 MEMORY.md 追加一条训练记录。这是“越用越懂选手”的闭环入口。",
        obj_schema(
            {
                "problem_id": {"type": "string", "description": "题目标识，如 cf1920c 或 case_01_multitest_reset"},
                "bug_id": {
                    "type": "string",
                    "description": "Bug 指纹 ID，优先取 BUG_INT_OVERFLOW / BUG_MULTITEST_DIRTY / BUG_BOUNDARY_N1 / BUG_GREEDY_FALSE",
                },
                "bug_summary": {"type": "string", "description": "一句话根因（含具体行号/变量更佳）"},
                "topic": {"type": "string", "description": "算法专题，如 dp / graph / greedy / data_structures"},
                "mastery_delta": {"type": "integer", "description": "掌握度增量，通常 -2 ~ -5；传 0 表示本次不调整分数"},
            },
            ["problem_id", "bug_id", "bug_summary", "topic"],
        ),
        _run,
    )


def _goal_complete_tool(goal: Any) -> Tool:
    def _run(summary: str) -> str:
        goal.complete(summary)
        return json.dumps({"status": "goal_completed", "summary": summary}, ensure_ascii=False)

    return Tool(
        "goal_complete",
        "标记当前外层 /goal 目标已完成并退出自治循环。必须在对拍、点拨、画像更新都真正做完后调用。",
        obj_schema({"summary": {"type": "string", "description": "本次目标完成情况的一句话总结"}}, ["summary"]),
        _run,
    )


def _goal_pause_tool(goal: Any) -> Tool:
    def _run(question_for_user: str) -> str:
        goal.pause(question_for_user)
        return json.dumps({"status": "goal_paused", "question": question_for_user}, ensure_ascii=False)

    return Tool(
        "goal_pause",
        "挂起当前 /goal 并等待选手本人回答（例如 L2 点拨后请选手先自己改代码）。仅在确实需要人类输入时使用。",
        obj_schema(
            {"question_for_user": {"type": "string", "description": "要问选手的问题（明确、可回答）"}},
            ["question_for_user"],
        ),
        _run,
        enabled=lambda: bool(getattr(goal, "active", False) and not getattr(goal, "paused", False)),
    )


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #
def get_tools(
    *,
    base_dir: str | Path | None = None,
    profile: Any = None,
    memory: Any = None,
    goal: Any = None,
    skills_dir: str | Path | None = None,
    include_search: bool = False,
    allow_spawn: bool = True,
    policy: SecurityPolicy | None = None,
) -> ToolRegistry:
    """组装工具注册表（顺序固定，利于 KV Cache）。"""
    base = Path(base_dir) if base_dir else PROJECT_ROOT
    skills = Path(skills_dir) if skills_dir else DEFAULT_SKILLS_DIR
    policy = policy or SecurityPolicy(base)
    registry = ToolRegistry()
    for tool in build_file_tools(base, include_search=include_search, policy=policy):
        registry.register(tool)
    registry.register(build_bash_tool(base, policy=policy))
    registry.register(_stress_tool(base, policy))
    registry.register(_load_skill_tool(skills))
    if allow_spawn:
        registry.register(_spawn_tool(base, policy))
    if profile is not None:
        registry.register(_update_profile_tool(profile, memory))
    if goal is not None:
        registry.register(_goal_complete_tool(goal))
        registry.register(_goal_pause_tool(goal))
    return registry


build_tool_registry = get_tools
