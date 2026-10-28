"""CLI 骨架冒烟测试：不联网验证命令分发、渲染与错误兜底。"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rich.console import Console  # noqa: E402

from main import Runtime  # noqa: E402


class CliSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.buffer = io.StringIO()
        console = Console(file=self.buffer, width=120, force_terminal=False, color_system=None)
        self.runtime = Runtime.build(console=console, memory_dir=Path(self.tmp.name), stream=False)

    def tearDown(self):
        self.tmp.cleanup()

    def output(self) -> str:
        return self.buffer.getvalue()

    def test_local_commands(self):
        for line in ("/help", "/profile", "/skills", "/goal status", "/goal clear", "/mode L3"):
            self.assertTrue(self.runtime.handle_input(line), line)
        text = self.output()
        self.assertIn("可用命令", text)
        self.assertIn("算法掌握度", text)
        self.assertIn("可插拔技能", text)
        self.assertIn("Goal 状态", text)
        self.assertEqual(self.runtime.profile.hint_level, 3)

    def test_invalid_mode_is_reported_not_raised(self):
        self.assertTrue(self.runtime.handle_input("/mode L9"))
        self.assertIn("非法点拨层级", self.output())

    def test_unknown_command(self):
        self.assertTrue(self.runtime.handle_input("/nope"))
        self.assertIn("未知命令", self.output())

    def test_clear_keeps_profile(self):
        self.runtime.memory.append({"role": "user", "content": "hello"})
        self.assertTrue(self.runtime.handle_input("/clear"))
        self.assertEqual(self.runtime.memory.messages, [])
        self.assertTrue(self.runtime.profile.path.is_file())

    def test_exit_returns_false(self):
        self.assertFalse(self.runtime.handle_input("/exit"))

    def test_hack_without_api_key_does_not_start_goal(self):
        with patch("main.has_api_key", return_value=False):
            self.assertTrue(self.runtime.handle_input("/hack evals/cases/case_01_multitest_reset/wa.cpp"))
        self.assertFalse(self.runtime.goal.active)
        self.assertIn("缺少 API Key", self.output())

    def test_hack_missing_file(self):
        self.assertTrue(self.runtime.handle_input("/hack not_exists.cpp"))
        self.assertIn("代码文件不存在", self.output())

    def test_allow_grants_session_read_only(self):
        samples = Path(self.tmp.name) / "samples"
        samples.mkdir()
        code = samples / "my_sol.cpp"
        code.write_text("int main(){return 0;}\n", encoding="utf-8")

        self.assertFalse(self.runtime.policy.check_read(code).allowed)  # 默认不可读
        self.assertTrue(self.runtime.handle_input(f"/allow {code}"))
        self.assertIn("已授权", self.output())
        self.assertTrue(self.runtime.policy.check_read(code).allowed)
        # 授权只影响读取，写入仍被限制在工作区
        self.assertFalse(self.runtime.policy.check_write(code).allowed)

    def test_allow_rejects_env_file(self):
        env_file = Path(self.tmp.name) / ".env"
        env_file.write_text("OPENAI_API_KEY=sk-should-never-be-read\n", encoding="utf-8")
        self.assertTrue(self.runtime.handle_input(f"/allow {env_file}"))
        self.assertIn("禁止访问 .env", self.output())
        self.assertFalse(self.runtime.policy.check_read(env_file).allowed)

    def test_hack_authorizes_user_files(self):
        code = ROOT / "evals" / "cases" / "case_01_multitest_reset" / "wa.cpp"
        problem = ROOT / "evals" / "cases" / "case_01_multitest_reset" / "problem.md"
        # 应用内但不在可读范围内的 evals/ 文件，靠 /hack 的会话授权才能读
        self.assertFalse(self.runtime.policy.check_read(code).allowed)
        with patch.object(self.runtime, "start_goal") as fake_start:
            self.assertTrue(self.runtime.handle_input(f"/hack {code} {problem}"))
            fake_start.assert_called_once()
        self.assertTrue(self.runtime.policy.check_read(code).allowed)
        self.assertTrue(self.runtime.policy.check_read(problem).allowed)

    def test_read_tool_cannot_read_project_env(self):
        env_file = ROOT / ".env"
        if not env_file.is_file():
            self.skipTest("项目根目录没有 .env")
        result = self.runtime.registry.execute("read", {"file_path": str(env_file)})
        self.assertIn("安全策略", result)
        self.assertNotIn("sk-", result)


if __name__ == "__main__":
    unittest.main()
