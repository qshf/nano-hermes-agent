"""``/load`` — 运行时动态加载 plugins/ 下的工具文件。

历史等价：``/load <filename>`` → 把 plugins/ 里的某个 .py 注入工具注册表。
"""

from cli.context import AgentCtx
from cli.registry import command, split_args


@command(
    "/load",
    description="Load a plugin file from plugins/ at runtime",
    args_hint="<filename>",
    category="tools",
)
def cmd_load(args: str, ctx: AgentCtx) -> None:
    parts = split_args(args)
    if not parts:
        print("  Usage: /load <filename>")
        return

    filename = parts[0]
    try:
        from tools import load_plugin

        old_gen = ctx.registry.generation
        load_plugin(filename)
        print(
            f"  [loaded] {filename} (generation: {old_gen} → "
            f"{ctx.registry.generation})"
        )
        print(f"  [tools] {ctx.registry.tool_names}")
    except Exception as exc:
        print(f"  [error] {exc}")
