"""输出模式与运行日志测试：终端只显示状态+耗时，细节（含思考过程）落盘。"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rich.console import Console  # noqa: E402

from core.agent_nodes import AgentSession, SessionState  # noqa: E402
from core.logbook import RunLogger  # noqa: E402
from core.memory import Memory  # noqa: E402
from core.profile import Profile  # noqa: E402
from core.security import SecurityPolicy  # noqa: E402
from main import Runtime  # noqa: E402
from tools import get_tools  # noqa: E402


class RunLoggerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_jsonl_and_markdown_written(self):
        logger = RunLogger(self.dir / "logs", session_id="unit")
        logger.log("user_message", content="帮我看看这题")
        logger.log("llm_start", message_count=3, tool_count=9)
        logger.log(
            "llm_end",
            elapsed=1.25,
            content="这是最小反例",
            reasoning="先读题，再构造 T=2 的数据",
            tool_calls=["read"],
            usage={"total_tokens": 42},
            final=True,
        )
        logger.log("tool_end", name="run_stress_test", args={"iterations": 10}, elapsed=3.5, ok=True, result="HACK_SUCCESS")

        self.assertTrue(logger.jsonl_path.is_file())
        self.assertTrue(logger.md_path.is_file())
        self.assertEqual(logger.event_count, 4)

        events = [json.loads(line) for line in logger.jsonl_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["kind"] for e in events], ["user_message", "llm_start", "llm_end", "tool_end"])
        self.assertEqual(events[2]["reasoning"], "先读题，再构造 T=2 的数据")
        self.assertEqual(events[2]["usage"]["total_tokens"], 42)

        md = logger.md_path.read_text(encoding="utf-8")
        self.assertIn("帮我看看这题", md)
        self.assertIn("先读题，再构造 T=2 的数据", md)  # 思考过程完整归档
        self.assertIn("这是最小反例", md)
        self.assertIn("run_stress_test", md)
        self.assertIn("HACK_SUCCESS", md)

    def test_tail_and_summary(self):
        logger = RunLogger(self.dir / "logs", session_id="tail")
        logger.log("user_message", content="第一行")
        logger.log("user_message", content="第二行")
        tail = logger.markdown_tail(3)
        self.assertIn("第二行", tail)
        self.assertIn("日志(Markdown)", logger.summary())


class DisplayModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.buffer = io.StringIO()

    def tearDown(self):
        self.tmp.cleanup()

    def _runtime(self, display: str) -> Runtime:
        console = Console(file=self.buffer, width=120, force_terminal=False, color_system=None)
        return Runtime.build(
            console=console, memory_dir=self.dir, display=display, stream=True, enable_hotkeys=False
        )

    def output(self) -> str:
        return self.buffer.getvalue()

    def _emit_round(self, runtime: Runtime) -> None:
        runtime.on_event("llm_start", {"message_count": 3, "tool_count": 9})
        runtime.on_event(
            "llm_end",
            {
                "elapsed": 2.5,
                "content": "最终答复：改清空范围",
                "reasoning": "秘密推理：diff[n+1] 残留",
                "tool_calls": [],
                "usage": {"total_tokens": 5},
                "final": True,
                "streamed": False,
            },
        )
        runtime.on_event("tool_start", {"name": "run_stress_test", "args": {"workspace_dir": "workspace/wa"}})
        runtime.on_event("tool_end", {"name": "run_stress_test", "elapsed": 12.3, "ok": True, "result": "HACK_SUCCESS"})

    def test_compact_shows_status_and_elapsed_only(self):
        runtime = self._runtime("compact")
        self._emit_round(runtime)
        out = self.output()
        self.assertIn("[思考中]", out)
        self.assertIn("[思考完成] 2.5s", out)
        self.assertIn("[测试中] run_stress_test", out)
        self.assertIn("[完成] run_stress_test 12.3s", out)
        self.assertIn("最终答复", out)
        # 简略模式不展开思考与工具参数
        self.assertNotIn("秘密推理", out)
        self.assertNotIn("workspace_dir", out)

    def test_verbose_shows_reasoning_and_tool_args(self):
        runtime = self._runtime("verbose")
        runtime.show_thinking = True  # 思考默认隐藏，需要显式展开
        self._emit_round(runtime)
        out = self.output()
        self.assertIn("秘密推理", out)
        self.assertIn("workspace_dir", out)
        self.assertIn("[思考完成] 2.5s", out)
        # 顺序必须是：状态 -> 思考 -> 正文，避免状态行粘在正文末尾
        self.assertLess(out.index("[思考完成]"), out.index("秘密推理"))
        self.assertLess(out.index("秘密推理"), out.index("最终答复"))

    def test_display_command_persists(self):
        runtime = self._runtime("compact")
        self.assertTrue(runtime.session.stream)  # 流式默认开启
        self.assertTrue(runtime.handle_input("/display verbose"))
        self.assertEqual(runtime.display, "verbose")
        self.assertTrue(runtime.session.stream)  # 展示模式不影响流式
        self.assertEqual(Profile(self.dir).display_mode, "verbose")

        self.assertTrue(runtime.handle_input("/display compact"))
        self.assertEqual(runtime.display, "compact")
        self.assertIn("非法展示模式", self._invalid_mode_output(runtime))

    def test_thinking_hidden_by_default_then_toggled(self):
        runtime = self._runtime("compact")
        self.assertFalse(runtime.show_thinking)

        runtime.on_event("llm_start", {"message_count": 1, "tool_count": 9})
        runtime.on_reasoning("隐藏的推理")
        runtime.on_event(
            "llm_end",
            {
                "elapsed": 1.0,
                "content": "答案",
                "reasoning": "隐藏的推理",
                "reasoning_streamed": True,
                "tool_calls": [],
                "usage": {"total_tokens": 3},
                "final": True,
                "streamed": True,
            },
        )
        self.assertNotIn("隐藏的推理", self.output())

        before = len(self.output())
        self.assertTrue(runtime.handle_input("/think"))
        self.assertIn("已展开", self.output()[before:])
        self.assertTrue(Profile(self.dir).show_thinking)

        runtime.on_event("llm_start", {"message_count": 1, "tool_count": 9})
        runtime.on_reasoning("可见的推理")
        self.assertIn("可见的推理", self.output())
        self.assertIn("🧠 思考过程", self.output())

    def test_status_closed_before_streamed_content(self):
        runtime = self._runtime("compact")
        runtime.on_event("llm_start", {"message_count": 1, "tool_count": 9})
        runtime.on_text("流式正文开始")
        runtime.on_text("继续输出")
        out = self.output()
        self.assertEqual(out.count("流式正文开始"), 1)
        self.assertLess(out.index("[思考完成]"), out.index("流式正文开始"))

    def _invalid_mode_output(self, runtime: Runtime) -> str:
        before = len(self.buffer.getvalue())
        runtime.handle_input("/display 不存在的模式")
        return self.buffer.getvalue()[before:]

    def test_log_command_reports_paths(self):
        runtime = self._runtime("compact")
        self.assertIsNotNone(runtime.logger)
        self.assertTrue(runtime.handle_input("/log"))
        out = self.output()
        self.assertIn("日志(JSONL)", out)
        self.assertIn("session_", out)

    def test_hotkey_window_starts_and_stops_listener(self):
        runtime = self._runtime("compact")

        class _FakeListener:
            def __init__(self):
                self.starts = 0
                self.stops = 0

            def start(self):
                self.starts += 1
                return True

            def stop(self):
                self.stops += 1

        fake = _FakeListener()
        runtime.hotkeys = fake  # type: ignore[assignment]
        with runtime._hotkey_window():
            self.assertEqual(fake.starts, 1)
        self.assertEqual(fake.stops, 1)

    def test_compact_renders_final_content_once(self):
        """回归：非流式正文只能由事件层渲染一次，且状态行在前。"""
        runtime = self._runtime("compact")
        runtime.session.llm_fn = lambda *a, **k: {
            "role": "assistant",
            "content": "答复正文",
            "tool_calls": None,
            "reasoning_content": "内部思考",
            "usage": {"total_tokens": 3},
        }
        with patch("main.has_api_key", return_value=True):
            self.assertTrue(runtime.handle_input("你好"))
        out = self.output()
        self.assertEqual(out.count("答复正文"), 1)
        self.assertLess(out.index("[思考完成]"), out.index("答复正文"))
        self.assertNotIn("内部思考", out)


class _ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    def __call__(self, messages, tools=None, system_prompt="", **kwargs):
        return self.responses.pop(0)


def _tool_call(name: str, args: dict) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


class AgentEventTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = self.root / "workspace" / "cf1"
        self.base.mkdir(parents=True)
        (self.base / "a.txt").write_text("hello\n", encoding="utf-8")
        memory = Memory(self.root / "chat_memory")
        profile = Profile(self.root / "chat_memory")
        registry = get_tools(base_dir=self.base, profile=profile, memory=memory, policy=SecurityPolicy(self.root))
        self.events: list[tuple[str, dict]] = []
        self.session = AgentSession(
            memory=memory,
            profile=profile,
            registry=registry,
            state=SessionState(),
            stream=False,
            llm_fn=_ScriptedLLM(
                [
                    _tool_call("read", {"file_path": "a.txt"}),
                    {
                        "role": "assistant",
                        "content": "读完了，第 1 行是 hello",
                        "tool_calls": None,
                        "reasoning_content": "先确认文件内容再回答",
                        "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
                    },
                ]
            ),
            on_event=lambda kind, data: self.events.append((kind, data)),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_event_stream_contains_reasoning_and_timings(self):
        self.session.chat("看看 a.txt")
        kinds = [kind for kind, _ in self.events]
        self.assertEqual(kinds.count("user_message"), 1)  # 工具往返不重复记录用户消息
        for expected in ("llm_start", "llm_end", "tool_start", "tool_end"):
            self.assertIn(expected, kinds)

        tool_end = next(data for kind, data in self.events if kind == "tool_end")
        self.assertEqual(tool_end["name"], "read")
        self.assertTrue(tool_end["ok"])
        self.assertGreaterEqual(tool_end["elapsed"], 0)
        self.assertIn("hello", tool_end["result"])

        final = [data for kind, data in self.events if kind == "llm_end"][-1]
        self.assertTrue(final["final"])
        self.assertEqual(final["reasoning"], "先确认文件内容再回答")
        self.assertGreaterEqual(final["elapsed"], 0)


if __name__ == "__main__":
    unittest.main()
