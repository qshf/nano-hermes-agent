# Voice Kit API Provider 方案计划

## 目标

借鉴 `RoversCode/streamvox-agent-voice-kit` 的整体接入形态，先实现一个可全局安装、可被任意项目中的 Codex / Claude Code 调用的语音工具包；底层 TTS 暂时使用 DashScope API，后期再替换或新增本地模型 provider。

核心判断：

- 保留原项目最方便的外壳：全局 CLI、本地 HTTP Runtime、全局 skill installer、`intent` 队列语义。
- 替换原项目最重的内核：`streamvox.TTSEngine` 本地模型调用、角色注册、模型 bundle、授权 key、GPU/device 管理。
- 对 Agent 暴露稳定协议，让 Agent 永远只调用 `nano-voice-say` 或本地 `/events`，不关心后端是云 API 还是本地模型。

## 当前参考项目

GitHub:

- `https://github.com/RoversCode/streamvox-agent-voice-kit`

本次调研时的本地临时克隆路径：

- `/tmp/streamvox-agent-voice-kit`

如果新窗口没有该目录，可重新克隆：

```bash
git clone --depth 1 https://github.com/RoversCode/streamvox-agent-voice-kit.git /tmp/streamvox-agent-voice-kit
```

## 原项目关键代码路径

### Skill 模板

- `/tmp/streamvox-agent-voice-kit/skills/streamvox-runtime/SKILL.md`

作用：

- 定义 `streamvox-runtime` skill。
- 要求 Agent 在任务起手、耗时步骤、风险、阻塞、阶段完成、最终完成时调用语音播报。
- 规定调用格式：`streamvox-say --intent <intent> --text "<文案>"`。
- 通过人格文档控制播报文案风格。

重点看：

- frontmatter 里的 `name`、`description`、`arguments`、`allowed-tools`。
- `Runtime 状态` / `Runtime 能力` 检查。
- `Intent 路由与文案规则`。
- 完成前必须播报 `done` 的规则。

### CLI 入口声明

- `/tmp/streamvox-agent-voice-kit/pyproject.toml`

重点段：

```toml
[project.scripts]
streamvox-agent = "streamvox_agent_voice.cli.agent:main"
streamvox-runtime = "streamvox_agent_voice.cli.runtime:main"
streamvox-say = "streamvox_agent_voice.cli.say:main"
```

作用：

- 安装后在任意项目目录都能调用 `streamvox-*` 命令。
- 这是原项目能接入任意项目的关键之一。

### Skill 安装器

- `/tmp/streamvox-agent-voice-kit/streamvox_agent_voice/template_installer.py`
- `/tmp/streamvox-agent-voice-kit/streamvox_agent_voice/cli/agent.py`

作用：

- 把内置 skill 模板复制到：
  - `~/.codex/skills/streamvox-runtime`
  - `~/.claude/skills/streamvox-runtime`
- 提供 `streamvox-agent init --target codex` / `--target claude-code`。

可借鉴：

- `install_builtin_skill`
- `target_skill_install_dir`
- `streamvox-agent init --force`

### 播报 CLI

- `/tmp/streamvox-agent-voice-kit/streamvox_agent_voice/cli/say.py`

作用：

- 实现 `streamvox-say --intent progress --text "..."`
- 校验 `intent`。
- 调 `VoiceClient.speak_intent(...)`。
- 通过 HTTP 把事件发给本地 Runtime。

可借鉴：

- 参数设计：`--intent`、`--text`、`--wait`、`--host`、`--port`、`--timeout`。
- 默认只给 Agent 暴露高层 `intent`，不要让 Agent 自己操作底层队列策略。

### HTTP Client

- `/tmp/streamvox-agent-voice-kit/streamvox_agent_voice/client.py`

作用：

- 统一封装 Runtime HTTP 请求。
- `speak_intent(intent, text)` 先通过 policy 解析，再 POST `/events`。

可借鉴：

- `VoiceClient.say`
- `VoiceClient.speak_intent`
- `VoiceClient.stop`
- `_post_json`

### Intent 策略映射

- `/tmp/streamvox-agent-voice-kit/streamvox_agent_voice/policy.py`

重点规则：

- `info` -> `enqueue`
- `progress` -> `replace_pending`
- `warning` -> `clear_pending_then_enqueue`
- `urgent` -> `interrupt`
- `done` -> `clear_pending_then_enqueue`

