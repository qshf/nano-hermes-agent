"""Explicit voice_say tool backed by the local nano-voice CLI."""

from __future__ import annotations

from agent.voice_dispatch import get_voice_dispatcher, normalize_voice_text
from tools.registry import registry
from tools.result import tool_error, tool_result

VOICE_SAY_SCHEMA = {
    "name": "voice_say",
    "description": "Speak one short sentence through the local voice runtime.",
    "parameters": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["info", "progress", "warning", "urgent", "done"],
                "description": "Speech intent used by the voice runtime.",
            },
            "text": {
                "type": "string",
                "description": "One short sentence to speak; do not include code or logs.",
            },
        },
        "required": ["intent", "text"],
    },
}


def voice_say_handler(args: dict) -> str:
    intent = (args.get("intent") or "info").strip()
    text = normalize_voice_text(args.get("text"))
    if not text:
        return tool_error("Parameter 'text' is required and must not be empty.")
    result = get_voice_dispatcher().speak(intent=intent, text=text, source="agent")
    payload = {
        "spoken": result.status == "accepted",
        "source": "agent",
        "intent": intent,
        "text_chars": result.text_chars,
        "status": result.status,
    }
    if result.error:
        payload["error"] = result.error
    return tool_result(payload)


registry.register(VOICE_SAY_SCHEMA, voice_say_handler)
