"""原子文件工具：read / write / edit / ls / grep / find。

所有相对路径都解析到 ``base_dir``（主 Agent 为项目根，Subagent 为自己的
``workspace/<slug>/``），从根上避免污染选手源码目录。
"""

from __future__ import annotations

import fnmatch
import os
import re
from functools import partial
from pathlib import Path
from typing import Any

from core.security import SecurityPolicy

from .tool_def import Tool, obj_schema

MAX_READ_LIMIT = 2000
MAX_MATCHES = 120
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache", ".uv-cache"}
TEXT_SUFFIXES = {
    ".py", ".pyi", ".md", ".txt", ".json", ".jsonl", ".toml", ".yml", ".yaml", ".ini", ".cfg",
    ".c", ".h", ".cpp", ".cc", ".hpp", ".java", ".rs", ".go", ".js", ".ts", ".sh", ".bat", ".ps1",
    ".html", ".css", ".csv", ".log", "", ".in", ".out", ".ans",
}


def resolve_path(path: str | os.PathLike[str], base_dir: Path | None = None) -> Path:
    raw = Path(str(path)).expanduser()
    if raw.is_absolute():
        return raw
    return (Path(base_dir) if base_dir else Path.cwd()) / raw


def _rel(path: Path, base_dir: Path | None) -> str:
    try:
        return str(path.resolve().relative_to(Path(base_dir or Path.cwd()).resolve()))
    except (ValueError, OSError):
        return str(path)


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        raise ValueError(f"{path} 看起来是二进制文件，拒绝按文本读取")
    return data.decode("utf-8", errors="replace")


def _read_denied(policy: SecurityPolicy | None, path: Path, base_dir: Path | None) -> str | None:
    if policy is None:
        return None
    decision = policy.check_read(path, base_dir)
    return None if decision.allowed else f"ERROR: 读取被安全策略拒绝：{decision.reason}"


def _write_denied(policy: SecurityPolicy | None, path: Path, base_dir: Path | None) -> str | None:
    if policy is None:
        return None
    decision = policy.check_write(path, base_dir)
    return None if decision.allowed else f"ERROR: 写入被安全策略拒绝：{decision.reason}"


# --------------------------------------------------------------------------- #
# read / write / edit
# --------------------------------------------------------------------------- #
def tool_read(
    file_path: str,
    offset: int = 1,
    limit: int = 200,
    *,
    base_dir: Path | None = None,
    policy: SecurityPolicy | None = None,
) -> str:
    path = resolve_path(file_path, base_dir)
    denied = _read_denied(policy, path, base_dir)
    if denied:
        return denied
    if not path.exists():
        return f"ERROR: 文件不存在: {path}"
    if path.is_dir():
        return f"ERROR: '{path}' 是目录，请用 ls 或 find 查看目录内容"
    try:
        text = _read_text(path)
    except (OSError, ValueError) as exc:
        return f"ERROR: 读取失败: {exc}"

    lines = text.splitlines()
    total = len(lines)
    offset = max(1, int(offset or 1))
    limit = max(1, min(MAX_READ_LIMIT, int(limit or 200)))
    chunk = lines[offset - 1 : offset - 1 + limit]
    if not chunk:
        return f"文件 {path} 共 {total} 行，offset={offset} 超出范围"
    body = "\n".join(f"{offset + i}: {line}" for i, line in enumerate(chunk))
    footer = ""
    if offset - 1 + len(chunk) < total:
        footer = f"\n... (共 {total} 行，已显示第 {offset}-{offset + len(chunk) - 1} 行，可增大 offset 继续读取)"
    return body + footer


def tool_write(
    file_path: str,
    content: str,
    *,
    base_dir: Path | None = None,
    policy: SecurityPolicy | None = None,
) -> str:
    path = resolve_path(file_path, base_dir)
    denied = _write_denied(policy, path, base_dir)
    if denied:
        return denied
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return f"OK: 已写入 {path}（{len(content)} 字符 / {content.count(chr(10)) + 1} 行）"


