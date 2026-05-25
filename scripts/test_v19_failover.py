"""V19 Failover + 健康检查（断路器） 不变量验证脚本。

不调真实 API；用 fake transport / fake exception 验证 chain + classifier 行为。

覆盖：
1. classify_error — 429 → FAILOVER/rate_limit
2. classify_error — 500 → RETRYABLE/server_error
3. classify_error — 400 + context-overflow 关键词 → FATAL/context_overflow
4. classify_error — auth 关键词 → FAILOVER/auth
5. classify_error — 未识别错误 → RETRYABLE/unknown
6. chain primary 成功 — 不切备家、breaker 保持 closed
7. chain primary 失败（FAILOVER）→ 切备家、备家成功、primary breaker 计数+1
8. chain RETRYABLE 错误 → 单 transport 内重试 N 次
9. chain 全失败 → raise FailoverExhausted，所有 attempts 都记录
10. 断路器打开 — 连续失败达到 threshold 后 opened_at != 0
11. 断路器跳过 — open 状态下直接跳过该 entry
12. 断路器半开自愈 — cooldown 过后允许探针，成功后 closed
13. FATAL 错误 — 直接抛出（不切下一家、不计入失败计数）
14. 单 transport 链 — 退化为"transport.call + 重试"，行为同 V18
15. V19.1 per-entry model — entry.model 覆盖调用方传入的 model
16. V19.1 per-entry model failover — 切备家时用备家自己的 model
17. V19.1 per-entry model None 回退 — entry.model=None 时用调用方传入的 model
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transports.chain import (
    FailoverExhausted, TransportChain, _ChainEntry, _BreakerState,
)
from transports.error_classifier import (
    ClassifiedError, ErrorAction, classify_error,
)
from transports.types import NormalizedResponse, Usage


# ── Fake transport ────────────────────────────────────────────────────────


@dataclass
class _FakeTransport:
    """模拟 ProviderTransport — 按 plan 列表依次返回 / 抛异常。"""

    api_mode: str
    plan: list  # 每项可以是 NormalizedResponse 或 Exception 实例
    call_count: int = 0
    received_kwargs: list = field(default_factory=list)

    def call(self, client: Any, **kwargs) -> NormalizedResponse:
        self.received_kwargs.append(kwargs)
        idx = self.call_count
        self.call_count += 1
        if idx >= len(self.plan):
            raise RuntimeError(f"plan exhausted for {self.api_mode}")
        item = self.plan[idx]
        if isinstance(item, BaseException):
            raise item
        return item


def _ok_response(content: str = "ok") -> NormalizedResponse:
    return NormalizedResponse(
        content=content, tool_calls=None, finish_reason="stop",
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


# ── Fake exceptions（模拟 SDK 错误） ───────────────────────────────────────


class _FakeStatusException(Exception):
    """带 status_code 属性的异常 — 模拟 OpenAI/Anthropic SDK 错误形态。"""
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


# ── 测试 ──────────────────────────────────────────────────────────────────


def test_classify_429_rate_limit() -> None:
    exc = _FakeStatusException("rate limit reached", 429)
    classified = classify_error(exc)
    assert classified.action == ErrorAction.FAILOVER
    assert classified.reason == "rate_limit"
    assert classified.status_code == 429


def test_classify_500_server_error_retryable() -> None:
    exc = _FakeStatusException("internal server error", 500)
    classified = classify_error(exc)
    assert classified.action == ErrorAction.RETRYABLE
    assert classified.reason == "server_error"


def test_classify_400_context_overflow_fatal() -> None:
    exc = _FakeStatusException(
        "input is too long: maximum context length exceeded", 400,
    )
    classified = classify_error(exc)
    assert classified.action == ErrorAction.FATAL
    assert classified.reason == "context_overflow"


def test_classify_auth_keyword_failover() -> None:
    exc = Exception("Invalid API key provided")
    classified = classify_error(exc)
    assert classified.action == ErrorAction.FAILOVER
    assert classified.reason == "auth"


def test_classify_unknown_retryable() -> None:
    exc = Exception("something weird happened")
    classified = classify_error(exc)
    assert classified.action == ErrorAction.RETRYABLE
    assert classified.reason == "unknown"


def test_chain_primary_success_no_failover() -> None:
    primary = _FakeTransport("chat_completions", [_ok_response("primary")])
    secondary = _FakeTransport("anthropic_messages", [_ok_response("secondary")])
    chain = TransportChain(
        [
            _ChainEntry("chat_completions", primary, client=None),
            _ChainEntry("anthropic_messages", secondary, client=None),
        ],
        sleep_fn=lambda _: None,
    )
    result = chain.call(model="m", messages=[{"role": "user", "content": "hi"}])
    assert result.content == "primary"
    assert primary.call_count == 1
    assert secondary.call_count == 0
    assert chain.entries[0].breaker.consecutive_failures == 0
    assert chain.entries[0].breaker.opened_at == 0.0


def test_chain_failover_on_rate_limit() -> None:
    primary = _FakeTransport(
        "chat_completions",
        [_FakeStatusException("rate limit", 429)],
    )
    secondary = _FakeTransport("anthropic_messages", [_ok_response("backup")])
    chain = TransportChain(
        [
            _ChainEntry("chat_completions", primary, client=None),
            _ChainEntry("anthropic_messages", secondary, client=None),
        ],
        sleep_fn=lambda _: None,
        max_retries=0,
    )
    result = chain.call(model="m", messages=[])
    assert result.content == "backup"
    assert primary.call_count == 1
    assert secondary.call_count == 1
    assert chain.entries[0].breaker.consecutive_failures == 1
    assert chain.entries[1].breaker.consecutive_failures == 0


def test_chain_retries_on_retryable_error() -> None:
    primary = _FakeTransport(
        "chat_completions",
        [
            _FakeStatusException("internal server error", 500),
            _FakeStatusException("internal server error", 500),
            _ok_response("third try"),
        ],
    )
    chain = TransportChain(
        [_ChainEntry("chat_completions", primary, client=None)],
        sleep_fn=lambda _: None,
        max_retries=3,
    )
    result = chain.call(model="m", messages=[])
    assert result.content == "third try"
    assert primary.call_count == 3
    # 重试期间不应记失败 — 最终成功了
    assert chain.entries[0].breaker.consecutive_failures == 0


def test_chain_all_fail_raises_exhausted() -> None:
    primary = _FakeTransport(
        "chat_completions",
        [_FakeStatusException("rate limit", 429)],
    )
    secondary = _FakeTransport(
        "anthropic_messages",
        [_FakeStatusException("overloaded", 503)],
    )
    chain = TransportChain(
        [
            _ChainEntry("chat_completions", primary, client=None),
            _ChainEntry("anthropic_messages", secondary, client=None),
        ],
        sleep_fn=lambda _: None,
        max_retries=0,
    )
    try:
        chain.call(model="m", messages=[])
        assert False, "expected FailoverExhausted"
    except FailoverExhausted as e:
        assert len(e.attempts) == 2
        assert e.attempts[0].reason == "rate_limit"
        assert e.attempts[1].reason == "overloaded"


def test_breaker_opens_after_threshold() -> None:
    primary = _FakeTransport(
        "chat_completions",
        [_FakeStatusException("rate limit", 429)] * 5,
    )
    secondary = _FakeTransport(
        "anthropic_messages",
        [_ok_response("backup")] * 5,
    )

    fake_now = [0.0]
    chain = TransportChain(
        [
            _ChainEntry("chat_completions", primary, client=None),
            _ChainEntry("anthropic_messages", secondary, client=None),
        ],
        sleep_fn=lambda _: None,
        clock_fn=lambda: fake_now[0],
        failure_threshold=3,
        cooldown_seconds=60.0,
        max_retries=0,
    )

    # 连续 3 次都让 primary 失败 → breaker 应该 open
    for i in range(3):
        fake_now[0] = float(i)
        chain.call(model="m", messages=[])

    assert chain.entries[0].breaker.consecutive_failures == 3
    assert chain.entries[0].breaker.opened_at != 0.0


def test_open_breaker_skips_entry() -> None:
    primary = _FakeTransport("chat_completions", [_ok_response("primary")])
    secondary = _FakeTransport("anthropic_messages", [_ok_response("backup")])

    fake_now = [100.0]
    chain = TransportChain(
        [
            _ChainEntry(
                "chat_completions", primary, client=None,
                breaker=_BreakerState(consecutive_failures=5, opened_at=99.0, last_reason="rate_limit"),
            ),
            _ChainEntry("anthropic_messages", secondary, client=None),
        ],
        sleep_fn=lambda _: None,
        clock_fn=lambda: fake_now[0],
        cooldown_seconds=60.0,
    )
    result = chain.call(model="m", messages=[])
    # primary 应该被跳过 — call_count 仍为 0
    assert primary.call_count == 0
    assert secondary.call_count == 1
    assert result.content == "backup"


def test_breaker_half_open_recovery() -> None:
    primary = _FakeTransport("chat_completions", [_ok_response("recovered")])

    fake_now = [200.0]
    # opened_at = 100, cooldown = 60 → 已经过了冷却期，半开探针
    chain = TransportChain(
        [
            _ChainEntry(
                "chat_completions", primary, client=None,
                breaker=_BreakerState(consecutive_failures=3, opened_at=100.0, last_reason="rate_limit"),
            ),
        ],
        sleep_fn=lambda _: None,
        clock_fn=lambda: fake_now[0],
        cooldown_seconds=60.0,
    )
    result = chain.call(model="m", messages=[])
    assert result.content == "recovered"
    # 探针成功 → breaker 重新关闭
    assert chain.entries[0].breaker.consecutive_failures == 0
    assert chain.entries[0].breaker.opened_at == 0.0


def test_fatal_error_raises_immediately() -> None:
    primary = _FakeTransport(
        "chat_completions",
        [_FakeStatusException("input is too long: maximum context length exceeded", 400)],
    )
    secondary = _FakeTransport("anthropic_messages", [_ok_response("should not reach")])
    chain = TransportChain(
        [
            _ChainEntry("chat_completions", primary, client=None),
            _ChainEntry("anthropic_messages", secondary, client=None),
        ],
        sleep_fn=lambda _: None,
    )
    try:
        chain.call(model="m", messages=[])
        assert False, "expected exception"
    except _FakeStatusException:
        pass
    # FATAL 不应该切到备家
    assert secondary.call_count == 0
    # FATAL 也不应该计入失败次数（用户/输入问题，不是 provider 故障）
    assert chain.entries[0].breaker.consecutive_failures == 0


def test_single_transport_chain_behaves_like_v18() -> None:
    primary = _FakeTransport("chat_completions", [_ok_response("only one")])
    chain = TransportChain(
        [_ChainEntry("chat_completions", primary, client=None)],
        sleep_fn=lambda _: None,
    )
    result = chain.call(model="m", messages=[])
    assert result.content == "only one"
    assert primary.call_count == 1
    # 单 transport 链 — primary 属性可读
    assert chain.primary.api_mode == "chat_completions"


def test_per_entry_model_overrides_call_kwarg() -> None:
    """V19.1: 每个 entry 自带 model 时，覆盖调用方传入的 model（仿源项目
    fallback chain 每条 entry 自包含 ``{provider, model}`` 的设计）。"""
    primary = _FakeTransport("chat_completions", [_ok_response("primary")])
    chain = TransportChain(
        [_ChainEntry("chat_completions", primary, client=None, model="deepseek-chat")],
        sleep_fn=lambda _: None,
    )
    chain.call(model="global-model", messages=[{"role": "user", "content": "hi"}])
    assert primary.received_kwargs[0]["model"] == "deepseek-chat"


def test_per_entry_model_failover_uses_correct_model() -> None:
    """V19.1: failover 后切到备家时使用备家自己的 model，不带主家模型名过去。"""
    primary = _FakeTransport(
        "chat_completions",
        [_FakeStatusException("rate limit", 429)],
    )
    secondary = _FakeTransport("anthropic_messages", [_ok_response("backup")])
    chain = TransportChain(
        [
            _ChainEntry("chat_completions", primary, client=None, model="deepseek-chat"),
            _ChainEntry("anthropic_messages", secondary, client=None, model="qwen3.6-plus"),
        ],
        sleep_fn=lambda _: None,
        max_retries=0,
    )
    chain.call(model="ignored", messages=[])
    # 主家用 deepseek-chat,备家用 qwen3.6-plus
    assert primary.received_kwargs[0]["model"] == "deepseek-chat"
    assert secondary.received_kwargs[0]["model"] == "qwen3.6-plus"


def test_entry_without_model_falls_back_to_call_kwarg() -> None:
    """V19.1: entry.model 为 None 时回退到调用方传入的 model（兼容 V18 单家行为）。"""
    primary = _FakeTransport("chat_completions", [_ok_response("ok")])
    chain = TransportChain(
        [_ChainEntry("chat_completions", primary, client=None, model=None)],
        sleep_fn=lambda _: None,
    )
    chain.call(model="env-default", messages=[])
    assert primary.received_kwargs[0]["model"] == "env-default"


# ── 入口 ──────────────────────────────────────────────────────────────────


TESTS = [
    test_classify_429_rate_limit,
    test_classify_500_server_error_retryable,
    test_classify_400_context_overflow_fatal,
    test_classify_auth_keyword_failover,
    test_classify_unknown_retryable,

    test_chain_primary_success_no_failover,
    test_chain_failover_on_rate_limit,
    test_chain_retries_on_retryable_error,
    test_chain_all_fail_raises_exhausted,
    test_breaker_opens_after_threshold,
    test_open_breaker_skips_entry,
    test_breaker_half_open_recovery,
    test_fatal_error_raises_immediately,
    test_single_transport_chain_behaves_like_v18,
    test_per_entry_model_overrides_call_kwarg,
    test_per_entry_model_failover_uses_correct_model,
    test_entry_without_model_falls_back_to_call_kwarg,
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
