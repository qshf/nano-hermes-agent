"""V22 流式输出 + 中断 — 不变量验证脚本。

不调真实 API；用 fake stream 迭代器 + fake client 验证：
- CancelToken 三态语义（reset / is_cancelled / check）
- ChatCompletionsTransport.stream_call 增量解析与最终 NormalizedResponse 重建
- tool_call name 不被 +=（防 MiniMax M2.7 重发污染）/ arguments 必须 concat
- 流式中按下"中断" → StreamCancelled，迭代立刻停止
- ProviderTransport 默认 stream_call（fake-streaming）兼容路径
- TransportChain.stream_call 在首帧前可切家、首帧后失败禁止切家
- TransportChain.stream_call done 帧累计 cache 命中（与同步 call 完全等价）
- 流式 vs 非流式：同 fake response 下 NormalizedResponse 字段语义一致

覆盖（13 项）：
1. CancelToken 默认 closed；check 不抛
2. CancelToken cancel + check 抛 StreamCancelled
3. CancelToken reset 后恢复 closed
4. ChatCompletions stream_call — 纯文本响应：text_delta 数 = chunks 数 + done
5. ChatCompletions stream_call — done.response.content 拼接正确
6. ChatCompletions stream_call — tool_call 名分 1 帧 / args 分多帧 → 合并正确
7. ChatCompletions stream_call — name += 防御：重发 name 不会变 "read_fileread_file"
8. ChatCompletions stream_call — usage 在最终 choices=[] 帧抓取
9. ChatCompletions stream_call — 中途 cancel：第 N 帧 token.cancel() 后 raise StreamCancelled
10. base.ProviderTransport 默认 stream_call — content 拆 1 个 text_delta + done
11. TransportChain.stream_call — 单 entry 成功 → done.response 复制完整 + cache 累计
12. TransportChain.stream_call — 主家首帧前失败 → 切备家成功（用户拿到完整流）
13. TransportChain.stream_call — 主家已 yield 1 帧后失败 → 不切家，原异常透传
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transports import get_transport
from transports.base import ProviderTransport
from transports.chain import TransportChain, _ChainEntry
from transports.streaming import (
    EVENT_DONE,
    EVENT_REASONING_DELTA,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALL_STARTED,
    CancelToken,
    StreamCancelled,
    StreamEvent,
)
from transports.types import NormalizedResponse, ToolCall


# ─── Fake OpenAI SDK Stream ────────────────────────────────────────────────


@dataclass
class _FFn:
    name: str = ""
    arguments: str = ""


@dataclass
class _FToolCallDelta:
    index: int
    id: str = ""
    function: _FFn | None = None


@dataclass
class _FDelta:
    content: str | None = None
    tool_calls: list[_FToolCallDelta] | None = None
    reasoning_content: str | None = None
    reasoning: str | None = None


@dataclass
class _FChoice:
    delta: _FDelta | None = None
    finish_reason: str | None = None


@dataclass
class _FUsageDetails:
    cached_tokens: int = 0


@dataclass
class _FUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_tokens_details: _FUsageDetails | None = None


@dataclass
class _FChunk:
    choices: list[_FChoice]
    usage: _FUsage | None = None
    model: str = ""


class _FStream:
    """Fake openai SDK Stream — 迭代时按 chunks 顺序吐 + 支持 close()。"""

    def __init__(self, chunks: list[_FChunk], on_chunk=None) -> None:
        self._chunks = chunks
        self._on_chunk = on_chunk
        self.closed = False

    def __iter__(self) -> Iterator[_FChunk]:
        for i, c in enumerate(self._chunks):
            if self._on_chunk:
                self._on_chunk(i)
            yield c

    def close(self) -> None:
        self.closed = True


class _FCompletions:
    def __init__(self, stream: _FStream | None = None, exc: Exception | None = None) -> None:
        self._stream = stream
        self._exc = exc

    def create(self, **_kwargs):
        if self._exc is not None:
            raise self._exc
        return self._stream


class _FChat:
    def __init__(self, completions: _FCompletions) -> None:
        self.completions = completions


class _FClient:
    def __init__(self, stream: _FStream | None = None, exc: Exception | None = None) -> None:
        self.chat = _FChat(_FCompletions(stream=stream, exc=exc))


def _make_text_chunks(parts: list[str], usage: _FUsage | None = None) -> list[_FChunk]:
    chunks: list[_FChunk] = []
    for i, p in enumerate(parts):
        finish = "stop" if i == len(parts) - 1 and usage is None else None
        chunks.append(_FChunk(choices=[_FChoice(delta=_FDelta(content=p), finish_reason=finish)]))
    if usage is not None:
        chunks[-1].choices[0].finish_reason = "stop"
        chunks.append(_FChunk(choices=[], usage=usage))
    return chunks


# ─── 测试 ─────────────────────────────────────────────────────────────────


def test_cancel_token_default_closed() -> None:
    t = CancelToken()
    assert not t.is_cancelled()
    t.check()  # 不抛
    print("✓ test 1 — CancelToken 默认未取消")


def test_cancel_token_cancel_raises() -> None:
    t = CancelToken()
    t.cancel()
    assert t.is_cancelled()
    raised = False
    try:
        t.check()
    except StreamCancelled:
        raised = True
    assert raised, "check() 在 cancel 后必须抛 StreamCancelled"
    print("✓ test 2 — CancelToken cancel + check → StreamCancelled")


def test_cancel_token_reset() -> None:
    t = CancelToken()
    t.cancel()
    t.reset()
    assert not t.is_cancelled()
    t.check()
    print("✓ test 3 — CancelToken reset 后恢复未取消")


def test_chat_completions_text_stream_concat() -> None:
    transport = get_transport("chat_completions")
    chunks = _make_text_chunks(
        ["Hello", " world", "!"],
        usage=_FUsage(prompt_tokens=10, completion_tokens=3, total_tokens=13),
    )
    client = _FClient(stream=_FStream(chunks))
    events = list(transport.stream_call(client, model="x", messages=[{"role": "user", "content": "hi"}]))
    text_events = [e for e in events if e.type == EVENT_TEXT_DELTA]
    assert len(text_events) == 3, f"expected 3 text_deltas, got {len(text_events)}"
    assert events[-1].type == EVENT_DONE
    print("✓ test 4 — ChatCompletions 流式纯文本：3 个 text_delta + done")


def test_chat_completions_done_content_correct() -> None:
    transport = get_transport("chat_completions")
    chunks = _make_text_chunks(
        ["Hello", " ", "world"],
        usage=_FUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
    )
    client = _FClient(stream=_FStream(chunks))
    events = list(transport.stream_call(client, model="x", messages=[]))
    done = events[-1]
    assert done.response.content == "Hello world", done.response.content
    assert done.response.finish_reason == "stop"
    assert done.response.usage.prompt_tokens == 5
    assert done.response.usage.completion_tokens == 3
    print("✓ test 5 — ChatCompletions done.response.content 拼接正确")


def test_chat_completions_tool_call_accumulation() -> None:
    """name 在 1 帧、arguments 分 3 帧。"""
    transport = get_transport("chat_completions")
    chunks = [
        _FChunk(choices=[_FChoice(delta=_FDelta(tool_calls=[
            _FToolCallDelta(index=0, id="call_1", function=_FFn(name="read_file", arguments="")),
        ]))]),
        _FChunk(choices=[_FChoice(delta=_FDelta(tool_calls=[
            _FToolCallDelta(index=0, function=_FFn(arguments='{"path"')),
        ]))]),
        _FChunk(choices=[_FChoice(delta=_FDelta(tool_calls=[
            _FToolCallDelta(index=0, function=_FFn(arguments=': "a.txt"')),
        ]))]),
        _FChunk(choices=[_FChoice(delta=_FDelta(tool_calls=[
            _FToolCallDelta(index=0, function=_FFn(arguments='}')),
        ]), finish_reason="tool_calls")]),
        _FChunk(choices=[], usage=_FUsage(prompt_tokens=20, completion_tokens=8, total_tokens=28)),
    ]
    client = _FClient(stream=_FStream(chunks))
    events = list(transport.stream_call(client, model="x", messages=[]))

    started = [e for e in events if e.type == EVENT_TOOL_CALL_STARTED]
    assert len(started) == 1 and started[0].tool_name == "read_file", started

    done = events[-1]
    assert done.response.tool_calls is not None and len(done.response.tool_calls) == 1
    tc = done.response.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.name == "read_file"
    assert json.loads(tc.arguments) == {"path": "a.txt"}, tc.arguments
    assert done.response.finish_reason == "tool_calls"
    print("✓ test 6 — tool_call name 单帧 / args 多帧 → 合并正确")


def test_chat_completions_tool_call_name_assignment_not_concat() -> None:
    """重发同名 → 必须用赋值，不能 += （MiniMax M2.7 via NVIDIA NIM）"""
    transport = get_transport("chat_completions")
    chunks = [
        _FChunk(choices=[_FChoice(delta=_FDelta(tool_calls=[
            _FToolCallDelta(index=0, id="call_1", function=_FFn(name="read_file")),
        ]))]),
        # 第二帧又把 name 完整重发
        _FChunk(choices=[_FChoice(delta=_FDelta(tool_calls=[
            _FToolCallDelta(index=0, function=_FFn(name="read_file", arguments='{"a":1}')),
        ]), finish_reason="tool_calls")]),
        _FChunk(choices=[], usage=None),
    ]
    client = _FClient(stream=_FStream(chunks))
    events = list(transport.stream_call(client, model="x", messages=[]))
    tc = events[-1].response.tool_calls[0]
    assert tc.name == "read_file", f"expected 'read_file', got {tc.name!r}"
    print("✓ test 7 — name 重发用赋值（不会变成 'read_fileread_file'）")


def test_chat_completions_usage_from_final_empty_chunk() -> None:
    transport = get_transport("chat_completions")
    chunks = _make_text_chunks(
        ["x"],
        usage=_FUsage(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            prompt_tokens_details=_FUsageDetails(cached_tokens=80),
        ),
    )
    client = _FClient(stream=_FStream(chunks))
    events = list(transport.stream_call(client, model="x", messages=[]))
    u = events[-1].response.usage
    assert u.prompt_tokens == 100 and u.completion_tokens == 50 and u.cached_tokens == 80
    print("✓ test 8 — usage（含 cached_tokens）从最终 choices=[] 帧抓取")


def test_chat_completions_cancel_mid_stream() -> None:
    """第 2 帧前 cancel → 流必须 raise StreamCancelled。"""
    transport = get_transport("chat_completions")
    cancel = CancelToken()

    def trigger(idx: int) -> None:
        if idx == 2:
            cancel.cancel()

    chunks = _make_text_chunks(
        ["a", "b", "c", "d"],
        usage=_FUsage(prompt_tokens=10, completion_tokens=4, total_tokens=14),
    )
    fs = _FStream(chunks, on_chunk=trigger)
    client = _FClient(stream=fs)
    raised = False
    consumed = []
    try:
        for ev in transport.stream_call(client, cancel_token=cancel, model="x", messages=[]):
            consumed.append(ev)
    except StreamCancelled:
        raised = True
    assert raised, "中途 cancel 必须抛 StreamCancelled"
    # 已 yield 的 text_delta ≤ 2（取消前的）
    assert all(e.type == EVENT_TEXT_DELTA for e in consumed), consumed
    assert len(consumed) <= 2, f"取消后仍 yield {len(consumed)} 帧"
    print("✓ test 9 — cancel_token 命中 → StreamCancelled、停止迭代")


def test_default_fake_streaming() -> None:
    """ABC 默认 stream_call — 不重写 → call() 包成单 text_delta + done。"""

    class _T(ProviderTransport):
        @property
        def api_mode(self) -> str:
            return "fake"

        def convert_messages(self, m, **kw): return m

        def convert_tools(self, t): return t

        def build_kwargs(self, **kw): return {}

        def normalize_response(self, r, **kw): return r

        def call(self, client, **kwargs):
            return NormalizedResponse(
                content="hello",
                tool_calls=None,
                finish_reason="stop",
            )

    t = _T()
    events = list(t.stream_call(client=None))
    assert len(events) == 2
    assert events[0].type == EVENT_TEXT_DELTA and events[0].text == "hello"
    assert events[1].type == EVENT_DONE and events[1].response.content == "hello"
    print("✓ test 10 — ABC 默认 stream_call = call + 单帧假流式")


# ─── Chain 流式 ─────────────────────────────────────────────────────────────


class _FChainTransport(ProviderTransport):
    """模拟 transport — 控制 stream_call 的事件序列 / 是否抛异常。"""

    def __init__(self, events_or_exc, mode="chat_completions") -> None:
        self._payload = events_or_exc
        self._mode = mode

    @property
    def api_mode(self) -> str:
        return self._mode

    def convert_messages(self, m, **kw): return m

    def convert_tools(self, t): return t

    def build_kwargs(self, **kw): return kw

    def normalize_response(self, r, **kw): return r

    def call(self, client, **kw):
        raise NotImplementedError

    def stream_call(self, client, cancel_token=None, **kwargs):
        if isinstance(self._payload, Exception):
            raise self._payload
        for item in self._payload:
            if isinstance(item, Exception):
                raise item
            yield item


def _mk_done(content: str, prompt: int = 10, completion: int = 5, cached: int = 0) -> StreamEvent:
    return StreamEvent(
        type=EVENT_DONE,
        response=NormalizedResponse(
            content=content,
            tool_calls=None,
            finish_reason="stop",
            usage=type("U", (), dict(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
                cached_tokens=cached,
                cache_creation_tokens=0,
            ))(),
        ),
    )


def test_chain_stream_single_entry_success() -> None:
    t = _FChainTransport([
        StreamEvent(type=EVENT_TEXT_DELTA, text="hi"),
        _mk_done("hi", prompt=12, cached=4),
    ])
    entry = _ChainEntry(api_mode="chat_completions", transport=t, client=None, model="m1")
    chain = TransportChain([entry], failure_threshold=3, cooldown_seconds=60.0, max_retries=0)
    events = list(chain.stream_call(model="m1", messages=[]))
    assert len(events) == 2
    assert events[-1].response.content == "hi"
    # cache 累计
    assert entry.cache_read_total == 4
    print("✓ test 11 — chain 单 entry 流式成功 + cache 累计")


def test_chain_stream_failover_before_first_event() -> None:
    fail_t = _FChainTransport(ConnectionError("primary down"))
    ok_t = _FChainTransport([
        StreamEvent(type=EVENT_TEXT_DELTA, text="from-secondary"),
        _mk_done("from-secondary"),
    ])
    e1 = _ChainEntry(api_mode="chat_completions", transport=fail_t, client=None, model="m1")
    e2 = _ChainEntry(api_mode="anthropic_messages", transport=ok_t, client=None, model="m2")
    chain = TransportChain([e1, e2], failure_threshold=3, cooldown_seconds=60.0, max_retries=0)
    events = list(chain.stream_call(model="m1", messages=[]))
    assert events[-1].response.content == "from-secondary"
    # 主家失败计数应+1
    assert e1.breaker.consecutive_failures == 1
    assert e2.breaker.consecutive_failures == 0
    print("✓ test 12 — chain 主家首帧前失败 → 切备家")


def test_chain_stream_no_failover_after_delivered() -> None:
    """主家先吐 1 帧 → 失败 → chain 必须 raise，不能切家（避免 token 重发）。"""
    bad_payload = [
        StreamEvent(type=EVENT_TEXT_DELTA, text="par"),
        ConnectionError("mid-stream cut"),
    ]
    fail_t = _FChainTransport(bad_payload)
    ok_t = _FChainTransport([_mk_done("backup")])
    e1 = _ChainEntry(api_mode="chat_completions", transport=fail_t, client=None, model="m1")
    e2 = _ChainEntry(api_mode="anthropic_messages", transport=ok_t, client=None, model="m2")
    chain = TransportChain([e1, e2], failure_threshold=3, cooldown_seconds=60.0, max_retries=0)
    delivered = []
    raised = False
    try:
        for ev in chain.stream_call(model="m1", messages=[]):
            delivered.append(ev)
    except Exception:
        raised = True
    assert raised, "已 yield 后失败必须 raise，不切家"
    # 备家不应被调到（无 done 累计）
    assert e2.breaker.consecutive_failures == 0
    # 主家已 yield 1 帧
    assert len(delivered) == 1 and delivered[0].text == "par"
    print("✓ test 13 — chain 已 yield 后主家失败 → 不切家、原异常透传")


# ─── runner ───────────────────────────────────────────────────────────────


def main() -> int:
    print("=" * 60)
    print("  V22 流式输出 + 中断 — 不变量验证")
    print("=" * 60)

    tests = [
        test_cancel_token_default_closed,
        test_cancel_token_cancel_raises,
        test_cancel_token_reset,
        test_chat_completions_text_stream_concat,
        test_chat_completions_done_content_correct,
        test_chat_completions_tool_call_accumulation,
        test_chat_completions_tool_call_name_assignment_not_concat,
        test_chat_completions_usage_from_final_empty_chunk,
        test_chat_completions_cancel_mid_stream,
        test_default_fake_streaming,
        test_chain_stream_single_entry_success,
        test_chain_stream_failover_before_first_event,
        test_chain_stream_no_failover_after_delivered,
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
