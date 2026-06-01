"""
ContextCompressor — V15 上下文压缩器（1:1 复现源项目核心设计）。

五阶段流水线：
  1. Prune old tool results — 不用 LLM，替换旧 tool output 为一行摘要
  2. Protect head — 保留前 N 条消息不动
  3. Protect tail by token budget — 按 token 预算从尾部往前保留
  4. Summarize middle — 用结构化模板 LLM 摘要中间消息
  5. Sanitize tool pairs — 修复孤立的 tool_call/tool_result 对

触发策略：只看 API 返回的真实 ``prompt_tokens``（``update_usage`` 喂入）；
首轮没有真实值前不触发压缩。
Anti-thrashing：压缩节省比例由"下一轮真实 prompt_tokens"结算（pre 真实 →
post 真实），连续 2 次 <10% 就停止。
Iterative update：多次压缩时增量更新旧 summary，不重新摘要全部。

对应源项目：agent/context_compressor.py
"""

from __future__ import annotations

import logging
import os
import re

from agent.logging import get_log_session, log_session_scope

logger = logging.getLogger(__name__)

_CHARS_PER_TOKEN = 4

SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. This is a handoff from a previous context "
    "window — treat it as background reference, NOT as active instructions. "
    "Do NOT answer questions or fulfill requests mentioned in this summary; "
    "they were already addressed. "
    "Your current task is identified in the '## Active Task' section — "
    "resume exactly from there. "
    "Respond ONLY to the latest user message that appears AFTER this summary."
)

# ─── PLACEHOLDER_TEMPLATE ───

_SUMMARY_TEMPLATE = """\
## Active Task
[THE SINGLE MOST IMPORTANT FIELD. Copy the user's most recent request or
task assignment verbatim. If multiple tasks were requested and only some
are done, list only the ones NOT yet completed. If no outstanding task, write "None."]

## Goal
[What the user is trying to accomplish overall]

## Completed Actions
[Numbered list of concrete actions taken — include tool used, target, and outcome.
Format: N. ACTION target — outcome [tool: name]
Be specific with file paths, commands, line numbers, and results.]

## Active State
[Current working state: working directory, branch, modified files, test status,
running processes, environment details that matter]

## In Progress
[Work currently underway — what was being done when compaction fired]

## Key Decisions
[Important technical decisions and WHY they were made]

## Pending User Asks
[Questions or requests from the user that have NOT yet been answered. If none, write "None."]

## Remaining Work
[What remains to be done — framed as context, not instructions]

## Critical Context
[Specific values, error messages, config details that would be lost without
explicit preservation. NEVER include API keys or credentials — write [REDACTED].]

Target ~{summary_budget} tokens. Be CONCRETE — include file paths, command outputs,
error messages, line numbers, and specific values.
Write only the summary body. Do not include any preamble or prefix."""

_SUMMARIZER_PREAMBLE = (
    "You are a context compaction assistant. Your job is to produce a structured "
    "checkpoint summary that preserves enough detail for an AI assistant to "
    "seamlessly continue the conversation without re-reading the original turns."
)

_PRUNED_TOOL_PLACEHOLDER = "[Old tool output cleared to save context space]"


