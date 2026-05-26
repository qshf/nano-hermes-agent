"""``/transport`` — V19 健康检查 + V20 prompt cache 命中率。"""

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
