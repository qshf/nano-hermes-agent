"""
model_tools — Agent 获取工具定义的入口。

V2：薄包装层，串联 toolsets 展开 + registry 过滤。
Agent 只需调 get_tool_definitions(["core"]) 即可拿到最终 schema 列表。
"""

import tools  # noqa: F401 — 触发自动发现
from tools.registry import registry
from toolsets import resolve_toolsets


def get_tool_definitions(enabled_toolsets: list[str]) -> list[dict]:
    """根据启用的 toolset 列表，返回可用工具的 OpenAI schema。"""
    tool_names = resolve_toolsets(enabled_toolsets)
    return registry.get_definitions(tool_names)


def get_available_tool_names(enabled_toolsets: list[str]) -> list[str]:
    """返回当前可用的工具名（经过 check_fn 过滤）。"""
    tool_names = resolve_toolsets(enabled_toolsets)
    return [
        name for name in tool_names
        if name in registry.available_tool_names
    ]
