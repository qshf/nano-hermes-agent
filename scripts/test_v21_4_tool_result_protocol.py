"""V21.4 工具结果协议不变量验证脚本。

不调真实 API；用 registry.dispatch 直接打全部已注册工具，验证返回值都满足
"合法 JSON 字符串 + 顶层是 dict + 至少含 error 或 output/content 之一"。
另外造几个故意违例的工具，验证 dispatch 最终防线兜底。

覆盖（10 项）：
1. tool_result(dict) 与 tool_result(**kwargs) 等价
2. tool_error(msg) 含 error 字段
3. tool_error(msg, **extra) 合并 extra
4. read_file 成功 → {"content": ...}
5. read_file 失败 → {"error": ...}
6. terminal 成功 → {"output": ...}
7. skill_view 未挂载 loader → {"error": ...}（不崩）
8. dispatch 兜底：handler 抛异常 → {"error": ...}
9. dispatch 兜底：handler 返回非 str → {"output": str(...)}
10. dispatch 兜底：handler 返回非 JSON 字符串 → {"output": <原文>}
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import registry as registry_module  # 触发自注册
from tools.registry import registry
from tools.result import tool_error, tool_result


# ─── tool_result / tool_error ─────────────────────────────────────────────


def test_tool_result_equivalence() -> None:
    a = tool_result({"output": "hi", "exit_code": 0})
    b = tool_result(output="hi", exit_code=0)
    assert json.loads(a) == json.loads(b) == {"output": "hi", "exit_code": 0}
    print("✓ test 1 — tool_result(dict) 与 tool_result(**kwargs) 等价")


def test_tool_error_basic() -> None:
    parsed = json.loads(tool_error("nope"))
    assert parsed == {"error": "nope"}
    print("✓ test 2 — tool_error(msg) 含 error 字段")


def test_tool_error_extra() -> None:
    parsed = json.loads(tool_error("timed out", timeout=30, command="sleep 60"))
    assert parsed["error"] == "timed out"
    assert parsed["timeout"] == 30
    assert parsed["command"] == "sleep 60"
    print("✓ test 3 — tool_error(msg, **extra) 合并 extra 字段")


# ─── 真实工具协议 ─────────────────────────────────────────────────────────


def test_read_file_success() -> None:
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as f:
        f.write("hello\nworld\n")
        path = f.name
    try:
        raw = registry.dispatch("read_file", {"path": path})
        parsed = json.loads(raw)
        assert "content" in parsed, parsed
        assert "hello" in parsed["content"]
        assert "world" in parsed["content"]
    finally:
        Path(path).unlink(missing_ok=True)
    print("✓ test 4 — read_file 成功 → {\"content\": ...}")


def test_read_file_error() -> None:
    raw = registry.dispatch("read_file", {"path": "/no/such/file/xxx"})
    parsed = json.loads(raw)
    assert "error" in parsed, parsed
    assert "output" not in parsed and "content" not in parsed
    print("✓ test 5 — read_file 失败 → {\"error\": ...}（独占语义）")


def test_terminal_success() -> None:
    raw = registry.dispatch("terminal", {"command": "echo nano-v21.4"})
    parsed = json.loads(raw)
    assert "output" in parsed, parsed
    assert "nano-v21.4" in parsed["output"]
    print("✓ test 6 — terminal 成功 → {\"output\": ...}")


def test_skill_view_no_loader() -> None:
    # skill_view 在 loader 未注入时，handler 直接拒绝（check_fn 也会让它在
    # available_tool_names 里隐身，但 dispatch 仍可被显式调用，要返回 error 而非崩溃）
    raw = registry.dispatch("skill_view", {"name": "anything"})
    parsed = json.loads(raw)
    assert "error" in parsed, parsed
    print("✓ test 7 — skill_view 未挂载 loader → {\"error\": ...}")


# ─── dispatch 最终防线 ────────────────────────────────────────────────────


_ROGUE_SCHEMA_TEMPLATE = {
    "description": "Rogue tool for V21.4 final-line guard tests.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def _register_rogue(name: str, handler) -> None:
    schema = {"name": name, **_ROGUE_SCHEMA_TEMPLATE}
    registry.register(schema, handler)


def test_dispatch_guard_exception() -> None:
    def boom(_args: dict) -> str:
        raise RuntimeError("kaboom")

    _register_rogue("rogue_raises", boom)
    try:
        raw = registry.dispatch("rogue_raises", {})
        parsed = json.loads(raw)
        assert "error" in parsed, parsed
        assert "kaboom" in parsed["error"]
        assert "RuntimeError" in parsed["error"]
    finally:
        registry.deregister("rogue_raises")
    print("✓ test 8 — dispatch 兜底：handler 抛异常 → {\"error\": ...}")


def test_dispatch_guard_non_str() -> None:
    def returns_dict(_args: dict):
        return {"i_forgot_to_dump": True}

    _register_rogue("rogue_dict", returns_dict)
    try:
        raw = registry.dispatch("rogue_dict", {})
        assert isinstance(raw, str)
        parsed = json.loads(raw)
        assert "output" in parsed, parsed
        assert "i_forgot_to_dump" in parsed["output"]  # str(dict) 化
    finally:
        registry.deregister("rogue_dict")
    print("✓ test 9 — dispatch 兜底：handler 返回非 str → {\"output\": str(...)}")


def test_dispatch_guard_raw_string() -> None:
    def returns_raw(_args: dict) -> str:
        return "this is not json at all"

    _register_rogue("rogue_raw", returns_raw)
    try:
        raw = registry.dispatch("rogue_raw", {})
        parsed = json.loads(raw)
        assert parsed.get("output") == "this is not json at all", parsed
    finally:
        registry.deregister("rogue_raw")
    print("✓ test 10 — dispatch 兜底：handler 返回非 JSON 字符串 → {\"output\": <原文>}")


# ─── runner ───────────────────────────────────────────────────────────────


def main() -> int:
    print("=" * 60)
    print("  V21.4 工具结果协议 — 不变量验证")
    print("=" * 60)

    tests = [
        test_tool_result_equivalence,
        test_tool_error_basic,
        test_tool_error_extra,
        test_read_file_success,
        test_read_file_error,
        test_terminal_success,
        test_skill_view_no_loader,
        test_dispatch_guard_exception,
        test_dispatch_guard_non_str,
        test_dispatch_guard_raw_string,
    ]

    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failed += 1
            print(f"✗ {t.__name__} — {exc}")
        except Exception as exc:
            failed += 1
            print(f"✗ {t.__name__} — UNEXPECTED {type(exc).__name__}: {exc}")
            traceback.print_exc()

    print("=" * 60)
    if failed:
        print(f"  FAIL: {failed}/{len(tests)} 不变量未通过")
        return 1
    print(f"  PASS: {len(tests)}/{len(tests)} 不变量")
    return 0


if __name__ == "__main__":
    sys.exit(main())
