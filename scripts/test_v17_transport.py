"""V17 transport 抽象层最小不变量验证脚本。

不调真实 API；用 fake ChatCompletion 对象验证 transport 行为。

覆盖：
1. 注册表 — get_transport("chat_completions") 返回非 None
2. build_kwargs — 组装出包含 model/messages/tools 的 dict
3. normalize_response — 普通文本响应（无 tool_calls）字段抽取正确
4. normalize_response — 含 tool_calls 响应正确解析
5. 向后兼容 — ToolCall.function.name / .function.arguments / .type 可访问
6. validate_response — 空 choices 应返回 False
7. usage 字段抽取正确（含 cached_tokens）
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transports import get_transport
from transports.types import NormalizedResponse, ToolCall, build_tool_call


# ─── Fake OpenAI SDK 响应对象（最小集合） ──────────────────────────────────


@dataclass
class _FakeFn:
    name: str
    arguments: str


@dataclass
class _FakeToolCall:
    id: str
    function: _FakeFn
    type: str = "function"


@dataclass
class _FakeMsg:
    content: str | None
    tool_calls: list[_FakeToolCall] | None = None
    reasoning: str | None = None
    reasoning_content: str | None = None


@dataclass
class _FakeChoice:
    message: _FakeMsg
    finish_reason: str = "stop"


@dataclass
class _FakeUsageDetails:
    cached_tokens: int = 0


@dataclass
class _FakeUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_tokens_details: _FakeUsageDetails | None = None


@dataclass
class _FakeChatCompletion:
    choices: list[_FakeChoice]
    usage: _FakeUsage | None = None


# ─── 测试 ─────────────────────────────────────────────────────────────────


def test_registry_returns_transport() -> None:
    transport = get_transport("chat_completions")
    assert transport is not None, "expected ChatCompletionsTransport from registry"
    assert transport.api_mode == "chat_completions"


def test_registry_returns_none_for_unknown() -> None:
    assert get_transport("nonexistent_mode") is None


def test_build_kwargs_minimal() -> None:
    transport = get_transport("chat_completions")
    kwargs = transport.build_kwargs(
        model="deepseek-chat",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert kwargs["model"] == "deepseek-chat"
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert "tools" not in kwargs
    assert "max_tokens" not in kwargs


def test_build_kwargs_with_tools_and_options() -> None:
    transport = get_transport("chat_completions")
    tools = [{"type": "function", "function": {"name": "echo", "parameters": {}}}]
    kwargs = transport.build_kwargs(
        model="deepseek-chat",
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        max_tokens=512,
        temperature=0.2,
        timeout=30,
    )
    assert kwargs["tools"] == tools
    assert kwargs["max_tokens"] == 512
    assert kwargs["temperature"] == 0.2
    assert kwargs["timeout"] == 30


def test_normalize_text_response() -> None:
    transport = get_transport("chat_completions")
    fake = _FakeChatCompletion(
        choices=[_FakeChoice(message=_FakeMsg(content="hello!"), finish_reason="stop")],
        usage=_FakeUsage(prompt_tokens=10, completion_tokens=3, total_tokens=13),
    )
    result = transport.normalize_response(fake)
    assert isinstance(result, NormalizedResponse)
    assert result.content == "hello!"
    assert result.tool_calls is None
    assert result.finish_reason == "stop"
    assert result.usage.prompt_tokens == 10
    assert result.usage.completion_tokens == 3
    assert result.usage.total_tokens == 13


def test_normalize_with_tool_calls_and_backward_compat() -> None:
    transport = get_transport("chat_completions")
    fake = _FakeChatCompletion(
        choices=[
            _FakeChoice(
                message=_FakeMsg(
                    content=None,
                    tool_calls=[
                        _FakeToolCall(
                            id="call_abc",
                            function=_FakeFn(name="read_file", arguments='{"path":"a.py"}'),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        ],
        usage=_FakeUsage(prompt_tokens=20, completion_tokens=5, total_tokens=25),
    )
    result = transport.normalize_response(fake)
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls is not None and len(result.tool_calls) == 1

    tc = result.tool_calls[0]
    # 向后兼容 properties — agent.py 现有读法零改动
    assert tc.function.name == "read_file"
    assert tc.function.arguments == '{"path":"a.py"}'
    assert tc.type == "function"
    assert tc.id == "call_abc"
    assert json.loads(tc.function.arguments) == {"path": "a.py"}


def test_validate_response_rejects_empty_choices() -> None:
    transport = get_transport("chat_completions")
    assert transport.validate_response(_FakeChatCompletion(choices=[])) is False
    assert transport.validate_response(None) is False
    fake_ok = _FakeChatCompletion(
        choices=[_FakeChoice(message=_FakeMsg(content="x"))]
    )
    assert transport.validate_response(fake_ok) is True


def test_cached_tokens_extracted() -> None:
    transport = get_transport("chat_completions")
    fake = _FakeChatCompletion(
        choices=[_FakeChoice(message=_FakeMsg(content="hi"))],
        usage=_FakeUsage(
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            prompt_tokens_details=_FakeUsageDetails(cached_tokens=80),
        ),
    )
    result = transport.normalize_response(fake)
    assert result.usage.cached_tokens == 80


def test_reasoning_content_in_provider_data() -> None:
    transport = get_transport("chat_completions")
    fake = _FakeChatCompletion(
        choices=[
            _FakeChoice(
                message=_FakeMsg(content="ok", reasoning_content="thinking..."),
                finish_reason="stop",
            )
        ],
    )
    result = transport.normalize_response(fake)
    # 通过兼容 property 访问
    assert result.reasoning_content == "thinking..."
    # 也存在 provider_data 字典里（协议感知代码用）
    assert result.provider_data is not None
    assert result.provider_data["reasoning_content"] == "thinking..."


def test_build_tool_call_factory_serializes_dict() -> None:
    tc = build_tool_call(id="x", name="f", arguments={"k": 1}, call_id="cc")
    assert tc.arguments == '{"k": 1}'
    assert tc.provider_data == {"call_id": "cc"}
    assert tc.function.name == "f"


# ─── 入口 ─────────────────────────────────────────────────────────────────


TESTS = [
    test_registry_returns_transport,
    test_registry_returns_none_for_unknown,
    test_build_kwargs_minimal,
    test_build_kwargs_with_tools_and_options,
    test_normalize_text_response,
    test_normalize_with_tool_calls_and_backward_compat,
    test_validate_response_rejects_empty_choices,
    test_cached_tokens_extracted,
    test_reasoning_content_in_provider_data,
    test_build_tool_call_factory_serializes_dict,
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
