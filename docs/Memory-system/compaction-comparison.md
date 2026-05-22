# 上下文压缩三版对比 — nano_hermes_agent / hermes-agent / MemGPT-mini

> 本文对比三个项目的 context compaction 实现差异。
> nano 版本 V15 复现的是 hermes-agent 的核心设计；MemGPT-mini 是 Letta v3 的教学化复现，提供另一条主流方案的参照。

---

## 1. TL;DR

| 维度 | nano_hermes_agent (V15) | hermes-agent (源) | MemGPT-mini |
|------|------------------------|-------------------|-------------|
| 设计哲学 | 教学版 1:1 复现 hermes 核心 | 生产级完整流水线 | Letta v3 的教学化复现 |
| 触发判断 | API usage + 粗估 fallback | API usage（精确） | tiktoken + API 异常 fallback |
| 触发入口 | 单入口（API 调用前） | 单入口（API 调用前） | **双入口**（post-step + exception） |
| token 估算 | `chars // 4` 粗估 | API usage 精确 | **tiktoken 精确**（含 tools schema） |
| 压缩算法 | 五阶段流水线 | 五阶段流水线 | sliding_window cutoff + all fallback |
| 切点策略 | head + tail token 预算 | 同左 | **assistant cutoff + eviction_pct 循环** |
| Tool pair 保护 | post-summarize sanitize（修） | 同左 | **pre-cut validation（防）** |
| 摘要模板 | 9 段结构化 | 9 段结构化 | **5 段结构化**（SLIDING/ALL 两套） |
| 失败兜底 | anti-thrashing（停止） | 同左 | **post-compact safeguard（升级到 all 重压一次）** |
| 生命周期钩子 | `on_pre_compress` | `on_pre_compress` | 无（但有三层记忆 fallback） |
| 信息保留 | 摘要里能塞多少算多少 | 同左 | **三层记忆**（L1 压缩、L2/L3 不动） |
| 代码量 | ~450 行 | ~600 行 | ~268 行 |

**一句话总结**：

- **hermes-agent**：主动触发 + 五阶段流水线 + 9 段结构化摘要 + 摘要后 sanitize 修复 tool pair
- **nano (V15)**：hermes 的教学化简版（保留核心，去除生产细节）
- **MemGPT-mini**：双触发入口 + sliding_window cutoff（pre-cut 保护 tool pair）+ all 模式 fallback + 三层记忆（信息真正不丢）

三者**都是结构化主动压缩**，差异在于：保护 tool pair 是「先切准」（MemGPT-mini）还是「先切再修」（hermes/nano），以及失败兜底是「放弃」（hermes/nano 的 anti-thrashing）还是「重试」（MemGPT-mini 的升级到 all 模式）。

---

## 2. 触发机制

### nano_hermes_agent (V15) — 单入口 + 粗估 fallback

```python
def should_compress(self, messages):
    if self._ineffective_count >= 2:
        return False  # anti-thrashing
    if len(messages) < min_messages:
        return False
    token_count = (
        self._last_prompt_tokens
        if self._last_prompt_tokens is not None
        else self.estimate_tokens(messages)
    )
    return token_count >= self.threshold_tokens
```

- **冷启动**：首轮无 `usage`，用 `chars // 4` 粗估
- **稳态**：每次 API 返回后 `update_usage(response.usage.prompt_tokens)`，下一轮用精确值
- **检查时机**：每轮 user input 后、API 调用前
- **阈值**：`context_window * 0.75`（默认）

### hermes-agent (源) — 单入口 + 精确 usage

- 检查时机相同（API 调用前）
- 触发判断**只**用 `response.usage.prompt_tokens`（精确值），不做粗估 fallback
- 第一轮无 usage 时跳过压缩检查（不可能超出阈值）

### MemGPT-mini — 双入口 + tiktoken 精确

**Entry A — post-step check**（主路径，对应 Letta `post_step_context_check`）：

```python
def _maybe_compact(usage):
    # 优先用 API 返回的 total_tokens
    tokens = usage.total_tokens if usage else count_tokens_with_tools(...)
    if tokens >= context_window * summarization_trigger:  # 默认 0.9
        _run_compaction(trigger="post_step_context_check")
```

