"""
Toolsets — 工具分组管理。

V2 核心：用 toolset 名管理工具组合，不用逐个列工具名。
agent 只需指定 enabled_toolsets=["core"]，自动展开为具体工具列表。

V21.3：新增 ``skill_view`` 到 core / docker — progressive disclosure tier 2 入口。
``check_fn`` 在 loader 未注入时把它从可见列表里隐藏，所以"不挂 skill"的部署
仍然干净，不需要为此切 toolset。

V23.0：新增 ``delegate_task`` 到 core / docker — 多智能体入口。``check_fn``
在 main.py 调 ``set_delegate_context`` 之前隐藏；调过之后子工具集为空时
也隐藏。这样"不挂 delegate"的部署仍干净。
"""

# 工具组定义：toolset 名 → 工具名列表
TOOLSETS: dict[str, list[str]] = {
    "core": [
        "terminal",
        "read_file",
        "write_file",
        "async_demo",
        "skill_view",
        "delegate_task",
        "voice_say",
    ],
    "docker": [
        "terminal",
        "read_file",
        "write_file",
        "docker_exec",
        "async_demo",
        "skill_view",
        "delegate_task",
        "voice_say",
    ],
}


def resolve_toolsets(enabled: list[str]) -> list[str]:
    """将 toolset 名列表展开为去重的工具名列表。"""
    tool_names: set[str] = set()
    for toolset_name in enabled:
        tools = TOOLSETS.get(toolset_name, [])
        tool_names.update(tools)
    return sorted(tool_names)
