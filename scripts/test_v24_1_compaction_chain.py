"""V24.1 append-only + 压缩链 行为验证脚本。

验证 8 个关键不变量（行为契约，非实现细节）：
11. append-only 游标：两次 append（第二次是第一次超集）只插增量，
    旧行不重复、不被删；append 短列表是 no-op（旧行原样保留）
12. 压缩链核心：append 旧 N 条 → end_session → create_session(new,parent=old)
    → append 新 M 条；旧 session 仍 load 出 N 条原文、新出 M 条、parent 正确
13. resume 重定向：resolve_resume_tip(旧 id) 无条件跳最末 tip；
    无子代原样返回；链深 >1 跳最末；构造环不死循环（上限 32）
14. 列表折叠：fold_chains=True 一条链只出 tip 一行（preview 取链 root 首问）；
    False 展开所有节点（含 root）
15. end_session 只打标不删消息（标记后仍 load 出全文）
16. 迁移兼容：v24.0 用 save 写的旧库，v24.1 的 append 能在其上正确续写
    （游标从已有 COUNT 起算，不重复插）
17. 共用 apply_compaction（手动 /compress 与自动压缩同走）：真压缩时会话分裂
    + 旧全文归档 + 压缩后续写能进盘（修手动 /compress 静默丢数据）；
    无效压缩时不分裂、不建空子会话

零外部依赖：用 :memory: 库，秒级完成，不需要 pytest。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.session_store import SessionStore
from agent.compaction import apply_compaction


def _make_store() -> SessionStore:
    return SessionStore(":memory:")


def _msgs(*pairs: tuple[str, str]) -> list[dict]:
    """便捷构造 [{role,content}, ...]，首条自动塞 system（验证剔除）。"""
    out: list[dict] = [{"role": "system", "content": "你是助手"}]
    for role, content in pairs:
        out.append({"role": role, "content": content})
    return out


class _FakeCompressor:
    """模拟 ContextCompressor：compress() 把 middle 砍成一条摘要，count +1。

    ``effective=False`` 时模拟"压缩没真发生"（LLM 失败 / 无 middle）—— 返回原列表、
    count 不变，用来验证 apply_compaction 的 did_compress 守卫。
    """

    def __init__(self, *, effective: bool = True, protect_first_n: int = 0) -> None:
        self.compression_count = 0
        self.protect_first_n = protect_first_n
        self._effective = effective

    def compress(self, messages, client, model, transport=None):
        if not self._effective:
            return list(messages)  # 早返回，count 不 +1
        self.compression_count += 1
        # 砍成 [摘要 + 最后一条]，模拟真压缩让 messages 变短
        head = [m for m in messages if m.get("role") == "system"]
        return head + [{"role": "user", "content": "[摘要] 前面的对话"},
                       messages[-1]]


class _NullMemoryManager:
    def on_pre_compress_all(self, msgs) -> None:
        pass

    def on_session_switch_all(self, sid, reset=False) -> None:
        pass


class _FakeRuntime:
    def __init__(self) -> None:
        self.session_tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


class _FakeCtx:
    """apply_compaction 只 duck-type 用到这些字段。"""

    def __init__(self, store, messages, session_id) -> None:
        self.compressor = _FakeCompressor(protect_first_n=0)
        self.memory_manager = _NullMemoryManager()
        self.session_store = store
        self.messages = messages
        self.current_session_id = session_id
        self.turn_count = 1
        self.model = "m"
        self.client = None
        self.chain = None
        self.runtime = _FakeRuntime()



def test_append_only_cursor() -> None:
    """不变量 11：append 只插游标之后的增量，旧行不重复、不删；短列表 no-op。"""
    store = _make_store()
    # 第一次 append 2 条非 system
    store.append("s1", _msgs(("user", "q1"), ("assistant", "a1")),
                 turn_count=1, model="m")
    first = store.load("s1")["messages"]
    assert len(first) == 2, f"expected 2, got {len(first)}"

    # 第二次 append 是第一次超集（前 2 条不变 + 新 2 条）—— 只插增量
    store.append("s1", _msgs(("user", "q1"), ("assistant", "a1"),
                             ("user", "q2"), ("assistant", "a2")),
                 turn_count=2, model="m")
    second = store.load("s1")["messages"]
    assert len(second) == 4, f"expected 4 after append, got {len(second)}"
    # 旧 2 条原样在前（顺序 + 内容），新 2 条接在后 —— 没重复、没错位
    assert [m["content"] for m in second] == ["q1", "a1", "q2", "a2"]

    # append 一个更短的列表（理论上不该发生）→ range 空 → no-op，旧 4 条不变
    store.append("s1", _msgs(("user", "q1")), turn_count=2, model="m")
    after_short = store.load("s1")["messages"]
    assert len(after_short) == 4, f"short append must be no-op, got {len(after_short)}"
    store.close()
    print("  append_only_cursor OK")


def test_compaction_chain_core() -> None:
    """不变量 12：会话分裂后旧 session 留 N 条原文、新 session M 条、parent 正确。"""
    store = _make_store()
    # 旧 session 攒 4 条
    store.append("A", _msgs(("user", "q1"), ("assistant", "a1"),
                            ("user", "q2"), ("assistant", "a2")),
                 turn_count=2, model="m")
    # 分裂：end + create child
    store.end_session("A", "compression")
    store.create_session("A-c1", parent_session_id="A", model="m", turn_count=2)
    # 新 session 从 seq 0 起插压缩后视图（1 条摘要 + 1 条新问）
    store.append("A-c1", _msgs(("user", "[摘要] 前面聊了 q1/q2"),
                               ("user", "q3")),
                 turn_count=2, model="m")

    old = store.load("A")
    new = store.load("A-c1")
    assert len(old["messages"]) == 4, f"old must retain 4 原文, got {len(old['messages'])}"
    assert [m["content"] for m in old["messages"]] == ["q1", "a1", "q2", "a2"]
    assert len(new["messages"]) == 2, f"new expected 2, got {len(new['messages'])}"
    assert new["messages"][0]["content"].startswith("[摘要]")
    # parent 链正确
    parent_row = store.conn.execute(
        "SELECT parent_session_id FROM sessions WHERE session_id=?", ("A-c1",)
    ).fetchone()
    assert parent_row["parent_session_id"] == "A", "parent link broken"
    store.close()
    print("  compaction_chain_core OK")


def test_resolve_resume_tip() -> None:
    """不变量 13：无条件跳最末 tip；无子代原样；链深>1 跳末；环不死循环。"""
    store = _make_store()
    # 无链：原样返回
    store.append("solo", _msgs(("user", "hi")), turn_count=1, model="m")
    assert store.resolve_resume_tip("solo") == "solo"

    # 链 A → A-c1 → A-c2，深度 2，应跳到最末 A-c2
    store.append("A", _msgs(("user", "q1")), turn_count=1, model="m")
    store.end_session("A", "compression")
    store.create_session("A-c1", parent_session_id="A", model="m")
    store.append("A-c1", _msgs(("user", "q2")), turn_count=1, model="m")
    store.end_session("A-c1", "compression")
    store.create_session("A-c2", parent_session_id="A-c1", model="m")
    store.append("A-c2", _msgs(("user", "q3")), turn_count=1, model="m")
    assert store.resolve_resume_tip("A") == "A-c2", "must walk to last tip"
    assert store.resolve_resume_tip("A-c1") == "A-c2", "mid-chain → tip"
    assert store.resolve_resume_tip("A-c2") == "A-c2", "tip → self"

    # 不存在的 id 原样返回（不崩）
    assert store.resolve_resume_tip("ghost") == "ghost"

    # 构造环：X.parent=Y, Y.parent=X —— 上限 32 兜底不死循环
    store.create_session("X", parent_session_id=None, model="m")
    store.create_session("Y", parent_session_id="X", model="m")
    # 手动制造环（X 的子是 Y，Y 的子又指回 X）
    store.conn.execute(
        "UPDATE sessions SET parent_session_id=? WHERE session_id=?", ("Y", "X")
    )
    store.conn.commit()
    # 不应卡死；返回某个节点即可（seen 去重提前退出）
    result = store.resolve_resume_tip("X")
    assert result in ("X", "Y"), f"cycle guard failed: {result}"
    store.close()
    print("  resolve_resume_tip OK")


def test_list_fold_chains() -> None:
    """不变量 14：fold=True 一条链只出 tip 一行（preview 取 root 首问）；
    fold=False 展开所有节点。"""
    store = _make_store()
    # 独立会话
    store.append("solo", _msgs(("user", "独立对话")), turn_count=1, model="m")
    # 压缩链 A（原始首问"原始问题"）→ A-c1（摘要）
    store.append("A", _msgs(("user", "原始问题"), ("assistant", "答")),
                 turn_count=1, model="m")
    store.end_session("A", "compression")
    store.create_session("A-c1", parent_session_id="A", model="m")
    store.append("A-c1", _msgs(("user", "[摘要] xxx"), ("user", "新问")),
                 turn_count=1, model="m")

    folded = store.list_sessions(fold_chains=True)
    folded_ids = {r["session_id"] for r in folded}
    # A 被折叠隐藏（它是 A-c1 的 parent），只剩 tip A-c1 + solo
    assert "A" not in folded_ids, f"root A should be folded away: {folded_ids}"
    assert "A-c1" in folded_ids and "solo" in folded_ids
    # tip 的 preview 取自链 root A 的首问"原始问题"，而非摘要
    tip_row = next(r for r in folded if r["session_id"] == "A-c1")
    assert tip_row["preview"] == "原始问题", f"preview must come from root: {tip_row['preview']}"

    # 展开：A 和 A-c1 都在
    unfolded = store.list_sessions(fold_chains=False)
    unfolded_ids = {r["session_id"] for r in unfolded}
    assert "A" in unfolded_ids and "A-c1" in unfolded_ids, f"--all must show all: {unfolded_ids}"
    store.close()
    print("  list_fold_chains OK")


def test_end_session_marks_not_deletes() -> None:
    """不变量 15：end_session 只打 ended_at/end_reason 标，不删消息。"""
    store = _make_store()
    store.append("A", _msgs(("user", "q1"), ("assistant", "a1")),
                 turn_count=1, model="m")
    store.end_session("A", "compression")
    # 标记后仍能 load 出全文
    data = store.load("A")
    assert len(data["messages"]) == 2, "end_session must not delete messages"
    # 标记字段真写进去了
    row = store.conn.execute(
        "SELECT ended_at, end_reason FROM sessions WHERE session_id=?", ("A",)
    ).fetchone()
    assert row["ended_at"] is not None, "ended_at must be set"
    assert row["end_reason"] == "compression", f"end_reason={row['end_reason']}"
    store.close()
    print("  end_session_marks_not_deletes OK")


def test_migration_save_then_append() -> None:
    """不变量 16：v24.0 用 save 写的旧库，v24.1 append 在其上正确续写。"""
    store = _make_store()
    # v24.0 路径：save 全量写 2 条
    store.save("s1", _msgs(("user", "q1"), ("assistant", "a1")),
               turn_count=1, model="m")
    assert len(store.load("s1")["messages"]) == 2

    # v24.1 路径：append 超集（前 2 条 + 新 2 条）—— 游标从已有 COUNT=2 起算
    store.append("s1", _msgs(("user", "q1"), ("assistant", "a1"),
                             ("user", "q2"), ("assistant", "a2")),
                 turn_count=2, model="m")
    msgs = store.load("s1")["messages"]
    assert len(msgs) == 4, f"append after save must续写, got {len(msgs)}"
    assert [m["content"] for m in msgs] == ["q1", "a1", "q2", "a2"], "no dup/reorder"
    store.close()
    print("  migration_save_then_append OK")


def test_apply_compaction_splits_and_archives() -> None:
    """不变量 17：共用 apply_compaction（手动 /compress 与自动压缩同走）真压缩时
    会话分裂 —— 旧 session 留压缩前全文、新 session 是 旧-cN、ctx 切到新 id、
    压缩后续写能进盘（修掉手动 /compress 静默丢数据 bug）。"""
    store = _make_store()
    # 先攒 4 条历史并落盘（模拟聊了几轮）
    msgs = _msgs(("user", "q1"), ("assistant", "a1"),
                 ("user", "q2"), ("assistant", "a2"))
    store.append("A", msgs, turn_count=2, model="m")

    # 手动 /compress：apply_compaction 真压缩 → 分裂
    ctx = _FakeCtx(store, msgs, "A")
    ctx.turn_count = 2
    did = apply_compaction(ctx)
    assert did is True, "effective compress must report did=True"

    # ctx 切到子会话 A-c1
    assert ctx.current_session_id == "A-c1", f"got {ctx.current_session_id}"
    # 内存 messages 被原地压短（system + 摘要 + 最后一条）
    assert len(ctx.messages) == 3, f"compacted len={len(ctx.messages)}"

    # 旧 session A 留着压缩前 4 条全文（可回溯）
    old = store.load("A")
    assert len(old["messages"]) == 4, f"A must retain 4, got {len(old['messages'])}"
    # A 被打了 compression 封存标
    row = store.conn.execute(
        "SELECT end_reason FROM sessions WHERE session_id=?", ("A",)
    ).fetchone()
    assert row["end_reason"] == "compression"

    # 关键回归点：压缩后继续聊 → 轮末 append 到新 session 能真写进盘
    ctx.messages.append({"role": "user", "content": "q3-压缩后的新对话"})
    store.append(ctx.current_session_id, ctx.messages,
                 turn_count=3, model="m")
    new = store.load("A-c1")
    # 新 session = 摘要 + 最后一条 + q3（system 被剔除）= 3 条
    assert len(new["messages"]) == 3, f"A-c1 expected 3, got {len(new['messages'])}"
    assert new["messages"][-1]["content"] == "q3-压缩后的新对话", "压缩后新对话必须能落盘"
    store.close()
    print("  apply_compaction_splits_and_archives OK")


def test_apply_compaction_noop_when_ineffective() -> None:
    """不变量 17b：压缩没真发生（compress 不 +1）时 apply_compaction 不分裂、
    不换 id、不建空子会话。"""
    store = _make_store()
    msgs = _msgs(("user", "q1"), ("assistant", "a1"))
    store.append("A", msgs, turn_count=1, model="m")

    ctx = _FakeCtx(store, msgs, "A")
    ctx.compressor = _FakeCompressor(effective=False)  # 模拟无效压缩
    did = apply_compaction(ctx)
    assert did is False, "ineffective compress must report did=False"
    assert ctx.current_session_id == "A", "no split → id unchanged"
    # 没建出 A-c1 这种空子会话
    children = store.conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE parent_session_id=?", ("A",)
    ).fetchone()["n"]
    assert children == 0, f"must not create child sessions, got {children}"
    store.close()
    print("  apply_compaction_noop_when_ineffective OK")


def main() -> int:
    tests = [
        test_append_only_cursor,
        test_compaction_chain_core,
        test_resolve_resume_tip,
        test_list_fold_chains,
        test_end_session_marks_not_deletes,
        test_migration_save_then_append,
        test_apply_compaction_splits_and_archives,
        test_apply_compaction_noop_when_ineffective,
    ]
    print(f"Running {len(tests)} V24.1 compaction-chain tests...")
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
