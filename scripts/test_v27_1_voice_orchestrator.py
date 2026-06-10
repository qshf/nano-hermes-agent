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
from agent.turn_events import PhasePreview, build_turn_event_envelope
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
    # v2: user_goal 只取最近一条 user（话题），不再外发 recent_messages
    assert env["user_goal"].startswith("最新问题")
    assert "旧问题" not in text
    assert "SYSTEM SECRET" not in text
    assert "/Users/qshf" not in text
    assert "sk-secret-token" not in text
    assert env["redacted"] is True
    # v2 扁平形状：无 context / assistant_activity / tool / phase / safety 嵌套
    assert "context" not in env
    assert "safety" not in env
    assert env["event_type"] == "turn_started"



def test_2_phase_translates_to_v2_activity():
    """v2: phase 预览在 envelope 边界翻译成 activity_* 事件 + activity 子对象。"""
    messages = [{"role": "user", "content": "重构 main.py"}]
    phase = PhasePreview(
        name="tool_executing",
        status="phase_finished",
        span_id="span_7",
        elapsed_ms=8200,
        activity={"tool_name": "read_file", "result_kind": "ok", "result": "找到 3 个文件", "total_chars": 120},
    )
    env = build_turn_event_envelope(
        event_type="phase_finished", session_id="s", turn_id="t", messages=messages, phase=phase,
    ).to_dict()
    assert env["event_type"] == "activity_finished"   # status 翻译成 wire 词表
    act = env["activity"]
    assert act["kind"] == "tool"                       # name 归一成 kind
    assert act["name"] == "read_file"                  # tool_name 提到 activity.name
    assert act["elapsed_ms"] == 8200
    assert act["span_id"] == "span_7"
    assert act["outcome"] == "ok"
    assert act["result"] == "找到 3 个文件"             # 工具结果短预览
    assert act["total_chars"] == 120
    assert env["user_goal"] == "重构 main.py"           # 话题每事件都带



def test_3_activity_result_redacted_and_capped():
    """v2: 工具结果经 activity.result 外发时仍脱敏，且按 ≤200 上限截断。"""
    messages = [{"role": "user", "content": "q"}]
    long_result = "head /Users/qshf/.env\n" + ("x" * 2000) + "\nTraceback (most recent call last): boom"
    phase = PhasePreview(
        name="tool_executing", status="phase_finished", span_id="s1", elapsed_ms=10,
        activity={"tool_name": "terminal", "result_kind": "ok", "result": long_result},
    )
    env = build_turn_event_envelope(
        event_type="phase_finished", session_id="s", turn_id="t", messages=messages, phase=phase,
    ).to_dict()
    result = env["activity"]["result"]
    assert "/Users/qshf" not in result
    assert "Traceback" not in result
    assert len(result) <= 200            # 默认 DEFAULT_MAX_ACTIVITY_RESULT_CHARS
    assert env["redacted"] is True



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
    # v2: 普通 registry 工具完成时也带 result（供 orchestrator 一视同仁播报）
    assert "result" in seen[-1][1].activity
    assert seen[-1][1].activity["result_kind"] == "ok"


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


def test_14_activity_result_send_toggle_off():
    """SEND_TOOL_PREVIEW=0 时 activity 不带 result（其余字段照常）。"""
    os.environ["VOICE_ORCHESTRATOR_SEND_TOOL_PREVIEW"] = "0"
    try:
        messages = [{"role": "user", "content": "q"}]
        phase = PhasePreview(
            name="tool_executing", status="phase_finished", span_id="s1", elapsed_ms=5,
            activity={"tool_name": "demo", "result_kind": "ok", "result": "should-not-leak"},
        )
        env = build_turn_event_envelope(
            event_type="phase_finished", session_id="s", turn_id="t", messages=messages, phase=phase,
        ).to_dict()
        assert "result" not in env["activity"]
        assert "should-not-leak" not in json.dumps(env, ensure_ascii=False)
        assert env["activity"]["outcome"] == "ok"
    finally:
        os.environ.pop("VOICE_ORCHESTRATOR_SEND_TOOL_PREVIEW", None)


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


def test_16_progress_carries_user_goal_and_no_message_leak():
    """v2: 高频 activity_progress 带 user_goal（话题），但不外发任何历史消息正文。"""
    messages = [
        {"role": "system", "content": "secret system prompt"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    phase = PhasePreview(
        name="assistant_generating_tool_arguments", status="phase_activity",
        span_id="s1", elapsed_ms=3000, activity={"tool_name": "write_file", "total_chars": 1800},
    )
    env = build_turn_event_envelope(
        event_type="phase_activity", session_id="s", turn_id="t", messages=messages, phase=phase,
    ).to_dict()
    assert env["event_type"] == "activity_progress"
    assert env["activity"]["kind"] == "generating_args"
    assert env["activity"]["total_chars"] == 1800
    assert env["user_goal"] == "hello"            # 话题保留
    text = json.dumps(env, ensure_ascii=False)
    assert "secret system prompt" not in text     # system 永不外发
    assert "hi there" not in text                 # 历史 assistant 正文不外发


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
        test_2_phase_translates_to_v2_activity,
        test_3_activity_result_redacted_and_capped,
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
        test_14_activity_result_send_toggle_off,
        test_15_child_agent_scope_suppresses_tool_phase,
        test_16_progress_carries_user_goal_and_no_message_leak,
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
