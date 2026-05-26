"""``/session`` / ``/new`` / ``/resume`` — 生命周期相关命令。

把"会话切换语义"集中到一个文件。``/new`` 和 ``/resume`` 都会调用
``memory_manager.on_session_switch_all`` 然后 mutate ``ctx.messages``
和 ``ctx.current_session_id``，重置 ``ctx.turn_count``。

V21.2 起：system prompt 重建优先走 ``ctx.prompt_builder.build()``；
``ctx.build_system_prompt`` 仍保留为 fallback（V21.1 单元测试用 mock 时
往往不构造完整 PromptBuilder）。
"""

import uuid

from cli.context import AgentCtx
from cli.registry import command


def _rebuild_system_prompt(ctx: AgentCtx) -> str:
    if ctx.prompt_builder is not None:
        return ctx.prompt_builder.build()
    return ctx.build_system_prompt()


@command(
    "/session",
    description="Show current session_id",
    category="session",
)
def cmd_session(args: str, ctx: AgentCtx) -> None:
    print(f"  [session] {ctx.current_session_id}")


@command(
    "/new",
    description="Start a new session (fresh session_id + history)",
    category="session",
)
def cmd_new(args: str, ctx: AgentCtx) -> None:
    new_id = f"session-{uuid.uuid4().hex[:8]}"
    ctx.memory_manager.on_session_switch_all(new_id, reset=True)
    ctx.current_session_id = new_id
    ctx.messages[:] = [{"role": "system", "content": _rebuild_system_prompt(ctx)}]
    ctx.turn_count = 0
    print(f"  [session] New session: {ctx.current_session_id}")


@command(
    "/resume",
    description="Resume an existing session by id",
    args_hint="<session_id>",
    category="session",
)
def cmd_resume(args: str, ctx: AgentCtx) -> None:
    target = args.strip()
    if not target:
        print("  Usage: /resume <session_id>")
        return

    ctx.memory_manager.on_session_switch_all(target, reset=False)
    ctx.current_session_id = target
    ctx.messages[:] = [{"role": "system", "content": _rebuild_system_prompt(ctx)}]
    ctx.turn_count = 0
    print(f"  [session] Resumed: {ctx.current_session_id}")

