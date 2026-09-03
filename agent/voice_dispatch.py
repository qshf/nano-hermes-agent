"""Voice dispatch through the local ``nano-voice-say`` CLI."""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

from agent.redact import redact

log = logging.getLogger(__name__)

_INTENTS = {"info", "progress", "warning", "urgent", "done"}
_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class VoiceResult:
    status: str
    intent: str
    text_chars: int
    error: str | None = None


def normalize_voice_text(value: object, *, limit: int = 160) -> str:
    """Return one short, complete sentence suitable for speech."""
    text = " ".join(str(value or "").split())
    text = text.strip("\"'` ")
    if not text:
        return ""
    for index, char in enumerate(text):
        if char in "。！？.!?":
            text = text[: index + 1]
            break
    return redact(text[:limit].strip())


class CliVoiceDispatcher:
    """Invoke the local CLI; voice failures never escape into the agent turn."""

    def __init__(self) -> None:
        self.binary = os.environ.get(
            "NANO_VOICE_SAY_BIN",
            "/Users/qshf/my-project/nano_hermes_agent/.venv/bin/nano-voice-say",
        )
        self.host = os.environ.get("VOICE_RUNTIME_HOST", "127.0.0.1")
        self.port = os.environ.get("VOICE_RUNTIME_PORT", "8920")
        self.timeout = float(os.environ.get("VOICE_DISPATCH_TIMEOUT_SECONDS", "2"))

    @staticmethod
    def enabled() -> bool:
        return os.environ.get("VOICE_READOUT_ENABLED", "0").strip().lower() in _TRUE_VALUES

    def speak(self, *, intent: str, text: str, source: str = "middleware") -> VoiceResult:
        if intent not in _INTENTS:
            return VoiceResult("failed", intent, 0, f"unsupported intent: {intent}")
        text = normalize_voice_text(text)
        if not text:
            return VoiceResult("failed", intent, 0, "empty text")
        if not self.enabled():
            return VoiceResult("disabled", intent, len(text))

        command = [
            self.binary,
            "--intent", intent,
            "--text", text,
            "--host", self.host,
            "--port", str(self.port),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("voice dispatch failed source=%s: %s", source, exc)
            return VoiceResult("failed", intent, len(text), str(exc))
        if completed.returncode != 0:
            error = (completed.stderr or completed.stdout or "CLI returned non-zero").strip()
            log.warning("voice dispatch failed source=%s: %s", source, error)
            return VoiceResult("failed", intent, len(text), error[:500])
        return VoiceResult("accepted", intent, len(text))


_dispatcher: CliVoiceDispatcher | None = None


def get_voice_dispatcher() -> CliVoiceDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = CliVoiceDispatcher()
    return _dispatcher
