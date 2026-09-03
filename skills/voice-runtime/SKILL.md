---
name: voice-runtime
description: "本地 nano_voice_kit Runtime、nano-voice-say CLI 的连接配置与故障处理。"
metadata:
  category: voice
  requires_tools: []
---

# voice-runtime

底层是常驻的本地语音 Runtime（`nano_voice_kit`，云 API 合成 + 本机播放）。模型生命周期
播报由宿主的 `before_model_call` / `after_model_call` hook 负责；本 skill 只说明 Runtime
和 CLI 的可用性，不要求模型自行播报。

## 调用方式

用 `terminal` 工具执行：

```bash
nano-voice-say --intent <intent> --text "<文案>"
```

若提示找不到命令（venv 未激活），改用绝对路径：

```bash
/Users/qshf/my-project/nano_hermes_agent/.venv/bin/nano-voice-say --intent <intent> --text "<文案>"
```

`<intent>` 只能是以下 5 个之一，按语义选：

| intent | 何时用 | 队列行为 |
|--------|--------|----------|
| `info` | 一般信息播报 | 排队依次播 |
| `progress` | 进度更新 | 替换还没播的旧进度（不堆积过期播报）|
| `warning` | 风险 / 注意事项 | 清空待播后插入 |
| `urgent` | 需要立刻打断当前播报 | 打断当前播放，立即播 |
| `done` | 任务最终完成 | 清空待播后播收尾 |

## 文案与执行边界

- 模型前后 hook 负责决定播报内容和 intent；本 skill 不增加额外时机。
- 每次先拿到完整短句，再一次性执行 CLI；不发送流式 token。
- 口语化、短句，不播报代码、路径或长串 ID。
- `--text` 里避免双引号；要带引号时用单引号包整个文案。

## 运行前检查

调用前用 `terminal` 确认 Runtime 在运行：

```bash
nano-voice-runtime status
```

**Runtime 没起时不要自己启动它。** `nano-voice-runtime start` 是常驻服务，应由用户预先在
独立终端用 `nano-voice-runtime start --daemon` 起好。你只负责 `say`。若 status 报未运行，
就提示用户执行 `nano-voice-runtime start --daemon`，然后**继续干主任务**，仅告知用户语音暂不可用——
`nano-voice-say` 在 Runtime 没起时会返回非零退出码，属预期，不要因此中断或反复重试启动。

> 门控说明：Runtime 是否可用由 `VOICE_READOUT_ENABLED`、CLI 路径和 Runtime 地址决定；
> 这些变量由宿主 dispatcher 在调用时读取，skill 索引本身不再强制要求 `terminal` 或云端 key。
