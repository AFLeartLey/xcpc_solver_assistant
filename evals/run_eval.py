#!/usr/bin/env python3
"""XCPC-Hack-Bench：最小完备自动化评测集。

默认是**确定性离线模式**：不调用大模型，直接用真实的 ``run_stress_test`` 引擎
（g++ 编译 + 造数据 + 对拍）跑 3 个黄金错题，验证：

* 能否捕获反例并二次复现（Hack Success）；
* 反例规模（Counterexample Size）与耗时（Iterations & Latency）；
* 画像闭环是否生效（Profile Updated）。

用法::

    uv run evals/run_eval.py                 # 离线引擎模式（无需 API Key）
    uv run evals/run_eval.py --iterations 500
    uv run evals/run_eval.py --agent         # 额外用真实 Agent 跑一遍（需要 API Key）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.profile import Profile  # noqa: E402
from core.security import SecurityPolicy  # noqa: E402
from tools.builtins.stress_runner import normalize_output, run_binary, stress_test  # noqa: E402

EVAL_DIR = ROOT / "evals"
CASES_DIR = EVAL_DIR / "cases"
EVAL_MEMORY = EVAL_DIR / ".eval_memory"
WORKSPACE = ROOT / "workspace" / "evals"
DELTA_REDUCE = ROOT / "tools" / "skills" / "stress-test-hack" / "scripts" / "delta_reduce.py"
EXE = ".exe" if os.name == "nt" else ""

# 最小化 checker：候选输入必须让 brute / target 都正常退出，且输出仍不同（退出码 0 = 仍复现）
CHECKER_TEMPLATE = '''"""eval 用最小化 checker：退出码 0 表示 bug 仍复现。"""
import subprocess
import sys
from pathlib import Path

WS = Path(__file__).resolve().parent
SUFFIX = ".exe" if sys.platform.startswith("win") else ""


def run(name, data):
    proc = subprocess.run(
        [str(WS / "build" / (name + SUFFIX))],
        input=data,
        capture_output=True,
        text=True,
        timeout=5,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout


def norm(text):
    return "\\n".join(line.rstrip() for line in text.strip().splitlines())


data = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
code_a, ans = run("brute", data)
code_b, out = run("target", data)
if code_a != 0 or code_b != 0:
    sys.exit(1)
sys.exit(0 if norm(ans) != norm(out) else 1)
'''

CASES = [
    {
        "case": "case_01_multitest_reset",
        "bug_id": "BUG_MULTITEST_DIRTY",
        "topic": "data_structures",
        "expect": "多测未清空：邻接表 head/ecnt 组间残留污染下一棵树",
    },
    {
        "case": "case_02_int_overflow",
        "bug_id": "BUG_INT_OVERFLOW",
        "topic": "math",
        "expect": "int 累加溢出：极值数据下符号翻转",
    },
    {
        "case": "case_03_greedy_counterexample",
        "bug_id": "BUG_GREEDY_FALSE",
        "topic": "greedy",
        "expect": "性价比贪心假做法：小规模权值组合下失效",
    },
]


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _force_utf8_stdio() -> None:
    """Windows 控制台默认 GBK，统一改成 UTF-8 避免中文报告乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def prepare_workspace(case_name: str) -> Path:
    source = CASES_DIR / case_name
    target = WORKSPACE / case_name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for filename in ("wa.cpp", "ac.cpp", "gen.py", "validate.py"):
        shutil.copy2(source / filename, target / filename)
    return target


def counterexample_stats(text: str) -> dict:
    tokens = text.split()
    lines = text.strip().splitlines()
    return {
        "tokens": len(tokens),
        "lines": len(lines),
        "head": tokens[0] if tokens else "-",
        "chars": len(text),
    }


def _one_line(text: str, limit: int) -> str:
    return " ".join((text or "").replace("\r", " ").replace("\n", " ").split())[:limit]


def verify_counterexample(ws: Path, input_text: str) -> tuple[bool, str]:
    """二次验证：直接用编译产物重跑，确认差异稳定复现（防止偶发/假阳性）。"""
    brute = ws / "build" / f"brute{EXE}"
    target = ws / "build" / f"target{EXE}"
    if not brute.is_file() or not target.is_file():
        return False, "未找到编译产物"
    ans = run_binary(brute, input_text, ws, 3.0)
    out = run_binary(target, input_text, ws, 3.0)
    if ans["timed_out"] or out["timed_out"]:
        return False, "复现超时"
    same = normalize_output(ans["stdout"]) == normalize_output(out["stdout"])
    detail = f"ans={_one_line(ans['stdout'], 40)!r} out={_one_line(out['stdout'], 40)!r}"
    return (not same), detail


