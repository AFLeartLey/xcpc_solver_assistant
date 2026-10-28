"""对拍引擎 / 反例最小化 / 造数据脚本的端到端测试（需要本机 g++ 与 python）。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.builtins.stress_runner import (  # noqa: E402
    compile_cpp,
    find_compiler,
    normalize_output,
    run_binary,
    run_stress_test,
    stress_test,
)
from core.security import SecurityPolicy  # noqa: E402

CASES = ROOT / "evals" / "cases"
SKILLS = ROOT / "tools" / "skills"
HAS_CXX = find_compiler() is not None


class NormalizeTest(unittest.TestCase):
    def test_ignore_trailing_space_and_blank_lines(self):
        self.assertEqual(normalize_output("1 2 \n3\n\n"), "1 2\n3")
        self.assertEqual(normalize_output("1 2\r\n3\r\n"), "1 2\n3")
        self.assertEqual(normalize_output(""), "")


@unittest.skipUnless(HAS_CXX, "需要 g++ 才能测试编译与对拍")
class StressRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "app"
        self.ws = self.root / "workspace" / "case"
        self.ws.mkdir(parents=True)
        self.policy = SecurityPolicy(self.root)
        for name in ("wa.cpp", "ac.cpp", "gen.py"):
            shutil.copy2(CASES / "case_01_multitest_reset" / name, self.ws / name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_compile_error_reported(self):
        bad = self.ws / "bad.cpp"
        bad.write_text("int main(){ this is not c++ }", encoding="utf-8")
        result = run_stress_test(
            str(self.ws), "bad.cpp", "ac.cpp", "gen.py", iterations=1, policy=self.policy
        )
        self.assertIn("COMPILE_ERROR", result)
        self.assertIn("stderr", result)

    def test_gen_failure_reported(self):
        gen = self.ws / "empty_gen.py"
        gen.write_text("pass\n", encoding="utf-8")
        result = stress_test(self.ws, "wa.cpp", "ac.cpp", "empty_gen.py", iterations=1, policy=self.policy)
        self.assertEqual(result["status"], "GEN_FAILED")

    def test_all_passed(self):
        result = stress_test(
            self.ws, "ac.cpp", "ac.cpp", "gen.py", iterations=5, time_limit=1.0, policy=self.policy
        )
        self.assertEqual(result["status"], "ALL_PASSED", result)
        self.assertEqual(result["iterations"], 5)

    def test_hack_success_and_artifacts(self):
        result = stress_test(
            self.ws, "wa.cpp", "ac.cpp", "gen.py", iterations=60, time_limit=1.0, policy=self.policy
        )
        self.assertEqual(result["status"], "HACK_SUCCESS", result)
        self.assertIn(result["verdict"], {"WA", "RE", "TLE", "OLE"})
        for key in ("in", "ans", "out"):
            self.assertTrue(Path(result["files"][key]).is_file())

        # 反例必须稳定复现（二次验证）
        hack_in = Path(result["files"]["in"]).read_text(encoding="utf-8", errors="replace")
        exe = ".exe" if sys.platform.startswith("win") else ""
        ac = run_binary(self.ws / "build" / f"brute{exe}", hack_in, self.ws, 3.0)
        wa = run_binary(self.ws / "build" / f"target{exe}", hack_in, self.ws, 3.0)
        self.assertNotEqual(normalize_output(ac["stdout"]), normalize_output(wa["stdout"]))

    def test_timeout_kills_process(self):
        loop = self.ws / "loop.cpp"
        loop.write_text("#include <cstdio>\nint main(){for(;;);return 0;}", encoding="utf-8")
        exe = self.ws / ("loop.exe" if sys.platform.startswith("win") else "loop")
        ok, log = compile_cpp(loop, exe)
        self.assertTrue(ok, log)
        result = run_binary(exe, "", self.ws, 0.5)
        self.assertTrue(result["timed_out"])

    def test_spj_accepts_divergent_output(self):
        # 多解题：输出与标程不同但 SPJ 判定合法 -> 不应该判 WA
        (self.ws / "spj_ok.cpp").write_text("#include <cstdio>\nint main(){return 0;}\n", encoding="utf-8")
        result = stress_test(
            self.ws,
            "wa.cpp",
            "ac.cpp",
            "gen.py",
            spj_cpp="spj_ok.cpp",
            iterations=3,
            time_limit=1.0,
            policy=self.policy,
        )
        self.assertEqual(result["status"], "ALL_PASSED", result)

    def test_spj_rejects_invalid_output(self):
        (self.ws / "spj_bad.cpp").write_text("#include <cstdio>\nint main(){return 1;}\n", encoding="utf-8")
        result = stress_test(
            self.ws,
            "wa.cpp",
            "ac.cpp",
            "gen.py",
            spj_cpp="spj_bad.cpp",
            iterations=2,
            time_limit=1.0,
            policy=self.policy,
        )
        self.assertEqual(result["status"], "HACK_SUCCESS", result)
        self.assertEqual(result["verdict"], "WA")

    def test_tool_entry_returns_json_string(self):
        payload = json.loads(
            run_stress_test(
                str(self.ws), "ac.cpp", "ac.cpp", "gen.py", iterations=2, time_limit=1.0, policy=self.policy
            )
        )
        self.assertEqual(payload["status"], "ALL_PASSED")

    def test_workspace_outside_app_is_denied(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        for name in ("wa.cpp", "ac.cpp", "gen.py"):
            shutil.copy2(CASES / "case_01_multitest_reset" / name, outside / name)
        result = stress_test(outside, "wa.cpp", "ac.cpp", "gen.py", iterations=1, policy=self.policy)
        self.assertEqual(result["status"], "POLICY_DENIED", result)
        self.assertEqual(result["stage"], "workspace")

    def test_source_outside_app_is_denied(self):
        secret = Path(self.tmp.name) / "outside.cpp"
        shutil.copy2(CASES / "case_01_multitest_reset" / "wa.cpp", secret)
        result = stress_test(
            self.ws, str(secret), "ac.cpp", "gen.py", iterations=1, policy=self.policy
        )
        self.assertEqual(result["status"], "POLICY_DENIED", result)
        self.assertEqual(result["stage"], "target")


class DeltaReduceTest(unittest.TestCase):
    def test_reduces_input_while_bug_persists(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            work = Path(tmp.name)
            original = work / "hack.in"
            original.write_text("9 9 9\n1 2 3 4 5\n100 200\n7\n", encoding="utf-8")
            checker = work / "check.py"
            checker.write_text(
                "import sys\n"
                "text = open(sys.argv[1], encoding='utf-8').read()\n"
                "sys.exit(0 if '5' in text.split() else 1)\n",
                encoding="utf-8",
            )
            out = work / "min.in"
            script = SKILLS / "stress-test-hack" / "scripts" / "delta_reduce.py"
            proc = subprocess.run(
                [sys.executable, str(script), "--input", str(original), "--output", str(out),
                 "--checker", f'"{sys.executable}" "{checker}"'],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            minimized = out.read_text(encoding="utf-8")
            self.assertIn("5", minimized.split())
            self.assertLess(len(minimized), len(original.read_text(encoding="utf-8")))
        finally:
            tmp.cleanup()

    def test_validator_prevents_broken_format(self):
        """没有 validator 时 ddmin 会删掉格式关键行；加上后必须保留合法性。"""
        tmp = tempfile.TemporaryDirectory()
        try:
            work = Path(tmp.name)
            original = work / "hack.in"
            original.write_text("9 9 9\n1 2 3 4 5\n100 200\n7\n", encoding="utf-8")
            checker = work / "check.py"
            checker.write_text(
                "import sys\n"
                "text = open(sys.argv[1], encoding='utf-8').read()\n"
                "sys.exit(0 if '5' in text.split() else 1)\n",
                encoding="utf-8",
            )
            validator = work / "validate.py"
            validator.write_text(
                "import sys\n"
                "tok = open(sys.argv[1], encoding='utf-8').read().split()\n"
                "sys.exit(0 if '9' in tok and '7' in tok else 1)\n",
                encoding="utf-8",
            )
            out = work / "min.in"
            script = SKILLS / "stress-test-hack" / "scripts" / "delta_reduce.py"
            proc = subprocess.run(
                [sys.executable, str(script), "--input", str(original), "--output", str(out),
                 "--checker", f'"{sys.executable}" "{checker}"',
                 "--validator", f'"{sys.executable}" "{validator}"'],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            tokens = out.read_text(encoding="utf-8").split()
            self.assertIn("5", tokens)
            self.assertIn("9", tokens)  # 结构关键 token 未被删掉
            self.assertIn("7", tokens)
            self.assertLess(len(tokens), len(original.read_text(encoding="utf-8").split()))
        finally:
            tmp.cleanup()


class TreeGenTest(unittest.TestCase):
    SCRIPT = SKILLS / "graph-tree-gen" / "scripts" / "tree_gen.py"

    def _run(self, *args: str) -> list[str]:
        proc = subprocess.run(
            [sys.executable, str(self.SCRIPT), *args], capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip().splitlines()

    def test_star_edges(self):
        lines = self._run("--mode", "star", "--n", "6")
        self.assertEqual(lines[0], "6")
        self.assertEqual(len(lines), 6)  # n + n-1 条边
        for line in lines[1:]:
            self.assertEqual(line.split()[0], "1")

    def test_chain_parents(self):
        lines = self._run("--mode", "chain", "--n", "5", "--format", "parents")
        self.assertEqual(lines[0], "5")
        self.assertEqual(lines[1].split(), ["1", "2", "3", "4"])

    def test_prufer_tree_is_connected_acyclic(self):
        lines = self._run("--mode", "prufer", "--n", "8", "--seed", "7")
        n = int(lines[0])
        edges = [tuple(map(int, line.split())) for line in lines[1:]]
        self.assertEqual(len(edges), n - 1)
        adjacency = {i: [] for i in range(1, n + 1)}
        for u, v in edges:
            adjacency[u].append(v)
            adjacency[v].append(u)
        seen, stack = {1}, [1]
        while stack:
            for nxt in adjacency[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        self.assertEqual(len(seen), n)  # 连通
        self.assertTrue(all(len(adjacency[i]) >= 1 for i in range(1, n + 1)))

    def test_values_extra_line(self):
        lines = self._run("--mode", "random", "--n", "4", "--seed", "1", "--values", "-3", "3")
        self.assertEqual(len(lines), 5)  # n + 权值行 + 3 条边
        self.assertEqual(len(lines[1].split()), 4)
        for token in lines[1].split():
            self.assertTrue(-3 <= int(token) <= 3)


if __name__ == "__main__":
    unittest.main()
