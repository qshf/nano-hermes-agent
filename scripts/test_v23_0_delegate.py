"""V23.0 多智能体（delegate_task）— 不变量验证脚本。

不调真实 API；用 fake chain（提前编排好的 NormalizedResponse 序列）+ 真 registry
验证以下不变量。承诺范围与 ``docs/Multi-agent-system/iteration-plan.md`` 对齐：

覆盖（10 项）：
1.  child_loop 隔离 — 父 messages 不被子 mutate；子内部新建 list
2.  父全集 → 子允许集 = 父全集 - {delegate_task} - {memory_*}
3.  delegate_task check_fn — 注入前隐藏，注入后暴露（registry.available_tool_names）
4.  child system prompt — 含 goal 字面量，不含父 PromptBuilder 的 skill 索引段标识
5.  child system prompt — context 为空时不出现 "CONTEXT:" 段；非空时出现
6.  delegate handler — goal 缺失 / 空白时返回 tool_error，不启动子 loop
7.  child_loop 自然终止 — finish_reason="stop" + 无 tool_calls → exit_reason="completed"
8.  child_loop max_iterations — 子 LLM 一直返回 tool_call → exit_reason="max_iterations"
9.  child_loop 黑名单二次校验 — 子 LLM 幻觉调用 ``delegate_task`` → tool 层返 error，不递归
10. handler 返回值 — V21.4 工具协议合法 JSON ``{"output": <summary>}``，dispatch 兜底不触发

覆盖外（已在前档不变量中验证，不重复）：
- transport chain 复用：``test_v19_failover.py`` 已验证 cache_stats 累计与断路器
- V21.4 dispatch 兜底：``test_v21_4_tool_result_protocol.py`` 已验证异常 / 非 str / 非 JSON 三类
- 流式路径：V23.2 才接，本档 stream_call 不参与
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.registry import registry
from tools.delegate_tool import (
    _DELEGATE_BLACKLIST_NAMES,
    _DELEGATE_BLACKLIST_PREFIXES,
    DelegateContext,
    _resolve_child_toolset,
    delegate_task_handler,
    set_delegate_context,
)
from agent.child_loop import _build_child_system_prompt, run_child_loop
from agent.prompt_builder import SKELETON_PROMPT
from transports.types import NormalizedResponse, ToolCall, Usage


# ─── Fake Chain ─────────────────────────────────────────────────────────────


@dataclass
class _FakeChain:
    """按顺序吐出预定义的 NormalizedResponse；记录每次调用参数。

    支持被 child_loop 当作真 chain 调 ``call(model, messages, tools)``。
    """

    responses: list[NormalizedResponse] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def call(self, *, model: str, messages: list[dict], tools: list[dict]) -> NormalizedResponse:
        self.calls.append({
            "model": model,
            "messages": [dict(m) for m in messages],  # snapshot
            "tools": list(tools),
        })
        if not self.responses:
            raise RuntimeError("FakeChain ran out of responses")
        return self.responses.pop(0)


def _stop_response(text: str) -> NormalizedResponse:
    return NormalizedResponse(
        content=text,
        tool_calls=None,
        finish_reason="stop",
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def _tool_call_response(name: str, args: dict, call_id: str = "tc-1") -> NormalizedResponse:
    return NormalizedResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=json.dumps(args))],
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=12, completion_tokens=8, total_tokens=20),
    )


# ─── 测试驱动 ───────────────────────────────────────────────────────────────


_passed = 0
_failed = 0


def _run(name: str, fn):
    global _passed, _failed
    try:
        fn()
    except AssertionError as e:
        _failed += 1
        print(f"  [FAIL] {name}")
        print(f"         {e}")
        traceback.print_exc(limit=2)
    except Exception as e:  # noqa: BLE001
        _failed += 1
        print(f"  [ERR ] {name}: {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)
    else:
        _passed += 1
        print(f"  [PASS] {name}")


# ─── 不变量 1: child_loop 隔离 ──────────────────────────────────────────────


def test_01_isolation_parent_messages_untouched():
    """父 messages 不被子 mutate；子在内部新建 list。"""
    chain = _FakeChain([_stop_response("done")])
    parent_messages = [
        {"role": "system", "content": "parent system"},
        {"role": "user", "content": "parent user msg"},
    ]
    parent_snapshot = json.dumps(parent_messages)

    result = run_child_loop(
        goal="say hello",
        context="",
        chain=chain,
        model="fake-model",
        registry=registry,
        allowed_tool_names={"read_file"},
    )

    assert json.dumps(parent_messages) == parent_snapshot, "parent messages mutated"
    assert result["exit_reason"] == "completed"
    assert result["summary"] == "done"
    # 子 messages 必须不含父对话
    sent_msgs = chain.calls[0]["messages"]
    serialized = json.dumps(sent_msgs)
    assert "parent system" not in serialized, "parent system leaked into child"
    assert "parent user msg" not in serialized, "parent user msg leaked into child"


# ─── 不变量 2: 黑名单语义 ───────────────────────────────────────────────────


def test_02_blacklist_resolution():
    """父全集 → 子允许集 = 父全集 - {delegate_task, memory} - {memory_*}。"""
    parent_full = [
        "terminal", "read_file", "skill_view",
        "memory",                      # nano 当前 manager 暴露的单一名（V8）
        "memory_recall_v2",            # 模拟未来 hindsight 拆分（前缀匹配）
        "delegate_task",
    ]
    set_delegate_context(DelegateContext(
        chain=_FakeChain(),
        model="fake",
        parent_toolset_names=parent_full,
    ))
    allowed = _resolve_child_toolset()
    assert allowed == {"terminal", "read_file", "skill_view"}, f"got {allowed}"
    # 黑名单常量本身正确（教学保证 — 如果有人改 frozenset，立即失败）
    assert "delegate_task" in _DELEGATE_BLACKLIST_NAMES
    assert "memory" in _DELEGATE_BLACKLIST_NAMES
    assert "memory_" in _DELEGATE_BLACKLIST_PREFIXES


# ─── 不变量 3: check_fn 注入前后语义 ───────────────────────────────────────


def test_03_check_fn_hides_before_inject():
    """注入前 check_fn 返回 False / 注入后 True。"""
    # 重置注入状态
    set_delegate_context(DelegateContext(chain=_FakeChain(), model="fake", parent_toolset_names=set()))
    registry._check_fn_cache.pop("delegate_task", None)
    available_no_tools = set(registry.available_tool_names)
    assert "delegate_task" not in available_no_tools, \
        "delegate_task visible when child toolset is empty"

    # 注入有效父全集后
    set_delegate_context(DelegateContext(
        chain=_FakeChain(),
        model="fake",
        parent_toolset_names=["terminal", "read_file"],
    ))
    registry._check_fn_cache.pop("delegate_task", None)
    available_with_tools = set(registry.available_tool_names)
    assert "delegate_task" in available_with_tools, \
        "delegate_task should be visible after inject with non-empty allowed set"


# ─── 不变量 4: child system prompt 内容 ────────────────────────────────────


def test_04_child_system_prompt_focused():
    """child system prompt — 含 goal 字面量，不含父 SKELETON_PROMPT 的标识段。"""
    sp = _build_child_system_prompt(
        goal="analyze transports/chain.py:80-150",
        context="",
        tool_names=["read_file"],
    )
    assert "analyze transports/chain.py:80-150" in sp, "goal not embedded"
    assert "sub-agent" in sp.lower(), "sub-agent role not declared"
    # 父 PromptBuilder 的 skeleton 不应出现在子 system prompt 里 — 取
    # SKELETON_PROMPT 的前 30 个非空白字符做存在性检查
    skeleton_marker = " ".join(SKELETON_PROMPT.split())[:60]
    assert skeleton_marker not in sp, \
        "parent SKELETON_PROMPT leaked into child system prompt"


# ─── 不变量 5: context 段条件渲染 ───────────────────────────────────────────


def test_05_context_section_conditional():
    sp_empty = _build_child_system_prompt("g", "", ["read_file"])
    assert "CONTEXT:" not in sp_empty, "empty context should not render CONTEXT section"

    sp_with = _build_child_system_prompt("g", "user prefers Python", ["read_file"])
    assert "CONTEXT:" in sp_with
    assert "user prefers Python" in sp_with


# ─── 不变量 6: handler 输入校验 ─────────────────────────────────────────────


def test_06_handler_validates_goal():
    """goal 缺失 / 空白 / 非字符串 → tool_error，子 loop 不启动。"""
    set_delegate_context(DelegateContext(
        chain=_FakeChain([_stop_response("should not be reached")]),
        model="fake",
        parent_toolset_names=["read_file"],
    ))

    for bad_args in ({}, {"goal": ""}, {"goal": "   "}):
        result = delegate_task_handler(bad_args)
        parsed = json.loads(result)
        assert "error" in parsed, f"expected error for {bad_args}, got {parsed}"

    # context 非字符串
    bad_ctx = delegate_task_handler({"goal": "ok", "context": 42})
    assert "error" in json.loads(bad_ctx)


# ─── 不变量 7: child_loop 自然终止 ──────────────────────────────────────────


def test_07_child_completes_naturally():
    chain = _FakeChain([_stop_response("answer is 42")])
    result = run_child_loop(
        goal="compute the answer",
        context="",
        chain=chain,
        model="fake",
        registry=registry,
        allowed_tool_names={"read_file"},
    )
    assert result["exit_reason"] == "completed"
    assert result["summary"] == "answer is 42"
    assert result["iterations"] == 1


# ─── 不变量 8: max_iterations 兜底 ──────────────────────────────────────────


def test_08_child_max_iterations_capped():
    """子 LLM 一直返回 tool_call（read_file），跑满 max_iterations 后停。"""
    # 准备 max_iterations 个 tool_call 响应，让 loop 自然撞顶
    responses = [
        _tool_call_response("read_file", {"path": "x"}, call_id=f"tc-{i}")
        for i in range(10)  # > max_iterations(8)
    ]
    chain = _FakeChain(responses)

    result = run_child_loop(
        goal="loop forever",
        context="",
        chain=chain,
        model="fake",
        registry=registry,
        allowed_tool_names={"read_file"},
        max_iterations=3,
    )
    assert result["exit_reason"] == "max_iterations"
    assert result["iterations"] == 3
    # 子 summary 走兜底文案（content 为 None 时）
    assert "max_iterations" in result["summary"] or result["summary"]


# ─── 不变量 9: 黑名单二次校验（防 LLM 幻觉调用） ───────────────────────────


def test_09_child_rejects_unauthorized_tool_call():
    """子 LLM 即便 hallucinate 调用 delegate_task，child_loop 在分发前拦截。"""
    chain = _FakeChain([
        _tool_call_response("delegate_task", {"goal": "recurse"}, call_id="tc-evil"),
        _stop_response("recovered after rejection"),
    ])
    result = run_child_loop(
        goal="don't recurse",
        context="",
        chain=chain,
        model="fake",
        registry=registry,
        allowed_tool_names={"read_file"},  # 不包含 delegate_task
    )
    assert result["exit_reason"] == "completed"
    # 第二次 LLM call 的 messages 应包含一条 tool 消息含 error
    second_call_msgs = chain.calls[1]["messages"]
    tool_msgs = [m for m in second_call_msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    parsed = json.loads(tool_msgs[0]["content"])
    assert "error" in parsed
    assert "delegate_task" in parsed["error"]


# ─── 不变量 10: V21.4 工具协议合规 ──────────────────────────────────────────


def test_10_handler_returns_v21_4_compliant_json():
    """delegate handler 返回值 — 合法 JSON object，含 'output' 字段。

    V23.4 协议升级：output 不再是 plain summary string，而是 ``json.dumps({
    "results": [<task_result>, ...], "total_duration_seconds": float})`` 的
    JSON 字符串；单任务也是含 1 条的 results 数组。父 LLM 永远 ``json.loads``
    二次解析（与源项目 ``tools/delegate_tool.py:2283`` 同向）。

    本断言 V23.0 起被 V23.4 推翻；保留测试编号 #10 + 升级断言到新 schema，
    决策日志 ``docs/decisions/v23.4.md`` 注明协议演进原因。
    """
    set_delegate_context(DelegateContext(
        chain=_FakeChain([_stop_response("hello from child")]),
        model="fake",
        parent_toolset_names=["read_file"],
    ))
    raw = delegate_task_handler({"goal": "say hi"})
    parsed = json.loads(raw)  # must be valid JSON
    assert isinstance(parsed, dict), f"expected dict, got {type(parsed)}"
    assert "output" in parsed, f"missing 'output' field: {parsed}"

    # V23.4: output 字段值是 JSON 字符串 — 二次解析得到 results 数组
    inner = json.loads(parsed["output"])
    assert isinstance(inner, dict) and "results" in inner, \
        f"V23.4 schema: output should be a JSON object with 'results': {inner!r}"
    results = inner["results"]
    assert isinstance(results, list) and len(results) == 1, \
        f"single task should still produce a 1-element results array: {results!r}"
    only = results[0]
    assert only["task_index"] == 0
    assert only["status"] == "completed"
    assert only["summary"] == "hello from child"
    # 完整 schema：tokens / tool_trace / iterations / duration_seconds 必填
    for f in ("tokens", "tool_trace", "iterations", "duration_seconds", "exit_reason"):
        assert f in only, f"missing v23.4 schema field {f!r}: {only!r}"

    # registry.dispatch 兜底不应触发（结果已合法）— 通过 dispatch 走一遍验证
    set_delegate_context(DelegateContext(
        chain=_FakeChain([_stop_response("via dispatch")]),
        model="fake",
        parent_toolset_names=["read_file"],
    ))
    registry._check_fn_cache.pop("delegate_task", None)
    dispatched = registry.dispatch("delegate_task", {"goal": "via dispatch path"})
    parsed2 = json.loads(dispatched)
    inner2 = json.loads(parsed2["output"])
    assert inner2["results"][0]["summary"] == "via dispatch", f"got {inner2}"


# ─── 主入口 ─────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print("=== V23.0 delegate_task — 不变量验证 ===")
    tests = [
        ("01 child_loop 隔离 — 父 messages 不被 mutate", test_01_isolation_parent_messages_untouched),
        ("02 黑名单 — delegate_task / memory_* 被过滤", test_02_blacklist_resolution),
        ("03 check_fn — 注入前隐藏 / 注入后暴露", test_03_check_fn_hides_before_inject),
        ("04 child system prompt — 聚焦 goal、不含父 SKELETON", test_04_child_system_prompt_focused),
        ("05 context 段 — 空时不渲染 / 非空时渲染", test_05_context_section_conditional),
        ("06 handler — 校验 goal/context 类型", test_06_handler_validates_goal),
        ("07 child_loop 自然终止 — exit=completed", test_07_child_completes_naturally),
        ("08 child_loop 兜底 max_iterations", test_08_child_max_iterations_capped),
        ("09 黑名单二次校验 — 子幻觉调用被拒", test_09_child_rejects_unauthorized_tool_call),
        ("10 handler 返回 V21.4 合规 JSON", test_10_handler_returns_v21_4_compliant_json),
    ]
    for name, fn in tests:
        _run(name, fn)
    print()
    print(f"=== {_passed}/{_passed + _failed} passed ===")
    sys.exit(0 if _failed == 0 else 1)
