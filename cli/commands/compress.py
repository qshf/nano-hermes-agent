"""``/compress`` — 手动触发上下文压缩（调试用）。"""

from cli.context import AgentCtx
from cli.registry import command


@command(
    "/compress",
    description="Manually trigger context compression",
    category="session",
)
def cmd_compress(args: str, ctx: AgentCtx) -> None:
    est = ctx.compressor.estimate_tokens(ctx.messages)
    print(
        f"  [compress] estimated tokens: {est}, "
        f"threshold: {ctx.compressor.threshold_tokens}"
    )
    if len(ctx.messages) < ctx.compressor.protect_first_n + 5:
        print("  [compress] not enough messages to compress")
        return

    ctx.memory_manager.on_pre_compress_all(
        ctx.messages[ctx.compressor.protect_first_n:]
    )
    # V19+: 摘要 LLM 调用走 chain；签名兼容 transport.call。
    ctx.messages[:] = ctx.compressor.compress(
        ctx.messages, ctx.client, ctx.model, transport=ctx.chain
    )
    print(f"  [compress] compacted to {len(ctx.messages)} messages")
