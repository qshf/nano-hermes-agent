"""``/mcp`` — 按需连接/断开 MCP server。

用法（与 V20 行为完全等价）：
    /mcp                      — list connected servers
    /mcp list                 — 同上
    /mcp connect <name> <cmd> [args...]
    /mcp disconnect <name>
    /mcp refresh <name>
"""

from cli.context import AgentCtx
from cli.registry import command, split_args


def _print_usage() -> None:
    print("  Usage:")
    print("    /mcp                          — list connected servers")
    print("    /mcp connect <name> <cmd> [args...]  — connect to MCP server")
    print("    /mcp disconnect <name>        — disconnect")
    print("    /mcp refresh <name>           — refresh tool list")


@command(
    "/mcp",
    description="Manage MCP server connections",
    args_hint="[connect|disconnect|refresh|list]",
    category="tools",
)
def cmd_mcp(args: str, ctx: AgentCtx) -> None:
    from tools.mcp_client import mcp_manager

    parts = split_args(args)

    if not parts or parts[0] == "list":
        servers = mcp_manager.connected_servers
        if not servers:
            print(
                "  [mcp] No connected servers. "
                "Use: /mcp connect <name> <command> [args...]"
            )
        else:
            for s in servers:
                tools = mcp_manager.get_tools(s)
                print(f"  [mcp] {s}: {', '.join(tools)}")
        return

    sub = parts[0]
    rest = parts[1:]

    if sub == "connect" and len(rest) >= 2:
        name = rest[0]
        srv_command = rest[1]
        srv_args = rest[2:]
        try:
            old_gen = ctx.registry.generation
            mcp_manager.connect(name, srv_command, srv_args)
            tools = mcp_manager.get_tools(name)
            print(
                f"  [mcp] Connected to '{name}' "
                f"(generation: {old_gen} → {ctx.registry.generation})"
            )
            print(f"  [mcp] Tools: {', '.join(tools)}")
        except Exception as exc:
            print(f"  [mcp error] {exc}")
        return

    if sub == "disconnect" and len(rest) >= 1:
        name = rest[0]
        old_gen = ctx.registry.generation
        disconnected = mcp_manager.disconnect(name)
        if disconnected:
            print(
                f"  [mcp] Disconnected '{name}' "
                f"(generation: {old_gen} → {ctx.registry.generation})"
            )
        else:
            print(
                f"  [mcp] '{name}' is not connected. "
                f"Use /mcp list to see connected servers."
            )
        return

    if sub == "refresh" and len(rest) >= 1:
        name = rest[0]
        old_gen = ctx.registry.generation
        mcp_manager.refresh(name)
        tools = mcp_manager.get_tools(name)
        print(
            f"  [mcp] Refreshed '{name}' "
            f"(generation: {old_gen} → {ctx.registry.generation})"
        )
        print(f"  [mcp] Tools: {', '.join(tools)}")
        return

    _print_usage()
