"""V22 — ``/stream`` 切换流式输出。

用法
----
    /stream            # 显示当前状态
    /stream on         # 启用流式（默认）
    /stream off        # 禁用流式 — 退化到 V20 行为，整段响应到达后一次性打印

为什么要保留关闭路径
================
V22 之前的 21 档全是同步 ``chain.call``，已有 4 套测试脚本依赖"一次性返回"
形态调试。``stream off`` 让那些路径仍可用，也方便对比"流式 vs 非流式"的
usage / cache 命中率是否一致（V22 验证条目之一）。

源项目 ``cli.py`` 的 ``/stream`` 命令更复杂（含 reasoning_box / per-provider
opt-out / autopilot 联动等），nano 只保留布尔开关。
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
