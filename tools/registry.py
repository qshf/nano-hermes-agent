"""
ToolRegistry — 工具注册表。

V2：新增 check_fn 支持，get_definitions() 按名称过滤 + 运行时可用性判断。
"""

import json
from typing import Callable, Optional


class ToolRegistry:
    """全局工具注册表：存储 schema + handler + check_fn 的绑定关系。"""

    def __init__(self):
        self._tools: dict[str, dict] = {}

    def register(
        self,
        schema: dict,
        handler: Callable[[dict], str],
        check_fn: Optional[Callable[[], bool]] = None,
    ):
        """注册一个工具。check_fn 返回 False 时该工具不会暴露给 LLM。"""
        name = schema["name"]
        self._tools[name] = {
            "schema": schema,
            "handler": handler,
            "check_fn": check_fn,
        }

    def get_definitions(self, names: list[str]) -> list[dict]:
        """按名称列表返回可用工具的 OpenAI schema。check_fn 为 False 的会被过滤。"""
        result = []
        for name in sorted(names):
            entry = self._tools.get(name)
            if entry is None:
                continue
            if entry["check_fn"] and not entry["check_fn"]():
                continue
            result.append({"type": "function", "function": entry["schema"]})
        return result

    def get_openai_tools(self) -> list[dict]:
        """返回所有可用工具（兼容 V1 用法）。"""
        return self.get_definitions(list(self._tools.keys()))

    def dispatch(self, name: str, args: dict) -> str:
        """根据工具名分发调用。"""
        entry = self._tools.get(name)
        if entry is None:
            return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)
        return entry["handler"](args)

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    @property
    def available_tool_names(self) -> list[str]:
        """只返回 check_fn 通过的工具名。"""
        return [
            name for name, entry in self._tools.items()
            if not entry["check_fn"] or entry["check_fn"]()
        ]


# 全局单例
registry = ToolRegistry()
