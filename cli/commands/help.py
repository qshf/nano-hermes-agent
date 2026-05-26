"""``/help`` — 从注册表渲染所有命令的元信息。

按 category 分组、按 name 排序。aliases 在描述行后追加。
"""

from cli.context import AgentCtx
from cli.registry import command, registered_commands


@command(
    "/help",
    description="Show all slash commands with descriptions",
    aliases=("h", "?"),
    category="meta",
)
def cmd_help(args: str, ctx: AgentCtx) -> None:
    cmds = registered_commands()
    if not cmds:
        print("  [help] (no commands registered)")
        return

    grouped: dict[str, list] = {}
    for c in cmds:
        grouped.setdefault(c.category, []).append(c)

    # 计算 name+args_hint 的最大宽度，对齐 description
    max_left = 0
    for c in cmds:
        left = f"/{c.name}"
        if c.args_hint:
            left += f" {c.args_hint}"
        max_left = max(max_left, len(left))

    print("  Available commands:")
    for category in sorted(grouped.keys()):
        print(f"    [{category}]")
        for c in grouped[category]:
            left = f"/{c.name}"
            if c.args_hint:
                left += f" {c.args_hint}"
            alias_suffix = ""
            if c.aliases:
                alias_suffix = f"  (aliases: {', '.join('/' + a for a in c.aliases)})"
            print(f"      {left.ljust(max_left)}  — {c.description}{alias_suffix}")
