"""
ToolRegistry — 工具注册表。

V1 核心：工具通过 register() 自注册，agent.py 不再需要逐个 import。
"""

from typing import Callable


class ToolRegistry:
    """全局工具注册表：存储 schema + handler 的绑定关系。"""

    def __init__(self):
        self._tools: dict[str, dict] = {}

    def register(self, schema: dict, handler: Callable[[dict], str]):
        """注册一个工具。schema 必须包含 name 字段。"""
        name = schema["name"]
        self._tools[name] = {
            "schema": schema,
            "handler": handler,
        }

    def get_openai_tools(self) -> list[dict]:
        """返回 OpenAI API 格式的 tools 列表。"""
        return [
            {"type": "function", "function": entry["schema"]}
            for entry in self._tools.values()
        ]

    def dispatch(self, name: str, args: dict) -> str:
        """根据工具名分发调用。"""
        import json

        entry = self._tools.get(name)
        if entry is None:
            return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)
        return entry["handler"](args)

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())


# 全局单例
registry = ToolRegistry()