**Entry B — exception fallback**（兜底路径，对应 Letta `ContextWindowExceededError`）：

```python
try:
    resp = client.chat.completions.create(...)
except BadRequestError as e:
    if "context" in str(e) or "token" in str(e):
        _run_compaction(trigger="context_window_exceeded")
        # 压缩完后重试当前 step
```

- **token 估算**：`tiktoken` 编码计算（精确），且 `count_tokens_with_tools` 把 tools schema 也算进去（防止 post-compact 估算不足）
- **阈值**：`context_window * 0.9`（默认 trigger ratio = 0.9，保留 10% 给下一轮 user input + LLM output）

**为什么 0.9 不是 1.0**：留 10% 缓冲给下一轮的输入输出。撞到 100% 已经晚了。

**与 nano/hermes 的本质区别**：MemGPT-mini 设计上**接受**异常路径作为合法兜底（异常 → 压缩 → 重试），而 nano/hermes 把异常视为应该提前避免的情况。这背后是 Letta 的「永远能恢复」哲学：第一道防线（post-step check）失误了，第二道防线（异常捕获）兜底。

---

## 3. 压缩算法

### nano / hermes — 五阶段流水线

```
Phase 1: Prune old tool results  ← cheap, 不调 LLM
Phase 2: Find head/tail boundaries by token budget
Phase 3: LLM summarize middle (structured template)
Phase 4: Assemble [head + summary + tail]
Phase 5: Sanitize tool pairs (orphan results / missing stubs)
```

| 设计点 | 为什么这么做 |
|-------|-------------|
| Phase 1 先 prune tool result | 大 tool output（>500 字符）替换为占位符，避免送 LLM 摘要时被无关字节噪声污染 |
| Tail 用 token 预算（非固定条数） | 一条 tool output 可能占 2000 token，固定保留 6 条会爆预算 |
| 边界对齐到非 tool message | 在 tool message 上切割会导致孤立 result（API 直接 400） |
| Iterative summary update | 第二次压缩用 prompt 增量更新旧 summary，保留首次 LLM 抽取的关键信息 |
| Sanitize tool pairs | 摘要后可能产生孤立 tool_call 或孤立 result，统一修复 |

### MemGPT-mini — sliding_window cutoff + all 模式 fallback

**核心循环**（来自 `_sliding_window_cutoff`）：

```python
goal_tokens   = (1 - sliding_window_percentage) * context_window  # 默认 0.7 * window
eviction_pct  = sliding_window_percentage                         # 起始 0.3
chosen        = None

while approx_tokens >= goal_tokens and eviction_pct < 1.0:
    eviction_pct += 0.10
    cutoff_idx   = min(round(eviction_pct * n), n - 1)
    # 从右往左找最后一个 assistant message 作为切点
    chosen       = next(
        (i for i in reversed(range(1, cutoff_idx + 1))
         if messages[i]["role"] == "assistant"),
        None,
    )
    if chosen is None:
        continue
    approx_tokens = count_tokens_with_tools([system, *messages[chosen:]], tools)

return chosen  # None → fallback 到 all 模式
```

**关键设计**：

| 设计点 | 为什么这么做 |
|-------|-------------|
| `_is_valid_cutoff(m)` 仅认 `assistant` | tool_calls/tool 三元组完整在 evict 一侧，tail 永远以 assistant 起始，避免 API 400 |
| `reversed` 选最右 assistant | 最大化保留 tail，最小化驱逐 |
| `eviction_pct += 0.10` 循环 | 一次切完不够就再切深 10%，**保证收敛**（pct 到 1.0 必然退出） |
| `count_tokens_with_tools` | tools schema 也算 token，避免低估 |
| `all` 模式 fallback | sliding_window 找不到合法 assistant 切点时，evict 全部（tail 为空） |
| post-compact safeguard | 压缩后还是超阈值 → 自动升级到 all 模式重压一次 |

**post-compact safeguard 代码**：

