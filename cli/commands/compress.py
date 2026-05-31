"""``/compress`` — 手动触发上下文压缩（调试用）。

V24.1：改走 ``agent.compaction.apply_compaction`` —— 与主循环自动压缩共用同一实现，
真压缩了会走会话分裂（压缩前全文落底可回溯）。修掉旧版只做 in-place 压缩、不碰
会话分裂导致 append-only 游标卡死、压缩后新对话静默写不进盘的丢数据 bug。
"""

from agent.compaction import apply_compaction
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

    pre_msgs = len(ctx.messages)
    old_sid = ctx.current_session_id
    # V24.1: 统一走 apply_compaction —— 真压缩了会话分裂，ctx.current_session_id
    # 会被改成 旧-cN；in-place 替换 ctx.messages（与主循环局部 messages 同引用）。
    did = apply_compaction(ctx)
    if not did:
        print("  [compress] skipped (no effective compaction)")
        return
    if ctx.current_session_id != old_sid:
        print(
            f"  [compress] {pre_msgs} → {len(ctx.messages)} messages; "
            f"split {old_sid} → {ctx.current_session_id} (pre-compaction archived)"
        )
    else:
        # session_store 关闭（持久化禁用）时不分裂，只 in-place 压缩
        print(
            f"  [compress] {pre_msgs} → {len(ctx.messages)} messages "
            f"(no persistence — not archived)"
        )