这是语音体验自然度的核心。尤其是 `progress` 替换旧进度，能避免用户听到过期播报。

### Runtime HTTP 服务

- `/tmp/streamvox-agent-voice-kit/streamvox_agent_voice/runtime/app.py`

重点端点：

- `GET /health`
- `GET /status`
- `GET /capabilities`
- `POST /events`
- `POST /stop`
- `POST /shutdown`

核心链路：

- `/events` 接收 JSON。
- `VoiceEvent.from_mapping(payload)` 校验协议。
- `runtime_speaker.validate_event_request(event)` 做运行时校验。
- `queue.enqueue(event)` 入队。
- `wait=false` 时立即返回 `accepted`。
- `wait=true` 时等待播放结果。

### 事件协议

- `/tmp/streamvox-agent-voice-kit/streamvox_agent_voice/events.py`

作用：

- 定义 `VoiceEvent`。
- 限制 `intent` 只能是 `info/progress/warning/urgent/done`。
- 限制 `action` 只能是 `enqueue/interrupt/stop/replace_pending/clear_pending_then_enqueue`。
- 校验非 `stop` 事件必须有文本。

可借鉴：

- 小集合协议，不允许 Agent 发明任意 intent。
- `metadata` 预留扩展字段。

### 队列

- `/tmp/streamvox-agent-voice-kit/streamvox_agent_voice/runtime/queue.py`

作用：

- 管理待播事件。
- 支持 `interrupt`、`stop`、`replace_pending`、`clear_pending_then_enqueue`。
- 后台 worker 消费队列，并在线程里调用 `speaker.speak(...)`。

可借鉴：

- `VoiceEventQueue.enqueue`
- `_drop_pending_by_intent`
- `_drop_all_pending`
- `_process_item`

### 原本地模型封装

- `/tmp/streamvox-agent-voice-kit/streamvox_agent_voice/runtime/engine.py`

当前原项目做法：

```python
from streamvox import TTSEngine

self.engine = TTSEngine(...)
chunks = self.engine.stream(text=event.text, **kwargs)
self.audio_sink.play_chunks(chunks, self.sample_rate, stop_event)
```

我们的替换点：

- 不使用 `streamvox.TTSEngine`。
- 改成 `DashScopeProvider`（实时流式，见下文「DashScope API Provider 初始实现」）。
- 底层走 WebSocket 流式，逐块产出 **PCM**（24kHz / mono / 16bit），不是 mp3。
- 流式产出天然适配 `engine.stream(...) -> chunks` 这种边收边播的形态，也是 `urgent` 打断的前提。
- 后续本地模型 provider 只需要实现同一个 provider 接口。

参考实现已落地：独立包 `/Users/qshf/my-project/nano_voice_kit`，对应 `nano_voice_kit/runtime/providers/dashscope.py`（由用户提供的 `aly_TTS.py` demo 重构而来）。

### 播放后端

- `/tmp/streamvox-agent-voice-kit/streamvox_agent_voice/runtime/audio_player.py`

作用：

- 把音频输出到 speaker / wav / null。

⚠️ 注意：provider 产出的是裸 PCM，**`afplay` 不能直接播放裸 PCM**（没有文件头）。两条路：

- **`sounddevice` 实时播**（推荐）：`RawOutputStream(samplerate=24000, channels=1, dtype='int16')`，边收边写。低延迟，且配合 `stop_event` 能实现 `urgent` 中途打断。
- **补 WAV 头后 afplay**：用 `pcm_to_wav()` 给 PCM 加头写临时文件再 `afplay`。简单，但要等整段合成完，做不了打断。

第一版可简化：

- macOS 试听：`sounddevice` 实时播；或 `pcm_to_wav` + `afplay`。
- Linux：`sounddevice`（跨平台），或补 WAV 头后 `aplay` / `ffplay`。
- 测试环境：`null` 后端（丢弃 PCM）。
- 临时 WAV 文件用完即清理。

## 计划中的新项目形态

建议做成独立 voice kit 包，而不是只放在 `nano_hermes_agent/skills/voice-runtime/` 下。

原因：

- 原项目能接任何项目，是因为它是独立包 + 全局命令 + 全局 skill installer。
- 如果只放在当前项目里，就天然只服务这个项目。
- 独立包能让任意 Codex / Claude Code 项目通过全局 skill 调用语音。

推荐目录：