def minimize_counterexample(ws: Path, original: str) -> str:
    """调用 delta_reduce.py 把反例缩到最小，并同步刷新 hack.ans/hack.out。"""
    checker = ws / "check.py"
    checker.write_text(CHECKER_TEMPLATE, encoding="utf-8")
    validator = ws / "validate.py"
    raw = ws / "hack.raw.in"
    raw.write_text(original, encoding="utf-8")
    target = ws / "hack.in"
    command = [
        sys.executable,
        str(DELTA_REDUCE),
        "--input",
        str(raw),
        "--output",
        str(target),
        "--checker",
        f'"{sys.executable}" "{checker}"',
        "--workdir",
        str(ws),
    ]
    if validator.is_file():
        command += ["--validator", f'"{sys.executable}" "{validator}"']
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        return original
    if proc.returncode != 0 or not target.is_file():
        return original
    minimized = target.read_text(encoding="utf-8", errors="replace")
    if len(minimized.split()) > len(original.split()) or not minimized.strip():
        return original
    # 同步刷新期望/实际输出，保证 hack.ans / hack.out 与最小反例一致
    ans = run_binary(ws / "build" / f"brute{EXE}", minimized, ws, 3.0)
    out = run_binary(ws / "build" / f"target{EXE}", minimized, ws, 3.0)
    (ws / "hack.ans").write_text(ans["stdout"], encoding="utf-8")
    (ws / "hack.out").write_text(out["stdout"], encoding="utf-8")
    return minimized


