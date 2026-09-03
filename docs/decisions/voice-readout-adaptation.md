# 语音播报适配方案

> 状态：模型 hook、CLI dispatcher、voice_say 工具和 skill 拆分已落地；价格账本和远端
> 隧道验证待后续阶段实施。
>
> 目标：把远端 DeerFlow 的语音能力适配到 nano-hermes。nano-hermes 本地进程直接调用
> `nano-voice-say` CLI；CLI 内部再通过 HTTP `POST /events` 把完整文案交给本地
> `nano_voice_kit` Runtime。语音挂在模型生命周期的 `before_model_call` /
> `after_model_call` hook 上：每次模型生成前后各有一次明确的宿主回调，不按任务阶段或耗时
> 另设策略。语音功能只由环境变量总开关控制，并为后续价格重构保留独立计量边界。

## 1. 背景与结论

远端实现位于：

```text
/home/ubuntu/zj/finance-main/backend/packages/harness/deerflow/agents/middlewares/voice_readout_middleware.py
```

它通过 LangChain 的 `wrap_model_call` / `awrap_model_call`，在模型调用前后自动生成并发送
播报。nano-hermes 采用同样的模型前后边界，但不照搬远端的阶段计数和静音状态机；只复用
它的文案裁剪、静音和失败隔离经验。远端
测试覆盖了：

- 首次 `info`、后续 `progress`；
- 最终响应才播 `done`；
- 模型要求继续调用工具时不播 `done`；
- skill 禁用、工具不可见时静默；
- 用户要求安静后本轮持续静音。

nano-hermes 当前没有 LangChain middleware 层，只有手写 tool loop。现有
`HookManager` 只包住工具执行；本方案为模型生命周期补充 `before_model_call` /
`after_model_call` 两个 hook。

最终采用：

```text
before_model_call / after_model_call
    -> VoiceReadoutService
    -> CliVoiceDispatcher
    -> nano-voice-say
    -> POST /events
    -> 本地 nano_voice_kit Runtime
```

不直接移植 DeerFlow 的 middleware 类；在 nano-hermes 的统一模型调用边界接入两个 hook。

## 2. 现状边界

### 2.1 nano-hermes 模型和工具边界

