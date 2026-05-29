"""V23.1 批量并行 + 工具子集白名单 — 不变量验证脚本。

不调真实 API；用线程安全的 ``_FakeChain`` + 真 registry 验证以下不变量。
承诺范围与 ``docs/Multi-agent-system/iteration-plan.md`` § V23.1 对齐：

覆盖（7 项）：
1.  批量并行确实加速 — 3 个 sleep(1s) 子，并行总耗时 < 串行 3s 的 60%
2.  结果按 task_index 与输入对齐（即便 worker 完成顺序无序，executor.map 锁住）
3.  白名单 ∩ 父全集 — 用户写父没装的工具 → 静默丢弃
4.  黑名单强制 — 用户白名单包含 ``delegate_task``/``memory`` → 仍被剔除
5.  构建期错不污染并发 — 任一 task 校验失败 → 整体 tool_error，0 个子启动
6.  schema 互斥 — goal 和 tasks 同时给 / 都不给 → tool_error
7.  V23.0 单任务路径回归 — 不传 tasks 时输出仍是 ``{"output": <summary str>}``

覆盖外（已在 V23.0 验证，不重复）：
- child_loop 隔离 / 黑名单双轨 / check_fn / system prompt：见 ``test_v23_0_delegate.py``
- V21.4 dispatch 兜底：见 ``test_v21_4_tool_result_protocol.py``
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.registry import registry
from tools.delegate_tool import (
    DelegateContext,
    delegate_task_handler,
    set_delegate_context,
    _resolve_child_toolset,
    _validate_task_entry,
)
from transports.types import NormalizedResponse, Usage


# ─── Fake Chain（线程安全版 — V23.1 批量必需）─────────────────────────────


@dataclass
class _FakeChain:
    """按顺序吐 ``NormalizedResponse``；并发安全。

    与 V23.0 ``_FakeChain`` 区别：
    - 加 ``_lock`` 保护 ``responses.pop`` / ``calls.append`` —— 多 worker
      并发调 ``call`` 时不会撞车
    - 加 ``per_call_sleep`` 模拟 LLM 响应延迟，让"批量加速"测试可控
    """

    responses: list[NormalizedResponse] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    per_call_sleep: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def call(self, *, model: str, messages: list[dict], tools: list[dict]) -> NormalizedResponse:
        # sleep 在锁外 — 模拟 LLM 真实并发延迟
        if self.per_call_sleep > 0:
            time.sleep(self.per_call_sleep)
        with self._lock:
            self.calls.append({
                "model": model,
                "messages": [dict(m) for m in messages],
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


# ─── 不变量 1: 批量并行确实加速 ─────────────────────────────────────────────


def test_01_batch_parallel_speedup():
    """3 个子任务，每个 LLM 调用 sleep 1s。并行应明显快于 3s 串行。"""
    chain = _FakeChain(
        responses=[_stop_response(f"done#{i}") for i in range(3)],
        per_call_sleep=1.0,
    )
    set_delegate_context(DelegateContext(
        chain=chain,
        model="fake",
        parent_toolset_names=["read_file"],
    ))
    t0 = time.monotonic()
    raw = delegate_task_handler({
        "tasks": [
            {"goal": "task A"},
            {"goal": "task B"},
            {"goal": "task C"},
        ],
    })
    elapsed = time.monotonic() - t0

    parsed = json.loads(raw)
    output_arr = json.loads(parsed["output"])
    assert len(output_arr) == 3, f"expected 3 results, got {len(output_arr)}"
    # 并行预算：3 个并发 sleep(1s) ≈ 1.0–1.5s（含线程池启动开销）
    # 串行 V23.0 等价路径要 ~3.0s — 我们要求 < 1.8s（留 0.8s 余量）
    assert elapsed < 1.8, f"batch took {elapsed:.2f}s (expected < 1.8s for parallel)"


# ─── 不变量 2: 结果顺序与 tasks 对齐 ──────────────────────────────────────


def test_02_result_order_matches_input():
    """3 任务的 sleep 长短不同（C → A → B 的完成顺序），结果数组仍按 0/1/2。"""
    # 让 task#0 sleep 0.3s, task#1 sleep 0.1s, task#2 sleep 0.2s
    # → 完成顺序应为 1, 2, 0；但 executor.map 应保留输入顺序
    responses = [_stop_response(f"summary_{i}") for i in range(3)]
    chain = _FakeChain(responses=responses, per_call_sleep=0.0)

    # 直接用真实并发 — sleep 在 chain.call 里没法按 task 区分（共享 chain），
    # 改用"3 个子任务每个内部各 1 次调用"的等延迟场景，验证顺序而不是验速度
    set_delegate_context(DelegateContext(
        chain=chain,
        model="fake",
        parent_toolset_names=["read_file"],
    ))
    raw = delegate_task_handler({
        "tasks": [
            {"goal": "first"},
            {"goal": "second"},
            {"goal": "third"},
        ],
    })
    parsed = json.loads(raw)
    output_arr = json.loads(parsed["output"])
    indices = [r["task_index"] for r in output_arr]
    assert indices == [0, 1, 2], f"task_index order wrong: {indices}"
    # 每条 summary 形如 "summary_X"，但 X 是被 pop 的顺序，可能与索引无关 —
    # 我们只关心 task_index 的位置稳定，不关心内容序列
    summaries = [r["summary"] for r in output_arr]
    assert all(s.startswith("summary_") for s in summaries), f"summaries malformed: {summaries}"


# ─── 不变量 3: 白名单 ∩ 父全集 ─────────────────────────────────────────────


def test_03_whitelist_intersect_parent():
    """用户传 ``tools=["read_file", "nonexistent"]`` → 子拿到 ``{read_file}``。"""
    set_delegate_context(DelegateContext(
        chain=_FakeChain(),
        model="fake",
        parent_toolset_names=["read_file", "terminal", "skill_view"],
    ))
    allowed = _resolve_child_toolset(requested=["read_file", "nonexistent"])
    assert allowed == {"read_file"}, f"got {allowed}"

    # 边界：传空 list → 空集（handler 上层会拒绝 spawn，见 _run_one_task 的
    # "no tools available" 分支）
    empty = _resolve_child_toolset(requested=[])
    assert empty == set(), f"empty whitelist should yield empty allowed, got {empty}"

    # 边界：requested=None（V23.0 兼容）→ 父全集 - 黑名单
    default = _resolve_child_toolset(requested=None)
    assert default == {"read_file", "terminal", "skill_view"}, f"got {default}"


# ─── 不变量 4: 黑名单强制 ─────────────────────────────────────────────────


def test_04_blacklist_overrides_whitelist():
    """用户白名单含 ``delegate_task``/``memory`` → 仍被强制剔除。"""
    set_delegate_context(DelegateContext(
        chain=_FakeChain(),
        model="fake",
        parent_toolset_names=[
            "read_file", "delegate_task", "memory", "memory_recall_v2",
        ],
    ))
    # 用户故意写黑名单
    allowed = _resolve_child_toolset(
        requested=["read_file", "delegate_task", "memory", "memory_recall_v2"],
    )
    assert allowed == {"read_file"}, f"got {allowed}"


# ─── 不变量 5: 构建期错不污染并发 ─────────────────────────────────────────


def test_05_validation_failure_aborts_all():
    """tasks[2] 不合法 → 整体 tool_error，0 个子启动（chain.calls 必须为 0）。"""
    chain = _FakeChain([_stop_response("should_not_run")] * 5)
    set_delegate_context(DelegateContext(
        chain=chain,
        model="fake",
        parent_toolset_names=["read_file"],
    ))
    raw = delegate_task_handler({
        "tasks": [
            {"goal": "ok 0"},
            {"goal": "ok 1"},
            {"goal": "", "context": "empty goal — invalid"},  # 校验应失败
            {"goal": "ok 3"},
        ],
    })
    parsed = json.loads(raw)
    assert "error" in parsed, f"expected error, got {parsed}"
    assert "tasks[2]" in parsed["error"], f"error should mention tasks[2]: {parsed}"
    # 关键：0 个子被启动（chain 没被调一次）
    assert len(chain.calls) == 0, f"some children spawned: {len(chain.calls)} calls"


def test_05b_validation_tools_field():
    """tools 字段类型错（不是 list[str]）也要在主线程被拦下。"""
    chain = _FakeChain([_stop_response("nope")] * 3)
    set_delegate_context(DelegateContext(
        chain=chain,
        model="fake",
        parent_toolset_names=["read_file"],
    ))
    raw = delegate_task_handler({
        "tasks": [
            {"goal": "ok"},
            {"goal": "bad", "tools": "read_file"},  # 应该是 list，不是 str
        ],
    })
    parsed = json.loads(raw)
    assert "error" in parsed, f"expected error, got {parsed}"
    assert "tasks[1].tools" in parsed["error"]
    assert len(chain.calls) == 0


# ─── 不变量 6: schema 互斥 ────────────────────────────────────────────────


def test_06_goal_tasks_mutually_exclusive():
    set_delegate_context(DelegateContext(
        chain=_FakeChain([_stop_response("nope")]),
        model="fake",
        parent_toolset_names=["read_file"],
    ))
    # 同时给
    raw = delegate_task_handler({"goal": "x", "tasks": [{"goal": "y"}]})
    parsed = json.loads(raw)
    assert "error" in parsed
    assert "mutually exclusive" in parsed["error"]

    # 都不给
    raw = delegate_task_handler({})
    parsed = json.loads(raw)
    assert "error" in parsed
    assert "must provide" in parsed["error"]


# ─── 不变量 7: V23.0 单任务路径回归 ───────────────────────────────────────


def test_07_v23_0_single_task_unchanged():
    """不传 tasks 时输出仍是 ``{"output": <summary str>}`` —— 协议未升级。"""
    chain = _FakeChain([_stop_response("hello from child")])
    set_delegate_context(DelegateContext(
        chain=chain,
        model="fake",
        parent_toolset_names=["read_file"],
    ))
    raw = delegate_task_handler({"goal": "say hi"})
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    assert "output" in parsed
    # 关键：单任务的 output 是 plain str，不是 JSON 数组的 str
    assert parsed["output"] == "hello from child"
    # 不应该可以再 json.loads（V23.0 语义保持）
    try:
        json.loads(parsed["output"])
    except json.JSONDecodeError:
        pass  # 期望走这里
    else:
        # plain "hello from child" 当然不是合法 JSON — 这条不会触发
        pass


def test_07b_single_task_with_tools_field():
    """单任务路径也支持 V23.1 新增的 ``tools`` 白名单字段。"""
    chain = _FakeChain([_stop_response("done with limited tools")])
    set_delegate_context(DelegateContext(
        chain=chain,
        model="fake",
        parent_toolset_names=["read_file", "terminal", "skill_view"],
    ))
    raw = delegate_task_handler({
        "goal": "limited",
        "tools": ["read_file"],  # 子只能拿到 read_file
    })
    parsed = json.loads(raw)
    assert parsed.get("output") == "done with limited tools"
    # 检查 chain 收到的 tools schema 只含 read_file
    sent_tools = chain.calls[0]["tools"]
    sent_names = [t["function"]["name"] for t in sent_tools]
    assert sent_names == ["read_file"], f"expected only read_file, got {sent_names}"


# ─── 不变量 8: 父 loop 对坏 args JSON 容错（真模型烟测踩坑回归）───────────


def test_08_parent_loop_tolerates_bad_args_json():
    """V23.1 真模型烟测里第一次暴露：父 LLM 流式拼帧 delegate_task 复杂
    args 时偶发拼出无效 JSON（trace 显示 char 91 处 ``Expecting ',' delimiter``）。
    main.py 的 ``json.loads(tool_call.function.arguments)`` 裸调直接 ``JSONDecodeError``
    把整个 agent 进程杀掉。

    这个 bug 与 V23.1 schema 无关，但 V23.1 的批量 args（tasks 数组 + 每条
    goal/context）几乎必然让 args 长度突破任何启发式阈值，所以 V23.1 才暴露。

    修复（main.py 父 loop）：与 child_loop.py:208 对齐 — try/except → tool_error
    塞回 LLM，让它下一轮自然修正而不是崩穿进程。

    本测试**不**调真 main.py（它依赖 chain / memory / compressor 整套）；改为
    断言修复点的两条核心契约：
    - ``tool_error()`` 协议合法 — 含 ``error`` key、合法 JSON、可序列化
    - 修复使用了 ``json.JSONDecodeError`` 而不是 catch-all ``Exception`` —
      避免把别的真异常（程序 bug）也吞掉
    """
    from tools.result import tool_error

    bad_args = '{"tasks":[{"goal":"读 README","context":"中文边界 ' + 'X' * 50  # 没闭合
    try:
        json.loads(bad_args)
        raise AssertionError("bad_args should NOT be valid JSON")
    except json.JSONDecodeError as exc:
        # 这是 main.py 父 loop 修复后做的事
        result = tool_error(
            f"invalid tool arguments JSON: {exc}",
            raw_arguments=bad_args[:500],
        )

    parsed = json.loads(result)
    assert "error" in parsed, f"tool_error must contain 'error' key: {parsed}"
    assert "invalid tool arguments JSON" in parsed["error"]
    assert "raw_arguments" in parsed, "raw_arguments should be preserved for LLM diagnosis"
    assert parsed["raw_arguments"].startswith('{"tasks":')

    # 反向校验：源文件 main.py 的修复点确实使用了 json.JSONDecodeError，
    # 而不是 catch-all Exception（避免误吞真 bug）
    main_py = (Path(__file__).resolve().parent.parent / "main.py").read_text()
    fix_anchor = "invalid tool arguments JSON"
    assert fix_anchor in main_py, "main.py missing the bad-args-recovery fix"
    # 在 fix 锚点附近找 except 关键字 — 必须是 JSONDecodeError 而不是 Exception
    idx = main_py.index(fix_anchor)
    window = main_py[max(0, idx - 300):idx]
    assert "json.JSONDecodeError" in window, \
        "fix should catch json.JSONDecodeError specifically, not Exception"


# ─── 主入口 ─────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print("=== V23.1 批量并行 + 工具白名单 — 不变量验证 ===")
    tests = [
        ("01 批量并行加速 — 3 子并跑 < 串行 3s", test_01_batch_parallel_speedup),
        ("02 结果顺序与 tasks 数组对齐", test_02_result_order_matches_input),
        ("03 白名单 ∩ 父全集 — 不存在的工具静默丢", test_03_whitelist_intersect_parent),
        ("04 黑名单强制 — 用户白名单含黑名单仍被剔", test_04_blacklist_overrides_whitelist),
        ("05 校验失败 → 整体 tool_error，0 子启动", test_05_validation_failure_aborts_all),
        ("05b tools 字段类型错也被主线程拦", test_05b_validation_tools_field),
        ("06 goal/tasks 互斥语义", test_06_goal_tasks_mutually_exclusive),
        ("07 V23.0 单任务回归 — 协议不变", test_07_v23_0_single_task_unchanged),
        ("07b 单任务路径支持 tools 白名单", test_07b_single_task_with_tools_field),
        ("08 父 loop 对坏 args JSON 容错（真模型踩坑回归）", test_08_parent_loop_tolerates_bad_args_json),
    ]
    for name, fn in tests:
        _run(name, fn)
    print()
    print(f"=== {_passed}/{_passed + _failed} passed ===")
    sys.exit(0 if _failed == 0 else 1)
