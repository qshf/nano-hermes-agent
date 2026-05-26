"""``/plugin`` — V5 插件生命周期管理。

用法：
    /plugin              — list loaded plugins
    /plugin list         — 同上
    /plugin load <file>
    /plugin unload <file>
"""

from cli.context import AgentCtx
from cli.registry import command, split_args


def _print_usage() -> None:
    print("  Usage:")
    print("    /plugin                  — list loaded plugins")
    print("    /plugin load <file>      — load plugin and register hooks")
    print("    /plugin unload <file>    — unload plugin and deregister hooks")


@command(
    "/plugin",
    description="Manage agent loop plugins (load / unload / list)",
    args_hint="[load|unload|list]",
    category="tools",
)
def cmd_plugin(args: str, ctx: AgentCtx) -> None:
    from tools import load_plugin, unload_plugin, list_plugins

    parts = split_args(args)

    if not parts or parts[0] == "list":
        plugins = list_plugins()
        if not plugins:
            print("  [plugin] No loaded plugins. Use: /plugin load <filename>")
        else:
            for name, hooks in plugins.items():
                joined = ", ".join(hooks) if hooks else "(no hooks)"
                print(f"  [plugin] {name}: {joined}")
        return

    sub = parts[0]
    rest = parts[1:]

    if sub == "load" and len(rest) >= 1:
        filename = rest[0]
        try:
            load_plugin(filename)
            plugins = list_plugins()
            stem = filename.replace(".py", "")
            hooks = plugins.get(stem, [])
            joined = ", ".join(hooks) if hooks else "(none)"
            print(f"  [plugin] Loaded '{filename}'")
            print(f"  [plugin] Hooks: {joined}")
        except Exception as exc:
            print(f"  [plugin error] {exc}")
        return

    if sub == "unload" and len(rest) >= 1:
        filename = rest[0]
        if unload_plugin(filename):
            print(f"  [plugin] Unloaded '{filename}'")
        else:
            print(f"  [plugin] '{filename}' is not loaded.")
        return

    _print_usage()
