"""内层 Flow / 外层 Goal / Subagent 隔离的行为测试（用脚本化假 LLM，不联网）。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agent_nodes import AgentSession, SessionState  # noqa: E402
from core.goal import GoalState, run_goal  # noqa: E402
from core.memory import Memory  # noqa: E402
from core.profile import Profile  # noqa: E402
from core.security import SecurityPolicy  # noqa: E402
from tools import get_tools  # noqa: E402
from tools.skill_loader import get_skill_catalog  # noqa: E402
from tools.subagent import spawn_subagent  # noqa: E402


def tool_call(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
    }


def text_response(text: str) -> dict:
    return {
        "role": "assistant",
        "content": text,
        "tool_calls": None,
        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
    }


class ScriptedLLM:
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, messages, tools=None, system_prompt="", **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError("脚本化 LLM 的响应已耗尽")
        return self.responses.pop(0)


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = self.root / "workspace" / "cf1920c"
        self.base.mkdir(parents=True)
        (self.base / "a.txt").write_text("hello from file\n", encoding="utf-8")
        self.memory = Memory(self.root / "chat_memory")
        self.profile = Profile(self.root / "chat_memory")
        self.goal = GoalState()
        self.policy = SecurityPolicy(self.root)
        self.registry = get_tools(
            base_dir=self.base,
            profile=self.profile,
            memory=self.memory,
            goal=self.goal,
            policy=self.policy,
        )
        self.llm = ScriptedLLM([])
        self.session = AgentSession(
            memory=self.memory,
            profile=self.profile,
            registry=self.registry,
            skill_catalog=get_skill_catalog(),
            state=SessionState(goal=self.goal),
            stream=False,
            llm_fn=self.llm,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _new_session(self, responses: list[dict]) -> ScriptedLLM:
        self.llm = ScriptedLLM(responses)
        self.session.llm_fn = self.llm
        return self.llm

    def test_core_tool_count_is_nine(self):
        # 常规对话恰好 9 个核心工具（goal_pause 仅在 Goal 运行时暴露）
        self.assertEqual(len(self.registry.names()), 9, self.registry.names())
        self.assertNotIn("goal_pause", self.registry.names())
        self.goal.start("测试目标")
        self.assertIn("goal_pause", self.registry.names())
        self.assertEqual(len(self.registry.names()), 10)

    def test_chat_executes_tool_and_appends_messages(self):
        self._new_session([tool_call("read", {"file_path": "a.txt"}), text_response("读完了")])
        final = self.session.chat("看一下 a.txt")

        self.assertEqual(final["content"], "读完了")
        roles = [m["role"] for m in self.memory.messages]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])
        tool_message = self.memory.messages[2]
        self.assertEqual(tool_message["tool_call_id"], "call_1")
        self.assertIn("hello from file", tool_message["content"])
        self.assertEqual(self.session.state.tool_calls_count, 1)

        # 第一次 LLM 调用：system prompt 含技能目录与工具 schema
        first_call = self.llm.calls[0]
        self.assertEqual(first_call["messages"][0]["role"], "system")
        self.assertIn("<available_skills>", first_call["messages"][0]["content"])
        tool_names = [t["function"]["name"] for t in first_call["tools"]]
        self.assertIn("run_stress_test", tool_names)
        self.assertIn("spawn_subagent", tool_names)

    def test_goal_complete_exits_loop(self):
        self._new_session([tool_call("goal_complete", {"summary": "全部完成"}), text_response("收尾")])
        self.goal.start("对拍并定位 Bug")
        outcome = run_goal(self.session, self.goal)

        self.assertEqual(outcome, "COMPLETED")
        self.assertFalse(self.goal.active)
        self.assertEqual(self.goal.current_round, 1)
        self.assertEqual(self.goal.last_summary, "全部完成")
        goal_messages = [m for m in self.memory.messages if m["role"] == "user" and "[GOAL 第 1/" in m["content"]]
        self.assertEqual(len(goal_messages), 1)

    def test_goal_pause_and_resume(self):
        self._new_session(
            [tool_call("goal_pause", {"question_for_user": "你的清空循环上界是多少？"}), text_response("等你回答")]
        )
        self.goal.start("苏格拉底点拨")
        outcome = run_goal(self.session, self.goal)
        self.assertEqual(outcome, "PAUSED")
        self.assertTrue(self.goal.paused)
        self.assertIn("清空循环", self.goal.pending_question)

        self.assertTrue(self.goal.resume())
        self._new_session([tool_call("goal_complete", {"summary": "ok"}), text_response("完成")])
        self.assertEqual(run_goal(self.session, self.goal), "COMPLETED")

    def test_goal_round_limit(self):
        self._new_session([text_response("我先想想"), text_response("还在想")])
        self.goal.start("永不完成的目标", max_rounds=2)
        outcome = run_goal(self.session, self.goal)
        self.assertEqual(outcome, "ROUND_LIMIT")
        self.assertFalse(self.goal.active)
        self.assertEqual(self.goal.current_round, 2)

    def test_update_profile_tool_writes_memory(self):
        self._new_session(
            [
                tool_call(
                    "update_profile",
                    {
                        "problem_id": "cf1920c",
                        "bug_id": "BUG_MULTITEST_DIRTY",
                        "bug_summary": "wa.cpp:19 邻接表未重置",
                        "topic": "graph",
                        "mastery_delta": -3,
                    },
                ),
                text_response("已记录"),
            ]
        )
        self.session.chat("记录一下")
        self.assertEqual(self.profile.record_count("BUG_MULTITEST_DIRTY"), 1)
        self.assertIn("cf1920c", (self.root / "chat_memory" / "MEMORY.md").read_text(encoding="utf-8"))

    def test_unknown_tool_returns_error_to_model(self):
        self._new_session([tool_call("not_a_tool", {}), text_response("我换个工具")])
        self.session.chat("试试")
        tool_message = next(m for m in self.memory.messages if m["role"] == "tool")
        self.assertIn("未知工具", tool_message["content"])


class SubagentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root / "workspace" / "cf1920c"
        self.ws.mkdir(parents=True)
        self.policy = SecurityPolicy(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_subagent_writes_in_isolated_workspace_and_returns_json(self):
        llm = ScriptedLLM(
            [
                tool_call("write", {"file_path": "brute.cpp", "content": "int main(){return 0;}\n"}),
                text_response(
                    json.dumps(
                        {
                            "status": "COUNTEREXAMPLE_FOUND",
                            "failure_type": "WA",
                            "minimal_input": "2\n1 2\n",
                            "expected_output": "3",
                            "actual_output": "2",
                        },
                        ensure_ascii=False,
                    )
                ),
            ]
        )
        raw = spawn_subagent("对拍并最小化", "请写 brute.cpp 并给出反例", self.ws, llm_fn=llm, policy=self.policy)
        report = json.loads(raw)

        self.assertEqual(report["status"], "SUBAGENT_DONE")
        self.assertEqual(report["rounds"], 2)
        self.assertTrue((self.ws / "brute.cpp").is_file())
        self.assertTrue((self.ws / "subagent_report.json").is_file())
        self.assertIn("COUNTEREXAMPLE_FOUND", report["report"])

        # Subagent 不应看到 spawn_subagent / update_profile / goal_complete
        names = [t["function"]["name"] for t in llm.calls[0]["tools"]]
        for forbidden in ("spawn_subagent", "update_profile", "goal_complete", "goal_pause"):
            self.assertNotIn(forbidden, names)
        self.assertIn("run_stress_test", names)
        self.assertIn("write", names)

    def test_subagent_round_limit(self):
        # 每轮都发工具调用，达到上限后返回 ROUND_LIMIT 而不是死循环
        responses = [tool_call("read", {"file_path": "missing.txt"}) for _ in range(3)]
        llm = ScriptedLLM(responses)
        report = json.loads(
            spawn_subagent("死循环测试", "一直读文件", self.ws, llm_fn=llm, max_rounds=3, policy=self.policy)
        )
        self.assertEqual(report["status"], "SUBAGENT_ROUND_LIMIT")
        self.assertEqual(report["rounds"], 3)


if __name__ == "__main__":
    unittest.main()
