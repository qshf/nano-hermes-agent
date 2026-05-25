"""V19 — 教学版 LLM 错误分类器。

设计与裁剪
----------
源项目 ``hermes-agent/agent/error_classifier.py`` 1058 行，区分 14 种 ``FailoverReason``
（auth / billing / rate_limit / overloaded / server_error / timeout /
context_overflow / payload_too_large / image_too_large / model_not_found /
provider_policy_blocked / format_error / thinking_signature / long_context_tier
/ oauth_long_context_beta_forbidden / llama_cpp_grammar_pattern / unknown），
每个 reason 还有十几种 provider-specific 串匹配 + status code 优先级。

nano 只保留**教学最小集** — failover 决策只关心一个二元问题：
**"该不该切到下一家？"**

三类即可表达：
1. ``RETRYABLE``  — 同一家再试一次（瞬时网络抖动 / 5xx / timeout）
2. ``FAILOVER``   — 这家彻底不行，切下一家（rate_limit / 401-403 auth / billing / overloaded）
3. ``FATAL``      — 用户/输入问题，切了也是错（400 format / context_overflow / model_not_found）

匹配优先级：先看 HTTP status code（如果 SDK 错误对象暴露了），再看错误消息里的关键词。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Optional


class ErrorAction(enum.Enum):
    """下一步动作 — 上层 chain 按此决策。"""

    RETRYABLE = "retryable"   # 同 transport 再试（带 backoff）
    FAILOVER = "failover"     # 切下一个 transport
    FATAL = "fatal"           # 切了也没用，直接抛出


@dataclass
class ClassifiedError:
    """分类结果。"""

    action: ErrorAction
    reason: str           # 人类可读的 reason，用于日志（"rate_limit" / "timeout" / ...）
    status_code: Optional[int] = None
    message: str = ""


# ── 关键词模式 ──────────────────────────────────────────────────────────
# 顺序敏感：billing 在 rate_limit 前，避免 "credit balance limit" 误判
_BILLING_PATTERNS = (
    "insufficient credits",
    "insufficient_quota",
    "insufficient balance",
    "credit balance",
    "billing",
    "payment required",
    "exceeded your current quota",
)

_RATE_LIMIT_PATTERNS = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "throttled",
    "requests per minute",
    "tokens per minute",
    "try again in",
    "resource_exhausted",
)

_AUTH_PATTERNS = (
    "invalid api key",
    "incorrect api key",
    "authentication",
    "unauthorized",
    "forbidden",
    "permission denied",
)

_OVERLOAD_PATTERNS = (
    "overloaded",
    "service unavailable",
    "temporarily unavailable",
    "capacity",
)

_TIMEOUT_PATTERNS = (
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "read timeout",
)

_CONTEXT_OVERFLOW_PATTERNS = (
    "context length",
    "context_length_exceeded",
    "maximum context",
    "input is too long",
    "prompt is too long",
    "too many tokens",
)

_FORMAT_ERROR_PATTERNS = (
    "invalid request",
    "bad request",
    "malformed",
    "validation error",
)

_MODEL_NOT_FOUND_PATTERNS = (
    "model not found",
    "model_not_found",
    "no such model",
    "unknown model",
)


def _extract_status_code(exc: BaseException) -> Optional[int]:
    """从常见 SDK 异常对象提取 HTTP status code。

    OpenAI SDK / Anthropic SDK / httpx 都把 status code 挂在 ``exc.status_code``
    或 ``exc.response.status_code`` 上，nano 兼容这两种形态即可。
    """
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(exc, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def classify_error(exc: BaseException) -> ClassifiedError:
    """把 SDK 抛出的 Exception 分类为 ErrorAction。

    决策顺序：
    1. status code 明确 → 直接判定
    2. 串匹配 → 兜底
    3. 都没匹配 → RETRYABLE（保守 — 让上层重试一次再说）
    """
    status_code = _extract_status_code(exc)
    msg = str(exc).lower()

    # ── 1. status code 优先 ──────────────────────────────────
    if status_code is not None:
        if status_code in (401, 403):
            return ClassifiedError(ErrorAction.FAILOVER, "auth", status_code, msg)
        if status_code == 402:
            return ClassifiedError(ErrorAction.FAILOVER, "billing", status_code, msg)
        if status_code == 429:
            return ClassifiedError(ErrorAction.FAILOVER, "rate_limit", status_code, msg)
        if status_code in (503, 529):
            return ClassifiedError(ErrorAction.FAILOVER, "overloaded", status_code, msg)
        if status_code in (500, 502, 504):
            return ClassifiedError(ErrorAction.RETRYABLE, "server_error", status_code, msg)
        if status_code == 408:
            return ClassifiedError(ErrorAction.RETRYABLE, "timeout", status_code, msg)
        if status_code == 404:
            return ClassifiedError(ErrorAction.FAILOVER, "model_not_found", status_code, msg)
        if status_code == 413:
            return ClassifiedError(ErrorAction.FATAL, "payload_too_large", status_code, msg)
        if status_code == 400:
            # 400 可能是 context_overflow 或 format_error — 看消息细分
            if any(p in msg for p in _CONTEXT_OVERFLOW_PATTERNS):
                return ClassifiedError(ErrorAction.FATAL, "context_overflow", status_code, msg)
            return ClassifiedError(ErrorAction.FATAL, "format_error", status_code, msg)

    # ── 2. 关键词兜底 ────────────────────────────────────────
    if any(p in msg for p in _BILLING_PATTERNS):
        return ClassifiedError(ErrorAction.FAILOVER, "billing", status_code, msg)
    if any(p in msg for p in _RATE_LIMIT_PATTERNS):
        return ClassifiedError(ErrorAction.FAILOVER, "rate_limit", status_code, msg)
    if any(p in msg for p in _AUTH_PATTERNS):
        return ClassifiedError(ErrorAction.FAILOVER, "auth", status_code, msg)
    if any(p in msg for p in _OVERLOAD_PATTERNS):
        return ClassifiedError(ErrorAction.FAILOVER, "overloaded", status_code, msg)
    if any(p in msg for p in _CONTEXT_OVERFLOW_PATTERNS):
        return ClassifiedError(ErrorAction.FATAL, "context_overflow", status_code, msg)
    if any(p in msg for p in _MODEL_NOT_FOUND_PATTERNS):
        return ClassifiedError(ErrorAction.FAILOVER, "model_not_found", status_code, msg)
    if any(p in msg for p in _TIMEOUT_PATTERNS):
        return ClassifiedError(ErrorAction.RETRYABLE, "timeout", status_code, msg)
    if any(p in msg for p in _FORMAT_ERROR_PATTERNS):
        return ClassifiedError(ErrorAction.FATAL, "format_error", status_code, msg)

    # ── 3. 兜底 — 未识别错误，保守重试 ────────────────────────
    return ClassifiedError(ErrorAction.RETRYABLE, "unknown", status_code, msg)
