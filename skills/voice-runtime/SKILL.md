---
name: voice-runtime
description: "把任务进展用语音播报出来。在任务起手、长耗时步骤、风险、阻塞、阶段完成、最终完成时调用，让用户不盯屏幕也能跟进。"
required_environment_variables:
  - DASHSCOPE_API_KEY
metadata:
  category: voice
  requires_tools:
    - terminal
inject_directive: |
  语音播报已就绪：你必须用 `terminal` 调
  `nano-voice-say --intent <intent> --text "<简短文案>"` 主动播报。这些是强制检查点，不是可选建议。

  存活心跳由 Runtime 自动维持（v26.4）：长命令阻塞、多步推进期间，宿主后台会自动
  播"还在处理"的存活进度，你**不需要**自己定时播心跳。你只负责在**状态真正变化**时播报。

  关键检查点（强制）：
  - 起手：在第一个实质性动作（任何工具调用、长分析、改文件、跑耗时命令）之前，必须先播一条
    `info`，说明你现在开始做什么。任务再短也要先播一条极短起手。
  - 阶段：每进入新的耗时步骤、或一批工具/检索/分析结束得出新状态结论时，播 `progress`。
  - 风险：发现风险或注意点播 `warning`；出现阻塞、继续无意义播 `urgent`。
  - 完成：输出最终文本回复之前，必须先做一次完成态检查——结果已成形且无未解决阻塞，就先播 `done`
    再回复；纯阅读/分析/总结/问答任务也不例外，不能因为"没改代码"就跳过完成播报。

  封堵失败模式（最重要）：
  - 文本更新与语音播报不是二选一。只要你正要发的中间文本里包含新状态（切了路径/数据源、拿到样本、
    阶段切换、下一步变了、风险已确认），就必须先播报再发文本——只发文字不发语音是错误。
  - 禁止出现"已经连续做了几步、文本里也多次同步了新状态，但期间一条语义播报都没播"。除非那些文本
    纯属礼貌过渡、不含任何新状态。（纯存活提示由 Runtime 自动兜底，不用你管。）

  规则：intent 五选一；文案口语化短句、一次一个要点、不念代码/路径/长 ID；必须真执行命令而非只打印
  命令字符串。若 `nano-voice-say` 返回非零（Runtime 未起），告知用户一次即可，继续干主任务，不要反复
  重试、也不要自己去 `start` 服务。详细规则按需 skill_view 读 voice-runtime 正文。
---

# voice-runtime

用语音播报任务进展。底层是常驻的本地语音 Runtime（`nano_voice_kit`，云 API 合成 + 本机播放）。
你通过 `terminal` 工具调用 `nano-voice-say` 命令把文案发给 Runtime。

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

## 何时播报

- **起手**：接到任务、开始执行时，用 `info` 说明要做什么。
- **进度**：多步骤任务每进入新阶段，用 `progress` 更新（旧进度会被自动替换）。
- **风险**：遇到风险操作、需要用户注意时，用 `warning`。
- **阻塞**：卡住、需要用户输入时，用 `warning` 或 `urgent`。
- **完成**：任务结束时**必须**用 `done` 播报，否则用户不知道已经完成。

> **存活心跳由 Runtime 自动维持（v26.4）。** 你**不需要**自己定时播"我还在"那类
> 心跳——长命令阻塞、多步推进期间，agent 宿主的后台守护线程会直连 Runtime 自动播
> 存活进度（沉默约 25 秒触发，env `VOICE_HEARTBEAT_SECONDS` 可调）。这恰好补上了你
> 物理上做不到的环节：工具阻塞期 / 生成 token 期你无法插播。你只管在**状态真正变化**
> 时播有语义的 `info`/`progress`/`warning`/`done`。

## 文案规则

- 口语化、短句，像同事在旁边随口说，不要念长段落。
- 一次一个要点，别把多件事塞进一句。
- 不要播报代码、路径、长串 ID。
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

> 门控说明：本 skill 声明 `required_environment_variables: [DASHSCOPE_API_KEY]`（缺失时索引里软标记
> `⚠ setup_needed`）与 `metadata.requires_tools: [terminal]`（无 terminal 工具时硬隐藏）。这也是 v26.1
> 可用性门控的真实用例。