def tool_edit(
    file_path: str,
    old_string: str,
    new_string: str,
    *,
    base_dir: Path | None = None,
    policy: SecurityPolicy | None = None,
) -> str:
    path = resolve_path(file_path, base_dir)
    denied = _write_denied(policy, path, base_dir)
    if denied:
        return denied
    if not path.is_file():
        return f"ERROR: 文件不存在: {path}"
    try:
        text = _read_text(path)
    except (OSError, ValueError) as exc:
        return f"ERROR: 读取失败: {exc}"
    occurrences = text.count(old_string)
    if occurrences == 0:
        return (
            f"ERROR: 在 {path} 中未找到待替换片段。请先用 read 确认原文（注意缩进与空格必须完全一致）。"
        )
    if occurrences > 1:
        return (
            f"ERROR: old_string 在 {path} 中出现了 {occurrences} 次，请扩大上下文使其唯一。"
        )
    start = text.index(old_string)
    updated = text[:start] + new_string + text[start + len(old_string) :]
    path.write_text(updated, encoding="utf-8", newline="\n")
    line_no = text[:start].count("\n") + 1
    return f"OK: 已修改 {path} 第 {line_no} 行附近的 {len(old_string)} 个字符"


# --------------------------------------------------------------------------- #
# ls / grep / find
# --------------------------------------------------------------------------- #
def tool_ls(path: str = ".", *, base_dir: Path | None = None, policy: SecurityPolicy | None = None) -> str:
    target = resolve_path(path, base_dir)
    denied = _read_denied(policy, target, base_dir)
    if denied:
        return denied
    if not target.is_dir():
        return f"ERROR: 目录不存在: {target}"
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if child.name in SKIP_DIRS:
            continue
        if child.is_dir():
            entries.append(f"[dir ] {child.name}/")
        else:
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            entries.append(f"[file] {child.name} ({size} B)")
    if not entries:
        return f"目录 {target} 为空"
    return f"{target}:\n" + "\n".join(entries[:400])


def _walk_files(root: Path):
    if root.is_file():
        yield root
        return
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            yield Path(current) / name


def tool_grep(
    pattern: str,
    path: str = ".",
    include: str = "*",
    *,
    base_dir: Path | None = None,
    policy: SecurityPolicy | None = None,
) -> str:
    root = resolve_path(path, base_dir)
    denied = _read_denied(policy, root, base_dir)
    if denied:
        return denied
    if not root.exists():
        return f"ERROR: 路径不存在: {root}"
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"ERROR: 非法正则表达式: {exc}"
    matches: list[str] = []
    scanned = 0
    for file_path in _walk_files(root):
        if not fnmatch.fnmatch(file_path.name, include):
            continue
        if file_path.suffix not in TEXT_SUFFIXES:
            continue
        if policy is not None and not policy.check_read(file_path, base_dir).allowed:
            continue  # 目录树里夹带的敏感文件不参与搜索
        try:
            if file_path.stat().st_size > 2_000_000:
                continue
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        for line_no, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                matches.append(f"{file_path}:{line_no}: {line.strip()[:300]}")
                if len(matches) >= MAX_MATCHES:
                    matches.append(f"... (已达到 {MAX_MATCHES} 条上限，请缩小范围)")
                    return "\n".join(matches)
    if not matches:
        return f"未匹配到 '{pattern}'（扫描 {scanned} 个文本文件）"
    return "\n".join(matches)


