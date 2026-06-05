"""``/session`` / ``/new`` / ``/resume`` / ``/sessions`` — 生命周期相关命令。

把"会话切换语义"集中到一个文件。``/new`` 和 ``/resume`` 都会调用
``memory_manager.on_session_switch_all`` 然后 mutate ``ctx.messages``
和 ``ctx.current_session_id``，重置 ``ctx.turn_count``。

V21.2 起：system prompt 重建优先走 ``ctx.prompt_builder.build()``；
``ctx.build_system_prompt`` 仍保留为 fallback（V21.1 单元测试用 mock 时
往往不构造完整 PromptBuilder）。

V24.0 起：``/resume`` 真 load —— 从 ``ctx.session_store`` 读回历史 messages，
而非 v14 那样只清成 system prompt（假 resume）。``/new`` 切走前先固化旧会话，
避免半截对话蒸发。新增 ``/sessions`` 列出所有已存会话。

V24.1 起：固化改走 ``append``（append-only，只增不删）；``/resume`` 先
``resolve_resume_tip`` 把旧 id 重定向到压缩链最新 tip（重定向时打提示）；
``/sessions`` 默认折叠压缩链（一条逻辑对话一行），``--all`` 展开看压缩前节点。
"""

import time
import uuid

from cli.context import AgentCtx
from cli.registry import command


def _rebuild_system_prompt(ctx: AgentCtx) -> str:
    if ctx.prompt_builder is not None:
        return ctx.prompt_builder.build()
    return ctx.build_system_prompt()


def _restore_session_tokens(ctx: AgentCtx, tokens: dict) -> None:
    """把 load 回来的 4 维 token 写回 runtime.session_tokens（就地，保持引用）。"""
    rt = ctx.runtime.session_tokens
    for k in ("input", "output", "cache_read", "cache_write"):
        rt[k] = tokens.get(k, 0)


def _persist_current(ctx: AgentCtx) -> None:
    """切走前固化当前会话 —— /new / /resume 共用。store 缺失（单测 mock）则跳过。

    V24.1：用 append（append-only，只增不删）取代 v24.0 的 save。幂等 —— 已写的
    不重插，游标自动续上；切走前把当前会话最新增量落底。
    """
    if ctx.session_store is None:
        return
    ctx.session_store.append(
        ctx.current_session_id,
        ctx.messages,
        turn_count=ctx.turn_count,
        model=ctx.model,
        session_tokens=ctx.runtime.session_tokens,
    )


def _fmt_age(updated_at: float | None) -> str:
    """相对时间（仿 /memory 列表风格）：刚刚 / Nm / Nh / Nd。"""
    if not updated_at:
        return "?"
    delta = max(0.0, time.time() - updated_at)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


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
    # 切走前固化旧会话，否则当前对话蒸发（假 resume 的另一面）
    _persist_current(ctx)
    new_id = f"session-{uuid.uuid4().hex[:8]}"
    ctx.memory_manager.on_session_switch_all(new_id, reset=True)
    ctx.current_session_id = new_id
    ctx.messages[:] = [{"role": "system", "content": _rebuild_system_prompt(ctx)}]
    ctx.turn_count = 0
    # 新会话的 token 计数归零（旧会话累计已随 save 落盘）
    _restore_session_tokens(ctx, {})
    print(f"  [session] New session: {ctx.current_session_id}")


@command(
    "/resume",
    description="Resume an existing session by id (real load — V24.0; redirects to compaction tip — V24.1)",
    args_hint="<session_id>",
    category="session",
)
def cmd_resume(args: str, ctx: AgentCtx) -> None:
    target = args.strip()
    if not target:
        print("  Usage: /resume <session_id>")
        return
    if ctx.session_store is None:
        print("  [session] no session store (persistence disabled)")
        return

    # 先沿压缩链重定向到最新 tip —— 用户敲的旧 id 若已被压缩分裂封存，
    # 跳到压缩后连续点（旧 root 虽留全文但 resume 它会立刻触发重压）。无链则原样。
    tip = ctx.session_store.resolve_resume_tip(target)

    # 先真 load —— 不存在则友好报错，**不动当前对话**
    data = ctx.session_store.load(tip)
    if data is None:
        print(f"  [session] No saved session: {target}")
        return

    # 命中后：先固化当前会话，再切到 tip
    _persist_current(ctx)
    ctx.memory_manager.on_session_switch_all(tip, reset=False)
    ctx.current_session_id = tip
    ctx.messages[:] = (
        [{"role": "system", "content": _rebuild_system_prompt(ctx)}] + data["messages"]
    )
    ctx.turn_count = data["turn_count"]
    _restore_session_tokens(ctx, data["session_tokens"])
    if tip != target:
        print(
            f"  [session] Resumed: {tip} "
            f"(redirected from {target} — compacted, {len(data['messages'])} msgs)"
        )
    else:
        print(
            f"  [session] Resumed: {tip} "
            f"({len(data['messages'])} msgs, turn {data['turn_count']})"
        )


@command(
    "/sessions",
    description="List saved sessions (most-recent first; --all unfolds compaction chains — V24.1)",
    args_hint="[--all]",
    category="session",
)
def cmd_sessions(args: str, ctx: AgentCtx) -> None:
    if ctx.session_store is None:
        print("  [session] no session store (persistence disabled)")
        return
    # 默认折叠压缩链（一条逻辑对话只显示 tip 一行）；--all 展开看压缩前节点
    fold = "--all" not in args.split()
    rows = ctx.session_store.list_sessions(fold_chains=fold)
    if not rows:
        print("  [sessions] (none)")
        return
    suffix = "" if fold else " (--all: chains unfolded)"
    print(f"  [sessions] {len(rows)} saved{suffix}")
    for s in rows:
        marker = "*" if s["session_id"] == ctx.current_session_id else " "
        # --all 模式下标出压缩链子节点（有 parent）
        chain = ""
        if not fold and s.get("parent_session_id"):
            chain = f"  ↳parent={s['parent_session_id']}"
        print(
            f"  {marker} {s['session_id']}  "
            f"{s['turn_count']}turns  {s['msg_count']}msgs  "
            f"{_fmt_age(s['updated_at'])}  {s['preview']}{chain}"
        )
