# Voice Orchestrator 外发数据契约（host → orchestrator）

> 目的：把 host 侧**实际发给外部 voice-orchestrator 的每一个字段**摊开 —— 在哪构建、
> 怎么传入、附代码行 —— 供裁剪决策（哪些必要 / 哪些可删）。本文档只描述现状，不改代码。

---

## 0. 一句话全景

**两个数据流源、一条管道、一种最外层 schema**。`_send_turn_event` 是 host 侧的唯一
**出口**，但喂进它的有两个并行的**源**：手写调用和 phase span。后者通过 listener 桥接
到同一个出口，所以最终 schema 完全一致：

```
源 A：手写调用 (turn_started / turn_finished / tool_finished)  ─┐
                                                                 ├─→ _send_turn_event(...)
源 B：phase_tracker.start/close/activity_event                   │      → build_turn_event_envelope(...)
       → PhaseSpan._emit(status) → listener (turn_loop.py:346)  ─┘      → TurnEventEnvelope
                                                                        → voice_event_sink.submit()
                                                                        → 后台线程 HTTP POST 一个 JSON
```

- 出口函数（唯一）：[turn_loop.py:308 `_send_turn_event`](../agent/turn_loop.py#L308)
- 源 B 的桥接点：[turn_loop.py:346 `phase_tracker.set_listener(...)`](../agent/turn_loop.py#L346)
  —— 把每个 phase 事件转成一次 `_send_turn_event(status, phase=...)`
- envelope 构建（唯一）：[turn_events.py:85 `build_turn_event_envelope`](../agent/turn_events.py#L85)
- wire 数据结构（唯一）：[turn_events.py:64 `TurnEventEnvelope`](../agent/turn_events.py#L64)

所谓「`_send_turn_event` 和 `phase_tracker.start` 格式不一样」，**不是外壳不同**，而是
源 A / 源 B **填了 envelope 的不同字段**（源 A 填 `tool`、用 `turn_*`/`tool_*` 命名；
源 B 填 `phase`、用 `phase_*` 命名）。详见 §1 / §3。

---

## 1. 触发点：谁在发、发什么 event_type

| # | 触发点（代码行） | event_type | 填充的关键字段 |
|---|------------------|-----------|----------------|
| 1 | [turn_loop.py:438](../agent/turn_loop.py#L438) `_send_turn_event("turn_started")` | `turn_started` | 仅 context + safety（messages 快照） |
| 2 | [turn_loop.py:622](../agent/turn_loop.py#L622) `_send_turn_event("turn_finished", assistant_text=...)` | `turn_finished` | `assistant_activity.visible_text_preview` |
| 3 | [turn_loop.py:588](../agent/turn_loop.py#L588) memory 工具完成 | `tool_finished` / `tool_error` | `tool`（ToolPreview：result_head/tail/duration/status） |
| 4 | [turn_loop.py:346](../agent/turn_loop.py#L346) phase listener | `phase_started` / `phase_activity` / `phase_finished` / `phase_error` / `phase_cancelled` | `phase`（PhasePreview：name/status/span_id/elapsed_ms/activity）；`send_message_preview=False` |

### 关键观察

- **触发点 1/2/3 是手写调用**，event_type 是字面量字符串（`turn_*` / `tool_*`）。
- **触发点 4 是 phase span 的副产物**：每次 `phase_tracker.start/close/activity_event`
  → `PhaseSpan._emit(status)` → listener → `_send_turn_event(status, phase=...)`。
  这里 **`event_type` 直接等于 phase 的 `status`**（`phase_started` 等），词表和 1/2/3 不同源。
- 触发点 4 的所有 phase span 起点见 [turn_loop.py:152/168/579](../agent/turn_loop.py#L152)（args/text/tool span）
  以及 [registry.py:156](../tools/registry.py#L156)（普通工具的 `tool_span`）。

> **这就是你看到的「两种格式」**：手写事件走 `turn_*`/`tool_*` 词表并填 `tool`；
> phase 事件走 `phase_*` 词表并填 `phase`。下面逐字段拆。

---

## 2. 完整 JSON 形状（TurnEventEnvelope.to_dict）

`asdict()` 递归展开后，POST 出去的 JSON 长这样（注释标了来源代码行）：

```jsonc
{
  // ── 顶层标识（每个事件都有，必发）──
  "schema_version": "voice-orchestrator.v1",   // 常量 SCHEMA_VERSION   turn_events.py:20
  "session_id":     "<safe>",                  // _safe_identifier      turn_events.py:145
  "turn_id":        "turn-<n>",                // f"turn-{turn_count}"  turn_loop.py:329
  "event_type":     "<see §1>",                // _safe_identifier      turn_events.py:147
  "timestamp":      1749.0,                     // time.time()          turn_events.py:148

  // ── context：用户/对话预览 ──
  "context": {
    "last_user_message_preview": {              // 总是构建            turn_events.py:111-112,150
      "text": "...", "chars": 0, "truncated": false, "redacted": false
    },
    "recent_messages": [                        // send_messages 开关控制 turn_events.py:118-125
      { "role": "user|assistant|tool",
        "preview": { "text": "...", "chars": 0, "truncated": false, "redacted": false } }
    ]
  },

  // ── assistant_activity：本轮助手动态 ──
  "assistant_activity": {
    "visible_text_preview": {                   // 来自 assistant_text  turn_events.py:127,154
      "text": "...", "chars": 0, "truncated": false, "redacted": false
    },
    "reasoning_safe_summary": "",               // 来自 reasoning_activity（当前无人传）turn_events.py:155
    "next_tool_name": ""                        // 来自 next_tool_name（当前无人传）turn_events.py:156
  },

  // ── tool：仅 tool_finished/tool_error 事件非 null ──
  "tool": {                                     // ToolPreview         turn_events.py:193
    "name": "...", "status": "ok|error|non_json",
    "duration_ms": 12,
    "result_head": { "text": "...", "chars": 0, "truncated": false, "redacted": false },
    "result_tail": { "text": "...", "chars": 0, "truncated": false, "redacted": false },
    "result_truncated": false
  },

  // ── phase：仅 phase_* 事件非 null ──
  "phase": {                                    // PhasePreview        turn_events.py:54
    "name": "tool_executing|assistant_generating_text|assistant_generating_tool_arguments|child_agent_running",
    "status": "phase_started|phase_activity|phase_finished|phase_error|phase_cancelled",  // ⚠️ == event_type
    "span_id": "span_<n>",
    "elapsed_ms": 5,
    "activity": { /* 已清洗，只剩简单值 */ }    // _safe_activity_dict  turn_events.py:141
  },

  // ── safety：脱敏/截断元信息 + 永不外发清单 ──
  "safety": {
    "message_preview_enabled": true,            // turn_events.py:161
    "redacted": false,                          // 折叠自所有嵌套预览  turn_events.py:135-138
    "truncated": false,
    "omitted": ["system_prompt","raw_tool_args","raw_tool_output",
                "file_contents","child_agent_transcripts","raw_chain_of_thought"]
  }
}
```

---

## 3. 每个字段：在哪构建、怎么传入

下表是裁剪的核心依据。「传入路径」列说明这个字段的值从哪来、经过几道手。

| 字段 | 构建位置 | 值来源 / 传入路径 | 脱敏·截断 |
|------|---------|-------------------|-----------|
| `schema_version` | [turn_events.py:144](../agent/turn_events.py#L144) | 常量 `SCHEMA_VERSION` | — |
| `session_id` | [turn_events.py:145](../agent/turn_events.py#L145) | `run_repl` 局部 `current_session_id` → `_send_turn_event` 闭包捕获 → 形参 | `_safe_identifier` 截 96 字符 |
| `turn_id` | [turn_loop.py:329](../agent/turn_loop.py#L329) | `f"turn-{turn_count}"`，turn_count 是循环可变局部 | `_safe_identifier` |
| `event_type` | [turn_events.py:147](../agent/turn_events.py#L147) | 手写字面量 或 phase status（见 §1） | `_safe_identifier` |
| `timestamp` | [turn_events.py:148](../agent/turn_events.py#L148) | `time.time()` 构建时取 | — |
| `context.last_user_message_preview` | [turn_events.py:111](../agent/turn_events.py#L111) | 从 `messages[]` 倒查最后一条 user → `preview_text` | redact + 截断 max_message_chars |
| `context.recent_messages[]` | [turn_events.py:118-125](../agent/turn_events.py#L118) | `messages[]` 最近 N 条非 system，逐条 `preview_text` | redact + 截断；**phase 事件传 send_message_preview=False 故为空** |
| `assistant_activity.visible_text_preview` | [turn_events.py:154](../agent/turn_events.py#L154) | 形参 `assistant_text`（仅 turn_finished 传 final 文本） | redact + 截断 |
| `assistant_activity.reasoning_safe_summary` | [turn_events.py:155](../agent/turn_events.py#L155) | 形参 `reasoning_activity` —— **全仓无人传，恒空** | `_safe_activity_label` |
| `assistant_activity.next_tool_name` | [turn_events.py:156](../agent/turn_events.py#L156) | 形参 `next_tool_name` —— **全仓无人传，恒空** | `_safe_identifier` |
| `tool.*` | [turn_events.py:193 `preview_tool_result`](../agent/turn_events.py#L193) | memory 路径 [turn_loop.py:590](../agent/turn_loop.py#L590) 造 ToolPreview 传入；**registry 工具不传 → 普通工具无此字段** | result_head/tail 各自 redact + 按 MAX_TOOL_RESULT_CHARS head/tail 切 |
| `phase.name` | [runtime_phase.py:109](../agent/runtime_phase.py#L109) | span 的 `name`（4 个常量之一） | — |
| `phase.status` | [runtime_phase.py:110](../agent/runtime_phase.py#L110) | **与 event_type 同值**（冗余） | — |
| `phase.span_id` | [runtime_phase.py:77](../agent/runtime_phase.py#L77) | `f"span_{自增计数}"` | — |
| `phase.elapsed_ms` | [runtime_phase.py:112](../agent/runtime_phase.py#L112) | `monotonic` 差，span 开始到 emit | — |
| `phase.activity` | [turn_events.py:141](../agent/turn_events.py#L141) | span 累积的 kwargs（delta_chars/tool_name/total_chars 等） | `_safe_activity_dict`：非简单值压成 ≤120 字符标签 |
| `safety.message_preview_enabled` | [turn_events.py:161](../agent/turn_events.py#L161) | `send_messages` 开关最终值 | — |
| `safety.redacted` / `truncated` | [turn_events.py:135-138](../agent/turn_events.py#L135) | 折叠所有嵌套预览的标记 | — |
| `safety.omitted` | [turn_events.py:164](../agent/turn_events.py#L164) | 静态常量清单 | — |

### `tool` 字段怎么传进来（最绕的一条，单独展开）

```
memory 工具执行完 (turn_loop.py:588-591)
  └─ preview_tool_result(name, kind, result, duration_ms)   # turn_events.py:193 造 ToolPreview
       └─ 作为 tool= 实参传给 _send_turn_event
            └─ build_turn_event_envelope(tool=...)          # turn_events.py:94 形参
                 └─ _coerce_dataclass_dict(tool)             # turn_events.py:131 dataclass→dict
                      └─ _fold_safety 把内部 redacted/truncated 冒泡到 envelope.safety  # :135-138
```

普通 registry 工具（[turn_loop.py:593](../agent/turn_loop.py#L593)）**不走这条**——它只在 `registry.dispatch`
内部开了个 `tool_executing` phase span（[registry.py:156](../tools/registry.py#L156)），于是 orchestrator
对普通工具**只收到 `phase` 事件、收不到 `tool` 预览**（没有 result_head/tail/duration）。

---

## 4. 裁剪决策表（待你抉择）

按「orchestrator 决定是否播报」的实际需要评估。`建议` 列是我的初判，`决策` 留空给你勾。

| 字段 | 当前状态 | 我的初判 | 理由 | 决策 |
|------|---------|---------|------|------|
| `schema_version` | 必发 | **保留** | 版本协商必需 | |
| `session_id` / `turn_id` | 必发 | **保留** | orchestrator 需要按会话/轮聚合 | |
| `event_type` | 必发 | **保留** | 路由事件类型的主键 | |
| `timestamp` | 必发 | **保留** | 节流/cooldown 判断需要 | |
| `context.last_user_message_preview` | 必发 | **保留** | 「用户在问什么」是播报措辞的核心输入 | |
| `context.recent_messages[]` | 开关控制 | **可砍/默认关** | 与 last_user 重叠；每个事件重算遍历全 messages，热路径上最重。orchestrator 多数只需最近一条 user + assistant | |
| `assistant_activity.visible_text_preview` | turn_finished 才有 | **保留** | 「助手说了什么」是播报内容来源 | |
| `assistant_activity.reasoning_safe_summary` | **恒空** | **删** | 全仓无人传值，纯占位 | |
| `assistant_activity.next_tool_name` | **恒空** | **删** | 全仓无人传值，纯占位 | |
| `tool.result_head` | memory 才有 | **保留** | 工具结果摘要，播报「查到了X」需要 | |
| `tool.result_tail` | memory 才有 | **可砍** | 仅截断时非空；语音播报极少需要结果尾部 | |
| `tool.duration_ms` | memory 才有 | **保留** | 「跑了很久」可触发播报 | |
| `phase.status` | 必发 | **删（冗余）** | 与顶层 `event_type` 完全同值，见 §5 | |
| `phase.span_id` | 必发 | **看需要** | 仅当 orchestrator 要配对 start/finish 才需要；否则可删 | |
| `phase.elapsed_ms` | 必发 | **保留** | 「这个阶段卡了多久」是播报触发信号 | |
| `phase.activity` | 必发 | **保留** | delta_chars/total_chars 给「还在生成」进度感 | |
| `safety.omitted` | 静态常量 | **可砍** | 每个事件重复一份固定清单；可移到一次性 handshake | |
| `safety.message_preview_enabled` | 必发 | **可砍** | orchestrator 一般不需要知道 host 的开关态 | |

---

## 5. 已知冗余 / 不一致（裁剪时一并考虑）

1. **`phase.status` == `event_type`**（同值重复）。
   phase 事件的 `event_type` 就是把 `PhaseSpan` 的 status 原样上送（[turn_loop.py:347](../agent/turn_loop.py#L347)），
   而 `phase.status` 又是同一个值（[runtime_phase.py:110](../agent/runtime_phase.py#L110)）。二选一即可。

2. **「工具完成」两种形状**：memory 工具发 `tool`（ToolPreview，含结果预览），
   普通 registry 工具只发 `phase`（tool_executing span，无结果预览）。若要 orchestrator
   对所有工具一视同仁，需让 registry 路径也补发 `tool_finished` + ToolPreview
   （改动 [registry.py:156](../tools/registry.py#L156) 区间或在 [turn_loop.py:593](../agent/turn_loop.py#L593) 后补一发）。

3. **两套 event_type 词表**：`turn_*` / `tool_*`（手写）vs `phase_*`（span 自动）。
   命名风格不同源；若要统一可加 namespace 前缀或归一动词。

4. **恒空字段**：`reasoning_safe_summary` / `next_tool_name` 全仓无写入点，
   是为未来预留的占位。当前纯增加 payload 体积。

> 改任何一条都涉及 wire schema 变更 —— 注意同步 `SCHEMA_VERSION` 与 orchestrator 端解析。

---

## 6. v2 设计提案：砍到最小的「语音填充」契约

> **使用目的**（确定后重新裁剪的依据）：外部 orchestrator 在 agent 工具调用的**等待
> 间隙**调 LLM 生成贴合上下文的短播报，避免用户干等。所以 host 只需发两类事实：
> ①「用户本轮想要什么」（生成措辞的话题）②「agent 此刻在做什么、做了多久」
> （判断该不该开口、说什么）。其余全砍。

### 6.1 设计原则

1. **一个事件词表**：合并现在的 `turn_*`/`tool_*`（手写）与 `phase_*`（span 自动）两套，
   统一成 5 个：`turn_started` / `turn_finished` / `activity_started` / `activity_progress` /
   `activity_finished`。orchestrator 不再关心事件来自手写还是 span。
2. **一个工具通道**：memory 工具与普通 registry 工具都只走 `tool_span`（activity 事件），
   删掉 memory 路径额外手写的 `tool_finished`。所有工具的「开始/进行/完成+结果」形状一致。
3. **话题发一次、增量保持轻**：`turn_started` 带 `user_goal`（话题）。高频的
   `activity_progress` 只带 span 元数据 + `elapsed_ms`，不重算消息预览。
4. **扁平化**：去掉 `context`/`assistant_activity`/`tool`/`phase`/`safety` 五层嵌套，
   合并成顶层 + 一个 `activity` 子对象。

### 6.2 v2 信封形状

```jsonc
{
  "schema_version": "voice-orchestrator.v2",
  "session_id": "default",
  "turn_id": "turn-3",
  "event_type": "activity_started",  // turn_started|turn_finished|activity_started|activity_progress|activity_finished
  "timestamp": 1749.0,

  // 用户本轮目标 —— 生成播报措辞的核心话题。每个事件都带（仅一次 last_user
  // 反查 + 一次 redact，开销小），让 orchestrator 无需自己缓存 turn 状态。
  "user_goal": "帮我重构 main.py 并加测试",

  // agent 此刻在做什么 —— 仅 activity_* 事件带
  "activity": {
    "kind": "tool",            // thinking|generating_text|generating_args|tool|child_agent
    "name": "read_file",       // 工具名 / 子 agent 标识；thinking/generating 时为 ""
    "elapsed_ms": 8200,        // 该活动已持续多久 ←「等太久该播报」的关键信号
    "span_id": "span_7",       // 关联同一活动的 started→progress→finished（可选）
    "outcome": "ok",           // 仅 activity_finished：ok|error|cancelled
    "result": "找到 3 个匹配文件" // 仅 activity_finished：工具结果安全短预览（≤200，redacted）
  },

  // 助手最终可见回复 —— 仅 turn_finished 带
  "assistant_text": "我已经重构完成，拆成了 bootstrap / turn_loop 两个模块...",

  "redacted": false            // 本事件是否发生过脱敏/截断（单一标记）
}
```

### 6.3 砍字段对照（相对 v1）

| v1 字段 | v2 处置 | 原因 |
|---------|---------|------|
| `context.recent_messages[]` | **删** | 最重（每事件遍历全 messages + 逐条 redact）；`user_goal` 已够生成填充播报 |
| `context.last_user_message_preview` | → `user_goal`（扁平、改名） | 话题，保留 |
| `assistant_activity.reasoning_safe_summary` | **删** | 全仓无人传，恒空 |
| `assistant_activity.next_tool_name` | **删** | 全仓无人传；工具名已在 `activity.name` |
| `assistant_activity.visible_text_preview` | → `assistant_text`（扁平、改名） | 终轮回复，保留 |
| `tool.{name,duration,result_head}` | → `activity.{name,elapsed_ms,result}` | 并入统一 activity |
| `tool.result_tail` | **删** | 截断时才非空；语音几乎不需要结果尾部 |
| `tool.result_truncated` | **删** | 合进 `redacted` 单标记 |
| `phase.name` | → `activity.kind`（值归一） | 保留语义，改词表 |
| `phase.status` | **删** | 与 `event_type` 完全同值（§5.1） |
| `phase.span_id` | → `activity.span_id`（可选） | 仅 orchestrator 要配对时留 |
| `phase.elapsed_ms` | → `activity.elapsed_ms` | 等待时长，核心信号，保留 |
| `phase.activity{delta_chars,total_chars}` | → 折进 `activity`（progress 时） | 「还在生成」进度感，保留 |
| `safety.omitted` | **删** | 每事件重复的固定常量；属文档不属 payload |
| `safety.message_preview_enabled` | **删** | orchestrator 不需要 host 开关态 |
| `safety.{redacted,truncated}` | → 顶层 `redacted` 单 bool | 二合一 |

**净效果**：5 层嵌套 → 2 层；恒空/冗余/重复常量字段全清；工具上报从两种形状归一。

### 6.4 落地改动点（host 侧）

| 改动 | 文件 / 位置 | 内容 |
|------|------------|------|
| 重写信封 dataclass | [turn_events.py:64 `TurnEventEnvelope`](../agent/turn_events.py#L64) | 扁平 7 字段 + `activity` 子 dict；删 `ToolPreview`/`PhasePreview` 嵌套外壳 |
| 改构建函数 | [turn_events.py:85 `build_turn_event_envelope`](../agent/turn_events.py#L85) | 入参精简为 `user_goal`/`activity`/`assistant_text`；删 recent_messages 循环、reasoning/next_tool 参数 |
| 事件词表归一 | [runtime_phase.py:61 `PhaseEventType`](../agent/runtime_phase.py#L61) | `phase_started/activity/finished/...` → `activity_started/progress/finished`；`PhaseSpan.name` → `activity.kind` |
| listener 改映射 | [turn_loop.py:346](../agent/turn_loop.py#L346) | 把 span 事件映射成 `event_type=activity_*` + 填 `activity` 子对象 |
| 删 memory 手写发送 | [turn_loop.py:588-591](../agent/turn_loop.py#L588) | 删掉额外的 `_send_turn_event("tool_finished", tool=...)`；memory 工具已被 tool_span 覆盖，结果预览改由 `tool_span.finish(result=...)` 带出 |
| tool_span 带结果 | [registry.py:156](../tools/registry.py#L156) | `span.finish` 增补 `result`（安全短预览）—— 让**所有**工具完成时都能带结果，不止 memory |
| 同步版本号 | [turn_events.py:20 `SCHEMA_VERSION`](../agent/turn_events.py#L20) | `v1` → `v2` |
| 删测试旧断言 | `scripts/test_v27_1_voice_orchestrator.py` | 改成断言 v2 形状 |

> 脱敏 / 截断函数（`redact_text` / `preview_text` / `_safe_*`）全部**保留复用**——
> 只是调用点变少。安全边界不降级。

### 6.5 orchestrator 侧怎么用（「等待间隙播报」时序）

```
用户输入「重构 main.py 并加测试」
  │
  ├─ turn_started        {user_goal:"重构 main.py 并加测试"}
  │     → orchestrator 记下话题，先不播（刚开始无需填充）
  │
  ├─ activity_started    {kind:"generating_text"}
  ├─ activity_started    {kind:"tool", name:"read_file", span_id:"s1"}
  ├─ activity_progress   {kind:"tool", name:"read_file", elapsed_ms:6000}
  │     → elapsed_ms 超阈值！orchestrator 调 LLM：
  │        prompt = user_goal + "正在读取文件" → 「正在分析你的 main.py，稍等」→ 播放
  │
  ├─ activity_finished   {kind:"tool", name:"read_file", outcome:"ok", result:"读到 1068 行"}
  ├─ activity_started    {kind:"tool", name:"write_file", span_id:"s2"}
  ├─ activity_progress   {kind:"tool", name:"write_file", elapsed_ms:9000}
  │     → 再次超阈值 → 「正在拆分模块并写入，马上好」→ 播放
  │
  └─ turn_finished       {assistant_text:"已拆成 bootstrap / turn_loop 两个模块..."}
        → orchestrator 可选播一句收尾「重构完成了」
```

**关键**：host 不决定「该不该播」「播几次」「冷却多久」「说什么」——这些全在 orchestrator。
host 只如实上报 `elapsed_ms`（等多久）+ `user_goal`/`activity`（在干什么），让 orchestrator
有足够事实去调 LLM 生成贴合当下的填充语音。这正是 v27.1「host 发事实、服务端决策」的边界。

### 6.6 待确认

- `activity_progress` 节流阈值：当前 host 侧 `VOICE_ORCHESTRATOR_TEXT/ARGUMENT_DELTA_MIN_CHARS`
  控制发送频率。v2 是否改成「按时间」节流（如每 2s 发一次 progress）更贴合「等待感知」？
- `activity.result` 长度上限：语音填充用，建议比 v1 的 1200 更短（≤200），仅够 orchestrator
  生成「查到了 X」。是否同意？
- `span_id`：orchestrator 是否需要配对 started→finished？不需要则连这个也砍。
