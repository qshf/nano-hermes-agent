"""V27.3 — voice_say 工具：结果朗读（非进度播报）。

边界（与 v27.1「主智能体零语音工具调用」的关系）见
``tools/voice_say_tool.py`` 模块 docstring 与 ``skills/voice-result-readout``。
本suite只验工具契约：注册、进 core toolset、Runtime 缺失优雅降级、空文本报错、
intent 兜底。**不**起真 Runtime（:8920）—— 把 ``NANO_VOICE_SAY_BIN`` 指向一个
必定不存在的路径，验证「语音不可用绝不掀翻 agent 任务」。

Run::

    .venv/bin/python scripts/test_v27_3_voice_say.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 指向必定不存在的二进制 —— 全程不碰真喇叭，验证降级路径。
os.environ["NANO_VOICE_SAY_BIN"] = "/nonexistent/path/nano-voice-say"

import tools  # noqa: F401  触发 *_tool.py 扫描自注册
from tools.registry import registry
from toolsets import resolve_toolsets


def _inner(raw: str) -> dict:
    """剥 V21.4 协议外壳 tool_result(output=...)，拿到内层 dict。"""
    payload = json.loads(raw)
    out = payload["output"]
    return out if isinstance(out, dict) else json.loads(out)


def test_1_registered_and_in_core_toolset():
    assert "voice_say" in registry.tool_names
    assert "voice_say" in resolve_toolsets(["core"])
    assert "voice_say" in resolve_toolsets(["docker"])


def test_2_degrades_when_runtime_absent():
    """二进制不存在 → spoken=false + reason，绝不抛、绝不让任务崩。"""
    inner = _inner(registry.dispatch("voice_say", {"text": "上海今天小雨，记得带伞。"}))
    assert inner["spoken"] is False
    assert "nano-voice-say" in inner["reason"]


def test_3_empty_text_is_error():
    """空文本是用法错误（非降级）—— 走 tool_error 独占 error 语义。"""
    payload = json.loads(registry.dispatch("voice_say", {"text": "   "}))
    assert payload.get("error") is not None
    assert "spoken" not in payload


def test_4_bad_intent_falls_back():
    """非法 intent 不报错，兜底到 info（仍走降级路径，spoken=false）。"""
    inner = _inner(registry.dispatch("voice_say", {"text": "结论是这样。", "intent": "bogus"}))
    assert inner["spoken"] is False  # 二进制不存在仍降级；关键是没因 intent 抛


def main() -> None:
    tests = [
        test_1_registered_and_in_core_toolset,
        test_2_degrades_when_runtime_absent,
        test_3_empty_text_is_error,
        test_4_bad_intent_falls_back,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"✓ {test.__name__}")
        except AssertionError as exc:
            print(f"✗ {test.__name__} — {exc}")
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
