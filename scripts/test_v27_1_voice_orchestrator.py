"""V27.1 外部 Voice Orchestrator host-side 不变量验证脚本。

不依赖真实 orchestrator / voice runtime / LLM。验证主仓只发送受限事实：
TurnEventEnvelope、VoiceEventSink、PhaseSpan、tool argument metadata、
tool registry phase、delegate aggregate child phase，以及 voice-runtime 不再指示
assistant 调 nano-voice-say。
"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.runtime import AgentRuntime
from agent.runtime_phase import (
    PHASE_CHILD_AGENT_RUNNING,
    PHASE_TOOL_EXECUTING,
    PhaseTracker,
)
from agent.turn_events import build_turn_event_envelope, preview_tool_result
from agent.voice_orchestrator_client import RecordingVoiceOrchestratorClient, VoiceEventSink
from tools.registry import ToolRegistry
from tools.result import tool_result
from transports.chat_completions import _ChatStreamAccumulator
from transports.streaming import (
    CancelToken,
    EVENT_TOOL_ARGUMENTS_DELTA,
    EVENT_TOOL_ARGUMENTS_FINISHED,
)

TEST_CASE_TIMEOUT_SECONDS = float(os.getenv("VOICE_ORCHESTRATOR_TEST_CASE_TIMEOUT", "3000"))


class TestCaseTimeoutError(TimeoutError):
    pass


@contextmanager
def _test_case_timeout(test_name: str):
    if TEST_CASE_TIMEOUT_SECONDS <= 0:
        yield
        return

    old_handler = signal.getsignal(signal.SIGALRM)

    def _raise_timeout(_signum, _frame):
        raise TestCaseTimeoutError(f"{test_name} exceeded {TEST_CASE_TIMEOUT_SECONDS:g}s")

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, TEST_CASE_TIMEOUT_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


class _Delta:
    def __init__(self, *, content=None, tool_calls=None, reasoning_content=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content
        self.reasoning = None


class _ToolDelta:
    def __init__(self, *, idx=0, call_id="call_1", name="write_file", arguments=""):
        self.index = idx
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=arguments)


class _Chunk:
    def __init__(self, delta, finish_reason=None, usage=None):
        self.choices = [SimpleNamespace(delta=delta, finish_reason=finish_reason)]
        self.usage = usage


def test_1_envelope_uses_latest_user_and_omits_system():
    messages = [
        {"role": "system", "content": "SYSTEM SECRET"},
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "处理中"},
        {"role": "user", "content": "最新问题 /Users/qshf/.env sk-secret-token"},
    ]
    env = build_turn_event_envelope(
        event_type="turn_started",
        session_id="sess/unsafe",
        turn_id="turn-1",
        messages=messages,
    ).to_dict()
    text = json.dumps(env, ensure_ascii=False)
    assert "最新问题" in text
    assert "旧问题" in text
    assert "SYSTEM SECRET" not in text
    assert "/Users/qshf" not in text
    assert "sk-secret-token" not in text
    assert env["safety"]["redacted"] is True


def test_2_envelope_bounds_recent_messages(monkey=None):
    old = os.environ.get("VOICE_ORCHESTRATOR_MAX_MESSAGE_PREVIEWS")
    os.environ["VOICE_ORCHESTRATOR_MAX_MESSAGE_PREVIEWS"] = "2"
    try:
        messages = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
        env = build_turn_event_envelope(event_type="x", session_id="s", turn_id="t", messages=messages).to_dict()
        previews = env["context"]["recent_messages"]
        assert [p["preview"]["text"] for p in previews] == ["a1", "u2"]
    finally:
        if old is None:
            os.environ.pop("VOICE_ORCHESTRATOR_MAX_MESSAGE_PREVIEWS", None)
        else:
            os.environ["VOICE_ORCHESTRATOR_MAX_MESSAGE_PREVIEWS"] = old


def test_3_tool_preview_head_tail_and_redaction():
    preview = preview_tool_result(
        "terminal",
        "ok",
        "head /Users/qshf/.env\n" + ("x" * 2000) + "\nTraceback (most recent call last): boom",
        duration_ms=42,
    )
    data = json.dumps(preview.__dict__, default=lambda o: o.__dict__, ensure_ascii=False)
    assert "/Users/qshf" not in data
    assert "Traceback" not in data
    assert preview.result_truncated is True
    assert preview.duration_ms == 42


def test_4_sink_client_failure_isolated():
    client = RecordingVoiceOrchestratorClient(raises=True)
    sink = VoiceEventSink(client, async_send=False)
    sink.submit({"event_type": "turn_started"})
    assert client.envelopes == []


def test_5_sink_records_envelope_sync():
    client = RecordingVoiceOrchestratorClient()
    sink = VoiceEventSink(client, async_send=False)
    sink.submit({"event_type": "phase_started"})
    assert client.envelopes == [{"event_type": "phase_started"}]


def test_6_phase_tracker_emits_metadata_only():
    seen = []
    tracker = PhaseTracker(lambda status, phase: seen.append((status, phase)))
    span = tracker.start(PHASE_TOOL_EXECUTING, tool_name="terminal", raw_args="should-stay-metadata")
    span.activity_event(duration_ms=10)
    tracker.close(span, result_kind="ok")
    assert [s for s, _ in seen] == ["phase_started", "phase_activity", "phase_finished"]
    assert seen[-1][1].name == PHASE_TOOL_EXECUTING
    assert seen[-1][1].activity["result_kind"] == "ok"


def test_7_chat_stream_argument_delta_is_metadata_only():
    acc = _ChatStreamAccumulator()
    events = list(acc.absorb(_Chunk(_Delta(tool_calls=[_ToolDelta(arguments='{"content":"secret text"}')] ))))
    types = [ev.type for ev in events]
    assert EVENT_TOOL_ARGUMENTS_DELTA in types
    delta = next(ev for ev in events if ev.type == EVENT_TOOL_ARGUMENTS_DELTA)
    assert delta.delta_chars == len('{"content":"secret text"}')
    assert delta.total_chars == delta.delta_chars
    assert delta.text == ""


def test_8_chat_stream_argument_finished_emits_metadata():
    acc = _ChatStreamAccumulator()
    list(acc.absorb(_Chunk(_Delta(tool_calls=[_ToolDelta(arguments='{"x": 1}')] ))))
    events = list(acc.absorb(_Chunk(_Delta(), finish_reason="tool_calls")))
    done = next(ev for ev in events if ev.type == EVENT_TOOL_ARGUMENTS_FINISHED)
    assert done.argument_field == "arguments"
    assert done.total_chars == len('{"x": 1}')
    assert done.text == ""


def test_9_tool_registry_phase_lifecycle():
    seen = []
    runtime = AgentRuntime(stream_enabled=True, cancel_token=CancelToken())
    runtime.phase_tracker.set_listener(lambda status, phase: seen.append((status, phase)))
    reg = ToolRegistry()
    reg.set_runtime(runtime)
    reg.register({"name": "demo", "parameters": {"type": "object", "properties": {}}}, lambda _args: tool_result(output="ok"))
    assert json.loads(reg.dispatch("demo", {}))["output"] == "ok"
    assert [s for s, _ in seen] == ["phase_started", "phase_finished"]
    assert seen[0][1].name == PHASE_TOOL_EXECUTING
    assert seen[0][1].activity["tool_name"] == "demo"


def test_10_tool_registry_error_phase():
    seen = []
    runtime = AgentRuntime(stream_enabled=True, cancel_token=CancelToken())
    runtime.phase_tracker.set_listener(lambda status, phase: seen.append((status, phase)))
    reg = ToolRegistry()
    reg.set_runtime(runtime)
    reg.register({"name": "boom", "parameters": {"type": "object", "properties": {}}}, lambda _args: (_ for _ in ()).throw(RuntimeError("x")))
    result = json.loads(reg.dispatch("boom", {}))
    assert "error" in result
    assert seen[-1][0] == "phase_error"
    assert seen[-1][1].activity["error_type"] == "RuntimeError"


def test_11_delegate_child_phase_helpers_are_aggregate_only():
    from tools import delegate_tool

    seen = []
    runtime = AgentRuntime(stream_enabled=True, cancel_token=CancelToken())
    runtime.phase_tracker.set_listener(lambda status, phase: seen.append((status, phase)))
    delegate_tool.set_delegate_context(delegate_tool.DelegateContext(chain=object(), model="fake", parent_toolset_names={"terminal"}, runtime=runtime))
    span = delegate_tool._start_child_agent_phase(mode="batch", task_count=3)
    delegate_tool._update_child_agent_phase(span, mode="batch", task_count=3, completed_count=1, failed_count=0)
    delegate_tool._close_child_agent_phase(span, mode="batch", task_count=3, completed_count=3, running_count=0, failed_count=0)
    payload = json.dumps([phase.__dict__ for _, phase in seen], ensure_ascii=False)
    assert PHASE_CHILD_AGENT_RUNNING in payload
    assert "completed_count" in payload
    assert "tool_trace" not in payload
    assert "summary" not in payload


def test_12_voice_skill_no_longer_instructs_terminal_say():
    text = (Path(__file__).resolve().parent.parent / "skills" / "voice-runtime" / "SKILL.md").read_text(encoding="utf-8")
    directive = text.split("---", 2)[1]
    assert "nano-voice-say" in text
    assert "不要" in directive and "nano-voice-say" in directive
    assert "requires_tools" not in directive
    assert "必须先" not in directive


def test_13_agent_exports_do_not_expose_abandoned_supervisor():
    import agent

    assert not hasattr(agent, "ProgressSupervisor")
    assert not hasattr(agent, "LLMPhraseProvider")
    assert hasattr(agent, "VoiceEventSink")
    assert hasattr(agent, "TurnEventEnvelope")


def test_14_preview_short_result_has_no_head_tail_overlap():
    """review #6：结果放得下时只放 head，不切重叠的 tail，且 result_truncated=False。"""
    os.environ["VOICE_ORCHESTRATOR_MAX_TOOL_RESULT_CHARS"] = "100"
    try:
        p = preview_tool_result("t", "ok", "abcdefg")
        assert p.result_truncated is False
        assert p.result_head.text == "abcdefg"
        assert p.result_tail.text == ""
        big = "H" * 60 + "M" * 200 + "T" * 60
        p2 = preview_tool_result("t", "ok", big)
        assert p2.result_truncated is True
        # head 只含开头段、tail 只含结尾段 —— 中段被丢，两段不重叠
        assert "T" not in p2.result_head.text
        assert "H" not in p2.result_tail.text
        assert len(p2.result_head.text) + len(p2.result_tail.text) <= 100
    finally:
        os.environ.pop("VOICE_ORCHESTRATOR_MAX_TOOL_RESULT_CHARS", None)