```text
nano_voice_kit/
  pyproject.toml

  nano_voice_kit/
    cli/
      agent.py          # nano-voice-agent init
      runtime.py        # nano-voice-runtime start/status/stop
      say.py            # nano-voice-say --intent ... --text ...

    runtime/
      app.py            # FastAPI app
      events.py         # VoiceEvent 协议
      policy.py         # intent -> action
      queue.py          # 播报队列
      player.py         # speaker/wav/null 播放后端
      config.py         # Runtime 配置
      providers/
        base.py         # TTSProvider 抽象
        dashscope.py    # 当前 DashScope API provider
        local_model.py  # 未来本地模型 provider 占位

  skills/
    voice-runtime/
      SKILL.md
      references/
        personality/
          default.md
```

建议命令：

```bash
nano-voice-runtime start --provider dashscope --model qwen3-tts-flash-realtime --voice Cherry
nano-voice-say --intent progress --text "我正在处理这个任务"
nano-voice-agent init --target codex
```

## DashScope API Provider 初始实现

用户最初提供的示例用的是 `dashscope.audio.tts_v2.SpeechSynthesizer`（一次性返回 mp3）。
但实际选定走 **`dashscope.audio.qwen_tts_realtime.QwenTtsRealtime`（WebSocket 实时流式）**，
原因：realtime 支持 `cancel_response()` 中途打断，正好实现 `urgent` intent；一次性接口做不到。
代价：只产出 PCM，没有 mp3 选项（SDK `AudioFormat` 仅 `PCM_24000HZ_MONO_16BIT`）。

参考实现：`nano_voice_kit/runtime/providers/dashscope.py`（独立包，见文末「实现进度」）。

Provider 接口（同步 + 流式两个方法，流式是 realtime 的价值所在）：

```python
class TTSProvider:
    def synthesize(self, text: str, *, voice: str | None = None) -> AudioResult:
        """一次性：内部仍走流式，拼成整段 PCM 返回。"""
        ...

    def synthesize_stream(
        self, text: str, *, voice: str | None = None,
        stop_event: threading.Event | None = None,
    ) -> Iterator[bytes]:
        """边收边吐 PCM 块；stop_event 置位时 cancel_response() 打断。"""
        ...
```

```python
@dataclass
class AudioResult:
    data: bytes
    format: str = "pcm"          # ⚠️ 不是 mp3
    sample_rate: int = 24000
    channels: int = 1
    session_id: str | None = None
    first_package_delay_ms: float | None = None
```

DashScope provider 行为：

- 从环境变量读取 `DASHSCOPE_API_KEY`。
- 区域 URL 查表：北京 `wss://dashscope.aliyuncs.com/api-ws/v1/realtime`、新加坡 `wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime`（**两地 key 不通用**）。
- 默认模型：`qwen3-tts-flash-realtime`（指令控制版为 `qwen3-tts-instruct-flash-realtime`）。
- 默认音色：`Cherry`（Qwen-TTS 系；注意与 CosyVoice 系的 `longanyang` 不通用）。
- 返回 PCM bytes（24kHz / mono / 16bit）。
- 通过 `get_first_audio_delay()` 记录首包延迟、`get_session_id()` 记录 session id，写入 Runtime 日志或 CLI JSON 输出。
- 错误经 `on_event` 的 `type=="error"` 事件传回（SDK 无独立 `on_error` 回调），合成超时/连接断开都会抛 `TTSError`，不静默挂起。

## 第一阶段 MVP 范围

### 必做

- 新建独立包骨架。
- 实现 `nano-voice-runtime start/status/stop`。
- 实现 `GET /health`、`GET /status`、`POST /events`、`POST /stop`。
- 实现 `nano-voice-say --intent ... --text ...`。
- 实现 `DashScopeProvider`（已落地：`nano_voice_kit/runtime/providers/dashscope.py`）。
- 实现最小播放器：
  - `null` 后端用于测试（丢弃 PCM）。
  - `sounddevice` 实时后端用于本机试听 + `urgent` 打断；或 `pcm_to_wav` + `afplay` 简易后端。
- 实现 intent 策略：
  - `info` -> 入队。
  - `progress` -> 替换未播旧 progress。
  - `warning` -> 清空待播后入队。
  - `urgent` -> 打断当前播放。
  - `done` -> 清空待播后入队。
- 实现 `nano-voice-agent init` 安装 skill 到 `~/.codex/skills/voice-runtime`。

