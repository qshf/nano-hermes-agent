"""Model lifecycle hooks for automatic voice readouts."""

from __future__ import annotations

import logging
from collections import defaultdict
from contextvars import ContextVar
from typing import Any

from agent.redact import redact
from agent.voice_dispatch import VoiceResult, get_voice_dispatcher, normalize_voice_text
from tools.hooks import hook_manager

log = logging.getLogger(__name__)
_VOICE_INTERNAL = ContextVar("voice_internal", default=False)


class VoiceReadoutService:
    """Keep readout policy outside the model and tool implementations."""

    def __init__(self) -> None:
        self._before_counts: dict[str, int] = defaultdict(int)
        self._readout_history: dict[str, list[str]] = defaultdict(list)

    @staticmethod
    def _tool_calls(response: object) -> list[Any]:
        return list(getattr(response, "tool_calls", None) or [])

    @staticmethod
    def _response_text(response: object) -> str:
        content = getattr(response, "content", "")
        if isinstance(content, list):
            content = " ".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            )
        return normalize_voice_text(content)

    def _fallback_text(self, *, count: int, turn_id: str, messages: list[dict]) -> str:
        """Produce a context-aware fallback without repeating a canned sentence."""
        hint = ""
        latest_role = ""
        for message in reversed(messages):
            role = str(message.get("role", ""))
            if role not in {"user", "assistant", "tool"}:
                continue
            latest_role = role
            if role == "user":
                content = message.get("content", "")
                if isinstance(content, list):
                    content = " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
                hint = " ".join(str(content or "").split()).strip("。！？.!? ")[:28]
            break

        if count == 0 and hint:
            candidates = [
                f"我先从“{hint}”入手。",
                f"我先梳理一下“{hint}”的处理方向。",
                f"我先把“{hint}”拆开确认关键点。",
            ]
        elif latest_role == "tool":
            candidates = [
                "工具结果已经回来，我接着核对下一步。",
                "前一步有结果了，我继续整理关键信息。",
                "我沿着刚才拿到的结果继续往下处理。",
            ]
        elif hint:
            candidates = [
                f"我继续围绕“{hint}”推进。",
                f"我根据刚才的进展继续处理“{hint}”。",
                f"我再核对一下“{hint}”的关键部分。",
            ]
        else:
            candidates = [
                "我先梳理任务的关键步骤。",
                "我已经进入处理流程，接着确认关键信息。",
                "我根据当前进展继续往下处理。",
            ]

        history = self._readout_history[turn_id]
        text = next((candidate for candidate in candidates if candidate not in history), candidates[0])
        history.append(text)
        if len(history) > 12:
            del history[:-12]
        return text

    def _remember(self, turn_id: str, text: str) -> None:
        history = self._readout_history[turn_id]
        if text and text not in history:
            history.append(text)
        if len(history) > 12:
            del history[:-12]

    @staticmethod
    def _render_recent_context(messages: list[dict], *, limit: int = 2200) -> str:
        """Build a bounded, redacted context for the helper readout model."""
        rendered: list[str] = []
        remaining = limit
        for message in reversed(messages):
            role = str(message.get("role", ""))
            if role not in {"user", "assistant", "tool"}:
                continue
            content = message.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(item.get("text", "")) if isinstance(item, dict) else str(item)
                    for item in content
                )
            text = redact(" ".join(str(content).split()))
            if not text:
                continue
            text = text[: min(700, remaining)]
            rendered.append(f"{role}: {text}")
            remaining -= len(text)
            if remaining <= 0 or len(rendered) >= 5:
                break
        return "\n".join(reversed(rendered)) or "(no readable task context)"

    @staticmethod
    def _helper_text(
        *,
        chain: Any,
        model: str,
        prompt: list[dict],
    ) -> str:
        """Generate one readout sentence with a non-streaming helper call."""
        if chain is None or _VOICE_INTERNAL.get():
            return ""
        token = _VOICE_INTERNAL.set(True)
        try:
            call_kwargs = {
                "model": model,
                "messages": prompt,
                "tools": [],
                # v4 reasoning models can consume a short budget without emitting
                # visible content. Readout text needs a direct, non-thinking answer.
                "max_tokens": 160,
                "temperature": 0.2,
                "voice_internal": True,
            }
            if model.lower().startswith("deepseek-v4"):
                call_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            response = chain.call(
                **call_kwargs,
            )
        finally:
            _VOICE_INTERNAL.reset(token)
        content = getattr(response, "content", "")
        if isinstance(content, list):
            content = " ".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            )
        return normalize_voice_text(content)

    def before_model(
        self,
        *,
        messages: list[dict],
        model: str,
        turn_id: str,
        stream_enabled: bool,
        tools: list[dict],
        chain: Any = None,
        **_: Any,
    ) -> VoiceResult | None:
        del stream_enabled, tools
        dispatcher = get_voice_dispatcher()
        if not dispatcher.enabled() or _VOICE_INTERNAL.get():
            return VoiceResult("disabled", "info", 0)
        count = self._before_counts[turn_id]
        self._before_counts[turn_id] += 1
        intent = "info" if count == 0 else "progress"
        phase = (
            "这是本次任务的起手播报，说明正在开始处理什么，不要说已经完成。"
            if count == 0
            else "这是一次新的模型调用前播报，说明当前正在推进什么或下一步做什么，不要伪造结果。"
        )
        previous = "\n".join(self._readout_history[turn_id][-4:]) or "(没有已播报句子)"
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是任务语音播报文案生成器。根据任务上下文生成一句口语化短句。跟随用户的主要语言；只输出播报正文，不要 Markdown、引号、标签、解释或工具调用。不要朗读代码、日志、长数据、秘密或原始工具输出。"
                    f"{phase}不要重复下面已经播报过的句子：\n{previous}"
                ),
            },
            {
                "role": "user",
                "content": f"当前任务上下文：\n{self._render_recent_context(messages)}",
            },
        ]
        try:
            text = self._helper_text(chain=chain, model=model, prompt=prompt)
        except Exception:
            log.warning("voice before-model helper failed; using fallback", exc_info=True)
            text = ""
        if text in self._readout_history[turn_id]:
            log.info("voice before-model helper repeated prior readout model=%s turn=%s", model, turn_id)
            text = ""
        if not text:
            log.warning("voice before-model helper returned empty content model=%s turn=%s", model, turn_id)
            text = self._fallback_text(count=count, turn_id=turn_id, messages=messages)
        self._remember(turn_id, text)
        return dispatcher.speak(intent=intent, text=text, source="middleware")

    def after_model(
        self,
        *,
        messages: list[dict],
        model: str,
        turn_id: str,
        response: object,
        stream_enabled: bool,
        chain: Any = None,
        **_: Any,
    ) -> VoiceResult | None:
        del stream_enabled
        dispatcher = get_voice_dispatcher()
        if not dispatcher.enabled() or _VOICE_INTERNAL.get():
            return VoiceResult("disabled", "done", 0)
        # A tool-call response is not final. The next before hook announces progress,
        # while the after hook remains available for metrics/observers.
        if self._tool_calls(response):
            return None
        previous = "\n".join(self._readout_history[turn_id][-4:]) or "(没有已播报句子)"
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是任务完成摘要生成器。根据最后一轮模型输出生成一句口语化收尾播报。"
                    "只输出播报正文，不要 Markdown、引号、标签、解释或工具调用。不要复述长代码、"
                    f"日志、表格或原始工具输出；如果答案很短，就压缩成一句自然的完成总结。不要重复这些已播报句子：\n{previous}"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"当前任务上下文：\n{self._render_recent_context(messages)}\n\n"
                    f"最后一轮模型输出：\n{redact(str(getattr(response, 'content', '') or ''))}"
                ),
            },
        ]
        try:
            text = self._helper_text(chain=chain, model=model, prompt=prompt)
        except Exception:
            log.warning("voice after-model helper failed; using response fallback", exc_info=True)
            text = ""
        if text in self._readout_history[turn_id]:
            log.info("voice after-model helper repeated prior readout model=%s turn=%s", model, turn_id)
            text = ""
        if not text:
            log.warning("voice after-model helper returned empty content model=%s turn=%s", model, turn_id)
            text = self._response_text(response) or "这轮处理已经完成。"
        self._remember(turn_id, text)
        result = dispatcher.speak(intent="done", text=text, source="middleware")
        self._before_counts.pop(turn_id, None)
        self._readout_history.pop(turn_id, None)
        return result


voice_readout_service = VoiceReadoutService()


def _before_model_call(**kwargs: Any) -> VoiceResult | None:
    return voice_readout_service.before_model(**kwargs)


def _after_model_call(**kwargs: Any) -> VoiceResult | None:
    return voice_readout_service.after_model(**kwargs)


_installed = False


def install_voice_hooks() -> None:
    global _installed
    if _installed:
        return
    hook_manager.register("before_model_call", _before_model_call)
    hook_manager.register("after_model_call", _after_model_call)
    _installed = True


def uninstall_voice_hooks() -> None:
    global _installed
    if not _installed:
        return
    hook_manager.deregister("before_model_call", _before_model_call)
    hook_manager.deregister("after_model_call", _after_model_call)
    _installed = False
