"""
Toolsets — 工具分组管理。

V2 核心：用 toolset 名管理工具组合，不用逐个列工具名。
agent 只需指定 enabled_toolsets=["core"]，自动展开为具体工具列表。
"""

# 工具组定义：toolset 名 → 工具名列表
TOOLSETS: dict[str, list[str]] = {
    "core": [
        "terminal",
        "read_file",
        "write_file",
        "async_demo",
        "memory",
    ],
    "docker": [
        "terminal",
        "read_file",
        "write_file",
        "docker_exec",
        "async_demo",
        "memory",
    ],
}


def resolve_toolsets(enabled: list[str]) -> list[str]:
    """将 toolset 名列表展开为去重的工具名列表。"""
    tool_names: set[str] = set()
    for toolset_name in enabled:
        tools = TOOLSETS.get(toolset_name, [])
        tool_names.update(tools)
    return sorted(tool_names)
