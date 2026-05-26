"""``/tools`` — 列出当前可用的工具。"""

from model_tools import get_available_tool_names

from cli.context import AgentCtx
from cli.registry import command


@command(
    "/tools",
    description="Show enabled toolsets and registered tools",
    category="tools",
)
def cmd_tools(args: str, ctx: AgentCtx) -> None:
    available = get_available_tool_names(ctx.enabled_toolsets)
    print(f"  [toolset] {ctx.enabled_toolsets}")
    print(f"  [available] {', '.join(available)}")
    print(f"  [registered] {', '.join(ctx.registry.tool_names)}")
