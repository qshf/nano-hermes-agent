"""V24.1 — 上下文压缩 + 会话分裂的共用执行器。

把"压缩 + （真压缩了的话）会话分裂"这套逻辑从 ``main.py`` 自动压缩点抽出来，
让**两条调用路径共用同一实现**：

1. ``main.py`` 主循环自动压缩（``should_compress`` 触发）
2. ``cli/commands/compress.py`` 手动 ``/compress``

**为什么必须抽出来**（v24.1 补档）：手动 ``/compress`` 原本只做 in-place 压缩
（``messages[:] = compress(...)``），**没碰会话分裂**。于是 append-only 游标（磁盘
``COUNT(*)``）会卡在压缩前的条数，而内存变短 → 下一轮 ``append`` 的 ``range(已写, 更少)``
为空 → **静默 no-op**，压缩后的新对话永远写不进盘。这是比自动路径漏接更隐蔽的丢数据
（无任何报错）。抽成共用函数后两条路径行为一致：都走会话分裂，压缩前全文落底可回溯。

**全程只读写 ctx**：``messages`` 原地替换（``ctx.messages[:] = compacted``，保持与
主循环局部 ``messages`` 同引用）；压缩分裂改 ``ctx.current_session_id``。主循环自动
路径调用前把局部 ``current_session_id`` / ``turn_count`` 同步进 ctx、调用后读回。
"""

from __future__ import annotations

from typing import Any


def apply_compaction(ctx: Any) -> bool:
    """执行一次上下文压缩 + （真压缩了才）会话分裂。返回"是否真发生了压缩"。

    步骤（顺序不能乱，见 [docs/decisions/v24.1.md](../docs/decisions/v24.1.md) 决策 2/7）：

    1. ``on_pre_compress_all`` 生命周期钩子（让 memory provider 有机会留存被压走的内容）
    2. 快照 ``compression_count`` → ``compress()`` → 比对是否真 +1
       （LLM 失败 / 无可摘要 middle 会早返回不 +1 —— 只有真压缩了才分裂，
       否则会建出空的 ``-cN`` 子会话 + 重复 flush）
    3. 真压缩了：**在 ``messages[:]=compacted`` 之前**串起会话分裂四步：
       append 旧 session 压缩前完整 messages（落底可回溯）→ ``end_session`` 打封存标
       → 换 ``session_id`` 为 ``旧-cN`` + ``create_session(parent=旧)`` 串链（新 session
       ``COUNT(*)=0`` → 游标归零，下轮从 seq 0 插压缩后视图）→ ``on_session_switch_all``
       联动 memory bank 轮换 → 同步 ``ctx.current_session_id``

    ``session_store`` 为 None（持久化关闭 / 单测 mock）时跳过分裂，只做 in-place 压缩
    （退化到 v14 ephemeral 行为，不丢比"本就没落盘"更多的东西）。
    """
    compressor = ctx.compressor
    head_end = compressor.protect_first_n
    ctx.memory_manager.on_pre_compress_all(ctx.messages[head_end:])

    cc_before = compressor.compression_count
    compacted = compressor.compress(
        ctx.messages, ctx.client, ctx.model, transport=ctx.chain
    )
    did_compress = compressor.compression_count > cc_before

    if not did_compress:
        # 压缩没真发生（LLM 失败 / 无可摘要 middle）—— 不分裂，messages 原样替换
        ctx.messages[:] = compacted
        return False

    store = ctx.session_store
    if store is not None:
        old_sid = ctx.current_session_id
        # 1. 旧 session 压缩前完整 messages 落底 —— 必须在 messages 被替换前
        store.append(
            old_sid, ctx.messages,
            turn_count=ctx.turn_count, model=ctx.model,
            session_tokens=ctx.runtime.session_tokens,
        )
        # 同一时机同一份 ctx.messages，顺手转 ShareGPT 落一份 trajectory。
        # 这段会话即将被压缩摘要替换（即将"丢"），正是数据飞轮接住它的时刻。
        # completed=True：自然压缩 = 这段跑完了。
        try:
            from agent.trajectory import flush_session_trajectory
            flush_session_trajectory(
                ctx.messages, model=ctx.model, completed=True,
                filename_stem=old_sid,
            )
        except Exception:  # noqa: BLE001 — trajectory 落盘永不阻断压缩主流程
            import logging
            logging.getLogger(__name__).warning("[compaction] trajectory flush failed", exc_info=True)
        # 2. 封存旧 session（只打标不删消息）
        store.end_session(old_sid, "compression")
        # 3. 换 id + 建子会话串链（游标天然归零）
        new_sid = f"{old_sid}-c{compressor.compression_count}"
        store.create_session(
            new_sid, parent_session_id=old_sid,
            model=ctx.model, turn_count=ctx.turn_count,
        )
        # 4. memory bank 轮换 + 同步 ctx
        ctx.memory_manager.on_session_switch_all(new_sid, reset=False)
        ctx.current_session_id = new_sid

    ctx.messages[:] = compacted
    return True