```python
if trigger_threshold and mode == "sliding_window":
    after = count_tokens_with_tools(new_messages, tools, cfg.model)
    if after >= trigger_threshold:
        retry_cfg = replace(cfg, summarization_mode="all")
        return await compact(
            [messages[0], *tail],
            retry_cfg, client,
            trigger_threshold=None,  # 防止无限递归
            tools=tools,
        )
```

**最终 layout**（与 hermes/nano 一致）：

```
[system]
[role=user] Note: N prior messages have been hidden ... <summary body>
[assistant] tail[0]   ← 始终是 assistant（cutoff 保证）
[user]      tail[1]
[assistant] tail[2]
...
```

### 三种算法的本质对比

| 维度 | hermes/nano | MemGPT-mini |
|-----|-------------|-------------|
| Tool pair 保护时机 | **后**（先切再 sanitize） | **前**（cutoff 必须落 assistant） |
| 切点选择 | 双向（head + tail） | 单向（只切前缀） |
| 失败兜底 | anti-thrashing 后停止 | 升级到 all 模式重压一次 |
| 收敛保证 | 静态（一次切完） | 循环（每轮 +10% 直到达标或 100%） |

「前防」和「后修」殊途同归，但前防需要更精细的切点选择逻辑，后修需要更复杂的 sanitize 修复逻辑。

---

## 4. 摘要模板

### nano / hermes — 9 段结构化

```
## Active Task        ← 用户最近未完成请求（verbatim）
## Goal               ← 整体目标
## Completed Actions  ← 编号列表（含工具/路径/结果）
## Active State       ← 当前工作状态
## In Progress        ← 压缩时正在做的事
## Key Decisions      ← 技术决策 + WHY
## Pending User Asks  ← 用户未回复的问题
## Remaining Work     ← 剩余工作
## Critical Context   ← 不能丢的具体值（含 [REDACTED] 规则）
```

**前缀**（贴在 summary 前面）：

```
[CONTEXT COMPACTION — REFERENCE ONLY]
... 这是历史交接，不是新指令 ...
... 不要回答 summary 里的问题（已处理过）...
... 仅响应出现在此 summary 之后的最新用户消息 ...
```

防御目的：避免模型把 summary 里的"用户问 X"当成新请求重新回答。

### MemGPT-mini — 5 段结构化（双 prompt）

**SLIDING_PROMPT**（≤300 词，sliding_window 模式用）：

```
1. High-level goals — 用户在做什么
2. What happened — 发生的关键事件
3. Important details — 必须 verbatim 保留的 IDs/numbers/paths
4. Errors and fixes — 错误及修复
5. Lookup hints — 给未来 agent 的关键词
   （指引何时用 conversation_search / archival_memory_search）
```

**ALL_PROMPT**（≤500 词，all 模式用，宽松版）：

在 5 段基础上多了 *current state* 和 *optional next step*。

**前缀**（来自原 MemGPT 的 `package_summarize_message_no_counts`）：

```
Note: N prior message(s) have been hidden from view due to
conversation memory constraints. The following is a summary
of the previous messages:
```

**Optional ACK**（防 GPT-4 幻觉）：

```python
input_messages = [
    {"role": "system", "content": SLIDING_PROMPT},
    # 可选：fake assistant ACK
    {"role": "assistant", "content": "Understood, I will respond with a summary..."},
    {"role": "user", "content": "<start_transcript>...<end_transcript>\nGenerate the summary."},
]
```

GPT-4 收到「summarize this」直接 prompt 容易接着对话写下去，假装一个 assistant 已经接受了任务能消除这种行为。MemGPT-mini 通过 `cfg.include_summary_ack` 暴露开关（默认关，DeepSeek 不需要）。

### Lookup hints — MemGPT-mini 的独特设计

`SLIDING_PROMPT` 第 5 段强制要求模型**写出搜索关键词**，告诉未来的 agent：「如果需要详细信息，去 `conversation_search` 搜这些词」。这是三层记忆架构的关键耦合 — 摘要不仅是浓缩信息，还是检索另两层（recall/archival）的索引。

