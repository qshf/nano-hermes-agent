"""voice_say — 朗读最终结果给用户（V27.3，结果朗读，非进度播报）。

为什么是「工具」而不是让 agent 直接拼 terminal
================================================
用 terminal 拼 ``nano-voice-say --text "..."`` 本就能做，但中文 + 引号转义易错，
失败退出码混在输出里，且没有可测路径。收成一个工具：一次调用、无 shell 转义、
失败结构化降级、registry dispatch 有测试路径。

与 v27.1 边界的关系（关键）
==========================
v27.1 的核心不变量是「主智能体零语音工具调用」—— host 只发 bounded envelope，
**进度**播不播由外部 orchestrator 定，agent 不自主插话。本工具是那条线的**正交
例外**，不是推翻：

- v27.1 禁的：agent 自主判断**进度**该不该播（value/cooldown/phrase-LLM）。
- 本工具做的：**用户显式要求**时，念**最终结果**——这是 agent 独有的交付物，
  orchestrator 只收 bounded envelope，物理上拿不到完整答案，没法替它念。

所以职责正交：orchestrator 管「过程旁白」，voice_say 管「用户点名要的结果朗读」。
触发边界（仅用户显式要求、念结果不念进度、不自主触发）由 description + 配套
skill ``voice-result-readout`` 焊死。

连接方式
========
subprocess 调 ``nano-voice-say`` 二进制（保持两仓解耦，nano 不 import
nano_voice_kit）。二进制路径走 env ``NANO_VOICE_SAY_BIN``，默认指向 voice_kit
的 .venv。Runtime（:8920）没起时 CLI 返回非零 + stderr，本工具捕获成
``{"spoken": false, "reason": ...}`` —— 语音不可用绝不让 agent 任务崩。
"""

import os
import subprocess

from tools.registry import registry
from tools.result import tool_error, tool_result

_DEFAULT_BIN = "/Users/qshf/my-project/nano_voice_kit/.venv/bin/nano-voice-say"
_INTENTS = ("info", "progress", "warning", "urgent", "done")

VOICE_SAY_SCHEMA = {
    "name": "voice_say",
    "description": (
        "Read the FINAL RESULT of a task aloud to the user through the voice speaker. "
        "Call this ONLY when the user has explicitly asked to hear the result spoken "
        "(e.g. '把结果念出来', 'read it to me', '播报一下总结'). "
        "Speak the RESULT/summary, never progress — process narration is handled "
        "separately by the external voice orchestrator, not by this tool. "
        "Do NOT call this proactively or to announce that you are working; only on an "
        "explicit user request to vocalize a result. Keep the text to a concise spoken "
        "summary (1-3 sentences), not a wall of text. If the speaker runtime is down "
        "the call degrades silently (spoken=false) and your task still succeeds."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Concise spoken summary of the result (1-3 sentences).",
            },
            "intent": {
                "type": "string",
                "enum": list(_INTENTS),
                "description": "Voice intent; use 'info' for a normal result readout (default).",
            },
        },
        "required": ["text"],
    },
}


def voice_say_handler(args: dict) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return tool_error("text is required (the spoken summary).")

    intent = args.get("intent") or "info"
    if intent not in _INTENTS:
        intent = "info"

    binary = os.environ.get("NANO_VOICE_SAY_BIN", _DEFAULT_BIN)

    try:
        result = subprocess.run(
            [binary, "--intent", intent, "--text", text],
            capture_output=True,
            text=True,
            timeout=float(os.environ.get("NANO_VOICE_SAY_TIMEOUT_SECONDS", "12")),
        )
    except FileNotFoundError:
        # 二进制不存在：语音不可用，但任务不该崩 —— 结构化降级。
        return tool_result(output={
            "spoken": False,
            "reason": f"nano-voice-say not found at {binary!r}; set NANO_VOICE_SAY_BIN",
        })
    except subprocess.TimeoutExpired:
        return tool_result(output={"spoken": False, "reason": "voice runtime timed out"})
    except Exception as exc:  # noqa: BLE001 - 语音失败绝不掀翻 agent 任务
        return tool_result(output={"spoken": False, "reason": f"{type(exc).__name__}: {exc}"})

    if result.returncode == 0:
        return tool_result(output={"spoken": True, "intent": intent, "text": text})
    # Runtime 没起 / 发送失败：CLI 已在 stderr 给了排查提示，原样带回。
    return tool_result(output={
        "spoken": False,
        "reason": (result.stderr or result.stdout or "voice runtime unavailable").strip(),
    })


# 自注册（tools/__init__.py 扫描 *_tool.py 时触发）
registry.register(VOICE_SAY_SCHEMA, voice_say_handler)
