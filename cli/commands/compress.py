"""``/compress`` — 手动触发上下文压缩（调试用）。"""

from cli.context import AgentCtx
from cli.registry import command


@command(
    "/compress",
    description="Manually trigger context compression",
    category="session",
)
def cmd_compress(args: str, ctx: AgentCtx) -> None:
    last_real = ctx.compressor._last_prompt_tokens
    threshold = ctx.compressor.threshold_tokens
    if last_real is None:
        print(
            "  [compress] no real prompt_tokens recorded yet — "
            "run at least one turn before manual compression."
        )
    else:
        print(
            f"  [compress] last real prompt_tokens: {last_real}, "
            f"threshold: {threshold}"
        )

    if len(ctx.messages) < ctx.compressor.protect_first_n + 5:
        print("  [compress] not enough messages to compress")
        return

    ctx.memory_manager.on_pre_compress_all(
        ctx.messages[ctx.compressor.protect_first_n:]
    )
    pre_msgs = len(ctx.messages)
    # V19+: 摘要 LLM 调用走 chain；签名兼容 transport.call。
    ctx.messages[:] = ctx.compressor.compress(
        ctx.messages, ctx.client, ctx.model, transport=ctx.chain
    )
    print(
        f"  [compress] {pre_msgs} → {len(ctx.messages)} messages "
        f"(real savings settled on next API call)"
    )
