"""``/stream`` 切换流式输出。

用法
----
    /stream            # 显示当前状态
    /stream on         # 启用流式（默认）
    /stream off        # 禁用流式 — 整段响应到达后一次性打印

关闭路径保留：让依赖"一次性返回"形态的测试脚本仍可用，也方便对比
流式 vs 非流式的 usage / cache 命中率是否一致。
"""

from cli.context import AgentCtx
from cli.registry import command


@command(
    "/stream",
    description="Toggle streaming output (on/off)",
    args_hint="[on|off]",
    category="transport",
)
def cmd_stream(args: str, ctx: AgentCtx) -> None:
    arg = args.strip().lower()
    if not arg:
        state = "on" if ctx.stream_enabled else "off"
        print(f"  [stream] currently {state} — use '/stream on|off' to switch")
        return

    if arg in ("on", "1", "true", "yes"):
        ctx.stream_enabled = True
        print("  [stream] enabled — responses will print token-by-token")
        return

    if arg in ("off", "0", "false", "no"):
        ctx.stream_enabled = False
        print("  [stream] disabled — responses will print after full completion")
        return

    print(f"  [stream] unknown arg: {arg!r} (expected on|off)")
