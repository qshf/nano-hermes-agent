"""``/transport`` — V19 健康检查 + V20 prompt cache 命中率 + V23.4 session tokens。"""

from cli.context import AgentCtx
from cli.registry import command


@command(
    "/transport",
    description="Show transport chain health and cache stats",
    category="transport",
)
def cmd_transport(args: str, ctx: AgentCtx) -> None:
    status = ctx.chain.status()
    for s in status:
        state_label = s["state"]
        model_label = s["model"] or f"{ctx.model} (fallback)"
        extra = ""
        if state_label != "closed":
            extra = (
                f" failures={s['consecutive_failures']}"
                f" last={s['last_reason']}"
                f" cooldown_left={s['cooldown_left']:.1f}s"
            )
        print(
            f"  [transport] {s['api_mode']}({model_label}): "
            f"{state_label}{extra}"
        )
        if s["cache_read"] or s["cache_write"] or s["cache_uncached"]:
            print(
                f"    cache: read={s['cache_read']} "
                f"write={s['cache_write']} "
                f"uncached={s['cache_uncached']} "
                f"hit_rate={s['cache_hit_rate']:.1%}"
            )

    # V23.4: 父子合计 session tokens —— 让用户在跑完几轮 delegate 后能直接看
    # 累加结果（避免"数据有了但没人能验证"的状态）。命名 / 字段 v24 insights
    # 时再统一升级，此处朴素打印。
    st = ctx.runtime.session_tokens
    print(
        f"  [session_tokens] input={st['input']} output={st['output']} "
        f"cache_read={st['cache_read']} cache_write={st['cache_write']}"
    )