# --------------------------------------------------------------------------- #
# 单个 case
# --------------------------------------------------------------------------- #
def run_case(case: dict, profile: Profile, iterations: int, time_limit: float, minimize: bool = False) -> dict:
    name = case["case"]
    ws = prepare_workspace(name)
    before = profile.record_count(case["bug_id"])

    started = time.perf_counter()
    result = stress_test(
        ws,
        target_cpp="wa.cpp",
        brute_cpp="ac.cpp",
        gen_py="gen.py",
        iterations=iterations,
        time_limit=time_limit,
        policy=SecurityPolicy(ROOT),
    )
    latency = time.perf_counter() - started

    row = {
        "case": name,
        "expect": case["expect"],
        "status": result.get("status"),
        "verdict": result.get("verdict") or result.get("status"),
        "iterations": result.get("iterations_run") or result.get("iterations") or 0,
        "latency": latency,
        "hack_success": False,
        "counterexample": {},
        "raw_tokens": 0,
        "minimized": False,
        "profile_before": before,
        "profile_after": before,
        "profile_updated": False,
        "main_tokens": 0,
        "detail": "",
        "files": result.get("files", {}),
    }

    if result.get("status") != "HACK_SUCCESS":
        row["detail"] = str(result.get("message") or "未捕获反例")[:160]
        return row

    hack_in = Path(result["files"]["in"])
    text = hack_in.read_text(encoding="utf-8", errors="replace")
    stats = counterexample_stats(text)
    row["raw_tokens"] = stats["tokens"]

    if minimize:
        text = minimize_counterexample(ws, text)
        stats = counterexample_stats(text)
        row["minimized"] = stats["tokens"] < row["raw_tokens"]

    verified, detail = verify_counterexample(ws, text)
    row.update(
        {
            "hack_success": verified,
            "counterexample": stats,
            "detail": detail if verified else f"二次验证失败: {detail}",
        }
    )

    if verified:
        entry = profile.record_bug_occurrence(
            case["bug_id"],
            name,
            f"{row['verdict']} @iter {result.get('iteration')}: {detail}",
            case["topic"],
            mastery_delta=-3,
            solution_status="hacked",
        )
        row["profile_after"] = int(entry.get("count") or 0)
        row["profile_updated"] = row["profile_after"] > before
    return row


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def render_markdown(rows: list[dict], iterations: int, time_limit: float, elapsed: float, minimize: bool) -> str:
    total = len(rows)
    hacked = sum(1 for r in rows if r["hack_success"])
    lines = [
        "# XCPC-Claw 评测报告（XCPC-Hack-Bench）",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 运行模式：确定性离线引擎（真实 g++ 编译 + 造数据 + 对拍，不调用大模型）",
        f"- 反例最小化：{'开启（delta_reduce.py ddmin）' if minimize else '关闭（--minimize 可开启）'}",
        f"- 环境：Python {sys.version.split()[0]} / 平台 {sys.platform} / 对拍上限 {iterations} 组 / 单组超时 {time_limit}s",
        "",
        "| Case | 期望捕获的根因 | Hack Success | Verdict | Iterations | Latency (s) | 反例 tokens | 反例行数 | Main Tokens | Profile Updated | 备注 |",
        "| :--- | :--- | :---: | :---: | ---: | ---: | :---: | ---: | ---: | :---: | :--- |",
    ]
    for row in rows:
        cex = row["counterexample"]
        raw = row.get("raw_tokens") or 0
        final = cex.get("tokens", 0) if cex else 0
        if not cex:
            cex_text = "-"
        elif row.get("minimized"):
            cex_text = f"{raw}→{final}"
        else:
            cex_text = str(final)
        lines.append(
            "| {case} | {expect} | {ok} | {verdict} | {iters} | {lat:.2f} | {cex} | {lines} | {tokens} | {profile} | {detail} |".format(
                case=row["case"],
                expect=row["expect"],
                ok="✅" if row["hack_success"] else "❌",
                verdict=row["verdict"],
                iters=row["iterations"],
                lat=row["latency"],
                cex=cex_text,
                lines=cex.get("lines", "-") if cex else "-",
                tokens=row["main_tokens"],
                profile="Yes" if row["profile_updated"] else "No",
                detail=(row["detail"] or "").replace("|", "/")[:80],
            )
        )
    avg_latency = sum(r["latency"] for r in rows) / total if total else 0
    avg_tokens = sum(r["counterexample"].get("tokens", 0) for r in rows if r["counterexample"]) / max(1, hacked)
    raw_avg = sum(r.get("raw_tokens") or 0 for r in rows if r["counterexample"]) / max(1, hacked)
    size_line = (
        f"- **平均反例规模**：{raw_avg:.1f} → {avg_tokens:.1f} tokens（越小越适合人类手算）"
        if minimize
        else f"- **平均反例规模**：{avg_tokens:.1f} tokens（生成器一开始就只造极小数据，便于人类手算）"
    )
    lines += [
        "",
        "## 汇总",
        "",
        f"- **反例捕获成功率 (HSR)**：{hacked}/{total} = {hacked / total:.0%}" if total else "-",
        f"- **平均单题耗时**：{avg_latency:.2f}s（含 g++ 编译，整体 {elapsed:.2f}s）",
        size_line,
        f"- **画像闭环**：{sum(1 for r in rows if r['profile_updated'])}/{total} 个 case 成功写入 Bug 指纹",
        "- **Main Context Tokens**：离线引擎模式不经过主 Agent，故为 0；"
        "Subagent 隔离收益请在 `uv run main.py` 的 `/hack` 流程中观察（或使用 `--agent`）。",
        "",
        "## 复现方式",
        "",
        "```bash",
        "uv run evals/run_eval.py                       # 离线引擎模式",
        "uv run evals/run_eval.py --minimize             # 额外执行反例最小化",
        "uv run evals/run_eval.py --agent                # 额外跑真实 Agent（需 API Key）",
        "```",
        "",
        "## 说明",
        "",
        "- 每个 case 的 `wa.cpp` 均为真实可编译的错解，`ac.cpp` 作为对拍标程，`gen.py` 按 seed 分阶段造数据；",
        "- `Hack Success` 要求反例经**二次重跑**仍能稳定复现差异，避免把偶发/假阳性算作成功；",
        "- 画像更新写入隔离目录 `evals/.eval_memory/PROFILE.json`，不会污染真实选手画像；",
        "- `--minimize` 会调用与 Agent 相同的 `delta_reduce.py`（ddmin + 数值收缩 + 合法性 validator）。"
        "注意：通用文本 ddmin 无法重排多测结构（改 `T` 必须同时增删整组数据），"
        "因此多测类反例通常只能收缩数值；真实 Agent 会按题面写更强的结构化 reducer。",
        "",
    ]
    return "\n".join(lines)


