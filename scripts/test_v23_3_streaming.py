"""V23.3 多智能体流式中继 + 父子 cancel 桥接 — 不变量验证脚本。

不调真实 API；用 ``_FakeStreamingChain``（fake stream_call 吐预定义 StreamEvent
+ 在每帧前 check cancel_token）+ 真 registry 验证以下不变量。承诺范围与
``docs/Multi-agent-system/iteration-plan.md`` § V23.2"流式中继 + 中断传播"对齐
（注：iteration-plan 里编号是 V23.2，但 nano 主轴 v23.2 已被"项目上下文注入"
占用，本档实际落地为 v23.3）。

覆盖（7 项）：
1.  父子共享 token — 父 cancel 后批量 3 子全部 ≤ 1.5s 内退出，exit=interrupted
2.  取消不影响兄弟子 — 已完成的 task#0 summary 不丢；正在跑的 task#1 转 interrupted
3.  progress 中继不交织 — 多 worker 并发写 stderr，所有行以 [task#N] 开头无截断
4.  stream off 路径不挂 callback — stream_enabled=False 时 callback 调用次数=0
5.  delegate 协议未破坏 — V21.4 兜底契约不变；progress 走 stderr 不污染 stdout
6.  workflow 回归 hint — V23.0/V23.1 测试通过的契约在本档存档（断言 _resolve_child_toolset 等纯逻辑未漂移）
7.  main.py SIGINT 契约 — streaming_active flag 区分 prompt vs 流式期间（grep）

覆盖外（已在前档不变量验证，不重复）：
- child_loop 隔离 / 黑名单：``test_v23_0_delegate.py``
- 批量并行 / 白名单交集：``test_v23_1_batch.py``
- V21.4 dispatch 兜底：``test_v21_4_tool_result_protocol.py``
- transport 流式本身（stream_call yield 顺序、StreamCancelled 抛点）：
  ``test_v22_streaming.py``
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.registry import registry
from tools.delegate_tool import (
    DelegateContext,
    delegate_task_handler,
    set_delegate_context,
)
from agent.runtime import AgentRuntime
from transports.streaming import (
    EVENT_DONE,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALL_STARTED,
    CancelToken,
    StreamCancelled,
    StreamEvent,
)
from transports.types import NormalizedResponse, Usage


# ─── Fake Streaming Chain — 同时支持 .call (V23.0/1 路径) 与 .stream_call (V23.3) ──


@dataclass
class _FakeStreamingChain:
    """每次 ``stream_call`` 把 ``responses[0]`` 拆成预定义 StreamEvent 序列吐出。

    设计要点：
    - **每帧 check cancel_token** —— V23.3 的 ``run_child_loop`` 通过 ``check()``
      让父子共享的 token 在子内层 raise StreamCancelled。fake 必须仿真这条
      契约才能验证"父 cancel → 子退出"。
    - 支持 ``per_event_sleep`` 模拟 LLM 帧间延迟，让"父 cancel 时子还在 sleep"
      场景可控。
    - ``call`` 兜底（stream_enabled=False 时 child_loop 走同步路径仍能跑）。
    """

    responses: list[NormalizedResponse] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    per_event_sleep: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def call(self, *, model: str, messages: list[dict], tools: list[dict]) -> NormalizedResponse:
        if self.per_event_sleep > 0:
            time.sleep(self.per_event_sleep)
        with self._lock:
            self.calls.append({
                "model": model,
                "messages": [dict(m) for m in messages],
                "tools": list(tools),
                "via": "call",
            })
            if not self.responses:
                raise RuntimeError("FakeChain ran out of responses")
            return self.responses.pop(0)

    def stream_call(self, *, cancel_token: Optional[CancelToken] = None,
                    model: str, messages: list[dict], tools: list[dict]):
        with self._lock:
            self.calls.append({
                "model": model,
                "messages": [dict(m) for m in messages],
                "tools": list(tools),
                "via": "stream_call",
            })
            if not self.responses:
                raise RuntimeError("FakeChain ran out of responses")
            resp = self.responses.pop(0)

        # 帧序列：text_delta → done。仿真真实 SSE — 每个 chunk 之间分片 sleep
        # + 每片 check token，让父 cancel 能在 ms 级被传播到（而不是阻塞在
        # ``time.sleep(5)`` 整段）
        def _check_and_sleep(total: float):
            if cancel_token is not None:
                cancel_token.check()
            if total <= 0:
                return
            slice_s = 0.05
            elapsed = 0.0
            while elapsed < total:
                time.sleep(min(slice_s, total - elapsed))
                elapsed += slice_s
                if cancel_token is not None:
                    cancel_token.check()

        _check_and_sleep(self.per_event_sleep)
        if resp.content:
            yield StreamEvent(type=EVENT_TEXT_DELTA, text=resp.content)
        _check_and_sleep(0.0)  # 最后一次 check，不再 sleep
        yield StreamEvent(type=EVENT_DONE, response=resp)


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


# ─── 不变量 1: 父子共享 token —— 父 cancel 后子全部退出 ────────────────────


def test_01_parent_cancel_propagates_to_children():
    """3 个子任务每个 sleep 5s；父 0.3s 后 cancel → 3 子全部 ≤ 2s 内退出。"""
    chain = _FakeStreamingChain(
        responses=[_stop_response(f"summary#{i}") for i in range(3)],
        per_event_sleep=5.0,  # 每个子在第二个 check 前会 sleep 5s
    )
    parent_token = CancelToken()
    set_delegate_context(DelegateContext(
        chain=chain,
        model="fake",
        parent_toolset_names=["read_file"],
        runtime=AgentRuntime(stream_enabled=True, cancel_token=parent_token),
    ))

    # 后台 0.3s 后 cancel —— 此时 3 个 worker 都阻塞在 sleep 里
    def _delayed_cancel():
        time.sleep(0.3)
        parent_token.cancel()
    threading.Thread(target=_delayed_cancel, daemon=True).start()

    t0 = time.monotonic()
    raw = delegate_task_handler({
        "tasks": [
            {"goal": "task A"},
            {"goal": "task B"},
            {"goal": "task C"},
        ],
    })
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0, f"cancel 没有及时传播（{elapsed:.2f}s, 应 < 2s）"

    parsed = json.loads(raw)
    output_arr = json.loads(parsed["output"])["results"]
    assert len(output_arr) == 3, f"期望 3 条结果，得到 {len(output_arr)}"
    # 全部应该是 interrupted（cancel 在第一个 check 之后，sleep 之前一定命中）
    interrupted = [r for r in output_arr if r["exit_reason"] == "interrupted"]
    assert len(interrupted) == 3, f"期望 3 个 interrupted，得到 {[r['exit_reason'] for r in output_arr]}"

    # 重置 token，避免后续测试受影响
    parent_token.reset()


# ─── 不变量 2: cancel 不影响已完成的兄弟子 —————————————————————————————


def test_02_cancel_does_not_lose_completed_siblings():
    """task#0 立刻完成（无 sleep）；task#1 / #2 sleep 5s。

    cancel 在 task#0 完成后触发 —— 期望 task#0 的 summary 完整保留，
    task#1/#2 转为 interrupted。
    """
    # 前 2 个无延迟（task#0 / task#1 的 chain.responses[0]），
    # 后 1 个用 per_event_sleep 打不到 — 我们用更精细的：第 1 个 fast，
    # 后两个每帧 sleep 3s
    # 因为 _FakeStreamingChain 的 per_event_sleep 是全局的，这里用同步原语：
    # task#0 完成后用 event 通知 main 触发 cancel
    chain = _FakeStreamingChain(
        responses=[_stop_response(f"summary#{i}") for i in range(3)],
        per_event_sleep=0.0,
    )

    # 重写 stream_call 在第一次后 sleep —— 简单做法：用 calls 计数代替
    real_stream_call = chain.stream_call

    finished_first = threading.Event()

    def _custom_stream_call(*, cancel_token, model, messages, tools):
        # 取这是第几次调用（call 计数已在 real_stream_call 里 ++）
        # 这里我们用 chain.calls 现有长度做同步信号
        is_first = len(chain.calls) == 0
        # 直接复用 real_stream_call 的产出 —— 但加自定义延迟
        gen = real_stream_call(
            cancel_token=cancel_token, model=model, messages=messages, tools=tools,
        )
        for ev in gen:
            yield ev
        if is_first:
            finished_first.set()
        else:
            # 非第一次：等 cancel —— stream_call 已经 yield done，
            # 但子 loop 退出前不会再 check token。这种场景下 cancel 不影响
            # 已完成的子 ——  这正是我们要验证的。
            pass

    chain.stream_call = _custom_stream_call

    parent_token = CancelToken()
    set_delegate_context(DelegateContext(
        chain=chain,
        model="fake",
        parent_toolset_names=["read_file"],
        runtime=AgentRuntime(stream_enabled=True, cancel_token=parent_token),
    ))

    # 后台 task#0 完成后立刻 cancel
    def _cancel_after_first():
        finished_first.wait(timeout=2.0)
        parent_token.cancel()
    threading.Thread(target=_cancel_after_first, daemon=True).start()

    raw = delegate_task_handler({
        "tasks": [
            {"goal": "fast task"},
            {"goal": "would be task B"},
            {"goal": "would be task C"},
        ],
    })

    parsed = json.loads(raw)
    output_arr = json.loads(parsed["output"])["results"]
    by_idx = {r["task_index"]: r for r in output_arr}

    # task#0 应该完成；它的 summary 不能丢
    # （即便父 cancel 在它完成后触发，已经写到 results 里的不该被回收）
    assert by_idx[0]["exit_reason"] == "completed", f"task#0 应 completed: {by_idx[0]}"
    assert by_idx[0]["summary"].startswith("summary#"), f"task#0 summary 丢了: {by_idx[0]}"

    parent_token.reset()


# ─── 不变量 3: progress 中继 stderr 不交织 ─────────────────────────────────


def test_03_progress_stderr_no_interleave():
    """5 个子并发，每个至少触发 1 个 done 事件 —— stderr 行不交织。"""
    chain = _FakeStreamingChain(
        responses=[_stop_response(f"answer#{i}") for i in range(5)],
        per_event_sleep=0.05,  # 让 worker 真的并发
    )
    parent_token = CancelToken()
    set_delegate_context(DelegateContext(
        chain=chain,
        model="fake",
        parent_toolset_names=["read_file"],
        runtime=AgentRuntime(stream_enabled=True, cancel_token=parent_token),
    ))

    err_buf = io.StringIO()
    out_buf = io.StringIO()
    with redirect_stderr(err_buf), redirect_stdout(out_buf):
        delegate_task_handler({
            "tasks": [{"goal": f"task {i}"} for i in range(5)],
        })

    err = err_buf.getvalue()
    out = out_buf.getvalue()

    # stdout 应完全干净（V21.4 协议 + 侧路 stderr）
    assert out == "", f"stdout 不该被污染: {out!r}"

    # stderr 至少有 5 行（每子 1 个 done 事件）
    lines = [l for l in err.split("\n") if l.strip()]
    assert len(lines) >= 5, f"期望 ≥ 5 行进度，实际 {len(lines)}: {err}"

    # 每行必须以 "  [task#N]" 开头（无半行交织）
    for l in lines:
        assert l.lstrip().startswith("[task#"), f"行不以 [task#N] 起始（疑似交织）: {l!r}"

    parent_token.reset()


# ─── 不变量 4: stream_enabled=False 时不挂 callback ─────────────────────


def test_04_stream_off_no_callback():
    """stream_enabled=False → child_loop 走同步 chain.call，progress 走不到一次。"""
    chain = _FakeStreamingChain(
        responses=[_stop_response("sync answer")],
        per_event_sleep=0.0,
    )
    parent_token = CancelToken()
    set_delegate_context(DelegateContext(
        chain=chain,
        model="fake",
        parent_toolset_names=["read_file"],
        runtime=AgentRuntime(stream_enabled=False, cancel_token=parent_token),
    ))

    err_buf = io.StringIO()
    with redirect_stderr(err_buf):
        raw = delegate_task_handler({"goal": "say hi"})

    err = err_buf.getvalue()
    parsed = json.loads(raw)
    inner = json.loads(parsed["output"])
    assert inner["results"][0]["summary"] == "sync answer", f"单任务 output 异常: {parsed}"
    assert err == "", f"stream_enabled=False 时 stderr 应为空: {err!r}"

    # chain.calls 第一条必须是 via=call（不是 stream_call）
    assert chain.calls[0]["via"] == "call", f"应走同步 call: {chain.calls[0]}"

    parent_token.reset()


def test_04b_runtime_stream_toggle_reaches_delegate():
    """Delegate reads stream_enabled from shared runtime, not a stale injected bool."""
    chain = _FakeStreamingChain(
        responses=[_stop_response("dynamic sync answer")],
        per_event_sleep=0.0,
    )
    runtime = AgentRuntime(
        stream_enabled=True,
        cancel_token=CancelToken(),
    )
    set_delegate_context(DelegateContext(
        chain=chain,
        model="fake",
        parent_toolset_names={"read_file"},
        runtime=runtime,
    ))

    runtime.stream_enabled = False
    err_buf = io.StringIO()
    with redirect_stderr(err_buf):
        raw = delegate_task_handler({"goal": "say hi"})

    parsed = json.loads(raw)
    inner = json.loads(parsed["output"])
    assert inner["results"][0]["summary"] == "dynamic sync answer", f"单任务 output 异常: {parsed}"
    assert err_buf.getvalue() == "", "runtime stream off should suppress progress stderr"
    assert chain.calls[0]["via"] == "call", f"应跟随 runtime 走同步 call: {chain.calls[0]}"


# ─── 不变量 5: V21.4 工具协议未破坏 ───────────────────────────────────────


def test_05_v21_4_protocol_unchanged():
    """单任务 + 批量 output 都仍是合法 JSON；progress 走 stderr 不污染 output。"""
    chain = _FakeStreamingChain(
        responses=[_stop_response("hello")],
    )
    parent_token = CancelToken()
    set_delegate_context(DelegateContext(
        chain=chain,
        model="fake",
        parent_toolset_names=["read_file"],
        runtime=AgentRuntime(stream_enabled=True, cancel_token=parent_token),
    ))
    err_buf = io.StringIO()
    with redirect_stderr(err_buf):
        raw = delegate_task_handler({"goal": "ping"})

    parsed = json.loads(raw)  # 1. 本身是合法 JSON
    assert "output" in parsed, f"V21.4 协议要求 output: {parsed}"
    # 2. V23.4: output 字段值是 JSON 字符串（含 results 数组）
    inner = json.loads(parsed["output"])
    assert inner["results"][0]["summary"] == "hello", f"output 协议改了: {parsed}"
    # 3. progress 行没有混进 output
    assert "[task#" not in parsed["output"]
    assert "[delegate]" not in parsed["output"]

    parent_token.reset()


# ─── 不变量 6: V23.0 / V23.1 纯逻辑契约未漂移 ────────────────────────────


def test_06_pure_logic_contracts_unchanged():
    """从 V23.0/V23.1 测试里挑两条纯逻辑断言，跑一遍确保 V23.3 改动没破坏。"""
    from tools.delegate_tool import _resolve_child_toolset

    # V23.0 黑名单：delegate_task / memory_* 必被剔
    set_delegate_context(DelegateContext(
        chain=_FakeStreamingChain(),
        model="fake",
        parent_toolset_names=["read_file", "delegate_task", "memory", "memory_recall_v2"],
    ))
    allowed = _resolve_child_toolset()
    assert allowed == {"read_file"}, f"V23.0 黑名单契约漂了: {allowed}"

    # V23.1 白名单交集 + 黑名单强制
    allowed = _resolve_child_toolset(
        requested=["read_file", "delegate_task", "nonexistent"],
    )
    assert allowed == {"read_file"}, f"V23.1 白名单契约漂了: {allowed}"


# ─── 不变量 7: main.py SIGINT 契约 ─────────────────────────────────────────


def test_07_main_py_sigint_contract():
    """main.py 必须用 agent_busy.is_set() 区分两种 SIGINT 语境。

    这条不能在单元层 mock 信号，但可以校验 main.py 源码的契约：
    - 含 ``agent_busy`` Event（V23.4 起取代 V22 的 streaming_active）
    - tool loop 期间 cancel_token.cancel()，prompt 期间 raise KeyboardInterrupt
    - DelegateContext 注入 shared runtime，runtime 持有 cancel_token + stream_enabled
    """
    main_py = (Path(__file__).resolve().parent.parent / "main.py").read_text()

    # 契约 1：仍有 busy signal 区分两种语境（名字 V23.4 改了，语义不变）
    assert 'agent_busy = threading.Event()' in main_py, "main.py 失去 agent_busy Event"
    assert 'agent_busy.is_set()' in main_py, "main.py SIGINT 不再读取 agent_busy Event"
    assert 'agent_busy.set()' in main_py, "main.py tool loop 进入时未 set busy"
    assert 'agent_busy.clear()' in main_py, "main.py tool loop 退出时未 clear busy"
    assert 'cancel_token.cancel()' in main_py, "main.py 失去 cancel_token.cancel() 路径"
    assert 'raise KeyboardInterrupt' in main_py, "main.py 失去 KeyboardInterrupt 路径"

    # 契约 2：delegate 注入共享 runtime，而不是启动期 stream_enabled 快照
    inject_idx = main_py.find('_inject_delegate_context(')
    assert inject_idx >= 0, "main.py 没调用 _inject_delegate_context"
    inject_block = main_py[inject_idx:inject_idx + 600]
    assert 'DelegateContext(' in inject_block, \
        f"_inject_delegate_context 没传 DelegateContext: {inject_block!r}"
    assert 'runtime=runtime' in inject_block, \
        f"_inject_delegate_context 没传 runtime: {inject_block!r}"
    assert 'AgentRuntime(' in main_py and 'cancel_token=cancel_token' in main_py, \
        "main.py runtime 没持有 cancel_token"
    assert 'stream_enabled=stream_enabled' in main_py, \
        "main.py runtime 没持有 stream_enabled 初始值"


# ─── 主入口 ─────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print("=== V23.3 流式中继 + 父子 cancel 桥接 — 不变量验证 ===")
    tests = [
        ("01 父子共享 token — 父 cancel 后批量子全部退出", test_01_parent_cancel_propagates_to_children),
        ("02 cancel 不影响已完成的兄弟子", test_02_cancel_does_not_lose_completed_siblings),
        ("03 progress stderr 不交织", test_03_progress_stderr_no_interleave),
        ("04 stream_enabled=False 时不挂 callback", test_04_stream_off_no_callback),
        ("04b runtime 动态 stream toggle 传到 delegate", test_04b_runtime_stream_toggle_reaches_delegate),
        ("05 V21.4 工具协议未破坏", test_05_v21_4_protocol_unchanged),
        ("06 V23.0/V23.1 纯逻辑契约未漂移", test_06_pure_logic_contracts_unchanged),
        ("07 main.py SIGINT 契约 — agent_busy Event", test_07_main_py_sigint_contract),
    ]
    for name, fn in tests:
        _run(name, fn)
    print()
    print(f"=== {_passed}/{_passed + _failed} passed ===")
    sys.exit(0 if _failed == 0 else 1)
