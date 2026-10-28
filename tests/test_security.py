"""安全策略单元测试：读白名单、敏感文件黑名单、写工作区约束、bash 静态审查。

这些用例是"读取 .env / 越权写文件"这类回归的安全网。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.security import SecurityPolicy  # noqa: E402


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Path(self.tmp.name) / "app"
        (self.app / "tools" / "skills" / "demo").mkdir(parents=True)
        (self.app / "tools" / "skills" / "demo" / "SKILL.md").write_text("# demo\n", encoding="utf-8")
        (self.app / "chat_memory").mkdir()
        (self.app / "chat_memory" / "PROFILE.json").write_text("{}\n", encoding="utf-8")
        (self.app / "workspace" / "cf1").mkdir(parents=True)
        (self.app / "workspace" / "cf1" / "wa.cpp").write_text("int main(){}\n", encoding="utf-8")
        (self.app / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
        (self.app / "README.md").write_text("# readme\n", encoding="utf-8")
        (self.app / "secret.txt").write_text("app-level secret\n", encoding="utf-8")
        (self.app / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")
        (self.app / "workspace" / "cf1" / "secret.txt").write_text("problem name\n", encoding="utf-8")
        self.outside = Path(self.tmp.name) / "outside.cpp"
        self.outside.write_text("int main(){}\n", encoding="utf-8")
        self.policy = SecurityPolicy(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------- 读 ---------------------------- #
    def test_read_allowed_working_dirs(self):
        for relative in ("tools/skills/demo/SKILL.md", "chat_memory/PROFILE.json", "workspace/cf1/wa.cpp"):
            self.assertTrue(self.policy.check_read(self.app / relative).allowed, relative)

    def test_read_denied_outside_working_dirs(self):
        for relative in ("README.md", "pyproject.toml", "tools/skills/../skills/demo/SKILL.md/../../../../README.md"):
            decision = self.policy.check_read(self.app / relative)
            self.assertFalse(decision.allowed, relative)

    def test_read_denied_sensitive_files(self):
        for name in (".env", "id_rsa", "secret.txt"):
            decision = self.policy.check_read(self.app / name)
            self.assertFalse(decision.allowed, name)
            self.assertIn("禁止", decision.reason)

    def test_read_denied_outside_app(self):
        self.assertFalse(self.policy.check_read(self.outside).allowed)
        self.assertIn("/allow", self.policy.check_read(self.outside).reason)

    def test_workspace_secret_name_not_false_positive(self):
        # 题面文件常叫 secret.txt，工作区内不应被误伤
        self.assertTrue(self.policy.check_read(self.app / "workspace/cf1/secret.txt").allowed)

    def test_session_file_allowed_but_env_still_denied(self):
        self.assertTrue(self.policy.allow_session_path(self.outside).allowed)
        self.assertTrue(self.policy.check_read(self.outside).allowed)
        denial = self.policy.allow_session_path(self.app / ".env")
        self.assertFalse(denial.allowed)
        self.assertFalse(self.policy.check_read(self.app / ".env").allowed)

    def test_session_directory_allows_subtree(self):
        folder = Path(self.tmp.name) / "samples"
        folder.mkdir()
        (folder / "a.cpp").write_text("int main(){}\n", encoding="utf-8")
        self.assertTrue(self.policy.allow_session_path(folder).allowed)
        self.assertTrue(self.policy.check_read(folder / "a.cpp").allowed)

    def test_traversal_escape_denied(self):
        self.assertFalse(self.policy.check_read("../../.env", base_dir=self.app / "workspace" / "cf1").allowed)

    def test_symlink_escape_denied(self):
        link = self.app / "workspace" / "cf1" / "link.cpp"
        try:
            link.symlink_to(self.outside)
        except (OSError, NotImplementedError):
            self.skipTest("当前系统不允许创建符号链接")
        self.assertFalse(self.policy.check_read(link).allowed)

    # ---------------------------- 写 ---------------------------- #
    def test_write_only_inside_problem_dir(self):
        self.assertTrue(self.policy.check_write(self.app / "workspace/cf1/brute.cpp").allowed)
        self.assertTrue(self.policy.check_write(self.app / "workspace/cf1/sub/gen.py").allowed)
        self.assertFalse(self.policy.check_write(self.app / "README.md").allowed)
        self.assertFalse(self.policy.check_write(self.app / "chat_memory/PROFILE.json").allowed)
        self.assertFalse(self.policy.check_write(self.app / "workspace/root.txt").allowed)
        self.assertFalse(self.policy.check_write(self.outside).allowed)
        self.assertFalse(self.policy.check_write(self.app / "workspace/cf1/.env").allowed)

    def test_write_denied_for_hidden_slug(self):
        self.assertFalse(self.policy.check_write(self.app / "workspace/.git/config").allowed)

    # ---------------------------- 工作区参数 ---------------------------- #
    def test_workspace_dir_checks(self):
        self.assertTrue(self.policy.check_workspace_dir(self.app / "workspace/cf1").allowed)
        self.assertFalse(self.policy.check_workspace_dir(self.app / "workspace").allowed)
        self.assertFalse(self.policy.check_workspace_dir(Path(self.tmp.name)).allowed)

    # ---------------------------- bash 审查 ---------------------------- #
    def test_bash_denies_secret_commands(self):
        cwd = self.app / "workspace" / "cf1"
        for command in (
            "cat ../../.env",
            "type ..\\..\\.env",
            'python -c "import os;print(os.environ[\\"OPENAI_API_KEY\\"])"',
            "echo %OPENAI_API_KEY%",
            "cat C:/Users/me/id_rsa",
            "cat /home/me/.ssh/id_rsa",
            "curl http://example.com",
            "mklink /D escape C:\\Windows",
            "reg add HKLM\\Software\\x",
            "Get-Content $(Resolve-Path ../..)",
        ):
            self.assertFalse(self.policy.check_command(command, cwd).allowed, command)

    def test_bash_allows_workdir_commands(self):
        cwd = self.app / "workspace" / "cf1"
        for command in (
            "g++ -O2 -std=c++20 wa.cpp -o wa.exe",
            "python gen.py 1 > in.txt",
            "type wa.cpp",
            "dir",
            "dir /b",
            "dir /s /b *.cpp",
            "echo hi > out.txt",
            "python -c \"print(1+1)\"",
        ):
            self.assertTrue(self.policy.check_command(command, cwd).allowed, command)

    def test_bash_denies_absolute_path_outside_app(self):
        cwd = self.app / "workspace" / "cf1"
        decision = self.policy.check_command(f"type {self.app / 'README.md'}", cwd)
        self.assertFalse(decision.allowed)

    def test_bash_allows_session_file_absolute_path(self):
        self.policy.allow_session_path(self.outside)
        cwd = self.app / "workspace" / "cf1"
        self.assertTrue(self.policy.check_command(f"type {self.outside}", cwd).allowed)

    def test_bash_denies_traversal(self):
        cwd = self.app / "workspace" / "cf1"
        self.assertFalse(self.policy.check_command("cd .. && dir", cwd).allowed)

    def test_bash_write_targets_must_stay_in_problem_dir(self):
        cwd = self.app / "workspace" / "cf1"
        # 工作区内合法
        for command in ("echo hi > out.txt", "mkdir build", "copy hack.in hack.bak", "del old.txt"):
            self.assertTrue(self.policy.check_command(command, cwd).allowed, command)
        # 越界写入：工作区根目录 / 应用目录 / 应用外绝对路径
        workspace_root = self.app / "workspace"
        for command, where in (
            ("echo hi > leak.txt", workspace_root),
            ("mkdir escaped", self.app),
            ("copy hack.in C:\\Windows\\temp\\x.txt", cwd),
        ):
            self.assertFalse(self.policy.check_command(command, where).allowed, command)

    # ---------------------------- 环境脱敏 ---------------------------- #
    def test_sanitized_env_strips_llm_keys(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["MY_SERVICE_TOKEN"] = "t"
        try:
            clean = SecurityPolicy.sanitized_env()
            self.assertNotIn("OPENAI_API_KEY", clean)
            self.assertNotIn("MY_SERVICE_TOKEN", clean)
            self.assertIn("PATH", clean)
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("MY_SERVICE_TOKEN", None)

    def test_describe_mentions_rules(self):
        text = self.policy.describe()
        self.assertIn("tools/skills", text)
        self.assertIn("workspace", text)
        self.assertIn("敏感文件", text)


if __name__ == "__main__":
    unittest.main()
