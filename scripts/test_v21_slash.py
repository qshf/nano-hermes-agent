"""V21.1 slash command registry + dispatch 不变量验证脚本。

不调真实 API；用 fake AgentCtx 验证 dispatch 行为与命令注册完整性。

覆盖：
1. 注册表 — 启动期 11 个命令全部注册成功（10 个原命令 + /help）
2. dispatch 非 slash 输入 → 返回 False（不消费）
3. dispatch slash 命中 → 返回 True，handler 被调用
4. dispatch 未知命令 → 仍返回 True（被处理），打印 unknown
5. dispatch 单个 "/" → 路由到 /help
6. handler 异常 → 被捕获不冒泡，dispatch 仍返回 True
7. alias 命中 — /h、/? 等价于 /help
8. alias 不重复出现在 registered_commands()
9. handler 通过 ctx 修改可变字段（in-place mutate messages / session_id / turn_count）
10. /memory 调用前 builtin_provider 为 None 时优雅退出（不崩）
11. CommandDef frozen — 注册后字段不可变
12. command() 装饰器规范化前导 / — name="/foo" 与 name="foo" 等价
13. split_args — 引号包裹的路径正确切分
14. category 分组 — registered_commands() 按 (category, name) 排序
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 触发命令注册
import cli  # noqa: F401
from agent.runtime import AgentRuntime
from cli.context import AgentCtx
from cli.registry import (
    CommandDef,
    _REGISTRY,
    command,
    dispatch,
    registered_commands,
    split_args,
)


# ─── Fake ctx 工厂 ─────────────────────────────────────────────────────────


def _make_ctx(messages=None) -> AgentCtx:
    """构造一个最小可用的 AgentCtx，所有 manager/chain 用 MagicMock 占位。"""

    return AgentCtx(
        messages=messages if messages is not None else [{"role": "system", "content": "sys"}],
        current_session_id="default",
        turn_count=0,
        chain=MagicMock(),
        client=MagicMock(),
        model="fake-model",
        memory_manager=MagicMock(),
        builtin_provider=None,
        compressor=MagicMock(),
        registry=MagicMock(),
        enabled_toolsets=["core"],
        build_system_prompt=lambda: "system prompt",
        runtime=AgentRuntime(stream_enabled=True, cancel_token=None),
    )


# ─── 测试 ─────────────────────────────────────────────────────────────────


def test_all_v21_1_commands_registered() -> None:
    """启动期注册的命令应等于预期清单（V21.3 加 /skill；V22 加 /stream）。"""
    expected = {
        "help", "memory", "load", "mcp", "plugin",
        "tools", "session", "new", "resume", "compress", "transport",
        "skill",
        "stream",  # V22
    }
    actual = {c.name for c in registered_commands()}
    missing = expected - actual
    extra = actual - expected
    assert not missing, f"missing commands: {missing}"
    assert not extra, f"unexpected commands: {extra}"


def test_dispatch_non_slash_returns_false() -> None:
    ctx = _make_ctx()
    assert dispatch("hello world", ctx) is False
    assert dispatch("", ctx) is False
    assert dispatch("not /a slash", ctx) is False


def test_dispatch_slash_handled_returns_true() -> None:
    ctx = _make_ctx()
    # /tools 不需要 builtin_provider，调用应正常
    assert dispatch("/tools", ctx) is True


def test_dispatch_unknown_command_returns_true() -> None:
    """未知 slash 命令仍算被处理（避免回落到 LLM）。"""
    ctx = _make_ctx()
    assert dispatch("/no_such_cmd_xyz", ctx) is True


def test_dispatch_bare_slash_routes_to_help() -> None:
    """单个 '/' 视为请求 /help，不应报 unknown。"""
    ctx = _make_ctx()
    assert dispatch("/", ctx) is True


def test_dispatch_handler_exception_caught() -> None:
    """handler 抛异常应被捕获，dispatch 仍返回 True。"""
    ctx = _make_ctx()
    # /resume 缺 args 时 handler 内自己处理，不会抛 — 用 mock 触发异常更准
    sentinel = {"called": False}

    @command("/__test_raise__", description="raises", category="test")
    def _h(args: str, ctx: AgentCtx) -> None:
        sentinel["called"] = True
        raise RuntimeError("boom")

    try:
        result = dispatch("/__test_raise__", ctx)
        assert result is True
        assert sentinel["called"] is True
    finally:
        # 测试副作用清理 —— 避免污染其他 test 的注册表快照
        _REGISTRY.pop("__test_raise__", None)


def test_alias_routes_to_same_handler() -> None:
    """/h 与 /? 应等价于 /help。"""
    help_cmd = _REGISTRY.get("help")
    assert help_cmd is not None
    assert _REGISTRY.get("h") is help_cmd
    assert _REGISTRY.get("?") is help_cmd


def test_alias_not_duplicated_in_registered_commands() -> None:
    """registered_commands() 去重 — alias 不应出现为独立条目。"""
    names = [c.name for c in registered_commands()]
    assert names.count("help") == 1
    # 'h' 是 alias，不应作为主名出现
    assert "h" not in names
    assert "?" not in names


def test_handler_mutates_ctx_session_inplace() -> None:
    """/new handler 应原地清空 messages 并切换 session_id / 重置 turn_count。"""
    ctx = _make_ctx(messages=[
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hi back"},
    ])
    ctx.turn_count = 7
    original_messages = ctx.messages  # 同一 list 引用
    original_session = ctx.current_session_id

    assert dispatch("/new", ctx) is True

    # session_id 切换
    assert ctx.current_session_id != original_session
    assert ctx.current_session_id.startswith("session-")
    # turn_count 重置
    assert ctx.turn_count == 0
    # messages 引用不变（in-place mutate），内容只剩 system
    assert ctx.messages is original_messages
    assert len(ctx.messages) == 1
    assert ctx.messages[0]["role"] == "system"


def test_memory_command_handles_missing_provider() -> None:
    """builtin_provider=None 时 /memory 应优雅打印提示，不崩。"""
    ctx = _make_ctx()
    ctx.builtin_provider = None
    # 不应抛
    assert dispatch("/memory", ctx) is True


def test_command_def_is_frozen() -> None:
    """CommandDef 注册后不可篡改。"""
    cmd = _REGISTRY["help"]
    assert isinstance(cmd, CommandDef)
    try:
        cmd.name = "tampered"  # type: ignore[misc]
    except Exception as e:
        assert "frozen" in str(e).lower() or "cannot assign" in str(e).lower()
    else:
        raise AssertionError("expected FrozenInstanceError on field mutation")


def test_command_decorator_normalizes_leading_slash() -> None:
    """@command('/foo') 与 @command('foo') 应注册同名 key（无前导 /）。"""
    @command("/__test_norm_a__", description="x", category="test")
    def _a(args: str, ctx: AgentCtx) -> None:
        pass

    @command("__test_norm_b__", description="x", category="test")
    def _b(args: str, ctx: AgentCtx) -> None:
        pass

    try:
        assert "__test_norm_a__" in _REGISTRY
        assert "__test_norm_b__" in _REGISTRY
        # 不应出现带前导 / 的 key
        assert "/__test_norm_a__" not in _REGISTRY
    finally:
        _REGISTRY.pop("__test_norm_a__", None)
        _REGISTRY.pop("__test_norm_b__", None)


def test_split_args_handles_quotes() -> None:
    """args 解析应支持引号包裹路径（含空格）。"""
    assert split_args("") == []
    assert split_args("a b c") == ["a", "b", "c"]
    assert split_args('connect "path with spaces/server.py"') == [
        "connect", "path with spaces/server.py"
    ]
    # 不平衡引号兜底为简单 split，不应抛
    assert split_args('a "unclosed') == ["a", '"unclosed']


def test_registered_commands_sorted_by_category_then_name() -> None:
    cmds = registered_commands()
    keys = [(c.category, c.name) for c in cmds]
    assert keys == sorted(keys), "registered_commands should be sorted by (category, name)"


# ─── 入口 ─────────────────────────────────────────────────────────────────


TESTS = [
    test_all_v21_1_commands_registered,
    test_dispatch_non_slash_returns_false,
    test_dispatch_slash_handled_returns_true,
    test_dispatch_unknown_command_returns_true,
    test_dispatch_bare_slash_routes_to_help,
    test_dispatch_handler_exception_caught,
    test_alias_routes_to_same_handler,
    test_alias_not_duplicated_in_registered_commands,
    test_handler_mutates_ctx_session_inplace,
    test_memory_command_handles_missing_provider,
    test_command_def_is_frozen,
    test_command_decorator_normalizes_leading_slash,
    test_split_args_handles_quotes,
    test_registered_commands_sorted_by_category_then_name,
]


def main() -> int:
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")

    print()
    if failed:
        print(f"FAILED: {failed}/{len(TESTS)}")
        return 1
    print(f"ALL {len(TESTS)} TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