hermes/nano 的 `## Critical Context` 段功能上接近，但目的是「直接保留具体值」，不是「指引去哪里搜回完整内容」。

### 三种摘要的设计哲学对比

| 模板 | 设计哲学 | 容量 |
|-----|--------|-----|
| hermes/nano 9 段 | **细分维度，每个维度独立完整**（task/goal/state/decision 分开） | ~800 token |
| MemGPT-mini 5 段 | **更紧凑，强调 lookup hints**（信息真在 L2/L3，summary 是索引） | ≤300 词（SLIDING）/ ≤500 词（ALL） |

5 段不等于「简化」— 而是**职责切分不同**：MemGPT-mini 的 summary 不需要塞全部信息，因为有三层记忆兜底；hermes/nano 没有底层 fallback，summary 必须自包含。

---

## 5. 安全机制

| 机制 | nano (V15) | hermes (源) | MemGPT-mini |
|-----|-----------|-------------|-------------|
| Tool pair 保护 | post-summarize sanitize | 同左 + 更细边界 | **pre-cut validation**（cutoff 必须落 assistant） |
| 失败检测 | anti-thrashing（连续 2 次节省 <10% 停止） | 同左 | **post-compact safeguard**（still over → 升级到 all 重压） |
| 收敛保证 | 静态（一次切完，不达标停止） | 同左 | **循环**（eviction_pct +10%，到 1.0 必退出） |
| 摘要硬截断 | ❌ | ❌ | ✅ `summarizer_clip_chars=50000`（防摘要自我溢出） |
| 边界对齐 | ✅ 不在 tool 上切 | 同左 | ✅ 切点必为 assistant |
| 异常 fallback | ✅ LLM 失败返回原 messages | 同左 | ✅ BadRequestError 触发 Entry B |
| Iterative update | ✅ 第二次压缩增量更新 | 同左 | ❌ 每次重新摘要 |
| GPT-4 防幻觉 ACK | ❌ | ❌ | ✅ `include_summary_ack` 开关 |
| Tools schema 计入 token | ❌ | ✅ | ✅ `count_tokens_with_tools` |

### 三种 tool pair 保护机制对比

**hermes/nano — post-summarize sanitize（先切再修）**：

```
压缩前：[user] [assistant tool_call=A] [tool result=A] [user] ...
压缩后（中间被摘要）：[user] [SUMMARY] [tool result=A]   ← 孤立！

Phase 5 修复：删除孤立 result OR 为缺失 result 的 tool_call 补 stub
```

**MemGPT-mini — pre-cut validation（切之前就保证不会孤立）**：

```python
def _is_valid_cutoff(m):
    return m["role"] == "assistant"

# 切点候选 reversed 扫描，找最右的 assistant
chosen = next(i for i in reversed(range(1, cutoff_idx+1))
              if messages[i]["role"] == "assistant")
```

切点只能落在 assistant 上 → 切点之前的 [assistant tool_call → tool result] 三元组必然完整在 evict 侧 → tail 永远以 assistant 起始 → 不可能产生孤立 tool result。

**两种思路的取舍**：

- pre-cut validation 实现简单（只需校验切点角色），但只适用于「单向切前缀」的场景
- post-summarize sanitize 实现复杂（需要扫两遍 messages），但适用于「双向切」（hermes/nano 同时保留 head + tail，中间被摘要，两侧都可能产生孤立对）

### Anti-thrashing vs post-compact safeguard

**Anti-thrashing**（hermes/nano）：

```
Round N:   12000 → 11500（节省 4%）
Round N+1: 12500 → 11800（节省 5%）
Round N+2: 12700 → 12000（节省 5%）

→ 连续 2 次低效，标记本会话不再尝试压缩
```

**post-compact safeguard**（MemGPT-mini）：

```
sliding_window 压缩 → 还是超阈值
  → 自动升级到 all 模式（evict 全部）重压一次
  → 一次性把上下文清干净，下一轮从最低点开始
```

哲学差异：

