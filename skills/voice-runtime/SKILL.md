---
name: voice-runtime
description: "Speak progress updates aloud via a TTS backend (demo skill for v26.1 availability gating)."
required_environment_variables:
  - DASHSCOPE_API_KEY
metadata:
  category: demo
  requires_tools:
    - terminal
---

# voice-runtime

把 agent 的进度更新念出来的演示 skill。它存在的唯一目的是演示 **v26.1 可用性门控**：

- 声明 `required_environment_variables: [DASHSCOPE_API_KEY]` —— 没设这个 env 时，
  `/skill list` 会显示 `⚠ (setup: set DASHSCOPE_API_KEY)`，system prompt 索引里
  也会带这个软标记。设了 env 重启后标记消失。
- 声明 `metadata.requires_tools: [terminal]` —— 如果当前 agent 没有暴露 `terminal`
  工具，这个 skill 会被**硬隐藏**（根本不进索引），因为没有 terminal 它无法朗读。

## 用法（占位）

真实实现会在这里调用 TTS API（如 DashScope 语音合成），把一段文本转成音频并播放。
本 nano 演示版不实现真实 TTS —— 它只是 v26.1 门控逻辑的可观测载体。
