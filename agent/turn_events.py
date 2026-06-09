"""V27.1 — safe turn envelopes for the external voice orchestrator.

The host process owns facts, not speech policy. This module builds compact,
redacted envelopes that another service can inspect to decide if/when/how to
speak. It intentionally avoids raw tool arguments, full outputs, file content,
system prompts, and chain-of-thought.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from agent.env import env_bool as _env_bool, env_int as _env_int
from agent.redact import redact as _redact_secrets


SCHEMA_VERSION = "voice-orchestrator.v1"
DEFAULT_MAX_MESSAGE_PREVIEWS = 4
DEFAULT_MAX_MESSAGE_CHARS = 800
DEFAULT_MAX_TOOL_RESULT_CHARS = 1200

_ABS_PATH_RE = re.compile(r"(?<!\w)(?:/Users/[^\s'\"]+|/var/[^\s'\"]+|/tmp/[^\s'\"]+|[A-Za-z]:\\[^\s'\"]+)")
_ENV_RE = re.compile(r"(?i)(?:^|[\s/])\.env(?:\b|[._-])")
_TRACE_RE = re.compile(r"(?is)Traceback \(most recent call last\):.*")
_LONG_ID_RE = re.compile(r"\b[a-f0-9]{32,}\b", re.IGNORECASE)


@dataclass
class TextPreview:
    """一段可外发的文本预览，以及它是否被截断/脱敏。"""

    text: str = ""
    chars: int = 0
    truncated: bool = False
    redacted: bool = False


@dataclass
class ToolPreview:
    """工具执行结果的安全预览，不包含原始参数和完整输出。"""

    name: str = ""
    status: str = ""
    duration_ms: Optional[int] = None
    result_head: TextPreview = field(default_factory=TextPreview)
    result_tail: TextPreview = field(default_factory=TextPreview)
    result_truncated: bool = False


@dataclass
class PhasePreview:
    """Runtime 当前阶段的 span 预览，用于告诉外部服务 host 正在做什么。"""

    name: str = ""
    status: str = ""
    span_id: str = ""
    elapsed_ms: Optional[int] = None
    activity: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnEventEnvelope:
    """发给 Voice Orchestrator 的最外层事件包。"""

    schema_version: str
    session_id: str
    turn_id: str
    event_type: str
    timestamp: float
    context: dict[str, Any] = field(default_factory=dict)
    assistant_activity: dict[str, Any] = field(default_factory=dict)
    tool: Optional[dict[str, Any]] = None
    phase: Optional[dict[str, Any]] = None
    safety: dict[str, Any] = field(default_factory=dict)

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
    reasoning_activity: str = "",
    next_tool_name: str = "",
    tool: Optional[ToolPreview | dict[str, Any]] = None,
    phase: Optional[PhasePreview | dict[str, Any]] = None,
    timestamp: Optional[float] = None,
    send_message_preview: Optional[bool] = None,
) -> TurnEventEnvelope:
    """构造一次可外发的语音编排事件。

    这里是 v27.1 的安全边界：host 只发送“事实预览”，不在本仓库决定
    是否播报、播几次、怎么说。外发内容会经过截断、脱敏和字段白名单处理，
    明确不发送 system prompt、原始工具参数、完整工具输出、文件内容和 CoT。
    """

    max_previews = _env_int("VOICE_ORCHESTRATOR_MAX_MESSAGE_PREVIEWS", DEFAULT_MAX_MESSAGE_PREVIEWS)
    max_message_chars = _env_int("VOICE_ORCHESTRATOR_MAX_MESSAGE_CHARS", DEFAULT_MAX_MESSAGE_CHARS)
    send_messages = _env_bool("VOICE_ORCHESTRATOR_SEND_MESSAGE_PREVIEW", True) if send_message_preview is None else send_message_preview

    # last_user_message_preview 总是保留，方便外部服务知道本轮用户目标。
    last_user = _last_role_content(messages, "user")
    user_preview = preview_text(last_user, max_chars=max_message_chars)
    recent_previews: list[dict[str, Any]] = []
    redactions = user_preview.redacted
    truncations = user_preview.truncated

    # recent_messages 是可配置的短窗口；system 消息会在 _recent_messages 里排除。
    if send_messages:
        for msg in _recent_messages(messages, max_previews):
            role = str(msg.get("role") or "")[:32]
            content = _message_content_preview(msg)
            p = preview_text(content, max_chars=max_message_chars)
            redactions = redactions or p.redacted
            truncations = truncations or p.truncated
            recent_previews.append({"role": role, "preview": asdict(p)})

    assistant_preview = preview_text(assistant_text, max_chars=max_message_chars)
    redactions = redactions or assistant_preview.redacted
    truncations = truncations or assistant_preview.truncated

    tool_dict = _coerce_dataclass_dict(tool)
    phase_dict = _coerce_dataclass_dict(phase)
    if tool_dict:
        # 工具预览本身可能带嵌套 TextPreview；把其中的安全标记折叠到 envelope 顶层。
        safety_ref = {"redactions": redactions, "truncations": truncations}
        _fold_safety(tool_dict, safety_ref)
        redactions = safety_ref["redactions"]
        truncations = safety_ref["truncations"]
    if phase_dict:
        # phase activity 只允许简单值；字符串统一做短标签化，避免把大段内容塞出去。
        phase_dict["activity"] = _safe_activity_dict(phase_dict.get("activity") or {})

    return TurnEventEnvelope(
        schema_version=SCHEMA_VERSION,
        session_id=_safe_identifier(session_id),
        turn_id=_safe_identifier(turn_id),
        event_type=_safe_identifier(event_type),
        timestamp=time.time() if timestamp is None else float(timestamp),
        context={
            "last_user_message_preview": asdict(user_preview),
            "recent_messages": recent_previews,
        },
        assistant_activity={
            "visible_text_preview": asdict(assistant_preview),
            "reasoning_safe_summary": _safe_activity_label(reasoning_activity),
            "next_tool_name": _safe_identifier(next_tool_name),
        },
        tool=tool_dict,
        phase=phase_dict,
        safety={
            "message_preview_enabled": send_messages,
            "redacted": redactions,
            "truncated": truncations,
            "omitted": [
                "system_prompt",
                "raw_tool_args",
                "raw_tool_output",
                "file_contents",
                "child_agent_transcripts",
                "raw_chain_of_thought",
            ],
        },
    )


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


def preview_tool_result(name: str, status: str, result: Any, *, duration_ms: Optional[int] = None) -> ToolPreview:
    """生成工具结果预览。

    工具输出可能很长或包含隐私信息，所以只取 head/tail 两段，并分别走
    ``preview_text``。是否携带工具输出预览由 ``VOICE_ORCHESTRATOR_SEND_TOOL_PREVIEW`` 控制。
    """

    max_chars = _env_int("VOICE_ORCHESTRATOR_MAX_TOOL_RESULT_CHARS", DEFAULT_MAX_TOOL_RESULT_CHARS)
    send_tool_preview = _env_bool("VOICE_ORCHESTRATOR_SEND_TOOL_PREVIEW", True)
    raw = "" if result is None or not send_tool_preview else str(result)
    truncated = len(raw) > max_chars
    if not truncated:
        # 整段放得下就只放 head，不再切 tail —— 否则 raw[:half] 和 raw[-half:]
        # 会重叠，把中段内容外发两遍（v27.1 review #6）。
        head = preview_text(raw, max_chars=max_chars)
        tail = preview_text("", max_chars=max_chars)
    else:
        # 真截断时才 head/tail 分头取，丢中段。head_len + tail_len == max_chars
        # < len(raw)，两段保证不重叠。
        head_len = max(0, max_chars // 2)
        tail_len = max_chars - head_len
        head = preview_text(raw[:head_len], max_chars=head_len)
        tail = preview_text(raw[-tail_len:] if tail_len else "", max_chars=tail_len)
    return ToolPreview(
        name=_safe_identifier(name),
        status=_safe_identifier(status),
        duration_ms=duration_ms,
        result_head=head,
        result_tail=tail,
        result_truncated=truncated,
    )


def redact_text(text: str) -> tuple[str, bool]:
    """对文本做保守脱敏，返回脱敏后的文本和是否发生过替换。

    密钥脱敏委托给 ``agent.redact.redact`` —— 那是经过校对的统一密钥表
    （sk-*/ghp_*/AKIA*/JWT/Bearer/私钥块/DB 连接串），避免本模块维护一份更弱的
    平行实现导致漏密（v27.1 review #2）。本函数只在其之上叠加语音外发场景特有的
    脱敏：traceback / 绝对路径 / .env / 长 hex id。
    """

    redacted = False
    secret_safe = _redact_secrets(text)
    redacted = redacted or (secret_safe != text)
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


def _recent_messages(messages: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """取最近的非 system 消息，避免把系统提示词发给外部语音服务。

    ``limit <= 0`` 显式返回空列表 —— 否则 ``candidates[-0:]`` 会退化成
    ``candidates[0:]`` 把整段对话全部外发（v27.1 review #3）。
    """

    if limit <= 0:
        return []
    candidates = [m for m in messages if m.get("role") != "system"]
    return candidates[-limit:]


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


def _fold_safety(value: Any, ref: dict[str, bool]) -> None:
    """递归收集嵌套预览里的 redacted/truncated 标记。"""

    if isinstance(value, dict):
        if "redacted" in value:
            ref["redactions"] = ref["redactions"] or bool(value["redacted"])
        if "truncated" in value or "result_truncated" in value:
            ref["truncations"] = ref["truncations"] or bool(value.get("truncated") or value.get("result_truncated"))
        for child in value.values():
            _fold_safety(child, ref)
    elif isinstance(value, list):
        for child in value:
            _fold_safety(child, ref)


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
