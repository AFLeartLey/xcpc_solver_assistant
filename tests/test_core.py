"""XCPC-Claw 单元测试（stdlib unittest，无需 API Key）。

运行：``python -m unittest discover -s tests -v``
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.memory import Memory, build_context, normalize_message  # noqa: E402
from core.node import Flow, Node  # noqa: E402
from core.profile import DEFAULT_PROFILE, Profile  # noqa: E402
from tools.builtins.tool_def import Tool, ToolRegistry, obj_schema  # noqa: E402
from tools.executor import ToolExecutor  # noqa: E402
from tools.skill_loader import get_skill_catalog, load_skill_content, scan_skills  # noqa: E402


class NodeFlowTest(unittest.TestCase):
    class Echo(Node):
        def __init__(self, action: str, value):
            super().__init__()
            self.action = action
            self.value = value

        def exec(self, payload):
            return self.action, self.value

    def test_operator_pairing_and_flow(self):
        first = self.Echo("next", 1)
        second = self.Echo("done", 2)
        first - "next" >> second
        action, payload = Flow(start=first).run(None)
        self.assertEqual(action, "done")
        self.assertEqual(payload, 2)

    def test_flow_without_successor_stops(self):
        node = self.Echo("done", "x")
        self.assertEqual(Flow(start=node).run(None), ("done", "x"))
        # 空 Flow 不执行任何节点（action 保持初始值）
        self.assertEqual(Flow().run(None), ("default", None))

    def test_retry_until_success(self):
        class Flaky(Node):
            def __init__(self):
                super().__init__(max_retries=3)
                self.calls = 0

            def exec(self, payload):
                self.calls += 1
                if self.calls < 3:
                    raise RuntimeError("boom")
                return "ok", self.calls

        with patch("core.node.time.sleep", lambda _: None):
            self.assertEqual(Flaky()._exec(None), ("ok", 3))

    def test_retry_exhausted(self):
        class Broken(Node):
            def exec(self, payload):
                raise ValueError("always")

        with patch("core.node.time.sleep", lambda _: None), self.assertRaises(ValueError):
            Broken(max_retries=2)._exec(None)


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _memory(self, **kwargs) -> Memory:
        return Memory(self.dir, **kwargs)

    def test_append_and_reload(self):
        memory = self._memory()
        memory.append({"role": "user", "content": "hello"})
        memory.append({"role": "assistant", "content": "hi", "reasoning_content": "think"})
        reloaded = self._memory()
        self.assertEqual(len(reloaded.messages), 2)
        self.assertEqual(reloaded.messages[1]["reasoning_content"], "think")
        self.assertNotIn("usage", reloaded.messages[1])  # 只保留 5 个字段

    def test_repair_drops_incomplete_tool_group(self):
        session = self.dir / "session.jsonl"
        session.write_text(
            "\n".join(
                [
                    json.dumps({"role": "user", "content": "读文件"}, ensure_ascii=False),
                    json.dumps(
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                                {"id": "c2", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps({"role": "tool", "tool_call_id": "c1", "content": "ok"}, ensure_ascii=False),
                ]
            ),
            encoding="utf-8",
        )
        memory = self._memory()
        self.assertEqual(memory.messages, [])
        self.assertIn("残尾", memory.recovery_note)
        self.assertEqual(session.read_text(encoding="utf-8").strip(), "")

    def test_repair_keeps_complete_group(self):
        session = self.dir / "session.jsonl"
        session.write_text(
            "\n".join(
                [
                    json.dumps({"role": "user", "content": "q"}),
                    json.dumps(
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
                        }
                    ),
                    json.dumps({"role": "tool", "tool_call_id": "c1", "content": "ok"}),
                    json.dumps({"role": "assistant", "content": "answer"}),
                ]
            ),
            encoding="utf-8",
        )
        memory = self._memory()
        self.assertEqual(len(memory.messages), 4)
        self.assertEqual(memory.recovery_note, "")

    def test_compress_protects_tool_pairs(self):
        messages = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "step"},
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "file body"},
            {"role": "assistant", "content": "done reading"},
            {"role": "user", "content": "next"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "again"},
            {"role": "assistant", "content": "ok2"},
        ]
        memory = self._memory(keep_messages=6)
        memory.messages = messages
        memory._rewrite_disk()

        def fake_llm(*args, **kwargs):
            return {"role": "assistant", "content": json.dumps({"summary": "摘要内容", "memory_update": "结论A"}, ensure_ascii=False)}

        memory.llm_fn = fake_llm
        self.assertTrue(memory.maybe_compress(force=True))

        self.assertEqual(memory.messages[0]["role"], "system")
        self.assertIn("摘要内容", memory.messages[0]["content"])
        self.assertLess(len(memory.messages), len(messages))
        # 压缩边界不能把 assistant(tool_calls) 与 tool 拆散
        roles = [m["role"] for m in memory.messages[1:]]
        if "tool" in roles:
            first_tool = roles.index("tool")
            self.assertEqual(roles[first_tool - 1], "assistant")
            self.assertTrue(memory.messages[1:][first_tool - 1].get("tool_calls"))
        self.assertIn("结论A", (self.dir / "MEMORY.md").read_text(encoding="utf-8"))

    def test_compress_falls_back_when_llm_fails(self):
        memory = self._memory(keep_messages=2)
        memory.messages = [{"role": "user", "content": f"m{i}"} for i in range(8)]
        memory._rewrite_disk()

        def boom(*args, **kwargs):
            raise RuntimeError("api down")

        memory.llm_fn = boom
        self.assertTrue(memory.maybe_compress(force=True))
        self.assertTrue(memory.compress_events[-1]["fallback"])
        self.assertIn("机械摘要", memory.messages[0]["content"])

    def test_compress_skipped_below_threshold(self):
        memory = self._memory(max_context_length=100000)
        memory.messages = [{"role": "user", "content": "x"} for _ in range(10)]
        memory.last_usage = {"total_tokens": 10}
        self.assertFalse(memory.maybe_compress())

    def test_long_term_memory_roundtrip(self):
        memory = self._memory()
        memory.append_long_term("第一次记录")
        self.assertIn("第一次记录", memory.long_term_memory())
        self.assertIn("第一次记录", (self.dir / "MEMORY.md").read_text(encoding="utf-8"))

    def test_clear_keeps_long_term(self):
        memory = self._memory()
        memory.append({"role": "user", "content": "hi"})
        memory.append_long_term("kept")
        memory.clear()
        self.assertEqual(memory.messages, [])
        self.assertIn("kept", memory.long_term_memory())


class BuildContextTest(unittest.TestCase):
    def test_system_prompt_order(self):
        messages = build_context(
            [{"role": "user", "content": "hi"}],
            base_prompt="BASE",
            skill_catalog="<available_skills>x</available_skills>",
            profile_snippet="<contestant_profile>p</contestant_profile>",
            long_term_memory="记忆",
        )
        self.assertEqual(messages[0]["role"], "system")
        content = messages[0]["content"]
        self.assertLess(content.index("BASE"), content.index("<available_skills>"))
        self.assertLess(content.index("<available_skills>"), content.index("<contestant_profile>"))
        self.assertLess(content.index("<contestant_profile>"), content.index("<long_term_memory>"))
        self.assertEqual(messages[1]["content"], "hi")

    def test_normalize_message_filters_fields(self):
        clean = normalize_message({"role": "assistant", "content": None, "usage": {"x": 1}, "tool_calls": []})
        self.assertEqual(clean, {"role": "assistant", "content": ""})


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.profile = Profile(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_created(self):
        self.assertTrue((self.dir / "PROFILE.json").is_file())
        self.assertEqual(self.profile.data["contestant_id"], DEFAULT_PROFILE["contestant_id"])
        self.assertEqual(len(self.profile.data["bug_fingerprints"]), 4)
        self.assertEqual(self.profile.hint_level, 2)

    def test_record_bug_occurrence(self):
        before = self.profile.data["topic_mastery"]["greedy"]["score"]
        entry = self.profile.record_bug_occurrence(
            "BUG_GREEDY_FALSE", "cf1899d", "wa.cpp:12 按性价比排序后直接贪心", "greedy", mastery_delta=-4
        )
        self.assertEqual(entry["count"], 1)
        self.assertEqual(self.profile.data["topic_mastery"]["greedy"]["score"], before - 4)
        self.assertEqual(self.profile.data["solved_or_hacked_history"][-1]["problem"], "cf1899d")
        snippet = self.profile.get_profile_prompt_snippet()
        self.assertIn("BUG_GREEDY_FALSE", snippet)
        self.assertIn("cf1899d", snippet)

    def test_zero_delta_keeps_score(self):
        before = self.profile.data["topic_mastery"]["dp"]["score"]
        self.profile.record_bug_occurrence("BUG_BOUNDARY_N1", "p", "detail", "dp", mastery_delta=0)
        self.assertEqual(self.profile.data["topic_mastery"]["dp"]["score"], before)

    def test_unknown_bug_id_creates_entry(self):
        self.profile.record_bug_occurrence("BUG_CUSTOM_X", "p1", "new bug", "math")
        ids = [bug["id"] for bug in self.profile.data["bug_fingerprints"]]
        self.assertIn("BUG_CUSTOM_X", ids)
        self.assertEqual(self.profile.record_count("BUG_CUSTOM_X"), 1)

    def test_hint_level(self):
        self.assertEqual(self.profile.set_hint_level("L3"), 3)
        self.assertEqual(self.profile.hint_level, 3)
        with self.assertRaises(ValueError):
            self.profile.set_hint_level("L9")
        reloaded = Profile(self.dir)
        self.assertEqual(reloaded.hint_level, 3)

    def test_corrupt_profile_recovers(self):
        (self.dir / "PROFILE.json").write_text("{not json", encoding="utf-8")
        recovered = Profile(self.dir)
        self.assertEqual(recovered.data["contestant_id"], "default_player")


class ToolExecutorTest(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.registry.register(
            Tool(
                "echo",
                "echo back",
                obj_schema({"text": {"type": "string"}}, ["text"]),
                lambda text: f"echo:{text}",
            )
        )
        self.registry.register(Tool("boom", "raise", obj_schema({}), lambda: (_ for _ in ()).throw(RuntimeError("kaboom"))))
        self.executor = ToolExecutor(self.registry, max_result_chars=200)

    def _call(self, name: str, args: str, call_id: str = "c1") -> dict:
        return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}

    def test_normal_call(self):
        result = self.executor.execute([self._call("echo", '{"text": "hi"}')])[0]
        self.assertEqual(result["role"], "tool")
        self.assertEqual(result["tool_call_id"], "c1")
        self.assertEqual(result["content"], "echo:hi")

    def test_bad_json(self):
        content = self.executor.execute([self._call("echo", "{oops")])[0]["content"]
        self.assertIn("不是合法 JSON", content)

    def test_unknown_tool(self):
        content = self.executor.execute([self._call("nope", "{}")])[0]["content"]
        self.assertIn("未知工具", content)

    def test_exception_isolated(self):
        content = self.executor.execute([self._call("boom", "{}")])[0]["content"]
        self.assertIn("kaboom", content)

    def test_missing_argument(self):
        content = self.executor.execute([self._call("echo", "{}")])[0]["content"]
        self.assertIn("参数不匹配", content)

    def test_output_truncated(self):
        self.registry.register(
            Tool("long", "long", obj_schema({}), lambda: "x" * 5000)
        )
        content = self.executor.execute([self._call("long", "{}")])[0]["content"]
        self.assertLessEqual(len(content), 300)
        self.assertIn("截断", content)


class SkillLoaderTest(unittest.TestCase):
    def test_catalog_contains_all_skills(self):
        catalog = get_skill_catalog()
        self.assertIn("<available_skills>", catalog)
        for name in (
            "stress-test-hack",
            "graph-tree-gen",
            "socratic-xcpc-tutor",
            "complexity-edge-audit",
            "profile-analyzer",
        ):
            self.assertIn(f"`{name}`", catalog)

    def test_skill_metadata_parsed(self):
        skills = scan_skills()
        self.assertEqual(len(skills), 5)
        stress = next(s for s in skills if s.name == "stress-test-hack")
        self.assertIn("对拍", stress.description)
        self.assertTrue(stress.scripts)
        self.assertTrue(any(s.name == "delta_reduce.py" for s in stress.scripts))

    def test_load_skill_content(self):
        content = load_skill_content("stress-test-hack")
        self.assertIn("SOP", content)
        self.assertIn("delta_reduce.py", content)
        self.assertIn("brute.cpp", content)

    def test_unknown_skill(self):
        self.assertIn("ERROR", load_skill_content("not-exist"))


class FileToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = self.root / "workspace" / "case"
        self.base.mkdir(parents=True)
        from tools.builtins.file_tools import build_file_tools
        from core.security import SecurityPolicy

        self.policy = SecurityPolicy(self.root)
        self.tools = {
            tool.name: tool
            for tool in build_file_tools(self.base, include_search=True, policy=self.policy)
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_read_edit(self):
        self.assertIn("OK", self.tools["write"].run(file_path="sub/a.cpp", content="line1\nline2\nline3\n"))
        self.assertTrue(
            self.tools["read"].run(file_path="sub/a.cpp", offset=2, limit=1).startswith("2: line2")
        )
        self.assertIn("OK", self.tools["edit"].run(file_path="sub/a.cpp", old_string="line2", new_string="LINE2"))
        self.assertIn("LINE2", self.tools["read"].run(file_path="sub/a.cpp"))
        self.assertIn("未找到待替换片段", self.tools["edit"].run(file_path="sub/a.cpp", old_string="nope", new_string="x"))

    def test_edit_ambiguous(self):
        self.tools["write"].run(file_path="b.cpp", content="x = 1;\nx = 1;\n")
        self.assertIn("出现了 2 次", self.tools["edit"].run(file_path="b.cpp", old_string="x = 1;", new_string="y"))

    def test_read_missing_and_dir(self):
        self.assertIn("不存在", self.tools["read"].run(file_path="none.cpp"))
        self.assertIn("是目录", self.tools["read"].run(file_path="."))

    def test_search_tools(self):
        self.tools["write"].run(file_path="src/x.py", content="def solve():\n    return 42\n")
        self.assertIn("solve", self.tools["grep"].run(pattern="solve", path=".", include="*.py"))
        self.assertIn("x.py", self.tools["find"].run(pattern="**/*.py"))
        self.assertIn("src/", self.tools["ls"].run(path="."))

    def test_read_and_write_are_policy_guarded(self):
        (self.root / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
        self.assertIn("安全策略", self.tools["read"].run(file_path="../../.env"))
        self.assertIn("安全策略", self.tools["write"].run(file_path="../../README.md", content="x"))
        self.assertIn("安全策略", self.tools["write"].run(file_path="../root.txt", content="x"))
        self.assertIn("OK", self.tools["write"].run(file_path="nested/ok.txt", content="ok"))
        # 工作区内的普通 secret.txt 不应被误伤
        self.assertIn("OK", self.tools["write"].run(file_path="secret.txt", content="prob"))
        self.assertIn("prob", self.tools["read"].run(file_path="secret.txt"))


if __name__ == "__main__":
    unittest.main()
