"""V18 AnthropicTransport 最小不变量验证脚本。

不调真实 API；用 fake Anthropic Message 对象验证 transport 行为。

覆盖：
1. 注册表 — get_transport("anthropic_messages") 返回非 None
2. convert_messages — system 拆出 + assistant tool_calls 转 tool_use + tool 转 tool_result
3. convert_tools — OpenAI schema → Anthropic input_schema
4. build_kwargs — 组装含 system/messages/tools/max_tokens/thinking 的 dict
5. normalize_response — text block 响应正确解析
6. normalize_response — tool_use block 响应正确解析 + 向后兼容 property
7. stop_reason 映射 — end_turn→stop, tool_use→tool_calls, max_tokens→length
8. validate_response — 空 content + end_turn 合法；None 不合法
9. usage 字段抽取（input_tokens / output_tokens → prompt/completion）
10. env-driven 路由 — TRANSPORT_MODE 切换 transport
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transports import get_transport
from transports.types import NormalizedResponse, ToolCall


# ─── Fake Anthropic SDK 响应对象 ──────────────────────────────────────────


@dataclass
class _FakeTextBlock:
    type: str = "text"
    text: str = ""


@dataclass
class _FakeToolUseBlock:
    type: str = "tool_use"
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class _FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _FakeMessage:
    content: list = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: _FakeUsage | None = None


# ─── 测试 ─────────────────────────────────────────────────────────────────


def test_registry_returns_anthropic_transport() -> None:
    transport = get_transport("anthropic_messages")
    assert transport is not None
    assert transport.api_mode == "anthropic_messages"


def test_convert_messages_extracts_system() -> None:
    transport = get_transport("anthropic_messages")
    messages = [
        {"role": "system", "content": "Be helpful"},
        {"role": "user", "content": "hello"},
    ]
    system, converted = transport.convert_messages(messages)
    assert system == "Be helpful"
    assert len(converted) == 1
    assert converted[0]["role"] == "user"
    assert converted[0]["content"] == "hello"


def test_convert_messages_tool_calls_and_results() -> None:
    transport = get_transport("anthropic_messages")
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "read a.py"},
        {
            "role": "assistant",
            "content": "Let me read that.",
            "tool_calls": [
                {
                    "id": "tc_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tc_1", "content": "print('hi')"},
    ]
    system, converted = transport.convert_messages(messages)
    assert system == "sys"
    assert len(converted) == 3  # user, assistant, user(tool_result)

    # assistant message has text + tool_use blocks
    assistant = converted[1]
    assert assistant["role"] == "assistant"
    blocks = assistant["content"]
    assert len(blocks) == 2
    assert blocks[0]["type"] == "text"
    assert blocks[0]["text"] == "Let me read that."
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["name"] == "read_file"
    assert blocks[1]["input"] == {"path": "a.py"}
    assert blocks[1]["id"] == "tc_1"

    # tool result in user message
    tool_result_msg = converted[2]
    assert tool_result_msg["role"] == "user"
    assert tool_result_msg["content"][0]["type"] == "tool_result"
    assert tool_result_msg["content"][0]["tool_use_id"] == "tc_1"


def test_convert_tools_to_anthropic_format() -> None:
    transport = get_transport("anthropic_messages")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo input",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        }
    ]
    converted = transport.convert_tools(tools)
    assert len(converted) == 1
    assert converted[0]["name"] == "echo"
    assert converted[0]["description"] == "Echo input"
    assert converted[0]["input_schema"]["type"] == "object"
    assert "text" in converted[0]["input_schema"]["properties"]


def test_build_kwargs_includes_required_fields() -> None:
    transport = get_transport("anthropic_messages")
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    kwargs = transport.build_kwargs(model="qwen3.6-plus", messages=messages, max_tokens=2048)
    assert kwargs["model"] == "qwen3.6-plus"
    assert kwargs["system"] == "sys"
    assert kwargs["max_tokens"] == 2048
    assert kwargs["thinking"] == {"type": "disabled"}
    assert len(kwargs["messages"]) == 1


def test_normalize_text_response() -> None:
    transport = get_transport("anthropic_messages")
    fake = _FakeMessage(
        content=[_FakeTextBlock(text="Hello!")],
        stop_reason="end_turn",
        usage=_FakeUsage(input_tokens=15, output_tokens=5),
    )
    result = transport.normalize_response(fake)
    assert isinstance(result, NormalizedResponse)
    assert result.content == "Hello!"
    assert result.tool_calls is None
    assert result.finish_reason == "stop"
    assert result.usage.prompt_tokens == 15
    assert result.usage.completion_tokens == 5
    assert result.usage.total_tokens == 20


def test_normalize_tool_use_response_with_backward_compat() -> None:
    transport = get_transport("anthropic_messages")
    fake = _FakeMessage(
        content=[
            _FakeToolUseBlock(id="tu_1", name="read_file", input={"path": "b.py"}),
        ],
        stop_reason="tool_use",
        usage=_FakeUsage(input_tokens=30, output_tokens=10),
    )
    result = transport.normalize_response(fake)
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls is not None and len(result.tool_calls) == 1

    tc = result.tool_calls[0]
    # 向后兼容 properties
    assert tc.function.name == "read_file"
    assert json.loads(tc.function.arguments) == {"path": "b.py"}
    assert tc.type == "function"
    assert tc.id == "tu_1"


def test_stop_reason_mapping() -> None:
    transport = get_transport("anthropic_messages")
    assert transport.map_finish_reason("end_turn") == "stop"
    assert transport.map_finish_reason("tool_use") == "tool_calls"
    assert transport.map_finish_reason("max_tokens") == "length"
    assert transport.map_finish_reason("stop_sequence") == "stop"
    assert transport.map_finish_reason("unknown") == "stop"


def test_validate_response() -> None:
    transport = get_transport("anthropic_messages")
    # None → invalid
    assert transport.validate_response(None) is False
    # empty content + end_turn → valid (model says "nothing more")
    assert transport.validate_response(_FakeMessage(content=[], stop_reason="end_turn")) is True
    # empty content + tool_use → invalid
    assert transport.validate_response(_FakeMessage(content=[], stop_reason="tool_use")) is False
    # normal → valid
    assert transport.validate_response(_FakeMessage(content=[_FakeTextBlock(text="x")])) is True


def test_usage_with_cached_tokens() -> None:
    transport = get_transport("anthropic_messages")
    fake = _FakeMessage(
        content=[_FakeTextBlock(text="ok")],
        stop_reason="end_turn",
        usage=_FakeUsage(input_tokens=100, output_tokens=20, cache_read_input_tokens=80),
    )
    result = transport.normalize_response(fake)
    assert result.usage.cached_tokens == 80


def test_transport_mode_env_routing() -> None:
    # 默认 chat_completions
    t1 = get_transport(os.environ.get("TRANSPORT_MODE", "chat_completions"))
    assert t1.api_mode == "chat_completions"

    # 切换到 anthropic_messages
    t2 = get_transport("anthropic_messages")
    assert t2.api_mode == "anthropic_messages"


# ─── 入口 ─────────────────────────────────────────────────────────────────


TESTS = [
    test_registry_returns_anthropic_transport,
    test_convert_messages_extracts_system,
    test_convert_messages_tool_calls_and_results,
    test_convert_tools_to_anthropic_format,
    test_build_kwargs_includes_required_fields,
    test_normalize_text_response,
    test_normalize_tool_use_response_with_backward_compat,
    test_stop_reason_mapping,
    test_validate_response,
    test_usage_with_cached_tokens,
    test_transport_mode_env_routing,
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
