"""V25.1 insights + 结构化日志 行为验证脚本。

验证 6 个关键不变量（行为契约，非实现细节）：
11. generate 读 v24 store：4 维 token 跨会话累计正确
12. tool 调用 top-N 从 messages.tool_calls JSON 解析正确（按 name 计数降序）
13. estimate_cost 按 model 选价、4 维算账正确；未知 model 标 unknown 记 0
14. 时间窗 days 过滤：窗外 session 不计（用固定 now 做决定性断言）
15. insights 不依赖 trajectory（决策 4）：没有任何 jsonl 时 generate 仍出报表
16. RedactingFormatter：日志行含密钥 → 写盘文件里被掩

零外部依赖：用 :memory: 库 + tmp 日志文件，秒级完成，不需要 pytest。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.session_store import SessionStore
from agent.insights import InsightsEngine, estimate_cost


def _make_store() -> SessionStore:
    return SessionStore(":memory:")


def _seed_session(store, sid, *, model, tokens, turn_count, created_at):
    """直接写一行 session（绕过 append 的 now 时间戳，让 created_at 可控）。"""
    store.conn.execute(
        "INSERT INTO sessions (session_id, created_at, updated_at, turn_count, model, "
        "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, created_at, created_at, turn_count, model,
         tokens.get("input", 0), tokens.get("output", 0),
         tokens.get("cache_read", 0), tokens.get("cache_write", 0)),
    )
    store.conn.commit()


def _add_msg(store, sid, seq, role, *, content=None, tool_calls=None):
    import json as _json
    store.conn.execute(
        "INSERT INTO messages (session_id, seq, role, content, tool_calls) "
        "VALUES (?,?,?,?,?)",
        (sid, seq, role, content,
         _json.dumps(tool_calls) if tool_calls else None),
    )
    store.conn.commit()


def _tc(name):
    return {"type": "function", "function": {"name": name, "arguments": "{}"}}


def test_token_accumulation() -> None:
    """不变量 11：generate 跨会话累计 4 维 token 正确。"""
    store = _make_store()
    _seed_session(store, "s1", model="deepseek-chat",
                  tokens={"input": 100, "output": 50, "cache_read": 10, "cache_write": 5},
                  turn_count=2, created_at=1000.0)
    _seed_session(store, "s2", model="deepseek-chat",
                  tokens={"input": 200, "output": 80, "cache_read": 20, "cache_write": 0},
                  turn_count=3, created_at=2000.0)
    report = InsightsEngine(store).generate(days=0)  # days<=0 = all
    tok = report["overview"]["tokens"]
    assert tok["input"] == 300, tok
    assert tok["output"] == 130, tok
    assert tok["cache_read"] == 30, tok
    assert tok["cache_write"] == 5, tok
    assert report["overview"]["total_tokens"] == 465, report["overview"]
    assert report["overview"]["total_sessions"] == 2
    assert report["overview"]["total_turns"] == 5
    assert abs(report["overview"]["avg_turns"] - 2.5) < 1e-9
    store.close()
    print("  token_accumulation OK")


def test_tool_top_n() -> None:
    """不变量 12：tool top-N 从 messages.tool_calls JSON 解析，按 name 计数降序。"""
    store = _make_store()
    _seed_session(store, "s1", model="deepseek-chat", tokens={}, turn_count=1,
                  created_at=1000.0)
    # read_file ×3, terminal ×1
    _add_msg(store, "s1", 0, "assistant", tool_calls=[_tc("read_file"), _tc("read_file")])
    _add_msg(store, "s1", 1, "assistant", tool_calls=[_tc("read_file"), _tc("terminal")])
    report = InsightsEngine(store).generate(days=0)
    top = dict(report["tool_top"])
    assert top["read_file"] == 3, top
    assert top["terminal"] == 1, top
    # 降序：read_file 在 terminal 前
    assert report["tool_top"][0][0] == "read_file", report["tool_top"]
    store.close()
    print("  tool_top_n OK")


def test_estimate_cost() -> None:
    """不变量 13：estimate_cost 按 model 选价、4 维算账；未知 model 标 unknown 记 0。"""
    # deepseek: input 0.27 / output 1.10 / cache_read 0.07 / cache_write 0.27 per 1M
    cost, status = estimate_cost("deepseek-chat",
                                 {"input": 1_000_000, "output": 1_000_000,
                                  "cache_read": 0, "cache_write": 0})
    assert status == "included", status
    assert abs(cost - (0.27 + 1.10)) < 1e-9, cost
    # qwen 子串匹配
    cost_q, status_q = estimate_cost("qwen3.6-plus", {"input": 1_000_000})
    assert status_q == "included" and abs(cost_q - 0.40) < 1e-9, (cost_q, status_q)
    # 未知 model → 0 + unknown
    cost_u, status_u = estimate_cost("gpt-9-ultra", {"input": 1_000_000})
    assert status_u == "unknown" and cost_u == 0.0, (cost_u, status_u)
    print("  estimate_cost OK")


def test_days_window() -> None:
    """不变量 14：days 时间窗过滤，窗外 session 不计（固定 now 决定性断言）。"""
    store = _make_store()
    now = 1_700_000_000.0  # 真实 epoch（~2023），40 天前仍为正，避免 cutoff=0 漏过滤
    day = 86400
    # 窗内：1 天前
    _seed_session(store, "recent", model="deepseek-chat",
                  tokens={"input": 100}, turn_count=1, created_at=now - 1 * day)
    # 窗外：40 天前
    _seed_session(store, "old", model="deepseek-chat",
                  tokens={"input": 999}, turn_count=1, created_at=now - 40 * day)
    report = InsightsEngine(store).generate(days=30, now=now)
    assert report["overview"]["total_sessions"] == 1, "窗外 session 不该计入"
    assert report["overview"]["tokens"]["input"] == 100, "只该算窗内的 100"
    # days=0 = 全部，两个都计
    report_all = InsightsEngine(store).generate(days=0, now=now)
    assert report_all["overview"]["total_sessions"] == 2
    store.close()
    print("  days_window OK")


def test_independent_of_trajectory() -> None:
    """不变量 15：insights 不依赖 trajectory（决策 4）—— 没有任何 jsonl 时仍出报表。"""
    store = _make_store()
    _seed_session(store, "s1", model="deepseek-chat",
                  tokens={"input": 100, "output": 50}, turn_count=1, created_at=1000.0)
    engine = InsightsEngine(store)
    report = engine.generate(days=0)
    # 不读任何 jsonl，纯靠 SQLite 出报表
    assert report["overview"]["total_tokens"] == 150
    text = engine.format_terminal(report)
    assert "[insights]" in text
    assert "tokens:" in text
    assert "est. cost:" in text
    store.close()
    print("  independent_of_trajectory OK")


def test_redacting_formatter() -> None:
    """不变量 16：RedactingFormatter 挂 root，任意模块的 logger 写盘前都被脱敏。

    v25.1 接线修正后 handler 挂 **root**（决策 9）—— 验证一个模拟 ``transports.chain``
    的 named logger（不是 nano 自己），其日志 propagate 到 root 后同样被掩密钥 + 注入
    session_id。这正是"已有 8 个模块日志都流经脱敏"的回归保护。
    """
    import importlib
    import logging
    import agent.logging as nano_logging
    importlib.reload(nano_logging)  # 重置 _configured 单例

    with tempfile.TemporaryDirectory() as td:
        log_path = str(Path(td) / "agent.log")
        nano_logging.setup_logging(log_file=log_path, level="INFO")
        nano_logging.set_log_session("sess-xyz")
        # 关键：用一个**别的模块名** logger（模拟 transports.chain），它不是 nano，
        # 只能靠 propagate 到 root 才会被脱敏 —— 验证修挂 root 的真效果。
        foreign = logging.getLogger("transports.chain")
        secret = "sk-abcdefghij1234567890ABCDEFGHIJ"
        foreign.info("calling transport with Authorization: Bearer %s", secret)

        root = logging.getLogger()
        for h in root.handlers:
            h.flush()
        content = Path(log_path).read_text(encoding="utf-8")
        # 原始密钥不该出现，掩码态（首6末4）应在
        assert secret not in content, f"raw secret leaked into log: {content!r}"
        assert "sk-abc...GHIJ" in content, f"masked token missing: {content!r}"
        # session_id 注入了
        assert "[sess-xyz]" in content, f"session_id not injected: {content!r}"
        # logger 名也带上了（root formatter 的 %(name)s）
        assert "transports.chain" in content, f"logger name missing: {content!r}"
        # 清理 root handler，避免污染后续测试
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for f in list(root.filters):
            root.removeFilter(f)
    print("  redacting_formatter OK")


def main() -> int:
    tests = [
        test_token_accumulation,
        test_tool_top_n,
        test_estimate_cost,
        test_days_window,
        test_independent_of_trajectory,
        test_redacting_formatter,
    ]
    print(f"Running {len(tests)} V25.1 insights tests...")
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
