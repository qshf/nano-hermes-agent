"""V27.1 — safe turn envelopes for the external voice orchestrator.

The host process owns facts, not speech policy. This module builds compact,
redacted envelopes that another service can inspect to decide if/when/how to
speak. It intentionally avoids raw tool arguments, full outputs, file content,
system prompts, and chain-of-thought.

v2 wire shape（voice-orchestrator.v2）
=====================================
扁平到「顶层 7 字段 + 一个 activity 子对象」。两个数据源（手写 turn 事件 / phase span
事件）在本模块出口归一成同一套词表：

    turn_started / turn_finished        ← 手写（带 user_goal / assistant_text）
    activity_started / _progress / _finished  ← phase span（带 activity{...}）

phase span 的内部状态（phase_started/activity/finished/error/cancelled）是**运行时
观测概念**，只在 ``build_turn_event_envelope`` 这个边界翻译成 wire 的 activity_* 词表 +
``activity.outcome``——运行时代码不需要知道 wire 词表（消除 v1 里 ``phase.status ==
event_type`` 的冗余）。
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from agent.env import env_bool as _env_bool, env_int as _env_int
from agent.redact import redact as _redact_secrets


SCHEMA_VERSION = "voice-orchestrator.v2"
DEFAULT_MAX_MESSAGE_CHARS = 800
DEFAULT_MAX_ACTIVITY_RESULT_CHARS = 200

_ABS_PATH_RE = re.compile(r"(?<!\w)(?:/Users/[^\s'\"]+|/var/[^\s'\"]+|/tmp/[^\s'\"]+|[A-Za-z]:\\[^\s'\"]+)")
_ENV_RE = re.compile(r"(?i)(?:^|[\s/])\.env(?:\b|[._-])")
_TRACE_RE = re.compile(r"(?is)Traceback \(most recent call last\):.*")
_LONG_ID_RE = re.compile(r"\b[a-f0-9]{32,}\b", re.IGNORECASE)

# phase span 内部状态 → v2 wire event_type（出口翻译，运行时不感知 wire 词表）
_PHASE_STATUS_TO_EVENT = {
    "phase_started": "activity_started",
    "phase_activity": "activity_progress",
    "phase_finished": "activity_finished",
    "phase_error": "activity_finished",
    "phase_cancelled": "activity_finished",
}
# 仅 *_finished 类事件带 outcome
_PHASE_STATUS_TO_OUTCOME = {
    "phase_finished": "ok",
    "phase_error": "error",
    "phase_cancelled": "cancelled",
}
# phase span 名 → v2 activity.kind（归一动词）
_PHASE_NAME_TO_KIND = {
    "assistant_generating_text": "generating_text",
    "assistant_generating_tool_arguments": "generating_args",
    "tool_executing": "tool",
    "child_agent_running": "child_agent",
}


@dataclass
class TextPreview:
    """一段可外发的文本预览，以及它是否被截断/脱敏。"""

    text: str = ""
    chars: int = 0
    truncated: bool = False
    redacted: bool = False


@dataclass
class PhasePreview:
    """Runtime 当前阶段的 span 预览（listener 内部载荷）。

    这是**运行时观测**结构，由 ``PhaseSpan.preview`` 构造，喂给
    ``build_turn_event_envelope`` 在边界翻译成 v2 activity。不直接外发。
    """

    name: str = ""
    status: str = ""
    span_id: str = ""
    elapsed_ms: Optional[int] = None
    activity: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnEventEnvelope:
    """发给 Voice Orchestrator 的最外层事件包（v2 扁平形状）。"""

    schema_version: str
    session_id: str
    turn_id: str
    event_type: str
    timestamp: float
    user_goal: str = ""
    activity: Optional[dict[str, Any]] = None
    assistant_text: str = ""
    redacted: bool = False

    def to_dict(self) -> dict[str, Any]:
        """把 dataclass 递归转成普通 dict，供 HTTP client JSON 序列化。"""

        return asdict(self)


def build_turn_event_envelope(
    *,
    event_type: str,
    session_id: str,
    turn_id: str,
    messages: list[dict[str, Any]],
    assistant_text: str = "",
    phase: Optional[PhasePreview | dict[str, Any]] = None,
    timestamp: Optional[float] = None,
) -> TurnEventEnvelope:
    """构造一次可外发的语音编排事件（v2）。

    安全边界不变：host 只发送“事实预览”，经过截断、脱敏和字段白名单，明确不发送
    system prompt、原始工具参数、完整工具输出、文件内容和 CoT。

    - 有 ``phase`` → activity 事件：event_type / activity 由 phase 翻译得到。
    - 无 ``phase`` → 手写 turn 事件：event_type 直接用传入值（turn_started/finished）。
    """

    max_message_chars = _env_int("VOICE_ORCHESTRATOR_MAX_MESSAGE_CHARS", DEFAULT_MAX_MESSAGE_CHARS)

    # user_goal：本轮用户目标，生成播报措辞的核心话题。仅一次 last-user 反查 + 一次
    # redact，远轻于 v1 的 recent_messages 全量遍历——可挂在高频 activity_progress 上。
    goal_preview = preview_text(_last_role_content(messages, "user"), max_chars=max_message_chars)
    redacted = goal_preview.redacted or goal_preview.truncated

    final_event_type = event_type
    activity: Optional[dict[str, Any]] = None
    phase_dict = _coerce_dataclass_dict(phase)
    if phase_dict is not None:
        final_event_type, activity, act_redacted = _build_activity(phase_dict)
        redacted = redacted or act_redacted

    assistant_out = ""
    if assistant_text:
        ap = preview_text(assistant_text, max_chars=max_message_chars)
        assistant_out = ap.text
        redacted = redacted or ap.redacted or ap.truncated

    return TurnEventEnvelope(
        schema_version=SCHEMA_VERSION,
        session_id=_safe_identifier(session_id),
        turn_id=_safe_identifier(turn_id),
        event_type=_safe_identifier(final_event_type),
        timestamp=time.time() if timestamp is None else float(timestamp),
        user_goal=goal_preview.text,
        activity=activity,
        assistant_text=assistant_out,
        redacted=redacted,
    )


def _build_activity(phase_dict: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
    """把内部 phase 预览翻译成 v2 (event_type, activity, redacted)。"""

    status = str(phase_dict.get("status") or "")
    event_type = _PHASE_STATUS_TO_EVENT.get(status, "activity_progress")

    raw = dict(phase_dict.get("activity") or {})
    activity: dict[str, Any] = {
        "kind": _PHASE_NAME_TO_KIND.get(str(phase_dict.get("name") or ""), _safe_identifier(phase_dict.get("name"))),
        "name": _safe_identifier(raw.pop("tool_name", "") or ""),
        "elapsed_ms": phase_dict.get("elapsed_ms"),
        "span_id": _safe_identifier(phase_dict.get("span_id") or ""),
    }
    redacted = False

    outcome = _PHASE_STATUS_TO_OUTCOME.get(status)
    if outcome is not None:
        # 应用层错误（如 memory 工具返回 {"error":...}）span 仍是正常 finish，
        # 用 result_kind 把这种错误冒到 outcome，保留可观测性。
        if outcome == "ok" and raw.get("result_kind") == "error":
            outcome = "error"
        activity["outcome"] = outcome

    # result 只在工具完成时携带，且受 SEND_TOOL_PREVIEW 开关与 ≤200 字上限约束。
    if "result" in raw:
        raw_result = raw.pop("result")
        if _env_bool("VOICE_ORCHESTRATOR_SEND_TOOL_PREVIEW", True):
            rp = preview_text(raw_result, max_chars=_env_int("VOICE_ORCHESTRATOR_MAX_ACTIVITY_RESULT_CHARS", DEFAULT_MAX_ACTIVITY_RESULT_CHARS))
            activity["result"] = rp.text
            redacted = redacted or rp.redacted or rp.truncated

    # 其余安全元信息（delta_chars/total_chars/completed_count/... 及 result_kind）折进 activity。
    activity.update(_safe_activity_dict(raw))
    return event_type, activity, redacted


def preview_text(value: Any, *, max_chars: int) -> TextPreview:
    """把任意值转成安全文本预览。

    先脱敏，再按字符数截断；``chars`` 记录的是原始长度，便于外部服务知道
    这是一段短文本还是被压缩过的大文本。
    """

    text = "" if value is None else str(value)
    original_len = len(text)
    text, redacted = redact_text(text)
    truncated = False
    if max_chars >= 0 and len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return TextPreview(text=text, chars=original_len, truncated=truncated, redacted=redacted)


def redact_text(text: str) -> tuple[str, bool]:
    """对文本做保守脱敏，返回脱敏后的文本和是否发生过替换。

    密钥脱敏委托给 ``agent.redact.redact`` —— 那是经过校对的统一密钥表
    （sk-*/ghp_*/AKIA*/JWT/Bearer/私钥块/DB 连接串），避免本模块维护一份更弱的
    平行实现导致漏密（v27.1 review #2）。本函数只在其之上叠加语音外发场景特有的
    脱敏：traceback / 绝对路径 / .env / 长 hex id。
    """

    secret_safe = _redact_secrets(text)
    redacted = secret_safe != text
    text = secret_safe
    for pattern, replacement in (
        (_TRACE_RE, "[redacted-traceback]"),
        (_ABS_PATH_RE, "[redacted-path]"),
        (_ENV_RE, " [redacted-env]"),
        (_LONG_ID_RE, "[redacted-id]"),
    ):
        text, n = pattern.subn(replacement, text)
        redacted = redacted or bool(n)
    return text, redacted


def _last_role_content(messages: list[dict[str, Any]], role: str) -> str:
    """从后往前找某个 role 的最后一条可预览内容。"""

    for msg in reversed(messages):
        if msg.get("role") == role:
            return _message_content_preview(msg)
    return ""


def _message_content_preview(msg: dict[str, Any]) -> str:
    """把 message 压成可预览字符串；tool call 只暴露数量，不暴露原始参数。"""

    content = msg.get("content")
    if content is None and msg.get("tool_calls"):
        return f"[{len(msg.get('tool_calls') or [])} tool call(s)]"
    return "" if content is None else str(content)


def _coerce_dataclass_dict(value: Any) -> Optional[dict[str, Any]]:
    """接受 dataclass 或 dict，统一转成 dict；其他类型直接丢弃。"""

    if value is None:
        return None
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return None


def _safe_activity_dict(value: dict[str, Any]) -> dict[str, Any]:
    """清洗 phase activity，只保留安全 key 和简单值。"""

    safe: dict[str, Any] = {}
    for key, val in value.items():
        k = _safe_identifier(key)
        if isinstance(val, (int, float, bool)) or val is None:
            safe[k] = val
        else:
            safe[k] = _safe_activity_label(str(val))
    return safe


def _safe_activity_label(value: str) -> str:
    """把 activity 中的字符串压成单行短标签。"""

    text, _ = redact_text(value.strip())
    return text.replace("\n", " ")[:120]


def _safe_identifier(value: Any) -> str:
    """把事件名、工具名、session id 等字段限制成短的安全标识符。"""

    text = "" if value is None else str(value)
    return "".join(ch for ch in text if ch.isalnum() or ch in "_:-.")[:96]