### 暂不做

- 角色注册。
- 音色克隆。
- 本地模型部署。
- GPU/device 配置。
- 多语言能力自动探测。
- 复杂人格库。
- skill 脚本自动执行。

## 与 nano_hermes_agent 的关系

当前 `nano_hermes_agent` 已有 skill 机制：

- `agent/skill_loader.py`
- `tools/skill_view_tool.py`
- `agent/prompt_builder.py`
- `skills/<name>/SKILL.md`

但语音 Runtime 不建议只作为当前项目内部模块实现。更好的分工：

- `nano_voice_kit`：提供全局语音服务、CLI、skill installer。
- `nano_hermes_agent`：继续完善 skill system，让本项目也能更好地读取和使用 skill。
- 任意用户项目：只需要 Codex/Claude 能看到全局安装的 `voice-runtime` skill，并且 PATH 里能调用 `nano-voice-say`。

## nano_hermes_agent 宿主侧语音系统设计（v26.3 → v26.5）

`nano_voice_kit` 负责“能不能播”：Runtime、TTS provider、队列、播放后端、CLI 和全局 skill installer。`nano_hermes_agent` 宿主侧负责“什么时候播、播什么”：把 agent 运行时的任务阶段、工具调用、delegate 子任务和 slash 命令转成稳定的语音进度事件。

### 分层职责

```text
LLM / main loop / tool loop / delegate / slash
        │
        │ 结构化事件 VoiceProgressEvent
        ▼
nano_hermes_agent: agent/voice_heartbeat.py
        │
        ├─ 当前任务上下文
        ├─ 最近模型阶段 / 工具 / delegate / slash
        ├─ 模板化文案选择
        ├─ debounce / 去重 / suppress voice CLI 套娃
        └─ 静默超时 fallback heartbeat
        │
        │ VoiceClient.speak_intent(intent, text)
        ▼
nano_voice_kit Runtime
        │
        ├─ intent -> queue action policy
        ├─ progress replace_pending
        ├─ warning/done clear_pending_then_enqueue
        ├─ urgent interrupt
        └─ provider + player
```

### 三阶段演进

| 版本 | 解决的问题 | 关键设计 |
|------|------------|----------|
| v26.3 | 模型不知道自己应该主动播报 | `SKILL.md` frontmatter 增加 `inject_directive`，skill 完全可用时注入 system prompt 常驻段。 |
| v26.4 | 模型在 terminal 阻塞 / token 生成期间物理上无法定时报活 | `VoiceHeartbeat` 后台线程直连 `VoiceClient`，`agent_busy.is_set()` 且静默超过阈值时播 fallback `progress`。 |
| v26.5 | 固定“还在处理，稍等”太死板，且模型后半程可能忘记继续播 | 直接扩展 `VoiceHeartbeat` 的实验已废弃：功能可跑但代码冗余、策略硬编码、侵入点过多；后续改走 EventBus + listener + policy 外置。 |

### 为什么不解析日志

用户直觉上可以说“监听日志”，但实现上不把 raw log 当运行时协议：

- 日志是排障文本，格式会为了人类阅读调整，不适合作稳定接口。
- 日志可能关闭、脱敏、截断或重排；并发 delegate 下顺序也可能交错。
- 日志可能含路径、堆栈、长输出或敏感字段，直接转语音容易泄漏或啰嗦。
- memory tools 在 `main.py` 中直接 dispatch，不保证所有语义都完整落入某类日志。

所以宿主侧选择显式发送 `VoiceProgressEvent`。这仍然是“语音服务监听全局进度”，只是监听稳定结构化事件，而不是猜日志文本。

### `VoiceProgressEvent` 协议

宿主侧事件保持小集合、短字段、可测试：

```python
@dataclass(frozen=True)
class VoiceProgressEvent:
    kind: str
    task: str = ""
    tool_name: str = ""
    slash_name: str = ""
    delegate_label: str = ""
    status: str = ""
    detail: str = ""
    duration_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

首版事件目录：

| 类别 | 事件 |
|------|------|
| turn/busy | `turn_started`, `busy_started`, `busy_finished`, `assistant_done` |
| model | `model_call_started`, `model_call_finished`, `model_call_error`, `model_reasoning_started`, `model_tool_call_planned`, `model_stream_done` |
| tool | `tool_started`, `tool_finished`, `tool_error` |
| delegate | `delegate_started`, `delegate_finished`, `delegate_error`, `delegate_interrupted`, `delegate_child_tool_started`, `delegate_child_done` |
| slash | `slash_command_started`, `slash_command_finished`, `slash_command_error` |

### 文案策略

v26.5 首版不让后台线程再调 LLM 生成句子，而是用确定性模板：

| 输入事件 / 上下文 | intent | 文案示例 |
|-------------------|--------|----------|
| `turn_started` | `info` | `开始处理这个任务` |
| `model_call_started` | `progress` | `正在分析下一步` |
| `model_reasoning_started` | `progress` | `正在推理方案` |
| `tool_started: terminal` | `progress` | `正在执行命令，这一步可能要等一下` |
| `tool_started: memory_*` | `progress` | `正在查询记忆` |
| `tool_started: delegate_task` | `progress` | `正在派发子任务` |
| `tool_error` | `warning` | `这个步骤出错了，我会换个办法` |
| `delegate_child_tool_started` | `progress` | `子任务正在使用工具` |
| `delegate_child_done` | `progress` | `子任务有阶段结果了` |
| heartbeat + terminal 上下文 | `progress` | `命令还在跑，稍等` |
| heartbeat + 无上下文 | `progress` | `还在处理，稍等` |

这样做的取舍：

- ✅ 零额外模型费用。
- ✅ 低延迟、可单测。
- ✅ 不引入“heartbeat 线程再发 LLM 请求”的递归复杂度。
- ✅ 失败时容易 fallback。
- ⏳ 文案自然度不如 LLM 生成，后续可抽可选 `PhraseProvider`。

### 降噪规则

语音是 side-channel，必须避免“太吵”：

- `emit()` 只入队，主流程不等待语音 HTTP 请求。
- 后台线程统一调用 `VoiceClient.speak_intent()`。
- 短时间内相同 `(kind, tool_name, slash_name)` debounce。
- streaming 只转发 milestone，不转每个 text/reasoning delta。
- `nano-voice-say` 自身作为 terminal 命令时 suppress，避免“正在执行语音播报命令”的套娃。
- `assistant_done` 首版只清状态，不自动播 `done`，避免和模型按 `voice-runtime` directive 自己播 `done` 重复。
- 成功播报后刷新 heartbeat 基准，避免刚说完又机械心跳。

### 宿主侧 hook 点

`main.py`：

- 非 slash 用户输入进入 LLM 流程后：`turn_started`。
- tool loop 开始/结束：`busy_started` / `busy_finished`。
- LLM 调用前后：`model_call_started` / `model_call_finished` / `model_call_error`。
- streaming milestone：首个 reasoning、tool call planned、done。
- parent tool dispatch 前后：`tool_started` / `tool_finished` / `tool_error`。
- memory tools 也在这里覆盖，因为它们绕过 `tools.registry` hooks。

`tools/delegate_tool.py`：

- `DelegateContext.voice_progress` 保存宿主注入的事件 sink。
- delegate spawn / done 发 `delegate_started` / `delegate_finished` 等。
- child stream callback 把 `EVENT_TOOL_CALL_STARTED` / `EVENT_DONE` 桥接成 `delegate_child_*` 事件，同时保留原 stderr side-channel。

`cli/registry.py`：

- 通过 `ctx.extras["voice_progress"]` 获取 sink，不改变 command handler 签名。
- 只对白名单长/状态类命令发事件，例如 `new/resume/compress/sessions/insights/trajectory/memory/plugin/load`。
- `/help`、`/tools` 等快命令不刷语音。

### 与 `voice-runtime` skill 的关系

v26.5 后，模型和宿主侧分工如下：

| 责任 | 归属 |
|------|------|
| 常规工具进度、delegate 子任务进度、slash 命令进度 | 宿主事件服务 |
| 长时间静默防挂机 heartbeat | 宿主事件服务 fallback |
| 风险判断、阻塞说明、需要用户决策、最终完成 | 模型按 `voice-runtime` directive 主动调用 `nano-voice-say` |

也就是说，模型不再背“定时我还在”的任务；但当它真的理解出新的语义状态时，仍应主动播报。

### 当前实现进度（nano_hermes_agent）

已完成并保留在主线：

1. ✅ v26.4：`agent/voice_heartbeat.py` 提供宿主侧后台 heartbeat fallback，解决 terminal / 模型生成阻塞期无法定时报活的问题。
2. ✅ `main.py` 只保留 heartbeat lifecycle / busy 状态接入，不把语音事件散落到各业务路径。
3. ✅ `skills/voice-runtime/SKILL.md` 保留模型主动语义播报职责，宿主侧只兜底“别挂机”。

已废弃的实验：

- ❌ v26.5：直接把 `VoiceHeartbeat` 扩展为事件驱动语音进度服务的实现已回滚。原因见 [v26.5 决策记录](../decisions/v26.5.md)：功能可跑，但策略硬编码、侵入点过多、横切逻辑污染主流程。

当前建议验证：

```bash
.venv/bin/python scripts/test_v26_4_voice_heartbeat.py
.venv/bin/python scripts/test_v26_3_inject_directive.py
.venv/bin/python scripts/test_v21_slash.py
.venv/bin/python scripts/test_v23_3_streaming.py
.venv/bin/python scripts/test_v23_0_delegate.py
.venv/bin/python scripts/test_v23_1_batch.py
.venv/bin/python scripts/test_v23_4_structured_result.py
.venv/bin/python -m py_compile agent/voice_heartbeat.py main.py tools/delegate_tool.py cli/registry.py cli/context.py
```

## 后续可选演进

### Provider 抽象增强

后期可以增加：

- `OpenAIProvider`
- `EdgeTTSProvider`
- `LocalCosyVoiceProvider`
- `StreamVoxProvider`

只要保持 `/events` 和 `nano-voice-say` 协议不变，Agent 侧无需修改。

### Skill system 增强

当前 nano 的 skill system 还只是说明书加载系统。后续可补：

- `references/` / `templates/` / `assets/` 资源索引。
- `skill_resource_read` 工具。
- `arguments` 参数替换。
- `allowed_tools` 元数据。
- `requires_env_vars` 可用性判断。
- `scripts/` 发现，但先不自动执行。

这部分是基础设施增强，和 voice kit 可以并行推进。

## 风险与注意事项

- 原项目仓库本次调研没有看到 `LICENSE` 文件。不要直接大段复制源码；建议借鉴架构和协议，自行实现瘦身版。
- DashScope WebSocket 首次连接有首包延迟，Runtime 常驻能减少进程启动开销，但是否复用连接需要实际测试。
- 临时 WAV 文件（`pcm_to_wav` + afplay 路线）每次写文件要做好清理；`sounddevice` 实时播则无临时文件。
- `urgent` 打断有两层：provider 侧 `cancel_response()` 停止继续合成 + 播放器侧停止当前 PCM 输出（`sounddevice` 停流，或终止 afplay 子进程）。两者都要做才算干净打断。
- Agent 靠 skill 自觉调用并非 100% 强制；如果未来需要强制播报，可在 agent loop 增加 hook 兜底。

## 实现进度

独立包已建：`/Users/qshf/my-project/nano_voice_kit`（与 nano_hermes_agent 平级，独立 git 仓）。

已完成（已验证，未碰真 API）：

1. ✅ 独立 `nano_voice_kit` 包骨架 + pyproject（依赖极简：dashscope/fastapi/uvicorn/httpx/typer）。
2. ✅ `null` + `sounddevice` 播放后端。
3. ✅ DashScope provider，流式产出 PCM（`runtime/providers/dashscope.py`）。
4. ✅ runtime：events(5-intent 协议) / policy / queue(单 worker) / app(FastAPI `/health /status /events /stop /shutdown`)。
5. ✅ `nano-voice-say` CLI（Runtime 没起时优雅失败）。
6. ✅ `nano-voice-runtime start/status/stop`。
7. ✅ `voice-runtime` skill 模板（SKILL.md + 人格参考）+ `nano-voice-agent init`（装到 `~/.codex|~/.claude`）。
8. ✅ 验证：包导入 / 三个 CLI 入口 / skill 安装器(临时 HOME) / FastAPI 端点(假 provider) / queue 不变量(16 项)。

未完成 / 待验证：

- ⏳ 真实 TTS 合成 + 本机播放 + urgent 真打断（需 `DASHSCOPE_API_KEY` + 音频设备 + API 费用）。
- ⏳ `/events` 的 `wait=true` 同步等待播放完成（当前一律返回 accepted）。
- ⏳ 在任意第三方项目里验证 Codex/Claude 能通过全局 skill 调用 `nano-voice-say`。
- ⏳ Linux 播放后端（aplay/ffplay）。