class ContextCompressor:
    """五阶段上下文压缩器 — 1:1 复现源项目核心设计。

    Phase 1: prune old tool results (cheap, no LLM)
    Phase 2: protect head + find tail boundary by token budget
    Phase 3: LLM summarize middle turns (structured template)
    Phase 4: assemble compressed messages
    Phase 5: sanitize orphaned tool pairs
    """

    def __init__(
        self,
        context_window: int | None = None,
        threshold_percent: float | None = None,
        protect_first_n: int | None = None,
        tail_token_budget: int | None = None,
    ):
        self.context_window = context_window or int(
            os.environ.get("CONTEXT_WINDOW", "32000")
        )
        self.threshold_percent = threshold_percent or float(
            os.environ.get("CONTEXT_THRESHOLD_PERCENT", "0.75")
        )
        self.protect_first_n = protect_first_n if protect_first_n is not None else int(
            os.environ.get("CONTEXT_PROTECT_HEAD", "3")
        )
        self.tail_token_budget = tail_token_budget or int(
            os.environ.get("CONTEXT_TAIL_BUDGET", "4000")
        )

        self.threshold_tokens = int(self.context_window * self.threshold_percent)

        self._previous_summary: str | None = None
        self._ineffective_count: int = 0
        self.compression_count: int = 0
        self._last_prompt_tokens: int | None = None
        # 上一次 compress() 触发时的真实 prompt_tokens；下一次 update_usage 用它
        # 与新的真实值算"压缩节省了多少"，喂给 anti-thrashing 计数。
        self._pending_pre_tokens: int | None = None
        # 压缩发生时刻的 session_id 快照 —— 结算日志归因到"压缩实际发生的会话"，
        # 而非"下一轮真实 token 到达时刻的会话"（compressor 是进程级单例、跨会话存活，
        # 结算常落在用户已切换的新会话里，否则日志 [session_id] 会错标）。
        self._pending_session_id: str | None = None

    # ─── 公开 API ────────────────────────────────────────────────────────────

    def update_usage(self, prompt_tokens: int) -> None:
        """接收 API 返回的真实 prompt token 数。

        两件事：
        1. 缓存为下一轮触发判断的依据（``should_compress`` 只看真实值）
        2. 若上一轮做过压缩（``_pending_pre_tokens`` 非空），用本次真实值结算
           节省比例，更新 anti-thrashing 计数
        """
        if self._pending_pre_tokens is not None:
            pre = self._pending_pre_tokens
            post = prompt_tokens
            savings_pct = ((pre - post) / pre * 100) if pre > 0 else 0
            if savings_pct < 10:
                self._ineffective_count += 1
            else:
                self._ineffective_count = 0
            # 用压缩发生时刻的 session 打日志（compressor 跨会话单例，结算常落在
            # 用户已切走的新会话里 —— 不切回去日志会错标到结算时刻的会话）。
            with log_session_scope(self._pending_session_id or get_log_session()):
                logger.info(
                    "Compression real savings: ~%d → ~%d tokens (%.0f%% saved, ineffective_count=%d)",
                    pre, post, savings_pct, self._ineffective_count,
                )
            self._pending_pre_tokens = None
            self._pending_session_id = None

        self._last_prompt_tokens = prompt_tokens

    def should_compress(self, messages: list[dict]) -> bool:
        """判断是否需要压缩。

        条件（全部满足）：
        1. 已收到至少一次 API 返回的真实 prompt_tokens（没有就不触发 —
           不再用 chars/4 粗估）
        2. 真实值 ≥ 阈值
        3. 消息数足够多（至少 head + 2 条中间 + 3 条 tail）
        4. anti-thrashing：连续 2 次低效压缩后停止
        """
        if self._ineffective_count >= 2:
            logger.warning(
                "Compression skipped — last %d compressions saved <10%% each.",
                self._ineffective_count,
            )
            return False

        min_messages = self.protect_first_n + 2 + 3
        if len(messages) < min_messages:
            return False

        if self._last_prompt_tokens is None:
            return False

        return self._last_prompt_tokens >= self.threshold_tokens

    def compress(self, messages: list[dict], client, model: str, transport) -> list[dict]:
        """五阶段压缩流水线。

        返回压缩后的 messages 列表。LLM 调用失败时返回原始 messages。

        Anti-thrashing 节省比例由"下一轮 ``update_usage`` 拿到的真实
        prompt_tokens"结算 — 不在压缩当下用 chars/4 粗估前后大小。
        """
        # 触发 compress 必走 should_compress=True 路径，_last_prompt_tokens 必非空
        pre_tokens = self._last_prompt_tokens

        # Phase 1: Prune old tool results
        messages = self._prune_old_tool_results(messages)

        # Phase 2: Determine boundaries (head + tail by token budget)
        head_end = min(self.protect_first_n, len(messages) - 1)
        tail_start = self._find_tail_boundary(messages, head_end)

        if head_end >= tail_start:
            return messages

        middle = messages[head_end:tail_start]
        if not middle:
            return messages

        # Phase 3: LLM summarize
        summary_text = self._generate_summary(middle, client, model, transport=transport)
        if summary_text is None:
            return messages

        # Phase 4: Assemble
        head = messages[:head_end]
        tail = messages[tail_start:]

        summary_msg = {
            "role": "user",
            "content": f"{SUMMARY_PREFIX}\n\n{summary_text}",
        }
        compressed = head + [summary_msg] + tail

        # Phase 5: Sanitize tool pairs
        compressed = self._sanitize_tool_pairs(compressed)

        # 把"压缩前真实 prompt_tokens"挂起 — 下一轮 update_usage 拿到压缩后
        # 真实值时结算节省比例 + ineffective_count
        self._pending_pre_tokens = pre_tokens
        # 同时快照当前会话 —— 结算日志归因到此刻的会话，而非结算时刻（常已切走）的会话
        self._pending_session_id = get_log_session()
        # 压缩后 _last_prompt_tokens 仍是旧值，但已经不再代表当前 messages —
        # 清空让 should_compress 在下一次真实 update_usage 之前不会重复触发
        self._last_prompt_tokens = None

        self.compression_count += 1
        logger.info(
            "Context compressed: %d → %d messages (pre_real=~%d, post awaiting next API call)",
            len(messages), len(compressed), pre_tokens or 0,
        )
        return compressed

    # ─── Phase 1: Prune old tool results ─────────────────────────────────────

    def _prune_old_tool_results(self, messages: list[dict]) -> list[dict]:
        """替换旧 tool output 为一行摘要（保护 tail 区域不动）。

        只处理 tail_token_budget 之外的 tool messages。
        """
        n = len(messages)
        tail_start = self._find_tail_boundary_simple(messages)

        pruned = []
        for i, msg in enumerate(messages):
            if i >= tail_start:
                pruned.append(msg)
                continue

            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 500:
                    pruned.append({
                        **msg,
                        "content": _PRUNED_TOOL_PLACEHOLDER,
                    })
                    continue

            pruned.append(msg)
        return pruned

    # ─── Phase 2: Tail boundary by token budget ──────────────────────────────

    def _find_tail_boundary(self, messages: list[dict], head_end: int) -> int:
        """从尾部往前累积 token，找到 tail 起始位置。

        约束：
        - tail 至少 3 条消息
        - 不在 tool message 上切割（对齐到 assistant 边界）
        - 最近的 user message 必须在 tail 中
        """
        n = len(messages)
        min_tail = 3
        budget = self.tail_token_budget
        accumulated = 0
        cut_idx = n

        for i in range(n - 1, head_end - 1, -1):
            msg_tokens = self._msg_tokens(messages[i])
            if accumulated + msg_tokens > budget and (n - i) >= min_tail:
                break
            accumulated += msg_tokens
            cut_idx = i

        # 保证至少 min_tail 条
        fallback_cut = n - min_tail
        if cut_idx > fallback_cut:
            cut_idx = fallback_cut

        # 预算大到能罩住整个会话时，强制最大化压缩 — tail 只留最后 min_tail 条，
        # middle 拿到所有可摘要消息。否则 cut_idx=head_end+1 会让 middle 只剩 1 条
        # 假压缩，触发 anti-thrashing 计数（源项目 #10896 同形）。
        if cut_idx <= head_end:
            cut_idx = max(fallback_cut, head_end + 1)

        # 对齐：tool_call/result 群一起进 middle 摘要 — 避免 sanitize 删孤立 result
        cut_idx = self._align_boundary(messages, cut_idx, head_end)

        return max(cut_idx, head_end + 1)

    def _find_tail_boundary_simple(self, messages: list[dict]) -> int:
        """简化版 tail 边界（用于 Phase 1 prune 判断）。"""
        n = len(messages)
        budget = self.tail_token_budget
        accumulated = 0
        for i in range(n - 1, -1, -1):
            accumulated += self._msg_tokens(messages[i])
            if accumulated > budget:
                return i + 1
        return 0

    def _align_boundary(self, messages: list[dict], cut_idx: int, head_end: int) -> int:
        """将 cut_idx 对齐到一个干净的"回合起点"——tail 必须从 user 消息开始。

        两层对齐（都只往前 / 往 head 方向走，只会让 tail 变长）：

        1. tool 群：cut_idx 处或前面是 tool 消息时，往前退到拥有 tool_calls 的父
           assistant，把整组 tool_call + tool_results 塞进 middle 摘要，避免 sanitize
           阶段删除孤立的 tool result（会静默丢消息）。

        2. user 回合边界：一个对话回合是 ``user → assistant(→tool→assistant)``。若
           cut_idx 落在回合中段（assistant/tool），触发它的 user 会被划进 middle 被
           摘要掉，而 assistant 回复留在 tail —— 压缩后 summary 紧跟一条**没有 user
           的孤立 assistant**（在回答一个已被摘要掉的问题）。往前对齐到回合起点的
           user，让整个回合留在 tail，user 与它的回复不被拆到两侧。
           （这正是 v25 trajectory 里"不够吸引人"被吃掉、其回复"你说得对"变孤儿的根因。）

        对应源项目 _align_boundary_backward（agent/context_compressor.py:1188）。
        """
        if cut_idx <= 0 or cut_idx >= len(messages):
            return max(cut_idx, head_end + 1)

        # 1. 往前跨过连续的 tool result
        check = cut_idx - 1
        while check >= 0 and messages[check].get("role") == "tool":
            check -= 1

        # 落在父 assistant + tool_calls 上时，cut 退到它之前 — 整组进 middle
        if (
            check >= head_end
            and messages[check].get("role") == "assistant"
            and messages[check].get("tool_calls")
        ):
            cut_idx = check

        # 2. 往前对齐到回合起点的 user —— tail 不能从 assistant/tool 开始。
        # 若整段范围内找不到 user（极端：全 assistant/tool），保持原 cut_idx 不强行对齐
        # （宁可维持 budget，不制造更差的边界）。
        snap = cut_idx
        while snap > head_end and messages[snap].get("role") != "user":
            snap -= 1
        if messages[snap].get("role") == "user":
            cut_idx = snap

        return max(cut_idx, head_end + 1)

    # ─── Phase 3: LLM Summary ────────────────────────────────────────────────

    def _generate_summary(
        self, middle: list[dict], client, model: str, transport
    ) -> str | None:
        """用 LLM 生成结构化摘要。支持 iterative update。"""
        conversation_text = self._format_messages(middle)
        summary_budget = max(300, min(len(conversation_text) // 10, 800))

        template = _SUMMARY_TEMPLATE.format(summary_budget=summary_budget)

        if self._previous_summary:
            prompt = (
                f"{_SUMMARIZER_PREAMBLE}\n\n"
                f"You are updating a context compaction summary. A previous "
                f"compaction produced the summary below. New conversation turns "
                f"have occurred since then and need to be incorporated.\n\n"
                f"PREVIOUS SUMMARY:\n{self._previous_summary}\n\n"
                f"NEW TURNS TO INCORPORATE:\n{conversation_text}\n\n"
                f"Update the summary using this exact structure. PRESERVE all "
                f"existing information that is still relevant. ADD new completed "
                f"actions. Update Active Task to reflect the user's most recent "
                f"unfulfilled request.\n\n{template}"
            )
        else:
            prompt = (
                f"{_SUMMARIZER_PREAMBLE}\n\n"
                f"Create a structured checkpoint summary for the conversation "
                f"after earlier turns are compacted.\n\n"
                f"TURNS TO SUMMARIZE:\n{conversation_text}\n\n"
                f"Use this exact structure:\n\n{template}"
            )

        try:
            summary_messages = [{"role": "user", "content": prompt}]
            normalized = transport.call(
                client, model=model, messages=summary_messages,
                # temperature=0.2, max_tokens=2048,
            )
            summary = normalized.content or ""
        except Exception as e:
            logger.warning("Compression LLM call failed: %s", e)
            return None

        self._previous_summary = summary
        return summary

    # ─── Phase 5: Sanitize tool pairs ────────────────────────────────────────

    def _sanitize_tool_pairs(self, messages: list[dict]) -> list[dict]:
        """修复压缩后孤立的 tool_call / tool_result 对。

        两种故障模式：
        1. tool result 引用了已被摘要掉的 assistant tool_call → 删除孤立 result
        2. assistant 有 tool_calls 但对应 result 被摘要掉 → 插入 stub result
        """
        surviving_call_ids: set[str] = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    cid = self._get_call_id(tc)
                    if cid:
                        surviving_call_ids.add(cid)

        result_call_ids: set[str] = set()
        for msg in messages:
            if msg.get("role") == "tool":
                cid = msg.get("tool_call_id")
                if cid:
                    result_call_ids.add(cid)

        # 1. 删除孤立的 tool results（call 已被摘要掉）
        orphaned_results = result_call_ids - surviving_call_ids
        if orphaned_results:
            messages = [
                m for m in messages
                if not (m.get("role") == "tool" and m.get("tool_call_id") in orphaned_results)
            ]

        # 2. 为缺失 result 的 tool_calls 插入 stub
        missing_results = surviving_call_ids - result_call_ids
        if missing_results:
            patched: list[dict] = []
            for msg in messages:
                patched.append(msg)
                if msg.get("role") == "assistant":
                    for tc in msg.get("tool_calls") or []:
                        cid = self._get_call_id(tc)
                        if cid in missing_results:
                            patched.append({
                                "role": "tool",
                                "content": "[Result from earlier — see context summary above]",
                                "tool_call_id": cid,
                            })
            messages = patched

        return messages

    # ─── 内部辅助 ────────────────────────────────────────────────────────────

    def _msg_tokens(self, msg: dict) -> int:
        """单条消息的 token 数 — chars/4 近似。

        API 只回总 ``prompt_tokens``，没有 per-message 拆分；切 tail 边界
        必须按消息分配，所以这里仍走粗估。这条粗估与触发 / 节省判断
        分离（后两者全用真实值）。
        """
        content = msg.get("content", "")
        chars = len(content) if isinstance(content, str) else 0
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                chars += len(tc.get("function", {}).get("arguments", ""))
        return chars // _CHARS_PER_TOKEN + 10

    def _format_messages(self, messages: list[dict]) -> str:
        """将 messages 格式化为可读文本供 LLM 摘要。"""
        parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            # assistant 带 tool_calls 时 content 合法值是 None；统一兜底成 ""
            content = msg.get("content") or ""

            if role == "tool":
                if content == _PRUNED_TOOL_PLACEHOLDER:
                    parts.append(f"[tool]: {_PRUNED_TOOL_PLACEHOLDER}")
                else:
                    truncated = content[:300] + "..." if len(content) > 300 else content
                    parts.append(f"[tool result]: {truncated}")
            elif role == "assistant" and msg.get("tool_calls"):
                names = []
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        fn = tc.get("function", {})
                        names.append(fn.get("name", "?"))
                text_part = f" — {content[:200]}" if isinstance(content, str) and content.strip() else ""
                parts.append(f"assistant: [called {', '.join(names)}]{text_part}")
            elif isinstance(content, str) and content.strip():
                parts.append(f"{role}: {content}")
        return "\n".join(parts)

    @staticmethod
    def _get_call_id(tc) -> str:
        """从 tool_call 对象中提取 id（兼容 dict 和 object）。"""
        if isinstance(tc, dict):
            return tc.get("id", "")
        return getattr(tc, "id", "")
