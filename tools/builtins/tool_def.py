"""Tool 数据类与注册表（极简，不引入 pydantic 运行时开销）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    tags: tuple[str, ...] = field(default_factory=tuple)
    # 条件可见：例如 goal_pause 只在 /goal 运行期间暴露给模型
    enabled: Callable[[], bool] | None = None

    def is_enabled(self) -> bool:
        return True if self.enabled is None else bool(self.enabled())

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def run(self, **kwargs: Any) -> str:
        result = self.func(**kwargs)
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False)


class ToolRegistry:
    """按 name 索引的工具集合；schema 顺序稳定（利于 Prefix KV Cache）。"""

    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def add(self, name: str, description: str, parameters: dict[str, Any], func: Callable[..., Any]) -> Tool:
        return self.register(Tool(name, description, parameters, func))

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return [name for name, tool in self._tools.items() if tool.is_enabled()]

    def tools(self) -> list[Tool]:
        return [tool for tool in self._tools.values() if tool.is_enabled()]

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values() if tool.is_enabled()]

    def subset(self, names: list[str]) -> "ToolRegistry":
        return ToolRegistry([self._tools[n] for n in names if n in self._tools])

    def without(self, names: list[str] | set[str]) -> "ToolRegistry":
        drop = set(names)
        return ToolRegistry([tool for name, tool in self._tools.items() if name not in drop])

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self.get(name)
        if tool is None:
            return f"ERROR: 未知工具 '{name}'。可用工具: {', '.join(self.names())}"
        return tool.run(**arguments)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


def obj_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """构造 OpenAI function parameters JSON Schema。"""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }
