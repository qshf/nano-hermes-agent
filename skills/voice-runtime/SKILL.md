---
name: voice-runtime
description: "外部语音运行时/编排器就绪提示。主智能体不直接调用语音工具，进度播报由 host side-channel 与 Voice Orchestrator 处理。"
required_environment_variables:
  - DASHSCOPE_API_KEY
metadata:
  category: voice
inject_directive: |
  语音播报由宿主 Runtime 的 v27.1 side-channel 与外部 Voice Orchestrator 处理。
  你不要为了语音播报调用 `terminal`，不要调用 `nano-voice-say`，也不要在最终回复前做
  额外的语音 done 调用。

  你的职责仍是把关键状态自然写进正常文本回复：阶段完成、风险、阻塞、需要用户选择、最终结论。
  语音服务会基于受限 envelope、assistant/tool preview 和 runtime phase lease 自行判断是否播、播几次、
  何时播、怎么播。若语音服务不可用，不影响主任务。
---

# voice-runtime

本 skill 表示本地语音运行时/外部 Voice Orchestrator 可以作为 side-channel 使用。

v27.1 起，assistant 不再通过工具主动播报语音。主 agent 只把受限运行事实发送给外部 orchestrator：

- 当前 turn 的安全 message preview；
- assistant 可见文本预览 / 安全 activity；
- 工具名、状态、耗时、截断后的结果 head/tail；
- runtime phase lease，例如模型生成工具参数、工具执行、父进程等待子 agent。

Voice Orchestrator 负责：

- 找到最后一轮 `role=user` 的输入；
- 根据 assistant 回复、工具状态和 active phase 判断是否播报；
- 控制每轮播报次数、间隔、降噪和 heartbeat；
- 调用 `nano_voice_kit` 完成实际播放。

assistant 不需要也不应该调用 `nano-voice-say`。如果需要同步进度，请只在正常文本回复里说明关键状态。