def print_console(rows: list[dict]) -> None:
    cols = ["case", "verdict", "iterations", "latency", "counterexample", "hack_success", "profile_updated"]
    widths = {c: len(c) for c in cols}
    table = []
    for row in rows:
        cex = row["counterexample"]
        line = {
            "case": row["case"],
            "verdict": row["verdict"],
            "iterations": str(row["iterations"]),
            "latency": f"{row['latency']:.2f}s",
            "counterexample": f"{cex.get('tokens', '-')}/{cex.get('lines', '-')}",
            "hack_success": "YES" if row["hack_success"] else "NO",
            "profile_updated": "YES" if row["profile_updated"] else "NO",
        }
        table.append(line)
        for c in cols:
            widths[c] = max(widths[c], len(line[c]))
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("  ".join("-" * widths[c] for c in cols))
    for line in table:
        print("  ".join(line[c].ljust(widths[c]) for c in cols))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="XCPC-Hack-Bench 自动化评测")
    parser.add_argument("--iterations", type=int, default=300, help="每个 case 的对拍组数上限")
    parser.add_argument("--time-limit", type=float, default=1.0, help="单组数据运行超时（秒）")
    parser.add_argument("--report", default=str(EVAL_DIR / "eval_report.md"), help="报告输出路径")
    parser.add_argument("--agent", action="store_true", help="额外用真实 Agent 跑一遍（需要 OPENAI_API_KEY）")
    parser.add_argument("--minimize", action="store_true", help="对捕获到的反例执行 delta_reduce 最小化")
    parser.add_argument("--max-rounds", type=int, default=12, help="Agent 模式下每个 case 的最大 Goal 轮数")
    args = parser.parse_args(argv)

    if EVAL_MEMORY.exists():
        shutil.rmtree(EVAL_MEMORY)
    EVAL_MEMORY.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)

    profile = Profile(EVAL_MEMORY)
    rows = []
    started = time.perf_counter()
    print("=" * 72)
    print("XCPC-Claw 评测：确定性离线引擎模式（真实编译 + 对拍，不调用大模型）")
    print("=" * 72)
    for index, case in enumerate(CASES, 1):
        print(f"[{index}/{len(CASES)}] {case['case']} ...", flush=True)
        row = run_case(case, profile, args.iterations, args.time_limit, minimize=args.minimize)
        rows.append(row)
        marker = "HACK_SUCCESS" if row["hack_success"] else row["status"]
        print(
            f"    -> {marker} verdict={row['verdict']} iters={row['iterations']} "
            f"latency={row['latency']:.2f}s profile={'Yes' if row['profile_updated'] else 'No'}"
        )

    if args.agent:
        rows = _run_agent_mode(rows, args)

    elapsed = time.perf_counter() - started
    markdown = render_markdown(rows, args.iterations, args.time_limit, elapsed, args.minimize)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(markdown, encoding="utf-8")

    print()
    print_console(rows)
    hacked = sum(1 for r in rows if r["hack_success"])
    print()
    print(f"HSR: {hacked}/{len(rows)}；报告已写入 {report_path}")
    print(f"Profile 计数: " + json.dumps(_profile_snapshot(profile), ensure_ascii=False))
    return 0 if hacked == len(rows) else 1


def _profile_snapshot(profile: Profile) -> dict:
    return {bug.get("id"): int(bug.get("count") or 0) for bug in profile.data.get("bug_fingerprints", [])}


def _run_agent_mode(rows: list[dict], args) -> list[dict]:
    """可选：用真实 Agent 跑 /hack 流程，采集主上下文 token 与画像更新情况。"""
    try:
        from core.llm import has_api_key
        from core.goal import run_goal
        from main import Runtime
    except Exception as exc:  # noqa: BLE001
        print(f"[agent] 初始化失败: {exc}")
        return rows
    if not has_api_key():
        print("[agent] 未配置 OPENAI_API_KEY，跳过 Agent 模式（保留离线引擎结果）。")
        return rows

    runtime = Runtime.build(memory_dir=EVAL_MEMORY, stream=False, max_rounds=args.max_rounds)
    for row in rows:
        case_dir = WORKSPACE / row["case"]
        problem_path = CASES_DIR / row["case"] / "problem.md"
        # 评测进程是可信宿主：把本题的选手代码与题面显式加入本次会话只读白名单
        runtime.policy.allow_session_path(case_dir / "wa.cpp")
        runtime.policy.allow_session_path(problem_path)
        objective = (
            f"[eval /hack] 代码: {case_dir / 'wa.cpp'}；题面: {problem_path}；"
            f"沙箱目录: {case_dir}。请按 /hack 闭环推进：读题 -> load_skill -> spawn_subagent 对拍 -> "
            f"按 L{runtime.profile.hint_level} 点拨 -> update_profile -> goal_complete。"
        )
        before_tokens = runtime.memory.usage_totals["total_tokens"]
        before_count = runtime.profile.record_count(next(c["bug_id"] for c in CASES if c["case"] == row["case"]))
        try:
            runtime.goal.start(objective)
            run_goal(runtime.session, runtime.goal)
        except Exception as exc:  # noqa: BLE001
            print(f"[agent] {row['case']} 运行失败: {exc}")
            continue
        row["main_tokens"] = runtime.memory.usage_totals["total_tokens"] - before_tokens
        bug_id = next(c["bug_id"] for c in CASES if c["case"] == row["case"])
        row["profile_updated"] = runtime.profile.record_count(bug_id) > before_count
        row["detail"] = (row["detail"] + " | agent 模式")[:120]
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
