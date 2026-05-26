"""``/memory`` — 查看 builtin provider 当前的记忆条目。"""

from cli.context import AgentCtx
from cli.registry import command


@command(
    "/memory",
    description="Show builtin memory store entries",
    category="memory",
)
def cmd_memory(args: str, ctx: AgentCtx) -> None:
    if ctx.builtin_provider is None:
        print("  [memory] (no builtin provider)")
        return

    store = ctx.builtin_provider.store
    entries = store.entries
    if not entries:
        print("  [memory] (empty)")
        return

    print(
        f"  [memory] {len(entries)} entries, "
        f"{store.char_count()}/{store.char_limit} chars"
    )
    for i, entry in enumerate(entries, 1):
        display = entry[:80] + "..." if len(entry) > 80 else entry
        print(f"    {i}. {display}")
