"""OpenAI ``tool_calls`` 解析与异常隔离执行器。

任何工具异常都转换成 ``role=tool`` 的错误字符串回填给模型，
保证一轮 Flow 绝不会因为某个工具崩溃而中断（模型可自行修正参数重试）。
"""

from __future__ import annotations

import json
import traceback
from typing import Any

from .builtins.bash_tool import truncate_output

MAX_RESULT_CHARS = 8000


class ToolExecutor:
    def __init__(self, registry: Any, max_result_chars: int = MAX_RESULT_CHARS):
        self.registry = registry
        self.max_result_chars = max_result_chars

    def execute(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for index, call in enumerate(tool_calls or []):
            call_id = str(call.get("id") or f"call_{index}")
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            raw_args = function.get("arguments")
            content = self._execute_one(name, raw_args)
            results.append({"role": "tool", "tool_call_id": call_id, "content": truncate_output(content, self.max_result_chars)})
        return results

    def _execute_one(self, name: str, raw_args: Any) -> str:
        if not name:
            return "ERROR: tool_call 缺少 function.name"
        arguments = self._parse_args(raw_args)
        if isinstance(arguments, str):
            return arguments  # 解析失败，返回错误提示
        tool = self.registry.get(name)
        if tool is None:
            return f"ERROR: 未知工具 '{name}'。可用工具: {', '.join(self.registry.names())}"
        try:
            return tool.run(**arguments)
        except TypeError as exc:
            return f"ERROR: 调用 {name} 的参数不匹配: {exc}。请检查必填参数与类型。"
        except Exception as exc:  # noqa: BLE001 - 工具异常必须隔离
            detail = traceback.format_exc(limit=2)
            return f"ERROR: 工具 {name} 执行异常: {type(exc).__name__}: {exc}\n{detail}"

    @staticmethod
    def _parse_args(raw_args: Any) -> dict[str, Any] | str:
        if raw_args is None or raw_args == "":
            return {}
        if isinstance(raw_args, dict):
            return raw_args
        try:
            parsed = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            return f"ERROR: 工具参数不是合法 JSON: {str(raw_args)[:300]}"
        if not isinstance(parsed, dict):
            return f"ERROR: 工具参数必须是 JSON 对象，实际为 {type(parsed).__name__}"
        return parsed
