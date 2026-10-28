"""受限命令行执行：安全策略审查 + 超时强杀进程树 + 输出截断。

与 ``subprocess.run(timeout=...)`` 的区别：超时后连孙进程一起杀掉（Windows
``taskkill /T /F``，POSIX ``killpg``），否则对拍/死循环程序会长期占满 CPU。

安全边界（见 ``core/security.py``）：命令必须工作区内执行、不得引用 ``..`` /
应用外绝对路径 / ``.env`` 等密钥文件，子进程环境变量会剥离 LLM API Key。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from functools import partial
from pathlib import Path

from core.security import SecurityPolicy

from .tool_def import Tool, obj_schema

MAX_OUTPUT_CHARS = 4000
DEFAULT_TIMEOUT = 10.0
_TRUNCATE_MARK = "\n... [输出过长已截断] ...\n"

# 极小破坏性命令黑名单：只拦截明显会毁坏宿主环境的调用
_DENY_PATTERNS = (
    "rm -rf /",
    "rm -rf ~",
    "mkfs.",
    "format c:",
    "del /f /s /q c:\\",
    "shutdown /s",
    "shutdown -h",
    ":(){:|:&};:",
    "dd if=/dev/zero of=/dev/sd",
)


def truncate_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """超长输出保留首尾，中间折叠，避免一次工具结果吃满上下文。"""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head - len(_TRUNCATE_MARK)
    return text[:head] + _TRUNCATE_MARK + text[-max(0, tail) :]


def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def kill_process_tree(proc: subprocess.Popen) -> None:
    """强杀进程及其所有子进程（幂等）。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        try:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _read_capped(stream, cap: int) -> tuple[str, bool, int]:
    size = stream.tell()
    stream.seek(0)
    data = stream.read(cap)
    text = data.decode("utf-8", errors="replace")
    return text, size > cap, size


def run_command(
    command: str | list[str],
    cwd: str | Path,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    shell: bool = True,
    stdin_data: bytes | None = None,
    output_cap: int = 8 * 1024 * 1024,
    env: dict[str, str] | None = None,
) -> dict:
    """执行命令并返回 ``{stdout, stderr, exit_code, timed_out, output_overflow}``。

    输出重定向到临时文件而非 PIPE，既避免死锁也能在程序疯狂打印时限制内存占用。
    """
    cwd = Path(cwd)
    if not cwd.exists():
        return {"stdout": "", "stderr": f"工作目录不存在: {cwd}", "exit_code": -1, "timed_out": False, "output_overflow": False}
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            shell=shell,
            stdin=subprocess.PIPE,
            stdout=fo,
            stderr=fe,
            env=env,
            **_popen_kwargs(),
        )
        timed_out = False
        try:
            proc.communicate(input=stdin_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_tree(proc)
            try:
                proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                pass
        stdout, out_overflow, _ = _read_capped(fo, output_cap)
        stderr, err_overflow, _ = _read_capped(fe, output_cap)
        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": int(proc.returncode if proc.returncode is not None else -1),
            "timed_out": timed_out,
            "output_overflow": out_overflow or err_overflow,
        }


# --------------------------------------------------------------------------- #
# Tool
# --------------------------------------------------------------------------- #
def tool_bash(
    command: str,
    workdir: str = ".",
    timeout: float = DEFAULT_TIMEOUT,
    *,
    base_dir: Path | None = None,
    policy: SecurityPolicy | None = None,
) -> str:
    lowered = command.lower()
    for pattern in _DENY_PATTERNS:
        if pattern in lowered:
            return f"ERROR: 命令包含被禁止的破坏性操作 '{pattern}'，已拒绝执行。"

    base = Path(base_dir) if base_dir else Path.cwd()
    policy = policy or SecurityPolicy(base)

    cwd = policy.resolve(workdir or ".", base)
    if not policy.is_workspace_dir(cwd):
        if str(workdir or ".").strip() in {"", ".", "./", ".\\"}:
            cwd = policy.workspace_root  # 默认落点改为工作区，而不是应用根目录
        else:
            return (
                f"ERROR: bash 只允许在工作区 {policy.workspace_root} 内执行。"
                f"当前 workdir={cwd} 不在允许范围内。"
            )
    try:
        cwd.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"ERROR: 无法创建工作目录 {cwd}: {exc}"

    decision = policy.check_command(command, cwd)
    if not decision.allowed:
        return f"ERROR: 命令被安全策略拒绝：{decision.reason}"

    try:
        timeout = max(0.5, min(120.0, float(timeout or DEFAULT_TIMEOUT)))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    result = run_command(command, cwd, timeout=timeout, shell=True, env=policy.sanitized_env())
    header = f"exit_code={result['exit_code']}" + (" (TIMEOUT, 进程树已强杀)" if result["timed_out"] else "")
    parts = [header]
    if result["stdout"].strip():
        parts.append("--- stdout ---\n" + truncate_output(result["stdout"]))
    if result["stderr"].strip():
        parts.append("--- stderr ---\n" + truncate_output(result["stderr"]))
    if result["output_overflow"]:
        parts.append("(输出超过 8MB，已截断)")
    if len(parts) == 1:
        parts.append("(无输出)")
    return "\n".join(parts)


def build_bash_tool(base_dir: Path, policy: SecurityPolicy | None = None) -> Tool:
    policy = policy or SecurityPolicy(base_dir)
    return Tool(
        "bash",
        "在**工作区**内执行 shell 命令（超时强杀整个进程树，输出超 4000 字符自动保留首尾截断）。"
        "安全策略：只能在工作区目录执行，禁止引用 '..'、应用外绝对路径、.env/密钥文件、联网命令与系统配置命令。",
        obj_schema(
            {
                "command": {"type": "string", "description": "要执行的命令"},
                "workdir": {"type": "string", "description": "工作目录（相对路径以工作区为基准，且必须落在工作区内），默认 ."},
                "timeout": {"type": "number", "description": "超时秒数，默认 10，最大 120"},
            },
            ["command"],
        ),
        partial(tool_bash, base_dir=Path(base_dir), policy=policy),
    )
