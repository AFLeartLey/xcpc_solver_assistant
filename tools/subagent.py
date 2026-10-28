"""Subagent 上下文隔离执行器。

主 Agent 只需一次 ``spawn_subagent`` 调用，就能把"写暴力 + 写生成器 + 反复对拍 +
反例最小化"这类产生海量日志的脏活隔离出去：Subagent 拥有独立的 ``messages``，
最多执行 ``max_rounds`` 轮工具调用，最终只回传一份结构化 JSON 战报。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from core.llm import call_llm
from core.memory import normalize_message
from core.security import SecurityPolicy

SUBAGENT_SYSTEM_PROMPT = """你是 XCPC-Claw 的 Hacker Subagent，在隔离沙箱中独立完成对拍脏活。

工作纪律：
1. 只在给定工作目录内创建文件（brute.cpp / gen.py / spj.cpp / *.txt），不要读写该目录之外的文件。
   文件访问受安全策略限制：只能读工作区文件或用户本次会话提供的文件，写入只能落在 workspace/<problem_slug>/；
   被拒绝时不要反复重试，直接在战报里说明 "BLOCKED" 原因。
2. 先用 `load_skill("stress-test-hack")` 加载对拍 SOP（若可用），再写代码。
3. brute.cpp 必须牺牲效率换绝对正确（O(2^N) / O(N!) / 朴素 DFS / O(N^3)），严禁复用选手的贪心或未证明性质。
4. gen.py 约定 `python gen.py <seed>` 把数据打印到 stdout；默认极小规模（N<=6，值域<=10）。
5. 用 `run_stress_test` 对拍；一旦捕获反例，尽量再缩小规模复现一次。
6. 禁止向用户提问，禁止输出长篇过程日志。

结束时只输出一个 JSON 对象（不要 Markdown 代码块之外的任何解释），格式：
{
  "status": "COUNTEREXAMPLE_FOUND" | "ALL_PASSED" | "BLOCKED",
  "failure_type": "WA" | "TLE" | "RE" | "",
  "iterations_run": 0,
  "minimal_input": "反例输入原文（可含换行）",
  "expected_output": "标程输出",
  "actual_output": "选手输出",
  "divergence_observation": "一句话说明触发条件",
  "files": {"in": "hack.in 路径", "ans": "hack.ans 路径", "out": "hack.out 路径"},
  "brute_source_path": "", "gen_source_path": "", "spj_source_path": "",
  "note": "给主 Agent 的下一步建议"
}
"""


def spawn_subagent(
    task_description: str,
    prompt: str,
    workspace_dir: str | Path,
    *,
    max_rounds: int = 12,
    model: str | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    llm_fn: Callable[..., dict[str, Any]] | None = None,
    write_report: bool = True,
    policy: SecurityPolicy | None = None,
) -> str:
    """运行一个隔离的 Subagent，返回 JSON 字符串战报（主 Agent 直接读取）。"""
    from tools import get_tools
    from tools.executor import ToolExecutor

    started = time.perf_counter()
    ws = Path(workspace_dir).expanduser().resolve()
    policy = policy or SecurityPolicy(Path(__file__).resolve().parents[1])
    decision = policy.check_workspace_dir(ws)
    if not decision.allowed:
        return _dump(
            {
                "status": "POLICY_DENIED",
                "error": decision.reason,
                "workspace": str(ws),
                "task": task_description,
            }
        )
    ws.mkdir(parents=True, exist_ok=True)
    llm = llm_fn or call_llm
    registry = get_tools(base_dir=ws, allow_spawn=False, include_search=True, policy=policy)
    executor = ToolExecutor(registry)

    user_prompt = (
        f"任务: {task_description}\n"
        f"工作目录(绝对路径): {ws}\n\n"
        f"{prompt.strip()}\n\n"
        "请立即开始执行（可用工具: " + ", ".join(registry.names()) + "）。"
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SUBAGENT_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    rounds = 0
    content = ""
    status = "SUBAGENT_ROUND_LIMIT"

    for rounds in range(1, max(1, max_rounds) + 1):
        if on_event:
            on_event("round", {"round": rounds, "max_rounds": max_rounds})
        try:
            resp = llm(messages, registry.schemas() or None, "", model=model, stream=False)
        except Exception as exc:  # noqa: BLE001 - 子代理失败必须转成战报而不是炸掉主循环
            return _dump(
                {
                    "status": "SUBAGENT_ERROR",
                    "error": f"{type(exc).__name__}: {exc}",
                    "rounds": rounds - 1,
                    "workspace": str(ws),
                    "task": task_description,
                }
            )

        for key in usage_totals:
            usage_totals[key] += int((resp.get("usage") or {}).get(key) or 0)
        stored = normalize_message(resp)
        messages.append(stored)
        content = str(stored.get("content") or "")

        calls = stored.get("tool_calls") or []
        if not calls:
            status = "SUBAGENT_DONE"
            break
        if on_event:
            on_event("tool_calls", {"round": rounds, "names": [(c.get("function") or {}).get("name") for c in calls]})
        for call, result in zip(calls, executor.execute(calls)):
            messages.append(result)
            if on_event:
                on_event(
                    "tool_result",
                    {
                        "name": (call.get("function") or {}).get("name", "?"),
                        "result": str(result.get("content") or "")[:400],
                    },
                )

    report = _dump(
        {
            "status": status,
            "task": task_description,
            "rounds": rounds,
            "elapsed_sec": round(time.perf_counter() - started, 3),
            "workspace": str(ws),
            "usage": usage_totals,
            "report": content[:4000],
        }
    )
    if write_report:
        try:
            (ws / "subagent_report.json").write_text(report, encoding="utf-8")
        except OSError:
            pass
    return report


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
