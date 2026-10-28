"""极简原子工具集：文件、受限 bash、高速对拍器。

对拍器采用惰性导出，避免 `python -m tools.builtins.stress_runner` 时重复导入告警。
"""

from .bash_tool import build_bash_tool, kill_process_tree, run_command, tool_bash, truncate_output
from .file_tools import build_file_tools, resolve_path, tool_edit, tool_read, tool_write
from .tool_def import Tool, ToolRegistry, obj_schema

_LAZY = {
    "stress_test": "stress_runner",
    "run_stress_test": "stress_runner",
    "run_binary": "stress_runner",
    "compile_cpp": "stress_runner",
    "normalize_output": "stress_runner",
    "find_compiler": "stress_runner",
}

__all__ = [
    "Tool",
    "ToolRegistry",
    "obj_schema",
    "build_file_tools",
    "build_bash_tool",
    "tool_read",
    "tool_write",
    "tool_edit",
    "resolve_path",
    "run_command",
    "tool_bash",
    "kill_process_tree",
    "truncate_output",
    "stress_test",
    "run_stress_test",
    "run_binary",
    "compile_cpp",
    "normalize_output",
    "find_compiler",
]


def __getattr__(name: str):
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(f"{__name__}.{module_name}"), name)