- hermes/nano：**保守**，承认压缩可能不奏效，避免无效 LLM 调用浪费成本
- MemGPT-mini：**激进**，宁可多压一次也要保证压缩成功（因为信息没丢，都在 L2/L3）

---

## 6. 三层记忆架构（MemGPT-mini 独有）

这是 MemGPT-mini 区别于 hermes/nano 的**核心架构特征**：

```
L1 in_context     ← 当前发给 LLM 的 messages（压缩只动这层）
L2 recall         ← Postgres messages 表，所有原始消息（按 agent_id 过滤）
L3 archival       ← 向量事实存储（archival_memory_search）
```

**信息流**：

```
用户消息 → 同时写 L1（in_context）和 L2（recall.append）
压缩时   → L1 摘要替换，L2/L3 不动
查询历史 → 工具 conversation_search 查 L2，archival_memory_search 查 L3
```

**意义**：摘要可以丢失细节，但**信息没真正丢失** — 只要 lookup hints 写得好，未来 agent 可以通过工具检索回完整原文。这是为什么 MemGPT-mini 的 summary 模板可以更紧凑（5 段 ≤300 词），而 hermes/nano 的 summary 必须自包含（9 段 ~800 token）。

### nano / hermes 的对应方案 — `on_pre_compress` 钩子

hermes/nano 没有自带的三层记忆，但通过 `on_pre_compress` 钩子让外部 provider（如 RemoteSemanticProvider）在压缩前**抢救**关键信息到知识图谱：

```python
# 时序
should_compress?
  → on_pre_compress_all(middle_messages)  ← 抢救
  → compress(messages)                     ← 实际压缩
  → API call
```

**RemoteSemanticProvider 的实现**：

```python
def on_pre_compress(self, messages, **kwargs):
    # 提取最近 10 条 user/assistant 对话
    # 入队 retain（异步，复用 V12 的 writer queue）
    # 标记 tag="pre-compress"，便于后续检索
```

**对比**：

| | MemGPT-mini | hermes/nano |
|---|------------|-------------|
| 信息保留方式 | 内置三层记忆（每条消息都进 L2） | 钩子机制（让外部 provider 选择性抢救） |
| 完整性 | 100%（L2 是原始消息） | 部分（仅压缩时窗口内 + provider 决定保留什么） |
| 实现成本 | 高（需要 Postgres + 持久化所有消息） | 低（钩子是 in-memory broadcast） |
| 召回机制 | 工具调用（模型主动 search） | 自动 prefetch（每轮注入） |

---

## 7. 优缺点

### nano_hermes_agent (V15)

**优点**：
- 核心设计与生产版一致，能演示工业级压缩流水线
- 五阶段拆分清晰，每个阶段单独可测
- on_pre_compress 让长期记忆与上下文压缩协同
- 教学友好（450 行可读完）

**缺点**：
- 边界处理比源项目少几个 corner case
- 没有图像 token 特殊计算（源项目对 vision 模型有 1500 tok/image 的扁平估算）
- /compress 命令仅本地调试，无 IDE 集成
- token 估算粗略（`chars // 4`），不如 tiktoken 精确

### hermes-agent (源)

**优点**：
- 边界检查最完整（图像、tool schema 大小、模型 max_tokens 限制）
- usage.prompt_tokens 接入精确，无粗估 fallback
- 多模型适配（不同上下文窗口动态阈值）

**缺点**：
- 600 行流水线，对教学场景偏重
- 与多个生产组件耦合（gateway、session store）
- 单触发入口，无异常 fallback

### MemGPT-mini

**优点**：
- 双触发入口（post-step + exception），鲁棒性强
- tiktoken 精确计算 + tools schema 计入 token
- pre-cut validation 实现简单优雅（切点必落 assistant）
- 三层记忆 → 信息真正不丢
- post-compact safeguard 保证压缩一定成功（升级到 all 模式）
- 收敛保证（eviction_pct 循环必然退出）
- 268 行实现，最简洁

