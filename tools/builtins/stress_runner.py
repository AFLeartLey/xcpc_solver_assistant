"""``run_stress_test``：确定性高速对拍加速器（编译 + 造数据 + 比对 + 反例固化）。

设计要点：

* **不依赖 shell 脚本**：LLM 只需给出 3~4 个文件，循环、超时、比对全在 Python 内完成；
* **跨平台**：Windows 生成 ``.exe``，POSIX 直接可执行；超时都强杀整个进程树；
* **遇到差异立即停止**，把反例固化为 ``hack.in / hack.ans / hack.out`` 并返回结构化 JSON；
* **规范化比对**：行末空格与文末多余换行按 XCPC 惯例忽略。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from core.security import SecurityPolicy

from .bash_tool import kill_process_tree

ITERATION_LIMIT = (1, 5000)
TIME_LIMIT_RANGE = (0.1, 20.0)
OUTPUT_CAP = 8 * 1024 * 1024
SNIPPET = 1200
EXE_SUFFIX = ".exe" if os.name == "nt" else ""
PROJECT_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def find_compiler() -> str | None:
    for candidate in (os.environ.get("CXX"), "g++", "clang++", "c++"):
        if not candidate:
            continue
        found = shutil.which(candidate)
        if found:
            return found
    # Windows 上常见的 MinGW 安装位置兜底
    if os.name == "nt":
        for pattern in (r"C:\mingw64\bin\g++.exe", r"C:\ProgramData\mingw64\mingw64\bin\g++.exe", r"C:\msys64\mingw64\bin\g++.exe"):
            if Path(pattern).is_file():
                return pattern
    return None


def normalize_output(text: str) -> str:
    """XCPC 判题惯例：忽略行末空白与文末多余空行。"""
    return "\n".join(line.rstrip() for line in (text or "").strip().splitlines())


def compile_cpp(
    src: Path,
    out: Path,
    *,
    cxx: str | None = None,
    timeout: float = 120.0,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    compiler = cxx or find_compiler()
    if not compiler:
        return False, "未找到 C++ 编译器：请安装 g++（MinGW-w64 / GCC / clang++）或设置环境变量 CXX。"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [compiler, "-O2", "-std=c++20", str(src), "-o", str(out)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        return False, f"编译超时（>{timeout:.0f}s）：{src}"
    except OSError as exc:
        return False, f"无法启动编译器 {compiler}: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "编译失败且无输出").strip()
    if not out.is_file():
        return False, "编译器未产出可执行文件"
    return True, (proc.stderr or "").strip()


def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        }
    return {"start_new_session": True}


def _limit_resources(memory_mb: int) -> Callable[[], None] | None:
    """POSIX 下用 rlimit 限制单进程地址空间与 CPU 时间；Windows 走超时熔断。"""
    if os.name == "nt" or memory_mb <= 0:
        return None

    def _apply() -> None:  # pragma: no cover - 仅在 POSIX 生效
        import resource

        limit = memory_mb * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ValueError, OSError):
            pass

    return _apply


def run_binary(
    exe: Path,
    input_data: str,
    cwd: Path,
    timeout: float,
    *,
    output_cap: int = OUTPUT_CAP,
    memory_mb: int = 1024,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """运行被测程序一次，返回 stdout/stderr/退出码/超时标记。"""
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        try:
            proc = subprocess.Popen(
                [str(exe)],
                cwd=str(cwd),
                stdin=subprocess.PIPE,
                stdout=fo,
                stderr=fe,
                env=env,
                preexec_fn=_limit_resources(memory_mb),
                **_popen_kwargs(),
            )
        except OSError as exc:
            return {"stdout": "", "stderr": f"无法启动程序 {exe}: {exc}", "exit_code": -1, "timed_out": False, "output_overflow": False, "stderr_head": ""}
        timed_out = False
        try:
            proc.communicate(input=(input_data or "").encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_tree(proc)
            try:
                proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                pass
        out_size = fo.tell()
        fo.seek(0)
        raw_out = fo.read(output_cap)
        fe.seek(0)
        raw_err = fe.read(64 * 1024)
        return {
            "stdout": raw_out.decode("utf-8", errors="replace"),
            "stderr": raw_err.decode("utf-8", errors="replace"),
            "exit_code": int(proc.returncode if proc.returncode is not None else -1),
            "timed_out": timed_out,
            "output_overflow": out_size > output_cap,
            "output_size": out_size,
        }


def _clip(text: str, limit: int = SNIPPET) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n...（已截断，原始 {len(text)} 字符）"


def _resolve(ws: Path, raw: str) -> Path:
    path = Path(str(raw)).expanduser()
    if path.is_absolute():
        return path
    candidate = ws / path
    return candidate if candidate.exists() else Path.cwd() / path


# --------------------------------------------------------------------------- #
# 核心对拍
# --------------------------------------------------------------------------- #
def stress_test(
    workspace_dir: str | Path,
    target_cpp: str,
    brute_cpp: str,
    gen_py: str,
    spj_cpp: str = "",
    iterations: int = 200,
    time_limit: float | None = None,
    *,
    progress: Callable[[int, int], None] | None = None,
    policy: SecurityPolicy | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    policy = policy or SecurityPolicy(PROJECT_ROOT)
    ws = policy.resolve(workspace_dir)
    decision = policy.check_workspace_dir(ws)
    if not decision.allowed:
        return {"status": "POLICY_DENIED", "stage": "workspace", "message": decision.reason, "workspace": str(ws)}
    ws.mkdir(parents=True, exist_ok=True)

    try:
        iterations = int(iterations or 200)
    except (TypeError, ValueError):
        iterations = 200
    iterations = max(ITERATION_LIMIT[0], min(ITERATION_LIMIT[1], iterations))
    if time_limit is None or float(time_limit) <= 0:
        try:
            time_limit = float(os.environ.get("XCPC_TIMEOUT_SEC", "2.0"))
        except ValueError:
            time_limit = 2.0
    try:
        time_limit = float(time_limit or 2.0)
    except (TypeError, ValueError):
        time_limit = 2.0
    time_limit = max(TIME_LIMIT_RANGE[0], min(TIME_LIMIT_RANGE[1], time_limit))

    sources = {
        "target": _resolve(ws, target_cpp),
        "brute": _resolve(ws, brute_cpp),
        "generator": _resolve(ws, gen_py),
    }
    for label, path in sources.items():
        if not path.is_file():
            return {
                "status": "COMPILE_ERROR",
                "stage": label,
                "message": f"{label} 源文件不存在: {path}",
                "files": {k: str(v) for k, v in sources.items()},
            }
        allowed = policy.check_read(path)
        if not allowed.allowed:
            return {
                "status": "POLICY_DENIED",
                "stage": label,
                "message": f"{label} 源文件读取被拒绝：{allowed.reason}",
                "files": {k: str(v) for k, v in sources.items()},
            }

    env = policy.sanitized_env()

    build_dir = ws / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    exes = {
        "target": build_dir / f"target{EXE_SUFFIX}",
        "brute": build_dir / f"brute{EXE_SUFFIX}",
    }
    warnings: list[str] = []
    sandbox_mode = (os.environ.get("XCPC_SANDBOX_MODE") or "local").strip().lower()
    if sandbox_mode == "docker":
        reason = "（宿主机未安装 docker）" if shutil.which("docker") is None else "（本版本仅实现进程级隔离）"
        warnings.append(f"XCPC_SANDBOX_MODE=docker：已自动降级为本地受限沙箱{reason}")
    for label in ("brute", "target"):
        ok, log = compile_cpp(sources[label], exes[label], env=env)
        if not ok:
            return {
                "status": "COMPILE_ERROR",
                "stage": label,
                "message": f"{label}.cpp 编译失败",
                "stderr": _clip(log, 3000),
                "files": {k: str(v) for k, v in sources.items()},
            }
        if log.strip():
            warnings.append(f"{label}: {_clip(log, 400)}")

    spj_exe: Path | None = None
    if str(spj_cpp or "").strip():
        spj_src = _resolve(ws, spj_cpp)
        if not spj_src.is_file():
            return {"status": "COMPILE_ERROR", "stage": "spj", "message": f"spj 源文件不存在: {spj_src}"}
        allowed = policy.check_read(spj_src)
        if not allowed.allowed:
            return {"status": "POLICY_DENIED", "stage": "spj", "message": f"spj 源文件读取被拒绝：{allowed.reason}"}
        spj_exe = build_dir / f"spj{EXE_SUFFIX}"
        ok, log = compile_cpp(spj_src, spj_exe, env=env)
        if not ok:
            return {"status": "COMPILE_ERROR", "stage": "spj", "message": "spj.cpp 编译失败", "stderr": _clip(log, 3000)}

    cur_in, cur_out, cur_ans = ws / "cur.in", ws / "cur.out", ws / "cur.ans"
    gen_cmd = [sys.executable, str(sources["generator"])]
    last_iteration = 0

    for iteration in range(1, iterations + 1):
        last_iteration = iteration
        if progress:
            progress(iteration, iterations)

        try:
            gen_result = subprocess.run(
                gen_cmd + [str(iteration)],
                cwd=str(ws),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=120,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "GEN_FAILED",
                "iteration": iteration,
                "message": "gen.py 运行超时（>120s）。请确保生成器是 O(N) 级别的快速随机生成。",
                "elapsed_sec": round(time.perf_counter() - started, 3),
            }
        if gen_result.returncode != 0:
            return {
                "status": "GEN_FAILED",
                "iteration": iteration,
                "message": "gen.py 执行失败（非 0 退出）。请检查生成器语法与参数（约定：`python gen.py <seed>` 输出到 stdout）。",
                "stderr": _clip(gen_result.stderr, 2000),
                "elapsed_sec": round(time.perf_counter() - started, 3),
            }
        input_data = gen_result.stdout
        if not input_data.strip():
            return {
                "status": "GEN_FAILED",
                "iteration": iteration,
                "message": "gen.py 没有向 stdout 输出数据。",
                "elapsed_sec": round(time.perf_counter() - started, 3),
            }
        cur_in.write_text(input_data, encoding="utf-8")

        brute = run_binary(exes["brute"], input_data, ws, max(3.0, time_limit * 3), env=env)
        if brute["timed_out"]:
            return {
                "status": "BRUTE_FAILED",
                "iteration": iteration,
                "message": "brute.cpp 超时。请缩小 gen.py 的数据规模（N<=6、值域<=10）后再对拍。",
                "input": _clip(input_data, 500),
                "elapsed_sec": round(time.perf_counter() - started, 3),
            }
        if brute["exit_code"] != 0:
            return {
                "status": "BRUTE_FAILED",
                "iteration": iteration,
                "message": "brute.cpp 非 0 退出（运行时错误）。请先修好暴力程序。",
                "input": _clip(input_data, 500),
                "stderr": _clip(brute["stderr"], 800),
                "elapsed_sec": round(time.perf_counter() - started, 3),
            }
        expected = brute["stdout"]

        target = run_binary(exes["target"], input_data, ws, time_limit, env=env)
        actual = target["stdout"]

        verdict: str | None = None
        if target["timed_out"]:
            verdict = "TLE"
        elif target["exit_code"] != 0:
            verdict = "RE"
        elif target["output_overflow"]:
            verdict = "OLE"
        elif spj_exe is not None:
            cur_out.write_text(actual, encoding="utf-8")
            cur_ans.write_text(expected, encoding="utf-8")
            spj = _run_spj(spj_exe, cur_in, cur_out, cur_ans, ws, max(3.0, time_limit), env=env)
            if spj["exit_code"] != 0:
                verdict = "WA"
        elif normalize_output(expected) != normalize_output(actual):
            verdict = "WA"

        if verdict:
            hack_in, hack_ans, hack_out = ws / "hack.in", ws / "hack.ans", ws / "hack.out"
            hack_in.write_text(input_data, encoding="utf-8")
            hack_ans.write_text(expected, encoding="utf-8")
            hack_out.write_text(actual, encoding="utf-8")
            return {
                "status": "HACK_SUCCESS",
                "verdict": verdict,
                "iteration": iteration,
                "iterations_run": iteration,
                "hack_input": _clip(input_data, 2000),
                "input_size": len(input_data),
                "expected_output": _clip(expected, 800),
                "actual_output": _clip(actual, 800),
                "stderr": _clip(target["stderr"], 800) if verdict == "RE" else "",
                "files": {"in": str(hack_in), "ans": str(hack_ans), "out": str(hack_out)},
                "sources": {k: str(v) for k, v in sources.items()},
                "elapsed_sec": round(time.perf_counter() - started, 3),
                "warnings": warnings,
            }

    for temp in (cur_in, cur_out, cur_ans):
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    return {
        "status": "ALL_PASSED",
        "iterations": last_iteration,
        "time_limit": time_limit,
        "elapsed_sec": round(time.perf_counter() - started, 3),
        "workspace": str(ws),
        "warnings": warnings,
    }


def _run_spj(
    spj_exe: Path,
    in_file: Path,
    out_file: Path,
    ans_file: Path,
    cwd: Path,
    timeout: float,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """SPJ 调用约定：``spj <in> <out> <ans>``，返回 0 表示选手输出合法。"""
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        proc = subprocess.Popen(
            [str(spj_exe), str(in_file), str(out_file), str(ans_file)],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=fo,
            stderr=fe,
            env=env,
            **_popen_kwargs(),
        )
        timed_out = False
        try:
            proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_tree(proc)
            try:
                proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                pass
        fe.seek(0)
        return {
            "exit_code": -1 if timed_out else int(proc.returncode or 0),
            "stderr": fe.read(4096).decode("utf-8", errors="replace"),
        }


def run_stress_test(
    workspace_dir: str,
    target_cpp: str,
    brute_cpp: str,
    gen_py: str,
    spj_cpp: str = "",
    iterations: int = 200,
    time_limit: float | None = None,
    *,
    policy: SecurityPolicy | None = None,
) -> str:
    """工具入口：返回 JSON 字符串（LLM 直接可读）。"""
    try:
        result = stress_test(
            workspace_dir,
            target_cpp,
            brute_cpp,
            gen_py,
            spj_cpp=spj_cpp,
            iterations=iterations,
            time_limit=time_limit,
            policy=policy,
        )
    except Exception as exc:  # noqa: BLE001 - 工具层必须把异常转成可读结果
        result = {"status": "ERROR", "message": f"{type(exc).__name__}: {exc}"}
    return json.dumps(result, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# 手工调试入口：python -m tools.builtins.stress_runner --help
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="XCPC-Claw 对拍器（无 LLM 手工调试入口）")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--target", required=True, help="选手代码 .cpp")
    parser.add_argument("--brute", required=True, help="暴力标程 .cpp")
    parser.add_argument("--gen", required=True, help="数据生成器 .py（python gen.py <seed> -> stdout）")
    parser.add_argument("--spj", default="")
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--time-limit", type=float, default=2.0)
    args = parser.parse_args(argv)
    payload = run_stress_test(
        args.workspace, args.target, args.brute, args.gen, args.spj, args.iterations, args.time_limit
    )
    print(payload)
    data = json.loads(payload)
    return 0 if data.get("status") in {"HACK_SUCCESS", "ALL_PASSED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
