---
name: voice-result-readout
description: "用户显式要求把最终结果念出来时，用 voice_say 工具朗读结果摘要（不是进度）。进度播报仍归外部 Voice Orchestrator。"
metadata:
  category: voice
  requires_tools:
    - voice_say
---

# voice-result-readout

把**最终结果**朗读给用户的唯一正确姿势。和 [[voice-runtime]] 是一对：voice-runtime
管「过程旁白由外部 orchestrator 自动处理，你别碰」，本 skill 管「用户点名要听结果时，
你主动念」——两者职责正交，别混。

## 什么时候用

**仅当用户显式要求把结果/总结读出来**，例如：

- 「把结果念出来」「读给我听」「播报一下总结」「说一下结论」
- 「voice 念一下」「用语音告诉我」

只有上面这类**明确的朗读请求**才触发。

## 什么时候**不要**用

- ❌ 不要用它播**进度**（「我正在查…」「马上好」）—— 那是外部 Voice Orchestrator
  基于 host side-channel 自动做的事，见 [[voice-runtime]]。你自主播进度会和它打架。
- ❌ 不要在用户没要求朗读时主动调用，哪怕你觉得结果很重要。
- ❌ 不要把整段长输出塞进去念。

## 怎么用

调 `voice_say` 工具，`text` 给一段**口语化的结果摘要（1–3 句）**，不是把屏幕上的
表格/markdown 原样念。`intent` 一般用 `info`（默认）。

例：用户问完天气后说「念给我听」——

```
voice_say(text="上海今天小雨，体感25度，未来三天都有雨，出门记得带伞。", intent="info")
```

念的是**提炼后的结论**，不是 wttr.in 的原始大段文本。

## 失败了怎么办

喇叭 Runtime（:8920）没起时，工具返回 `{"spoken": false, "reason": ...}` 而不是
报错——语音不可用绝不影响你的主任务。这种情况下正常用文本回复即可，可顺带提一句
「语音服务似乎没在运行」。
