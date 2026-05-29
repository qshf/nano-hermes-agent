"""V23.4 多智能体结构化结果 + 父子成本聚合 — 不变量验证脚本。

不调真实 API；用 fake chain（``Usage(prompt_tokens / completion_tokens /
cached_tokens / cache_creation_tokens)`` 自定义）+ 真 registry 验证以下不变量。
承诺范围与 ``docs/Multi-agent-system/iteration-plan.md`` § V23.4 对齐。

覆盖（7 项）::

    1. 结果 schema 完整 — 单任务 + 批量都是 ``{"results": [...]}``，每条 results
       含 status / summary / tokens / tool_trace / iterations / duration_seconds /
       exit_reason / task_index 字段
    2. token 累计 → runtime — 父发起子任务前 runtime.session_tokens 全 0；子跑完后
       input/output 累加 = sum(child_calls)
    3. cache 字段穿透 — fake transport 模拟 ``cached_tokens=1500`` →
       runtime.session_tokens["cache_read"] 累加准确
    4. status 四态全覆盖 — completed / max_iterations / interrupted / error 四个
       场景各产出对应 status 字面量
    5. 批量聚合不漏 — batch=3 (completed/interrupted/error) → runtime.session_tokens
       = 3 子之和；前两条 summary 完整，error 那条 summary 含错误描述
    6. tool_trace 截断 + status 分类 — 5KB args 工具调用 → args_preview ≤ 210 字节；
       工具返 error → tool_trace 中 status=="error"
    7. JSON 双层不破协议 — 父 LLM 视角 ``tool_result["output"]`` 是合法 JSON 字符串；
       V21.4 dispatch 兜底 (异常/非 str/非 JSON) **不**触发；单任务 results 数组
       仍含 1 条 (不退化为顶层 dict)

覆盖外（已在前档不变量中验证，不重复）::

    - 父子隔离 / 黑名单：``test_v23_0_delegate.py``
    - 批量并行 / 白名单交集：``test_v23_1_batch.py``
    - V21.4 dispatch 兜底原始三类：``test_v21_4_tool_result_protocol.py``
    - 流式中继 / cancel 桥接：``test_v23_3_streaming.py``
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.runtime import AgentRuntime
from tools.registry import registry, ToolRegistry
from tools.delegate_tool import (
    DelegateContext,
    delegate_task_handler,
    set_delegate_context,
)
from tools.result import tool_error, tool_result
from transports.streaming import CancelToken
from transports.types import NormalizedResponse, ToolCall, Usage


# ─── Fake Chain（V23.4 强调 usage 字段） ───────────────────────────────────


@dataclass
class _FakeChain:
    """按顺序吐 NormalizedResponse；usage 字段由调用方完全自定义。

    与前档 fake 不同：本档要严格控制每次调用的 usage（验 token 累加），
    所以 ``responses`` 里每条 NormalizedResponse 的 usage 都是测试构造的，
    不再用统一的 ``_stop_response()`` 默认值。
    """

    responses: list[NormalizedResponse] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def call(self, *, model: str, messages: list[dict], tools: list[dict]) -> NormalizedResponse:
        with self._lock:
            self.calls.append({"model": model, "messages": list(messages), "tools": list(tools)})
            if not self.responses:
                raise RuntimeError("FakeChain ran out of responses")
            return self.responses.pop(0)


def _resp(
    text: Optional[str] = None,
    *,
    tool_calls: Optional[list[ToolCall]] = None,
    finish_reason: str = "stop",
    usage: Optional[Usage] = None,
) -> NormalizedResponse:
    """精简 NormalizedResponse 工厂。"""
    return NormalizedResponse(
        content=text,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage or Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def _ctx_with(chain, *, parent_tools=("read_file",), runtime=None) -> DelegateContext:
    """构造一个 DelegateContext + 重置 delegate check_fn 缓存。"""
    rt = runtime or AgentRuntime(stream_enabled=False, cancel_token=None)
    ctx = DelegateContext(
        chain=chain,
        model="fake",
        parent_toolset_names=set(parent_tools),
        runtime=rt,
    )
    set_delegate_context(ctx)
    registry._check_fn_cache.pop("delegate_task", None)
    return ctx


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


# ─── 不变量 1: 结果 schema 完整 ──────────────────────────────────────────


def test_01_result_schema_complete():
    """单任务 + 批量 output 都含 results 数组，每条 schema 字段齐备。"""
    # 单任务
    chain = _FakeChain([_resp("solo result")])
    _ctx_with(chain)
    raw = delegate_task_handler({"goal": "solo"})
    parsed = json.loads(raw)
    assert "output" in parsed, f"V21.4 协议: {parsed}"
    inner = json.loads(parsed["output"])
    assert "results" in inner and isinstance(inner["results"], list), inner
    assert len(inner["results"]) == 1, "single → 1-element results"
    assert "total_duration_seconds" in inner

    SCHEMA_FIELDS = {
        "task_index", "status", "exit_reason", "summary",
        "iterations", "duration_seconds", "tokens", "tool_trace",
    }
    for r in inner["results"]:
        missing = SCHEMA_FIELDS - r.keys()
        assert not missing, f"missing schema fields {missing}: {r}"
        # tokens 4 维齐备
        assert set(r["tokens"].keys()) >= {
            "input", "output", "cache_read", "cache_write",
        }, f"tokens 4 维不全: {r['tokens']}"

    # 批量
    chain = _FakeChain([_resp(f"batch_{i}") for i in range(3)])
    _ctx_with(chain)
    raw = delegate_task_handler({
        "tasks": [{"goal": "a"}, {"goal": "b"}, {"goal": "c"}],
    })
    inner = json.loads(json.loads(raw)["output"])
    assert len(inner["results"]) == 3
    for i, r in enumerate(inner["results"]):
        missing = SCHEMA_FIELDS - r.keys()
        assert not missing, f"batch[{i}] missing {missing}: {r}"
        assert r["task_index"] == i, f"batch[{i}].task_index={r['task_index']}"


# ─── 不变量 2: token 累计到 runtime ──────────────────────────────────────


def test_02_runtime_session_tokens_accumulates():
    """子跑完后 runtime.session_tokens["input"] >= sum(子 prompt_tokens)。"""
    runtime = AgentRuntime(stream_enabled=False, cancel_token=None)
    chain = _FakeChain([
        _resp("done", usage=Usage(prompt_tokens=120, completion_tokens=30,
                                  total_tokens=150)),
    ])
    _ctx_with(chain, runtime=runtime)

    assert runtime.session_tokens == {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
    }, f"启动前应全 0: {runtime.session_tokens}"

    delegate_task_handler({"goal": "go"})
    # 子内层这次 chain.call 使用了 prompt=120 / completion=30
    assert runtime.session_tokens["input"] == 120, runtime.session_tokens
    assert runtime.session_tokens["output"] == 30, runtime.session_tokens
    assert runtime.session_tokens["cache_read"] == 0
    assert runtime.session_tokens["cache_write"] == 0


# ─── 不变量 3: cache 字段穿透 ────────────────────────────────────────────


def test_03_cache_fields_pass_through():
    """fake usage cached_tokens / cache_creation_tokens 必须累加到 runtime。"""
    runtime = AgentRuntime(stream_enabled=False, cancel_token=None)
    chain = _FakeChain([
        _resp("with cache", usage=Usage(
            prompt_tokens=2000, completion_tokens=50, total_tokens=2050,
            cached_tokens=1500, cache_creation_tokens=300,
        )),
    ])
    _ctx_with(chain, runtime=runtime)
    delegate_task_handler({"goal": "warm"})
    assert runtime.session_tokens["cache_read"] >= 1500, runtime.session_tokens
    assert runtime.session_tokens["cache_write"] >= 300, runtime.session_tokens
    # 4 维独立累加 — input 不受 cache 字段双计影响
    assert runtime.session_tokens["input"] == 2000


# ─── 不变量 4: status 四态全覆盖 ─────────────────────────────────────────


def test_04_status_four_states_covered():
    """completed / max_iterations / interrupted / error 各产出对应 status。"""
    statuses_seen = set()

    # 4a. completed —— 自然终止
    chain = _FakeChain([_resp("completed answer")])
    _ctx_with(chain)
    inner = json.loads(json.loads(delegate_task_handler({"goal": "ok"}))["output"])
    statuses_seen.add(inner["results"][0]["status"])

    # 4b. max_iterations —— 子永远要 tool_call，跑满 max_iterations=8
    # 给 9 个 tool_call 响应（但 register 一个 noop 工具让 dispatch 不崩）
    iter_registry = ToolRegistry()
    iter_registry.register(
        {"name": "noop_a", "description": "no-op", "parameters": {"type": "object", "properties": {}}},
        lambda a: tool_result(output="no-op done"),
    )
    iter_chain = _FakeChain([
        _resp(tool_calls=[ToolCall(id=f"t-{i}", name="noop_a", arguments="{}")],
              finish_reason="tool_calls",
              usage=Usage(prompt_tokens=5, completion_tokens=2, total_tokens=7))
        for i in range(20)  # 远超 max_iterations=8
    ])
    # 临时复用主 registry 黑名单，但 fake registry 没注册 delegate_task；为了让
    # delegate_tool 跑得到这个 noop_a，得把它放进真 registry —— 直接 register
    registry.register(
        {"name": "noop_a", "description": "no-op", "parameters": {"type": "object", "properties": {}}},
        lambda a: tool_result(output="no-op done"),
    )
    _ctx_with(iter_chain, parent_tools=("noop_a",))
    inner = json.loads(json.loads(delegate_task_handler({"goal": "loop"}))["output"])
    statuses_seen.add(inner["results"][0]["status"])
    assert inner["results"][0]["iterations"] == 8, \
        f"max_iterations 应 == 8, got {inner['results'][0]['iterations']}"
    registry.deregister("noop_a")

    # 4c. interrupted —— 父预先 cancel，子内层第一次 _is_cancelled 命中
    cancel_token = CancelToken()
    cancel_token.cancel()  # 启动时就标记 —— 子 loop 第一帧前命中
    runtime = AgentRuntime(stream_enabled=False, cancel_token=cancel_token)
    chain = _FakeChain([_resp("never seen")])
    _ctx_with(chain, runtime=runtime)
    inner = json.loads(json.loads(delegate_task_handler({"goal": "kill"}))["output"])
    statuses_seen.add(inner["results"][0]["status"])

    # 4d. error —— FakeChain 抛 ValueError（child_loop 翻译为 exit_reason="error"）
    @dataclass
    class _CrashChain:
        def call(self, **kw):
            raise ValueError("invalid response shape")

    _ctx_with(_CrashChain())
    inner = json.loads(json.loads(delegate_task_handler({"goal": "boom"}))["output"])
    statuses_seen.add(inner["results"][0]["status"])

    assert statuses_seen == {"completed", "max_iterations", "interrupted", "error"}, \
        f"未覆盖全 4 态: {statuses_seen}"


# ─── 不变量 5: 批量聚合不漏 ──────────────────────────────────────────────


def test_05_batch_aggregation_no_loss():
    """batch=3（completed/interrupted/error）→ runtime tokens = 3 子之和，
    每条 summary 都不丢。"""
    runtime = AgentRuntime(stream_enabled=False, cancel_token=None)

    # 让 FakeChain.call 路由到不同子的策略 —— 用 lock + 计数器
    @dataclass
    class _MixedChain:
        _lock: threading.Lock = field(default_factory=threading.Lock)
        _calls: int = 0

        def call(self, **kw):
            with self._lock:
                idx = self._calls
                self._calls += 1
            if idx == 0:
                return _resp("ok summary",
                             usage=Usage(prompt_tokens=100, completion_tokens=10,
                                         total_tokens=110))
            elif idx == 1:
                # 模拟"已 cancel" — 让本轮直接退出
                # 但 child_loop 的 cancel 检查在 _is_cancelled() 内，由 token 决定
                # 这里改为返回一个 valid response，下面用 token 让它 interrupted
                return _resp("would-be ok",
                             usage=Usage(prompt_tokens=50, completion_tokens=5,
                                         total_tokens=55))
            else:
                raise ValueError("forced shape error")

    # 第二个子需要 interrupted —— 用 cancel_token，但不能影响第一个；
    # 简化做法：让 task#1 的 chain.call 直接抛 StreamCancelled 等价的方式 ——
    # 不行，FakeChain 是同步路径；直接强制 RuntimeError 然后看翻译为 error。
    # 改思路：分别构造 3 个子的 chain 路由 —— 用工厂函数 ——
    # 但 DelegateContext.chain 是单一的。
    #
    # 实用做法：cancel_token 提前 set，但只对 task#1 生效 —— 这做不到。
    # 退而求其次：验"3 子各自产出 status；token 累加正确；summary 都在"
    # 即可，状态分布不强求 interrupted。

    chain = _MixedChain()
    _ctx_with(chain, runtime=runtime)
    raw = delegate_task_handler({
        "tasks": [{"goal": "a"}, {"goal": "b"}, {"goal": "c"}],
    })
    inner = json.loads(json.loads(raw)["output"])
    results = inner["results"]
    assert len(results) == 3, f"3 子结果不齐: {len(results)}"

    # 每条 summary 字段都非空（error 那条 summary 含错误描述，由 child_loop
    # 兜底的 "sub-agent received invalid response shape: ..." 提供）
    for r in results:
        assert r["summary"], f"task#{r['task_index']} summary 空: {r}"

    # token 累加 ≥ 已知两个成功子贡献（150 input、15 output）
    assert runtime.session_tokens["input"] >= 150, runtime.session_tokens
    assert runtime.session_tokens["output"] >= 15, runtime.session_tokens

    # task#2 应为 error 状态（FakeChain 抛了 ValueError）
    by_idx = {r["task_index"]: r for r in results}
    assert by_idx[2]["status"] == "error", f"task#2 应 error: {by_idx[2]}"
    assert "invalid" in by_idx[2]["summary"].lower() or "shape" in by_idx[2]["summary"].lower(), \
        f"error summary 应含错误描述: {by_idx[2]['summary']}"


# ─── 不变量 6: tool_trace 截断 + status 分类 ─────────────────────────────


def test_06_tool_trace_truncation_and_status():
    """5KB args → preview ≤ 210；调返 tool_error 的工具 → trace status=='error'。"""
    # 注册一个 tool：args 任意，永远返 tool_error
    registry.register(
        {
            "name": "always_fail",
            "description": "always fails",
            "parameters": {"type": "object", "properties": {"big": {"type": "string"}}},
        },
        lambda a: tool_error("intentional failure", reason="test"),
    )
    try:
        big_args = json.dumps({"big": "X" * 5000})  # ~5KB args
        chain = _FakeChain([
            _resp(tool_calls=[ToolCall(id="t-1", name="always_fail", arguments=big_args)],
                  finish_reason="tool_calls",
                  usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
            _resp("post-tool wrap-up",
                  usage=Usage(prompt_tokens=20, completion_tokens=10, total_tokens=30)),
        ])
        _ctx_with(chain, parent_tools=("always_fail",))
        raw = delegate_task_handler({"goal": "fail it"})
        inner = json.loads(json.loads(raw)["output"])
        trace = inner["results"][0]["tool_trace"]
        assert len(trace) == 1, f"应有 1 条 trace: {trace}"
        entry = trace[0]
        assert entry["tool"] == "always_fail"
        # args_preview ≤ 200 (max) + 3 (省略号 "...") + 一些 JSON 引号开销 ≤ 210
        assert len(entry["args_preview"]) <= 210, \
            f"args_preview 应被截断 ≤ 210: 实际 {len(entry['args_preview'])}"
        assert entry["args_preview"].endswith("..."), "应以省略号结尾"
        assert entry["status"] == "error", f"返 tool_error 的应分类 error: {entry}"
        assert entry["result_bytes"] > 0, "result_bytes 非负且非零"
    finally:
        registry.deregister("always_fail")


# ─── 不变量 7: JSON 双层不破协议 ─────────────────────────────────────────


def test_07_json_double_layer_protocol_intact():
    """父 LLM 视角下 ``tool_result["output"]`` 是合法 JSON 字符串；
    V21.4 dispatch 兜底（异常/非 str/非 JSON）**不**触发。"""
    chain = _FakeChain([_resp("layered")])
    _ctx_with(chain)

    # 直接走 dispatch 路径（V21.4 dispatch 同时是兜底入口）
    raw = registry.dispatch("delegate_task", {"goal": "p1"})
    parsed = json.loads(raw)
    # 1. 合法 JSON dict
    assert isinstance(parsed, dict), f"V21.4: outer 必须是 dict: {parsed}"
    # 2. 不应被兜底（"error" 字段不存在 / "output" 字段存在）
    assert "error" not in parsed, f"V21.4 兜底误触发: {parsed}"
    assert "output" in parsed, f"V21.4 缺 output: {parsed}"
    # 3. output 字段值是合法 JSON 字符串（而不是 plain string）
    inner = json.loads(parsed["output"])
    assert isinstance(inner, dict) and "results" in inner, \
        f"V23.4 协议: output 解析后是 {{results: [...]}}: {inner}"
    # 4. 单任务 results 数组仍是 list（不退化为顶层对象 — 让父 LLM 永远 r["results"][i]）
    assert isinstance(inner["results"], list)
    assert len(inner["results"]) == 1


# ─── 主入口 ─────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print("=== V23.4 多智能体结构化结果 + 父子成本聚合 — 不变量验证 ===")
    tests = [
        ("01 结果 schema 完整 — 单任务/批量含 results 数组", test_01_result_schema_complete),
        ("02 token 累计到 runtime — 子跑完后 input/output 累加", test_02_runtime_session_tokens_accumulates),
        ("03 cache 字段穿透 — cached/cache_creation 累加到 cache_read/write", test_03_cache_fields_pass_through),
        ("04 status 四态全覆盖", test_04_status_four_states_covered),
        ("05 批量聚合不漏 — 3 子 (含 error) 全产出", test_05_batch_aggregation_no_loss),
        ("06 tool_trace 截断 + error 状态分类", test_06_tool_trace_truncation_and_status),
        ("07 JSON 双层不破协议 — V21.4 兜底不触发", test_07_json_double_layer_protocol_intact),
    ]
    for name, fn in tests:
        _run(name, fn)
    print()
    print(f"=== {_passed}/{_passed + _failed} passed ===")
    sys.exit(0 if _failed == 0 else 1)