def test_15_child_agent_scope_suppresses_tool_phase():
    """review #1：子 agent 区间内 registry 不在父 tracker 上开工具 span。"""
    from agent.runtime_phase import enter_child_agent_scope, exit_child_agent_scope

    seen = []
    runtime = AgentRuntime(stream_enabled=True, cancel_token=None)
    runtime.phase_tracker = PhaseTracker(lambda status, phase: seen.append((status, phase.name)))
    reg = ToolRegistry()
    reg.set_runtime(runtime)
    reg.register({"name": "noop", "parameters": {"type": "object", "properties": {}}}, lambda _a: tool_result(output="ok"))

    tok = enter_child_agent_scope()
    try:
        reg.dispatch("noop", {})
    finally:
        exit_child_agent_scope(tok)
    assert seen == [], f"child-scope dispatch leaked phases: {seen}"

    # 区间外恢复正常：父工具仍上报 PHASE_TOOL_EXECUTING
    reg.dispatch("noop", {})
    assert [s for s, _ in seen] == ["phase_started", "phase_finished"]
    assert seen[0][1] == PHASE_TOOL_EXECUTING


def test_16_recent_messages_skipped_when_preview_disabled():
    """review #7：send_message_preview=False 时不走 recent_messages 预览循环。"""
    messages = [
        {"role": "system", "content": "secret system prompt"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    env = build_turn_event_envelope(
        event_type="phase_activity",
        session_id="s",
        turn_id="t",
        messages=messages,
        send_message_preview=False,
    ).to_dict()
    assert env["context"]["recent_messages"] == []
    # 最近用户目标仍单独保留
    assert env["context"]["last_user_message_preview"]["text"] == "hello"
    assert env["safety"]["message_preview_enabled"] is False


def test_17_shared_env_helpers_single_source():
    """review 折叠项：env 解析只有一份实现，两个 voice 模块都引用 agent.env。"""
    from agent import env as shared_env
    from agent import turn_events, voice_orchestrator_client

    assert turn_events._env_int is shared_env.env_int
    assert turn_events._env_bool is shared_env.env_bool
    assert voice_orchestrator_client.env_bool is shared_env.env_bool
    assert voice_orchestrator_client.env_float is shared_env.env_float
    assert shared_env.env_bool("X_NOPE_MISSING", True) is True
    assert shared_env.env_int("X_NOPE_MISSING", 9) == 9


def main() -> None:
    print("=" * 60)
    print("V27.1 Voice Orchestrator Host-Side Invariant Test")
    print(f"  case timeout: {TEST_CASE_TIMEOUT_SECONDS:g}s")
    print("=" * 60)

    tests = [
        test_1_envelope_uses_latest_user_and_omits_system,
        test_2_envelope_bounds_recent_messages,
        test_3_tool_preview_head_tail_and_redaction,
        test_4_sink_client_failure_isolated,
        test_5_sink_records_envelope_sync,
        test_6_phase_tracker_emits_metadata_only,
        test_7_chat_stream_argument_delta_is_metadata_only,
        test_8_chat_stream_argument_finished_emits_metadata,
        test_9_tool_registry_phase_lifecycle,
        test_10_tool_registry_error_phase,
        test_11_delegate_child_phase_helpers_are_aggregate_only,
        test_12_voice_skill_no_longer_instructs_terminal_say,
        test_13_agent_exports_do_not_expose_abandoned_supervisor,
        test_14_preview_short_result_has_no_head_tail_overlap,
        test_15_child_agent_scope_suppresses_tool_phase,
        test_16_recent_messages_skipped_when_preview_disabled,
        test_17_shared_env_helpers_single_source,
    ]
    failed = 0
    for test in tests:
        try:
            with _test_case_timeout(test.__name__):
                test()
            print(f"✓ {test.__name__}")
        except AssertionError as exc:
            print(f"✗ {test.__name__} — {exc}")
            failed += 1
        except TestCaseTimeoutError as exc:
            print(f"✗ {test.__name__} — TIMEOUT {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            import traceback
            print(f"✗ {test.__name__} — UNEXPECTED {type(exc).__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print("=" * 60)
    if failed:
        print(f"  FAIL: {failed}/{len(tests)} 不变量未通过")
        sys.exit(1)
    print(f"  PASS: {len(tests)}/{len(tests)} 不变量")


if __name__ == "__main__":
    main()