**缺点**：
- 单向切（只切前缀），无法保留中间历史
- 摘要模板比 hermes/nano 的 9 段粗（5 段），但配合 L2/L3 弥补
- 依赖 Postgres 三层记忆（增加部署复杂度）
- 异常路径（Entry B）会浪费一次 API 调用（虽然是合法兜底）
- 无 iterative summary update（每次重新摘要全部 evict）
- 无 on_pre_compress 类钩子（紧耦合在 step loop 里）

---

## 8. 选型建议

| 场景 | 推荐 | 理由 |
|------|-----|-----|
| 学习「工业级压缩怎么做」 | nano (V15) | 五阶段流水线最清晰 |
| 生产部署，要求稳定可控 | hermes-agent | 边界检查最完整 |
| 学习「记忆系统如何与压缩协同」 | MemGPT-mini | 三层记忆架构是范本 |
| 个人项目，想要最稳健的算法 | MemGPT-mini | 双入口 + safeguard + 收敛保证 |
| 需要保留中间历史（非只切前缀） | hermes / nano | 双向切（head + tail） |
| 重视长期记忆 / 知识图谱集成 | nano 或 hermes | on_pre_compress 钩子 |
| 部署有 Postgres 基础设施 | MemGPT-mini | L2/L3 自然落地 |
| 资源受限 / 不想引入数据库 | hermes / nano | 纯内存 + 可选远端 provider |

---

## 9. nano V15 实现的具体取舍

V15 在「完整复现 hermes 核心」和「教学简化」之间做了几个明确取舍：

| 简化项 | hermes 做法 | nano 做法 | 理由 |
|-------|-----------|----------|------|
| 触发 token 数 | 仅用 usage.prompt_tokens | usage 优先 + 粗估 fallback | 教学场景首轮也要能演示 |
| 图像 token | 单独 1500 tok/image | 不区分 | nano 不接 vision 模型 |
| 模型动态阈值 | 按 model 查表 | env CONTEXT_WINDOW 固定 | 单模型场景够用 |
| 边界检查 corner case | 多层兜底 | 关键检查 + LLM 失败返回原 messages | 教学优先可读性 |
| 工具调用形态 | 兼容多种 SDK 形态 | 仅 OpenAI dict 形态 | nano 只接一种 |

**保留的核心不变量**（与 hermes 1:1）：
- 五阶段流水线
- 9 段结构化模板
- Anti-thrashing
- Iterative update
- Tool pair sanitize（post-summarize）
- on_pre_compress 钩子

---

## 10. 关键代码对照

| 项目 | 关键文件 | 核心函数 |
|------|---------|---------|
| nano_hermes_agent | [context_compressor.py](../../context_compressor.py) | `should_compress` / `compress` / `_sanitize_tool_pairs` |
| nano_hermes_agent 测试 | [scripts/test_v15_compress.py](../../scripts/test_v15_compress.py) | 9 项不变量测试 |
| nano on_pre_compress | [memory/remote_semantic.py:350](../../memory/remote_semantic.py#L350) | `on_pre_compress` 异步入队 retain |
| hermes-agent | `/Users/qshf/my-project/hermes-agent/agent/context_compressor.py` | 同左 + 更多边界检查 |
| MemGPT-mini 实现 | https://github.com/qshf/MemGPT-mini/blob/main/memgpt/compaction.py | `_sliding_window_cutoff` / `_is_valid_cutoff` / `compact` |
| MemGPT-mini 文档 | https://github.com/qshf/MemGPT-mini/blob/main/docs/compaction.md | 设计原理、Letta 源码映射、walked example |

---

## 附：MemGPT-mini 关键不变量（来自源文档）

1. **Convergence**：`eviction_pct += 0.10` 循环，`pct < 1.0` 退出 → 必然终止
2. **Tool pair integrity**：`After[2]` 永远是 assistant（cutoff 保证）
3. **0.9 trigger ratio**：留 10% buffer 给下一轮 user input + LLM output
4. **Token undercount 防护**：`count_tokens_with_tools` 把 tools schema 也算进去
5. **Summary 自我溢出防护**：`summarizer_clip_chars=50000` 硬截断
6. **Post-compact safeguard**：还超阈值就升级到 all 模式重压一次（且 `trigger_threshold=None` 防止无限递归）