主模型同步、流式调用以及 tool loop 位于 [`main.py:745-827`](../../main.py#L745)。
`before_model_call` 放在 `chain.call()` / `_stream_one_turn()` 之前，`after_model_call`
紧跟标准化响应之后；无论同步还是流式，hook 都只在模型调用边界执行一次。

### 2.2 当前 HookManager 能做什么

[`tools/hooks.py:23`](../../tools/hooks.py#L23) 的合法 hook 只有三种：

```text
pre_tool_call
post_tool_call
transform_tool_result
```

[`tools/registry.py:118-180`](../../tools/registry.py#L118) 的 hook 只包住工具
`coerce -> pre -> handler -> post -> transform`，不能保证模型调用前执行，也不能判断
“这是最终响应还是还要继续调用工具”。

因此不通过 `pre_tool_call` 拦截 `terminal` 命令模拟 middleware。改造后保留这三种工具
hook，并新增两个模型生命周期 hook；工具级 hook 继续负责工具审计，模型级语音逻辑只
由生命周期 hook 承载。

## 3. 目标行为契约

语音执行同时满足两个条件：`VOICE_READOUT_ENABLED=1`，以及当前模型调用进入
`before_model_call` 或 `after_model_call` hook。每次模型生成前后各调用一次 hook，不按
任务阶段、工具耗时或时间间隔额外触发。

hook 内必须先拿到完整播报文案，再一次性执行 CLI：不逐 token 播放、不把流式中间片段发送
到 Runtime。CLI、Runtime 或 TTS 失败时只记录 side-channel 错误，主模型和工具结果照常
返回。

## 4. 模块设计

### 4.1 `VoiceReadoutService`

新增与框架无关的 `VoiceReadoutService`，负责 hook 级编排、文案清洗和失败隔离：

```python
class VoiceReadoutService:
    def before_model(self, context: dict) -> None: ...
    def after_model(self, response, context: dict) -> None: ...
```

`before_model` 根据当前 turn 调用次数和最近上下文额外调用一次非流式 helper model，生成
简短的准备/进展文案；`after_model` 每次都执行响应观察，但只有无 tool calls 的最终响应
才额外调用 helper model 生成并发送 `done`。下一轮 `before_model` 负责播报 `progress`，
避免同一轮 after 再重复播报。helper 的空响应或重复响应会记录 warning，并根据用户任务、
最近工具阶段和本轮已播报历史生成去重兜底；不会再使用固定的“开始处理/继续推进”句式。
两个 helper 调用都生成完整文本后才交给 dispatcher，且不修改主对话历史。

### 4.2 `VoiceDispatcher` 抽象

新增与框架无关的 `VoiceDispatcher`，只负责把一条已经完整生成的文案交给 CLI，
不负责判断什么时候应该播报：

```python
class VoiceDispatcher(Protocol):
    def speak(self, *, intent: str, text: str, source: str) -> "VoiceResult": ...
```

`CliVoiceDispatcher` 使用参数数组调用 subprocess，不使用 shell 拼接。文案在进入
dispatcher 前完成脱敏、首句提取和长度限制；dispatcher 不接收流式 token，也不向主
对话历史写入任何消息。

### 4.3 `CliVoiceDispatcher` 实现

`CliVoiceDispatcher` 使用参数列表调用 subprocess，不使用 shell 拼接：

```python
[
    nano_voice_say_bin,
    "--intent", intent,
    "--text", text,
    "--host", runtime_host,
    "--port", str(runtime_port),
]
```

本地默认 binary：

```text
/Users/qshf/my-project/nano_hermes_agent/.venv/bin/nano-voice-say
```

本地调用示例：

```bash
/Users/qshf/my-project/nano_hermes_agent/.venv/bin/nano-voice-say \
  --intent info --text "测试一下"
```

建议配置：

```text
VOICE_READOUT_ENABLED=0
NANO_VOICE_SAY_BIN=/Users/qshf/my-project/nano_hermes_agent/.venv/bin/nano-voice-say
VOICE_RUNTIME_HOST=127.0.0.1
VOICE_RUNTIME_PORT=8920
VOICE_DISPATCH_TIMEOUT_SECONDS=2
```

`VOICE_READOUT_ENABLED` 是唯一总开关：未设置或不是 `1/true/yes/on` 时，两个模型 hook
和显式 `voice_say` 都直接返回 disabled，不启动子进程。其余变量只改变 CLI 路径、Runtime
地址和超时，不改变播报语义。

### 4.4 `voice_say` 工具

保留 `voice_say` 注册工具，承载用户明确要求的额外播报。它与
`VoiceReadoutService` 共用 `VoiceDispatcher`，但不再自行触发模型生命周期 hook，避免
递归；自动播报和显式播报使用不同的 `source` 标识。

工具结果建议统一为：

```json
{
  "spoken": true,
  "source": "agent",
  "intent": "info",
  "text_chars": 12,
  "status": "accepted"
}
```

显式工具使用 `source=agent`，宿主命令可使用 `source=host`，便于审计和计费。

## 5. 主循环接线

扩展 [`tools/hooks.py`](../../tools/hooks.py) 的合法 hook：

```text
before_model_call
after_model_call
```

两个 hook 继续使用 `HookManager.invoke()` 的异常隔离语义。建议参数：

```text
before_model_call:
  messages, model, turn_id, stream_enabled, tools

after_model_call:
  messages, model, turn_id, response, stream_enabled
```

在 [`main.py:745-827`](../../main.py#L745) 接线，保持同步和流式模型调用共用同一顺序：

```text
append user message
 -> hook.before_model_call
 -> chain.call / _stream_one_turn
 -> hook.after_model_call
 -> append assistant history
 -> if tool_calls: execute tools and continue
 -> else: finish turn
```

`after_model_call` 必须在响应标准化后、工具执行前触发；这样能判断 `tool_calls`，仅在
无 tool calls 时发送 `done`。若 hook 内需要单独调用文案模型，必须传递
`voice_internal=True` 或使用独立的内部调用边界，确保内部调用不会再次触发两个 hook。
当前实现直接调用 `chain.call()`，该调用不经过 `main.py` 的 hook 接线，因此不会递归；
helper 使用 `tools=[]` 和较小的 `max_tokens`，不产生工具调用。

## 6. CLI、远程服务与网络拓扑

本地 Runtime 的实际链路是：

```text
nano-voice-say
    -> POST http://<host>:8920/events
    -> VoiceEventQueue
    -> DashScopeProvider
    -> 本地播放器
```

Runtime 的 `create_app()` 在 `runtime/app.py` 中创建 provider、player 和队列；
`POST /events` 只接受五种 intent，并把事件入队。

必须明确执行位置：

- nano-hermes 在 Mac 本机运行：直接执行本机绝对路径 CLI，默认 Runtime 为
  `127.0.0.1:8920`；
- nano-hermes 在 SSH 远端运行：远端不能执行 Mac 的 CLI，也不能把远端
  `127.0.0.1` 当作 Mac Runtime。此时由远端 CLI/HTTP client 通过 SSH 隧道访问
  Mac 的 `8920` 端口，或由本地宿主代发请求。

推荐远端部署优先使用 SSH 隧道，不直接把 TTS Runtime 暴露到公网。所有 HTTP/CLI
失败都只能影响语音 side channel，不能阻断 Agent turn。

## 7. Skill 拆分

当前 [`skills/voice-runtime/SKILL.md`](../../skills/voice-runtime/SKILL.md) 的
`inject_directive` 同时描述 Runtime、CLI 用法和模型前后播报规则，职责过多，应拆成两个
独立 skill：

```text
voice-runtime:
  Runtime 的地址、健康检查、CLI 可执行性和失败处理

voice-readout:
  before_model_call / after_model_call 的宿主播报契约、文案清洗和失败隔离

voice-say:
  可选的显式 voice_say 工具参数约束
```

`voice-runtime` 不再描述模型业务时机；`voice-readout` 只约束两个生命周期 hook，
不要求模型自己调用 terminal；`voice-say` 只服务显式播报。这样 skill 拆分后，运行时
能力、宿主 hook 和显式调用协议互不耦合。

## 8. 价格与用量账本

语音调用不能混入 `runtime.session_tokens`，因为那会把 Agent LLM 成本与语音附加
成本混在一起。建议新增独立的 `voice_usage` 表或 append-only JSONL，生产对账优先
使用 SQLite 表。

每次播报记录：

```text
event_id
session_id
turn_id
source                    middleware / agent / host
event_kind                before_model / after_model / explicit_voice_say
intent
llm_provider              helper model 的 provider
llm_model                 helper model 使用的模型名
llm_input_tokens
llm_output_tokens
tts_provider
tts_model
text_chars
audio_duration_ms
pricing_version
estimated_cost
actual_cost
status
error
created_at
```

成本拆成三类；当前实现会产生 helper model 的额外调用成本。本地部署只表示 TTS 播放
发生在本机，不代表 helper model 免费：

```text
主 Agent 模型成本
（可选）播报文案模型成本
TTS 合成成本
```

`nano-voice-say` 目前只负责提交事件，不能提供实际费用；TTS 字符数、音频时长和
provider usage 应在 dispatcher/runtime provider 边界采集。价格表变更只更新
`pricing_version`，不能覆盖历史记录。

## 9. 安全与可靠性要求

- 传给 readout 或 `voice_say` 的文本先脱敏，排除 system prompt、raw tool args、完整文件内容
  和原始终端输出；
- 使用 `agent.redact.redact()` 处理密钥、token、URL 中的敏感部分；
- 播报文本取首句，最大 160 字符；必须先完整生成，再一次性提交；
- helper 返回空文本或与本轮历史重复时，使用任务上下文兜底并避免重复句；
- CLI 使用参数数组，不使用 `shell=True`；
- Runtime 不可达时最多记录一次 warning，不循环重试；
- Ctrl+C/Esc 取消主任务时，不再追加新的 after-model 播报；
- 子 agent 默认沿用独立 hook 上下文，不复用父 turn 的播报状态；
- 所有语音错误均为 side-channel error，不改变主 turn 的成功/失败状态。

## 10. 测试与验收

保留远端测试中有价值的失败隔离和工具不可用场景，并增加 nano 特有场景：

1. `VOICE_READOUT_ENABLED=0` 时 before/after hook 都不启动 CLI 子进程；
2. 每次模型生成前后各触发一次对应 hook；
3. `after_model_call` 对 tool calls 响应只记录、不播报；最终无 tool calls 才播 `done`；
4. 流式主模型输出不会产生流式语音片段或重复调用；
5. CLI 不存在时主模型仍返回；
6. POST 超时不阻塞主 turn 超过配置上限；
7. 同步和流式主模型路径的 hook 行为一致；
8. `voice_usage` 正确记录 TTS 字符数和来源；
9. 远端通过 SSH 隧道时，Runtime 地址可配置；
10. 子 agent 的 hook 作用域独立，不重复父 turn 状态。

验收指标：

```text
语音不可用时主任务零失败
关闭开关时主任务零语音副作用
每个模型调用前后各至多执行一次 CLI
主 Agent token 报表不包含 voice helper token
voice_usage 可以按 pricing_version 重算估算成本
```

## 11. 分阶段落地

### Phase 1：dispatcher 和工具测试

- 新增 `VoiceDispatcher` 和 `voice_say` 工具；
- 新增 recording dispatcher；
- 不启动真实 CLI；
- 完成开关、完整文案和失败隔离测试。

### Phase 2：模型生命周期 hook 接线

- 增加 `before_model_call` / `after_model_call`；
- 接入同步和流式主 loop；
- 确保每个 hook 都在收到完整文案后调用一次 CLI。

### Phase 3：CLI/POST 适配

- 新增 `CliVoiceDispatcher`；
- 配置 binary、host、port、timeout；
- 验证本机 Runtime 和 SSH 隧道两种拓扑。

### Phase 4：Skill 拆分

- 拆出 `voice-runtime`、`voice-readout` 与可选 `voice-say`；
- 删除模型自行通过 terminal 播报的强制 directive；
- 由宿主 hook 统一触发 before/after 播报。

### Phase 5：价格账本

- 建立 `voice_usage`；
- 记录 helper LLM usage、TTS 字符/时长和 pricing version；
- 在 insights 中增加语音附加成本和未知价格提示；
- 用真实 provider 对账后再开放实际收费统计。

## 12. 明确不采用的方案

- 不用 `pre_tool_call` 拦截 `terminal` 命令模拟模型 middleware；
- 不把 hook 下沉到每个工具或 terminal 命令；
- 不把 `VoiceReadoutMiddleware` 原样引入 nano；
- 不把 TTS 成本塞入主 Agent 的四维 token 累计；
- 不按任务阶段或耗时另设播报触发器；
- 不在远端服务器假设存在扬声器或使用远端 `127.0.0.1` 代替本地 Runtime。
