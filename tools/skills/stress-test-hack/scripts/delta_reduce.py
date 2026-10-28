#!/usr/bin/env python3
"""反例最小化（Delta Debugging），供 ``stress-test-hack`` 技能调用。

思路：把 ``hack.in`` 看成"行列表 + 整数 token"，用 ddmin 反复删行、再逐个缩小数字，
只要 checker 仍然报告"差异可复现"就接受更小的候选。

checker 约定：``<checker 命令...> <candidate_path>``，**退出码 0 表示 Bug 仍复现**。

可选 ``--validator``：``<validator 命令...> <candidate_path>``，退出码 0 表示候选输入
**仍然合法**。强烈建议提供——否则 ddmin 可能删掉格式关键行，缩出一个让程序读入失败
却发现"输出不同"的非法反例（假阳性）。

示例::

    python delta_reduce.py \
        --input workspace/cf1920c/hack.in \
        --output workspace/cf1920c/hack.min.in \
        --checker "python workspace/cf1920c/check.py" \
        --validator "python workspace/cf1920c/validate.py"

checker 示例（check.py）::

    import subprocess, sys
    candidate = open(sys.argv[1]).read()
    a = subprocess.run(["./build/brute"], input=candidate, capture_output=True, text=True)
    b = subprocess.run(["./build/target"], input=candidate, capture_output=True, text=True)
    norm = lambda s: "\\n".join(l.rstrip() for l in s.strip().splitlines())
    # 两边都必须正常退出，避免拿"崩溃 vs 正常"当反例
    sys.exit(0 if a.returncode == 0 and b.returncode == 0 and norm(a.stdout) != norm(b.stdout) else 1)

validator 示例（validate.py，读入并校验题面约束）::

    import sys
    tok = open(sys.argv[1]).read().split()
    n = int(tok[0])
    sys.exit(0 if 1 <= n <= 8 and len(tok) == n + 1 else 1)
"""

from __future__ import annotations

import argparse
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?![\w.])")


def run_command_on_file(text: str, command: list[str], timeout: float, workdir: Path) -> bool:
    """把候选输入写到临时文件，交给外部命令判定（退出码 0 视为通过）。"""
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".in", delete=False, encoding="utf-8", dir=workdir) as handle:
            handle.write(text)
            tmp_path = Path(handle.name)
        proc = subprocess.run(
            command + [str(tmp_path)],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def run_checker(text: str, checker: list[str], timeout: float, workdir: Path) -> bool:
    return run_command_on_file(text, checker, timeout, workdir)


def ddmin(lines: list[str], test: Callable[[list[str]], bool]) -> list[str]:
    """经典 ddmin：反复尝试删除 1/n, 2/n ... 的行块。"""
    data = list(lines)
    n = 2
    while len(data) >= 2:
        chunk_size = max(1, len(data) // n)
        reduced = False
        for start in range(0, len(data), chunk_size):
            candidate = data[:start] + data[start + chunk_size :]
            if not candidate or len(candidate) == len(data):
                continue
            if test(candidate):
                data = candidate
                n = max(n - 1, 2)
                reduced = True
                break
        if not reduced:
            if n >= len(data):
                break
            n = min(len(data), n * 2)
    return data


def shrink_numbers(text: str, test: Callable[[str], bool], max_rounds: int = 400) -> str:
    """逐个把整数替换成更小的候选值（0/1/-1/半值/±1），保持反例仍复现。"""
    for _ in range(max_rounds):
        changed = False
        for match in list(NUMBER_RE.finditer(text)):
            original = int(match.group())
            if abs(original) <= 1:
                continue
            candidates = {0, 1, -1, original // 2, original + (-1 if original > 0 else 1)}
            for candidate in sorted(candidates, key=abs):
                if abs(candidate) >= abs(original):
                    continue
                trial = text[: match.start()] + str(candidate) + text[match.end() :]
                if test(trial):
                    text = trial
                    changed = True
                    break
            if changed:
                break
        if not changed:
            break
    return text


def minimize(
    text: str,
    checker: list[str],
    timeout: float,
    workdir: Path,
    validator: list[str] | None = None,
) -> tuple[str, bool]:
    def test_text(candidate: str) -> bool:
        if validator and not run_command_on_file(candidate, validator, timeout, workdir):
            return False  # 非法输入直接拒绝，避免缩出格式损坏的假反例
        return run_checker(candidate, checker, timeout, workdir)

    if not test_text(text):
        return text, False

    lines = text.splitlines(keepends=True)
    if len(lines) > 1:
        def test_lines(candidate: list[str]) -> bool:
            return test_text("".join(candidate))

        lines = ddmin(lines, test_lines)
    current = "".join(lines)
    current = shrink_numbers(current, test_text)
    return current, True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="反例最小化（checker 退出码 0 表示 Bug 仍复现）")
    parser.add_argument("--input", required=True, help="原始反例文件")
    parser.add_argument("--output", required=True, help="最小化后的输出文件")
    parser.add_argument("--checker", required=True, help='检查命令，如 "python check.py"')
    parser.add_argument("--validator", default="", help='可选合法性校验命令，如 "python validate.py"')
    parser.add_argument("--timeout", type=float, default=10.0, help="单次 checker 超时秒数")
    parser.add_argument("--workdir", default=".", help="checker 工作目录（相对产物路径的基准）")
    args = parser.parse_args(argv)

    in_path = Path(args.input)
    if not in_path.is_file():
        print(f"ERROR: 输入文件不存在: {in_path}", file=sys.stderr)
        return 2
    text = in_path.read_text(encoding="utf-8", errors="replace")
    checker = shlex.split(args.checker)
    validator = shlex.split(args.validator) if args.validator else None
    if not checker:
        print("ERROR: --checker 为空", file=sys.stderr)
        return 2
    workdir = Path(args.workdir).resolve()

    minimized, reproducible = minimize(text, checker, args.timeout, workdir, validator)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(minimized, encoding="utf-8")

    if not reproducible:
        print("WARNING: 原始反例在当前 checker 下无法复现，已原样输出。", file=sys.stderr)
    print(
        f"原始 {len(text.splitlines())} 行 / {len(text)} 字符 -> "
        f"最小化后 {len(minimized.splitlines())} 行 / {len(minimized)} 字符"
    )
    print(f"输出: {out_path}")
    return 0 if reproducible else 1


if __name__ == "__main__":
    raise SystemExit(main())
