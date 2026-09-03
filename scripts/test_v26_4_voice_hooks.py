"""V26.4 voice lifecycle hook invariants without a real Runtime."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.voice_dispatch as voice_dispatch
from agent.voice_readout import VoiceReadoutService
from tools.hooks import HookManager
from tools.registry import registry


class _Completed:
    returncode = 0
    stdout = ""
    stderr = ""


def test_disabled_does_not_spawn():
    old = os.environ.pop("VOICE_READOUT_ENABLED", None)
    calls = []
    original = voice_dispatch.subprocess.run
    voice_dispatch.subprocess.run = lambda *args, **kwargs: calls.append(args) or _Completed()
    try:
        result = voice_dispatch.CliVoiceDispatcher().speak(intent="info", text="测试一下")
        assert result.status == "disabled"
        assert not calls
    finally:
        voice_dispatch.subprocess.run = original
        if old is not None:
            os.environ["VOICE_READOUT_ENABLED"] = old


def test_disabled_does_not_call_helper_model():
    old = os.environ.pop("VOICE_READOUT_ENABLED", None)
    helper_calls = []

    class FakeChain:
        def call(self, **kwargs):
            helper_calls.append(kwargs)
            return SimpleNamespace(content="不应生成。", tool_calls=[])

    try:
        service = VoiceReadoutService()
        result = service.before_model(
            messages=[{"role": "user", "content": "测试"}],
            model="m", turn_id="off", stream_enabled=False, tools=[], chain=FakeChain(),
        )
        assert result.status == "disabled"
        assert helper_calls == []
    finally:
        if old is not None:
            os.environ["VOICE_READOUT_ENABLED"] = old


def test_before_after_order_and_tool_call_policy():
    old = os.environ.get("VOICE_READOUT_ENABLED")
    os.environ["VOICE_READOUT_ENABLED"] = "1"
    calls = []
    original = voice_dispatch.subprocess.run

    def fake_run(command, **kwargs):
        calls.append(command)
        return _Completed()

    voice_dispatch.subprocess.run = fake_run
    try:
        service = VoiceReadoutService()
        service.before_model(messages=[], model="m", turn_id="t", stream_enabled=False, tools=[])
        service.after_model(
            messages=[], model="m", turn_id="t",
            response=SimpleNamespace(content="工具继续。", tool_calls=[object()]),
            stream_enabled=False,
        )
        service.before_model(messages=[], model="m", turn_id="t", stream_enabled=False, tools=[])
        service.after_model(
            messages=[], model="m", turn_id="t",
            response=SimpleNamespace(content="全部完成。", tool_calls=[]),
            stream_enabled=False,
        )
        assert [command[1] for command in calls] == ["--intent", "--intent", "--intent"]
        assert [command[2] for command in calls] == ["info", "progress", "done"]
    finally:
        voice_dispatch.subprocess.run = original
        if old is None:
            os.environ.pop("VOICE_READOUT_ENABLED", None)
        else:
            os.environ["VOICE_READOUT_ENABLED"] = old


def test_helper_model_generates_complete_readout_text():
    old = os.environ.get("VOICE_READOUT_ENABLED")
    os.environ["VOICE_READOUT_ENABLED"] = "1"
    cli_calls = []
    helper_calls = []
    original_run = voice_dispatch.subprocess.run

    def fake_run(command, **kwargs):
        cli_calls.append(command)
        return _Completed()

    class FakeChain:
        def call(self, **kwargs):
            helper_calls.append(kwargs)
            if "完成摘要生成器" in kwargs["messages"][0]["content"]:
                return SimpleNamespace(content="整理好了，任务已经完成。", tool_calls=[])
            return SimpleNamespace(content="正在查阅任务相关信息。", tool_calls=[])

    voice_dispatch.subprocess.run = fake_run
    try:
        service = VoiceReadoutService()
        service.before_model(
            messages=[{"role": "user", "content": "查询配置"}],
            model="deepseek-v4-pro", turn_id="t", stream_enabled=False, tools=[], chain=FakeChain(),
        )
        service.after_model(
            messages=[{"role": "user", "content": "查询配置"}],
            model="deepseek-v4-pro", turn_id="t",
            response=SimpleNamespace(content="原始答案很长。", tool_calls=[]),
            stream_enabled=False, chain=FakeChain(),
        )
        assert len(helper_calls) == 2
        assert helper_calls[0]["tools"] == []
        assert helper_calls[0]["voice_internal"] is True
        assert helper_calls[0]["max_tokens"] == 160
        assert helper_calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
        assert cli_calls[0][4] == "正在查阅任务相关信息。"
        assert cli_calls[1][4] == "整理好了，任务已经完成。"
    finally:
        voice_dispatch.subprocess.run = original_run
        if old is None:
            os.environ.pop("VOICE_READOUT_ENABLED", None)
        else:
            os.environ["VOICE_READOUT_ENABLED"] = old


def test_empty_helper_uses_context_and_avoids_repetition():
    old = os.environ.get("VOICE_READOUT_ENABLED")
    os.environ["VOICE_READOUT_ENABLED"] = "1"
    cli_calls = []
    original_run = voice_dispatch.subprocess.run

    def fake_run(command, **kwargs):
        cli_calls.append(command)
        return _Completed()

    voice_dispatch.subprocess.run = fake_run
    try:
        service = VoiceReadoutService()
        context = [{"role": "user", "content": "帮我检查语音服务的调用链"}]
        service.before_model(
            messages=context, model="m", turn_id="fallback", stream_enabled=False, tools=[], chain=None,
        )
        service.before_model(
            messages=context, model="m", turn_id="fallback", stream_enabled=False, tools=[], chain=None,
        )
        first, second = cli_calls[0][4], cli_calls[1][4]
        assert first != second
        assert "语音服务" in first
        assert "语音服务" in second
    finally:
        voice_dispatch.subprocess.run = original_run
        if old is None:
            os.environ.pop("VOICE_READOUT_ENABLED", None)
        else:
            os.environ["VOICE_READOUT_ENABLED"] = old


def test_repeated_helper_output_is_replaced():
    old = os.environ.get("VOICE_READOUT_ENABLED")
    os.environ["VOICE_READOUT_ENABLED"] = "1"
    cli_calls = []
    original_run = voice_dispatch.subprocess.run

    def fake_run(command, **kwargs):
        cli_calls.append(command)
        return _Completed()

    class RepeatingChain:
        def call(self, **kwargs):
            return SimpleNamespace(content="我正在处理当前任务。", tool_calls=[])

    voice_dispatch.subprocess.run = fake_run
    try:
        service = VoiceReadoutService()
        context = [{"role": "user", "content": "分析订单数据"}]
        service.before_model(
            messages=context, model="m", turn_id="repeat", stream_enabled=False, tools=[], chain=RepeatingChain(),
        )
        service.before_model(
            messages=context, model="m", turn_id="repeat", stream_enabled=False, tools=[], chain=RepeatingChain(),
        )
        assert cli_calls[0][4] != cli_calls[1][4]
        assert "订单数据" in cli_calls[1][4]
    finally:
        voice_dispatch.subprocess.run = original_run
        if old is None:
            os.environ.pop("VOICE_READOUT_ENABLED", None)
        else:
            os.environ["VOICE_READOUT_ENABLED"] = old


def test_hook_manager_isolates_model_hook_failure():
    manager = HookManager()
    seen = []

    def broken(**kwargs):
        raise RuntimeError("boom")

    def healthy(**kwargs):
        seen.append(kwargs["model"])

    manager.register("before_model_call", broken)
    manager.register("before_model_call", healthy)
    manager.invoke("before_model_call", model="demo")
    assert seen == ["demo"]


def test_voice_say_tool_is_registered():
    assert "voice_say" in registry.tool_names


if __name__ == "__main__":
    tests = [
        test_disabled_does_not_spawn,
        test_disabled_does_not_call_helper_model,
        test_before_after_order_and_tool_call_policy,
        test_helper_model_generates_complete_readout_text,
        test_empty_helper_uses_context_and_avoids_repetition,
        test_repeated_helper_output_is_replaced,
        test_hook_manager_isolates_model_hook_failure,
        test_voice_say_tool_is_registered,
    ]
    for test in tests:
        test()
        print(f"✓ {test.__name__}")
    print(f"PASS: {len(tests)} voice hook tests")
