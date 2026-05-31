"""V24.0 session_store 行为验证脚本。

验证 10 个关键不变量（行为契约，非实现细节）：
1. save → load round-trip：messages 内容一致
2. 存档/load **不含** system message（由调用方重建 — 决策 3）
3. tool_calls（assistant）/ tool_call_id（tool）round-trip 重建正确
4. created_at 跨多次 save 不变，updated_at 递增
5. in-place 压缩前的全量重写：messages 变短后 save，load 回来 = 当前视图
   （v24.0 不留旧消息，符合预期 — append-only + 压缩链归 v24.1）
6. list_sessions 倒序 + preview 截断 + msg 数正确
7. 事务原子性：第二条 INSERT 抛异常，整次 save 回滚（不留半截）
8. load 不存在的 id 返回 None
9. session_tokens 4 维 round-trip
10. delete 删行 + 返回值；WAL 随 close 收尾

零外部依赖：用临时文件库（或 :memory:），秒级完成，不需要 pytest。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.session_store import SessionStore


def _make_store(tmp_path: str = ":memory:") -> SessionStore:
    return SessionStore(tmp_path)


# 一段含各类 role 的典型对话（带 system / user / assistant+tool_calls / tool）
def _sample_messages() -> list[dict]:
    return [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "北京天气?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"北京"}'},
                }
            ],
            "reasoning_content": "需要查天气",
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"output":"晴 25°C"}'},
        {"role": "assistant", "content": "北京今天晴，25°C"},
    ]


def test_save_load_roundtrip() -> None:
    """不变量 1：save → load，user/assistant/tool 内容逐条一致。"""
    store = _make_store()
    store.save("s1", _sample_messages(), turn_count=1, model="m")
    data = store.load("s1")
    assert data is not None
    msgs = data["messages"]
    # 5 条原始 - 1 条 system = 4 条
    assert len(msgs) == 4, f"expected 4 non-system msgs, got {len(msgs)}"
    assert msgs[0] == {"role": "user", "content": "北京天气?"}
    assert msgs[-1] == {"role": "assistant", "content": "北京今天晴，25°C"}
    store.close()
    print("  save_load_roundtrip OK")


def test_system_message_excluded() -> None:
    """不变量 2：存档剔除 system（决策 3 — resume 时重建，不存旧的）。"""
    store = _make_store()
    store.save("s1", _sample_messages(), turn_count=1, model="m")
    data = store.load("s1")
    roles = [m["role"] for m in data["messages"]]
    assert "system" not in roles, f"system leaked into store: {roles}"
    store.close()
    print("  system_message_excluded OK")


def test_tool_call_shape_roundtrip() -> None:
    """不变量 3：tool_calls / tool_call_id / reasoning_content 重建正确。"""
    store = _make_store()
    store.save("s1", _sample_messages(), turn_count=1, model="m")
    msgs = store.load("s1")["messages"]
    asst = next(m for m in msgs if m["role"] == "assistant" and m.get("tool_calls"))
    assert asst["tool_calls"][0]["function"]["name"] == "get_weather"
    assert asst["tool_calls"][0]["id"] == "call_1"
    assert asst["reasoning_content"] == "需要查天气"
    # content=None 的 assistant 不应带 content 键（_row_to_message 省略 None）
    assert "content" not in asst, "None content should be omitted on rebuild"
    tool_msg = next(m for m in msgs if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "call_1"
    # 普通 user 消息不应混入 tool_call_id / tool_calls
    user_msg = next(m for m in msgs if m["role"] == "user")
    assert "tool_call_id" not in user_msg and "tool_calls" not in user_msg
    store.close()
    print("  tool_call_shape_roundtrip OK")


def test_created_at_stable_updated_at_grows() -> None:
    """不变量 4：created_at 多次 save 不变，updated_at 递增。"""
    store = _make_store()
    store.save("s1", _sample_messages(), turn_count=1, model="m")
    first = store.load("s1")
    time.sleep(0.02)
    store.save("s1", _sample_messages() + [{"role": "user", "content": "再问"}],
               turn_count=2, model="m")
    second = store.load("s1")
    assert second["created_at"] == first["created_at"], "created_at must stay fixed"
    assert second["updated_at"] > first["updated_at"], "updated_at must grow"
    store.close()
    print("  created_at_stable_updated_at_grows OK")


def test_full_rewrite_shrinks() -> None:
    """不变量 5：messages 变短后 save（模拟压缩），load = 当前视图。

    v24.0 全量删重插语义：压缩后旧消息不保留（append-only + 压缩链归 v24.1）。
    """
    store = _make_store()
    store.save("s1", _sample_messages(), turn_count=3, model="m")
    assert len(store.load("s1")["messages"]) == 4
    # 模拟 in-place 压缩：messages 整个变短到 2 条
    compacted = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "[摘要] 之前聊了北京天气"},
    ]
    store.save("s1", compacted, turn_count=3, model="m")
    msgs = store.load("s1")["messages"]
    assert len(msgs) == 1, f"after shrink expected 1 non-system msg, got {len(msgs)}"
    assert msgs[0]["content"].startswith("[摘要]")
    store.close()
    print("  full_rewrite_shrinks OK")


def test_list_sessions_order_and_preview() -> None:
    """不变量 6：list 倒序（最近活跃在前）+ preview 截断 + msg 数正确。"""
    store = _make_store()
    store.save("old", [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "x" * 100},  # 超 60 字应被截断
        {"role": "assistant", "content": "ok"},
    ], turn_count=1, model="m")
    time.sleep(0.02)
    store.save("new", [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "hello"},
    ], turn_count=1, model="m")
    rows = store.list_sessions()
    assert [r["session_id"] for r in rows] == ["new", "old"], "must be updated_at DESC"
    old_row = next(r for r in rows if r["session_id"] == "old")
    assert old_row["msg_count"] == 2, f"old msg_count={old_row['msg_count']}"
    assert old_row["preview"].endswith("..."), "long preview must be truncated"
    assert len(old_row["preview"]) <= 63  # 60 + "..."
    store.close()
    print("  list_sessions_order_and_preview OK")


def test_transaction_atomic_rollback() -> None:
    """不变量 7：第二条 INSERT 抛异常 → 整次 save 回滚，不留半截。

    先存一个正常会话；再 monkeypatch _insert_message 在第 2 条抛错，断言
    旧数据没被新 save 的 DELETE 永久破坏（事务回滚）。
    """
    store = _make_store()
    store.save("s1", _sample_messages(), turn_count=1, model="m")
    before = store.load("s1")["messages"]

    original = store._insert_message
    calls = {"n": 0}

    def boom(session_id, seq, msg):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected INSERT failure")
        return original(session_id, seq, msg)

    store._insert_message = boom  # type: ignore[assignment]
    raised = False
    try:
        store.save("s1", _sample_messages(), turn_count=2, model="m")
    except RuntimeError:
        raised = True
    store._insert_message = original  # type: ignore[assignment]

    assert raised, "save should propagate the INSERT failure"
    after = store.load("s1")["messages"]
    # 事务回滚：DELETE + 半截 INSERT 都被撤销，旧 4 条原样还在
    assert after == before, f"rollback failed: {len(after)} msgs (expected {len(before)})"
    store.close()
    print("  transaction_atomic_rollback OK")


def test_load_missing_returns_none() -> None:
    """不变量 8：load 不存在的 id → None（调用方据此友好报错）。"""
    store = _make_store()
    assert store.load("nope") is None
    store.save("s1", _sample_messages(), turn_count=1, model="m")
    assert store.load("s1") is not None
    assert store.load("still-nope") is None
    store.close()
    print("  load_missing_returns_none OK")


def test_session_tokens_roundtrip() -> None:
    """不变量 9：4 维 session_tokens round-trip。"""
    store = _make_store()
    tokens = {"input": 1200, "output": 340, "cache_read": 800, "cache_write": 64}
    store.save("s1", _sample_messages(), turn_count=2, model="deepseek-chat",
               session_tokens=tokens)
    data = store.load("s1")
    assert data["session_tokens"] == tokens, f"got {data['session_tokens']}"
    assert data["model"] == "deepseek-chat"
    assert data["turn_count"] == 2
    store.close()
    print("  session_tokens_roundtrip OK")


def test_delete_and_close(tmp_db: str = "") -> None:
    """不变量 10：delete 删行 + 返回值；文件库 close 后 WAL 收尾。"""
    import tempfile

    d = tempfile.mkdtemp()
    db = str(Path(d) / "state.db")
    store = SessionStore(db)
    store.save("s1", _sample_messages(), turn_count=1, model="m")
    assert store.delete("s1") is True, "delete existing returns True"
    assert store.delete("s1") is False, "delete again returns False"
    assert store.load("s1") is None, "deleted session not loadable"
    # 还能正常写 — 连接未损坏
    store.save("s2", _sample_messages(), turn_count=1, model="m")
    store.close()
    # close 后主库文件应存在（WAL checkpoint 回收）
    assert Path(db).is_file(), "db file should persist after close"
    # 重开能读回 s2 —— 落盘真生效（这正是 v24.0 修掉假 resume 的根基）
    store2 = SessionStore(db)
    assert store2.load("s2") is not None, "data must survive close+reopen"
    assert store2.load("s1") is None
    store2.close()
    print("  delete_and_close OK")


def main() -> int:
    tests = [
        test_save_load_roundtrip,
        test_system_message_excluded,
        test_tool_call_shape_roundtrip,
        test_created_at_stable_updated_at_grows,
        test_full_rewrite_shrinks,
        test_list_sessions_order_and_preview,
        test_transaction_atomic_rollback,
        test_load_missing_returns_none,
        test_session_tokens_roundtrip,
        test_delete_and_close,
    ]
    print(f"Running {len(tests)} V24.0 session_store tests...")
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
