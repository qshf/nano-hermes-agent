---
name: voice-readout
description: "模型生命周期语音播报的宿主策略与文案约束。"
metadata:
  category: voice
---

# voice-readout

宿主在每次模型生成前后触发 `before_model_call` / `after_model_call`。宿主会用一次
非流式 helper model 生成短播报文案，再交给本地 CLI；本 skill 只描述策略边界，实际执行
由宿主 `VoiceReadoutService` 完成，不要求模型调用 `terminal`。

## Hook 约定

- `before_model_call`：每次模型生成前执行一次，首次使用 `info`，后续使用 `progress`。
- `after_model_call`：每次响应标准化后执行一次；包含 tool call 时只记录，不播报；最终无
  tool call 时使用 `done`。
- helper model 调用必须使用 `tools=[]`、短输出上限，并绕过两个语音 hook，避免递归。
- `VOICE_READOUT_ENABLED` 不是启用值时，两个 hook 都静默返回。

## 文案约束

- 先生成完整文案，再一次性提交给 `nano-voice-say`。
- 不发送流式中间片段；文案取首句，最长 160 个字符。
- 不播报密钥、完整日志、代码、路径和长 ID。
- CLI 或 Runtime 失败不能阻断主模型调用。