def tool_find(pattern: str, path: str = ".", *, base_dir: Path | None = None, policy: SecurityPolicy | None = None) -> str:
    root = resolve_path(path, base_dir)
    denied = _read_denied(policy, root, base_dir)
    if denied:
        return denied
    if not root.exists():
        return f"ERROR: 路径不存在: {root}"
    results: list[str] = []
    for file_path in _walk_files(root):
        if policy is not None and not policy.check_read(file_path, base_dir).allowed:
            continue
        rel = str(file_path.relative_to(root)) if file_path.is_relative_to(root) else str(file_path)
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(file_path.name, pattern):
            results.append(str(file_path))
            if len(results) >= MAX_MATCHES:
                results.append(f"... (已达到 {MAX_MATCHES} 条上限)")
                break
    if not results:
        return f"未找到匹配 '{pattern}' 的文件（root={root}）"
    return "\n".join(results)


# --------------------------------------------------------------------------- #
# 组装成 Tool
# --------------------------------------------------------------------------- #
def build_file_tools(
    base_dir: Path,
    *,
    include_search: bool = False,
    policy: SecurityPolicy | None = None,
) -> list[Tool]:
    base = Path(base_dir)
    policy = policy or SecurityPolicy(base)
    tools = [
        Tool(
            "read",
            "读取 UTF-8 文本文件并返回带行号的内容（格式 `12: int x = 0;`），便于精确定位 bug 行号。大文件请用 offset/limit 分段读。"
            "只能读取工作区/技能/记忆目录，或用户在本次会话中提供的文件。",
            obj_schema(
                {
                    "file_path": {"type": "string", "description": "文件路径（相对路径以工作区为基准）"},
                    "offset": {"type": "integer", "description": "起始行号（1-based，默认 1）"},
                    "limit": {"type": "integer", "description": "最多读取行数（默认 200，上限 2000）"},
                },
                ["file_path"],
            ),
            partial(tool_read, base_dir=base, policy=policy),
        ),
        Tool(
            "write",
            "创建或覆写 UTF-8 文本文件（自动创建父目录）。**只能写 workspace/<problem_slug>/ 下**，"
            "其余路径会被安全策略拒绝。写完后建议用 read 抽查关键行。",
            obj_schema(
                {
                    "file_path": {"type": "string", "description": "目标文件路径（必须在 workspace/<problem_slug>/ 内）"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                ["file_path", "content"],
            ),
            partial(tool_write, base_dir=base, policy=policy),
        ),
        Tool(
            "edit",
            "精确字符串替换修改文件：old_string 必须在文件中唯一出现且与原文完全一致（含缩进）。"
            "只能改 workspace/<problem_slug>/ 内的文件。",
            obj_schema(
                {
                    "file_path": {"type": "string", "description": "目标文件路径"},
                    "old_string": {"type": "string", "description": "被替换的原文片段（需唯一）"},
                    "new_string": {"type": "string", "description": "替换后的新片段"},
                },
                ["file_path", "old_string", "new_string"],
            ),
            partial(tool_edit, base_dir=base, policy=policy),
        ),
    ]
    if include_search:
        tools += [
            Tool(
                "ls",
                "列出目录内容（文件名、大小、是否为目录）。",
                obj_schema({"path": {"type": "string", "description": "目录路径，默认当前工作区"}}),
                partial(tool_ls, base_dir=base, policy=policy),
            ),
            Tool(
                "grep",
                "在文本文件中按正则搜索内容，返回 `文件:行号: 内容`。用于快速定位变量/函数出现位置。",
                obj_schema(
                    {
                        "pattern": {"type": "string", "description": "Python 正则表达式"},
                        "path": {"type": "string", "description": "搜索根目录，默认当前工作区"},
                        "include": {"type": "string", "description": "文件名通配，如 *.cpp，默认 *"},
                    },
                    ["pattern"],
                ),
                partial(tool_grep, base_dir=base, policy=policy),
            ),
            Tool(
                "find",
                "按 glob 通配查找文件，如 `**/*.cpp`。",
                obj_schema(
                    {
                        "pattern": {"type": "string", "description": "glob 通配符"},
                        "path": {"type": "string", "description": "搜索根目录，默认当前工作区"},
                    },
                    ["pattern"],
                ),
                partial(tool_find, base_dir=base, policy=policy),
            ),
        ]
    return tools
