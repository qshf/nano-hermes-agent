# Transports（多 LLM 适配）子系统 — 迭代拆分计划

> 目标：把 agent loop 从"硬绑 OpenAI SDK"演进到"通过 ABC 适配任意 LLM 家族"。
>
> 主线节点：V0–V16 是工具系统 + 记忆系统两条主线（详见 [CLAUDE.md](../../CLAUDE.md)）。本文档规划的 V17 起进入 **transports 子系统**。
>
> 上层路线参见 [docs/system-roadmap.md](../system-roadmap.md) — transports 排在 multi-agent 之前的理由也写在那里。

---

## 1. 源项目概述

源项目 [hermes-agent](/Users/qshf/my-project/hermes-agent) 把"和 LLM 对话"这件事抽成了一个 `ProviderTransport` ABC，每个 LLM 家族（OpenAI 兼容 / Anthropic / Bedrock / Codex Responses API / Gemini）都有独立的 transport 实现。同一份 agent loop 通过 `transport.build_kwargs(...)` → `client.create(**kwargs)` → `transport.normalize_response(...)` 三步就能跑在任意家族上。

### 1.1 源码规模

| 文件 | 行数 | 职责 |
|------|------|------|
| `agent/transports/base.py` | 89 | `ProviderTransport` ABC（5 核心 + 3 可选方法） |
| `agent/transports/types.py` | 162 | `NormalizedResponse` / `ToolCall` / `Usage` 数据类 |
| `agent/transports/__init__.py` | 68 | 注册表 + 自动发现 |
| `agent/transports/chat_completions.py` | 614 | OpenAI 兼容（含 16+ provider 的 quirks） |
| `agent/transports/anthropic.py` | 179 | Anthropic Messages API |
| `agent/anthropic_adapter.py` | 2064 | Anthropic 格式转换实现细节 |
| `agent/transports/bedrock.py` | 154 | Bedrock（继承 Anthropic + SigV4） |
| `agent/transports/codex.py` | 255 | Codex Responses API |

总计 **3500+ 行**。其中 ChatCompletionsTransport 的 **绝大部分行数是 provider-specific quirks**（Moonshot tool schema、Gemini thinking config、OpenRouter cache stats、LM Studio reasoning effort 等），核心模式本身只占 ~150 行。

### 1.2 核心抽象

```
agent loop                              ┌─────────────────────────────┐
  │                                     │  ChatCompletionsTransport   │
  ├──> transport.convert_messages()     ├─────────────────────────────┤
  ├──> transport.convert_tools()        │  AnthropicTransport         │
  ├──> transport.build_kwargs()  ──────>├─────────────────────────────┤
  │      │                              │  CodexTransport             │
  │      └──> client.create(**kwargs)   ├─────────────────────────────┤
  │                                     │  BedrockTransport           │
  └──> transport.normalize_response()   └─────────────────────────────┘
            │
            v
       NormalizedResponse(
         content, tool_calls, finish_reason,
         reasoning, usage, provider_data
       )
```

agent loop **只看到** OpenAI 风格的 messages + OpenAI function calling tools 这两个输入，**只消费** `NormalizedResponse` 这个统一返回类型。换 LLM 家族 = 换一个 transport 实例 + 换一个 SDK client，loop 一行不动。

---

## 2. 当前现状（V0–V16 之后的痛点）

[agent.py:103-107](../../agent.py) 直接 `from openai import OpenAI` 并实例化客户端：

```python
client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY"),
    base_url=os.environ.get("OPENAI_BASE_URL"),
)
```

[agent.py:369](../../agent.py) 直接调 OpenAI SDK：

```python
response = client.chat.completions.create(
    model=model, messages=messages, tools=tool_defs, ...
)
```

DeepSeek 走 OpenAI 兼容协议恰好能跑，但这种"看起来兼容"等同于把 LLM 调用边界**模糊化**了。一旦想：
- 换成 Anthropic SDK（Qwen 通过 DashScope 的 Anthropic 兼容端点）
- 跑 Bedrock / Codex Responses API
- 主家挂了自动切备家

agent loop 就要写一堆 `if provider == "anthropic":` 分支。这正是 V17 起要解决的事。

---

## 3. 真实可验证的 API（关键约束）

用户手头有两家**都能真跑通**的 API：

### 3.1 DeepSeek（OpenAI 兼容）
- 现有 `OPENAI_API_KEY` / `OPENAI_BASE_URL` 走的就是这家
- 走 `chat.completions.create` 端点
- 用于 V17 的 `ChatCompletionsTransport` 真跑

### 3.2 Qwen / DashScope（同时支持 OpenAI 和 Anthropic 两种格式）

```python
import anthropic, os
client = anthropic.Anthropic(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/apps/anthropic",
)
message = client.messages.create(
    model="qwen3.6-plus",
    max_tokens=1024,
    system="You are a helpful assistant",
    messages=[{"role": "user", "content": "你是谁？"}],
    thinking={"type": "disabled"},
)
```

这意味着 **V18 的 `AnthropicTransport` 不是 stub**，能真实跑通对话 + tool calling。这是把 transports 抽象从"纸面 ABC"推进到"两家真适配"的核心条件。

> **注意**：Qwen DashScope 的 Anthropic 端点要求 `max_tokens` 必填、`thinking={"type":"disabled"}`（除非你显式想用 reasoning）。这两点会在 V18 build_kwargs 里处理。

---

## 4. 版本规划

| 版本 | 标题 | 核心概念 | 真跑验证 | 对应源项目 |
|------|------|---------|---------|-----------|
| **V17** | Transport ABC + ChatCompletionsTransport | ABC / NormalizedResponse / format conversion / client 工厂 | DeepSeek | `transports/base.py` + `transports/types.py` + `transports/chat_completions.py`（核心 ~150 行） |
| **V18** | AnthropicTransport + Registry | 第二家 transport / 注册表 / env-driven 路由 / 格式差异具体化 | Qwen via DashScope Anthropic 端点 | `transports/anthropic.py` + `transports/__init__.py`（注册表）+ `anthropic_adapter.py` 子集 |
| **V19** | TransportChain + 断路器 | 多 transport 故障切换 / 错误三分类 / 断路器自愈 / jittered backoff | fake transport 不变量测试（14 项） | `agent/error_classifier.py` 简化版 + `agent/retry_utils.py` + `run_agent.py` fallback chain |
| **V20**（候选） | Prompt cache 控制 | Anthropic `cache_control: ephemeral` 显式标记 / 缓存命中率统计 | Qwen + 测命中率 | `agent/prompt_caching.py` + transport `extract_cache_stats()` |

### 4.1 为什么这样拆，不更细也不更粗

**不更细的拆法**（被否决）：
- "V17 抽 ABC、V17.1 实现 ChatCompletionsTransport、V17.2 改造 agent.py"
- 否决理由：ABC 没有任何具体实现是死的、不能跑。教学项目不接受"中间状态不能演示"。三件事（抽 ABC + 一个实现 + 改 agent.py）合并成 V17 仍然是最小可演示单元。

**不更粗的拆法**（被否决）：
- "V17 一档全做：ABC + ChatCompletions + Anthropic + 注册表"
- 否决理由：违反"每版本只解决一个问题"原则。学员看 git diff 时会同时面对"什么是 ABC"和"两家格式怎么差"两个抽象，认知负荷过高。V17 让学员先理解"为什么要抽这一层"（DeepSeek 一家就够），V18 才用第二家把抽象**真的**用起来。

**横切关注点（streaming / reasoning / prompt cache）的处理**：
- **prompt cache** → V20 单独一档（重要，体现 transport 抽象不同家族缓存语义的价值）
- **reasoning / thinking** → 不单独一档。V18 默认 `thinking={"type":"disabled"}` 让 Qwen 能跑；提取 thinking content 留作"已知简化"
- **streaming** → 不做。SSE 解析 + 部分 NormalizedResponse 构建 + 取消处理太复杂，对核心模式无新增贡献。在差异对比文档里标"省略掉"

### 4.2 V17 + V18 是核心，V19 / V20 按需推进

| 版本 | 优先级 | 触发条件 |
|------|-------|---------|
| V17 | **必做** | transports 子系统的最小可演示单元 |
| V18 | **必做** | 第二家适配是验证 ABC 价值的关键；Qwen 已可真跑 |
| V19 | **已完成** | TransportChain + 断路器 + jittered backoff，14 项不变量测试覆盖 |
| V20 | **已完成** | Prompt Cache 控制（Anthropic ephemeral system_and_3 + Usage 拆 read/write + chain 累计命中率），13 项不变量测试覆盖 |

---

## 5. V17 详细设计

### 5.1 标题
**Transport ABC + ChatCompletionsTransport — 把 LLM 调用收敛进抽象边界**

### 5.2 解决的问题
- agent.py 直接 `import openai`，agent loop 跨 6 个文件涉及 OpenAI 类型（`tc.function.name`、`response.choices[0].message`、`tc.function.arguments` 等 20+ 处）
- 即使 DeepSeek 走 OpenAI 兼容，也只是"碰巧能跑"，不是"接口边界划清楚"
- 后续要加任何不走 chat.completions 的家族（Anthropic / Codex Responses API）都会爆炸式蔓延 if/else

### 5.3 引入的概念

1. **`ProviderTransport` ABC**（仿源项目 `transports/base.py`，89 行版本几乎照搬）
   - 5 核心方法：`api_mode`（property）/ `convert_messages` / `convert_tools` / `build_kwargs` / `normalize_response`
   - 3 可选方法：`validate_response` / `extract_cache_stats` / `map_finish_reason`

2. **`NormalizedResponse` 数据类**（仿 `transports/types.py`）
   - 字段：`content` / `tool_calls: list[ToolCall]` / `finish_reason` / `reasoning` / `usage` / `provider_data`
   - `ToolCall`：`id` / `name` / `arguments`（JSON 字符串）+ 向后兼容的 `function` / `type` property（让 `tc.function.name` 仍能访问）
   - `Usage`：`prompt_tokens` / `completion_tokens` / `total_tokens` / `cached_tokens`
   - **保留向后兼容 properties** 是关键 — agent.py 现有的 `tc.function.name` 调用点不必动

3. **`ChatCompletionsTransport`**（仿 `transports/chat_completions.py` 的核心 ~150 行版本，跳过所有 provider quirks）
   - `convert_messages`：identity（OpenAI 格式直通）
   - `convert_tools`：identity
   - `build_kwargs`：组装 `{model, messages, tools, max_tokens, temperature, timeout}`
   - `normalize_response`：从 `ChatCompletion.choices[0].message` 抽 content / tool_calls / finish_reason

4. **客户端工厂**（最小版，未来 V18 扩展）
   - 函数 `make_llm_client(api_mode: str)`：当前只支持 `"chat_completions"` → 返回 `OpenAI(api_key=..., base_url=...)`
   - 未来 V18 加 `"anthropic_messages"` → `anthropic.Anthropic(...)`

### 5.4 agent.py 改造路径

```python
# 改造前
client = OpenAI(api_key=..., base_url=...)
response = client.chat.completions.create(model=..., messages=..., tools=...)
msg = response.choices[0].message
for tc in (msg.tool_calls or []):
    name, args = tc.function.name, tc.function.arguments

# 改造后
transport = ChatCompletionsTransport()
client = make_llm_client(transport.api_mode)
kwargs = transport.build_kwargs(model=model, messages=messages, tools=tool_defs)
raw = client.chat.completions.create(**kwargs)
normalized = transport.normalize_response(raw)
for tc in (normalized.tool_calls or []):
    name, args = tc.function.name, tc.function.arguments  # 兼容 properties 让这行不变
```

### 5.5 对应源项目

- `agent/transports/base.py:1-89` — ABC 完整对照
- `agent/transports/types.py:1-162` — 数据类对照（**含向后兼容 properties 设计**）
- `agent/transports/chat_completions.py:102-160` — convert_messages / convert_tools 的纯净版（跳过 codex sanitize）
- `agent/transports/chat_completions.py:509-595` — normalize_response 的核心实现

### 5.6 预估代码量

| 文件 | 行数 | 说明 |
|------|------|------|
| `transports/__init__.py` | ~10 | 包导出 |
| `transports/base.py` | ~80 | ABC（照搬源项目，去掉文档冗余） |
| `transports/types.py` | ~100 | NormalizedResponse / ToolCall / Usage（保留兼容 property） |
| `transports/chat_completions.py` | ~120 | ChatCompletionsTransport 核心 |
| `transports/client_factory.py` | ~30 | make_llm_client（V17 只支持 chat_completions） |
| **新增** | **~340** | — |
| `agent.py` | -10/+15 | 替换 client 创建 + 调用点 + 响应解析 |

### 5.7 暴露的下一档问题

- ABC 抽出来了，但只有一个实现 — 不能证明抽象有效。Anthropic 的 system 字段位置不同 / tool_use 结构不同 / max_tokens 必填这些**真正需要 transport 隔离的差异**还没遇到
- `make_llm_client` 仍是单分支 if-only，没有注册表机制

→ V18 必须加第二家 transport，让 ABC 的价值从"纸面"变"实证"。

### 5.8 验证方式

- **单元行为脚本** `scripts/test_v17_transport.py`：
  - Fake `ChatCompletion` 响应 → 通过 `ChatCompletionsTransport.normalize_response()` → 断言 `NormalizedResponse` 字段正确
  - 包含 tool_calls 的响应 → 断言 `ToolCall.function.name` 仍能访问（向后兼容 property）
  - empty content + finish_reason="stop" → 断言 valid
- **回归**：V12/V13/V14/V15/V16 现有脚本全过（agent loop 行为零变化）
- **真跑**：DeepSeek 跑两轮对话 + 一次 tool call（read_file 或 list_dir），目测正常输出

---

## 6. V19 详细设计（已完成）

### 6.1 标题
**TransportChain + 断路器 — 多 transport 故障切换 + 健康检查**

### 6.2 解决的问题
- V18 已有两家 transport 真跑，但任一家挂了 agent 就死（429 / 503 / 网络抖动 / billing 等都直接抛出，主循环没有兜底）
- 错误判断散落在调用点：哪些错误该重试？哪些该切下一家？哪些是用户输入问题切了也是错？没有统一决策
- 没有"刚刚连续失败的 transport，下一轮还要不要试"的概念 — 每次都重新调用一遍刚挂的家会浪费时间 + 加重对方故障
- 重试时机如果是固定 backoff，多 session 同步重试会形成 thundering herd 把刚恢复的服务再打挂

### 6.3 引入的概念

1. **`classify_error(exc)` 函数**（裁剪自源项目 `agent/error_classifier.py` 1058 行 → nano 170 行）
   - 返回 `ClassifiedError(action, reason, status_code, message)`
   - 三类 `ErrorAction` 表达完整决策空间：
     - `RETRYABLE` — 同一家再试一次（500/502/504/408、timeout 关键词）
     - `FAILOVER` — 这家彻底不行，切下一家（429 rate_limit / 401-403 auth / 402 billing / 503/529 overloaded / 404 model_not_found）
     - `FATAL` — 用户/输入问题，切了也是错（400 + context_overflow / 413 payload_too_large / 400 format_error）
   - 决策顺序：先看 status code → 再看消息关键词 → 兜底 RETRYABLE（保守）

2. **`TransportChain` 类**（裁剪自 `run_agent.py` 散落 fallback chain）
   - 持有 `list[_ChainEntry]`，每 entry = `(api_mode, transport, client, breaker)`
   - 主入口 `chain.call(client=None, **kwargs)` 签名兼容 `transport.call(client, **kwargs)` — `ContextCompressor` 等存量调用点零改动
   - 内部按链顺序：跳过 open 状态、对每 entry 做 RETRYABLE 内部重试、FAILOVER 切下家、FATAL 直接抛出、全失败 → `raise FailoverExhausted(attempts)`

3. **断路器三态（closed / open / half_open）**
   - 数据结构：`_BreakerState(consecutive_failures, opened_at, last_reason)` — 两字段表达三态
   - `opened_at == 0.0` → closed；`opened_at != 0` 且 `now - opened_at < cooldown` → open；过了 cooldown 但 `opened_at != 0` → half_open（探针）
   - 转换：连续失败 ≥ `failure_threshold` → 打开；冷却完毕的下一次调用是探针；探针成功 → 重置；探针失败 → 重新累计

4. **Jittered backoff**（仿源 `agent/retry_utils.py:19-57`）
   - `delay = min(base * 2^(attempt-1), 60.0) + uniform(0, 0.5*delay)`
   - jitter 防 thundering herd（多 session 同时重试），上限 60s 避免重试到天荒地老

5. **`/transport` 命令** — 健康检查可观测性
   - 展示每 entry 的 `state` / `consecutive_failures` / `last_reason` / `cooldown_left` / `model`
   - 教学场景必需：断路器是隐式状态，没 surface 学员根本看不到"为什么 primary 被跳过"

6. **Per-entry model（V19.1 修补）** — 每个 chain entry 自带 `model` 字段
   - 链字符串语法：`api_mode[:model]`，例：`chat_completions:deepseek-chat,anthropic_messages:qwen3.6-plus`
   - entry.model 为 None 时回退到调用方传入的 model（兼容 V18 单家行为）
   - 仿源项目 `run_agent.py:1742-1765` fallback chain 设计 — 每条 entry 自包含 `{provider, model}`，避免"主家 DeepSeek 但 MODEL=qwen3.6-plus"这种错配
   - 实现：`_try_with_retry` 用 `dict(kwargs)` 浅拷贝后覆盖 `model`，避免链上各 entry 互相污染调用 kwargs

### 6.4 agent.py 改造路径

```python
# 改造前（V18）
transport_mode = os.environ.get("TRANSPORT_MODE", "chat_completions")
transport = get_transport(transport_mode)
client = make_llm_client(transport.api_mode)
...
normalized = transport.call(client, model=model, messages=messages, tools=tools)

# 改造后（V19）
chain_env = os.environ.get("TRANSPORT_CHAIN") or os.environ.get("TRANSPORT_MODE", "chat_completions")
chain = build_chain_from_env(chain_env, client_factory=make_llm_client, ...)
client = chain.primary_client  # 兼容 compressor 等存量位置参数
...
try:
    normalized = chain.call(model=model, messages=messages, tools=tools)
except FailoverExhausted as e:
    print(f"[error] all transports failed: {e}")
```

### 6.5 对应源项目

- `agent/error_classifier.py` 1058 行 — nano `transports/error_classifier.py` 170 行
- `agent/retry_utils.py` 整个文件 — nano 的 `_jittered_backoff` 一个方法
- `run_agent.py:1655-1697` fallback 激活 + `run_agent.py:1742-1764` chain 初始化 — nano 的 `TransportChain.call` + `build_chain_from_env`

### 6.6 实际代码量

| 文件 | 行数 | 说明 |
|------|------|------|
| `transports/error_classifier.py` | ~170 | 3 类 ErrorAction + status code/关键词分类 |
| `transports/chain.py` | ~230 | TransportChain + _BreakerState + _ChainEntry + build_chain_from_env |
| **新增** | **~400** | — |
| `agent.py` | -3/+25 | 替换 transport 创建 + 增加 /transport 命令 + chain.call 调用点 |

### 6.7 裁剪权衡

源项目复杂度大头（被 nano 删掉的部分）：
- **provider-specific 错误串匹配** — gemini "thinking signature" / openrouter cache miss / llama_cpp grammar 等十几种 — 教学价值低于"看清三类决策"
- **`OAuthLongContextBetaForbidden` / `LongContextTier` / `LlamaCppGrammarPattern`** 等边缘 reason — 删
- **status code → reason 的优先级精细化**（同一个 400 在不同 provider 下 reason 不同）— nano 用直接映射 + 关键词兜底
- **多家轮询的 health check 后台线程** — nano 把"健康检查"做成请求路径上的副作用（call 时顺带更新 breaker），不开后台

### 6.8 暴露的下一档问题

- 不同 reason 是否应该有不同 cooldown？（rate_limit 几秒就好，billing 可能要几小时）
- 真实多家联跑测试缺失 — 需故意把主家 key 改错触发 401 验证 chain 是否切到备家
- `/transport reset` 命令？（手动复位断路器）
- chain 持有自己的 client，但 `MEMORY_PREFETCH_METHOD=reflect` 也调 LLM — mock_memory_server 是否该接 chain？

→ 后续可拆 V19.1（per-reason cooldown）、V19.2（health check 后台线程）、V19.3（mock server 接 chain）。

### 6.9 验证方式

- **不变量脚本** `scripts/test_v19_failover.py`（14 项，全部用 fake transport / fake exception）：
  - 5 项 classify_error：429 / 500 / 400-context-overflow / auth 关键词 / 未识别错误
  - 9 项 chain：primary 成功不切备 / FAILOVER 切备 / RETRYABLE 内部重试 / 全失败 raise FailoverExhausted / 断路器 threshold 打开 / open 状态跳过 entry / 半开探针 closed / FATAL 立即抛出 / 单 transport 链退化为 V18 行为
- **回归**：V12/V13/V14/V16/V17/V18 现有脚本全过（V15 的 2 项预存在错误来自 V18 改 `compress()` 签名时未同步该测试，与 V19 无关）
- **真跑**：当前 `TRANSPORT_CHAIN` 不设默认走 V18 单家行为已通过；联跑两家需后续真实环境验证

---

## 7. V20 详细设计（已完成）

### 7.1 标题
**Prompt Cache 控制（Anthropic ephemeral system_and_3）— 显式标记缓存边界 + 命中率统计**

### 7.2 解决的问题
- V18-V19 已经能跨两家 transport 跑通，但每轮都把整个 system prompt + 历史消息重发 — 多轮对话里几 KB 的稳定 prefix 反复占 input token 计费
- Anthropic 提供 `cache_control: ephemeral` 显式标记机制可省 ~75% input 计费，但需要调用方主动在消息上打 marker（与 OpenAI/DeepSeek 隐式 prefix 缓存不同）
- 没有命中率可观测性 — 调用方根本不知道"我打的 marker 真生效了吗""命中多少"

### 7.3 引入的概念

1. **`apply_anthropic_cache_control(messages, cache_ttl)`**（仿源项目 `prompt_caching.py` 72 行版本，几乎照搬）
   - **system_and_3 策略**：1 breakpoint 在 system + 3 breakpoints 在最后 3 条非 system 消息（Anthropic 单请求 4 个上限）
   - str content 自动升级为 `[{"type":"text", "text":..., "cache_control":...}]` block list
   - role=tool 直接在顶层挂 cache_control（convert_messages 后落到 tool_result 块）
   - 返回深拷贝，原 list 不被污染（避免跨轮污染）

2. **ABC hook `apply_prompt_cache(messages, cache_ttl)`**（在 `transports/base.py` 上加默认 identity）
   - 让"哪家需要主动打 cache_control"成为 transport 自己的协议特性
   - `ChatCompletionsTransport` 走默认 identity（DeepSeek/OpenAI 用 prefix 匹配自动缓存）
   - `AnthropicTransport` 重写为 `apply_anthropic_cache_control`
   - 设计原则：每加一种 cache 行为不同的 provider，只需在它的 transport 内部覆写 hook，chain 一行不动

3. **`Usage` 拆 read/write 两字段**
   - `cached_tokens`: cache 命中读取（Anthropic `cache_read_input_tokens` / OpenAI `prompt_tokens_details.cached_tokens`）
   - `cache_creation_tokens`: cache 首次写入（仅 Anthropic `cache_creation_input_tokens`，OpenAI 兼容 = 0）
   - 计费完全不同（read ~1/10 input 价，write ~1.25x input 价），合并会丢失关键信息

4. **chain 集成**（在 `_try_with_retry` 内）
   - 调 `entry.transport.apply_prompt_cache(messages)` 后再 call — 每个 entry 用自己的 transport hook 决定怎么标记
   - 累计 read/write/uncached 到 `_ChainEntry.cache_*_total`
   - failover 切备家时新 entry 用自己的 hook（chat_completions 走 identity，互不干扰）

5. **`/transport` 命令展示命中率**（V19 加入的命令上扩展）
   - 单次数字噪音大（一个字段差异就能让 prefix 失配率波动 30%+），累计统计才稳定可读
   - 输出格式：`cache: read=N write=M uncached=K hit_rate=XX.X%`

### 7.4 agent.py 改造路径

```python
# 改造前（V19）
chain = build_chain_from_env(chain_env, client_factory=make_llm_client, ...)

# 改造后（V20）— 多 2 个 cache 参数
chain = build_chain_from_env(
    chain_env, client_factory=make_llm_client,
    ...,
    cache_enabled=os.environ.get("PROMPT_CACHE_ENABLED", "0") not in ("0", "false", ""),
    cache_ttl=os.environ.get("PROMPT_CACHE_TTL", "5m"),
)

# /transport 命令多打印 cache 行
if s["cache_read"] or s["cache_write"] or s["cache_uncached"]:
    print(f"    cache: read={s['cache_read']} write={s['cache_write']} "
          f"uncached={s['cache_uncached']} hit_rate={s['cache_hit_rate']:.1%}")
```

### 7.5 对应源项目

- `agent/prompt_caching.py` 72 行 — nano `transports/prompt_caching.py` 几乎 1:1（删 `native_anthropic` 标志）
- `agent/transports/anthropic.py:150-159` `extract_cache_stats` — nano 同名方法 1:1
- `agent/transports/chat_completions.py:596-608` `extract_cache_stats` — nano 简化版（OpenAI 兼容只 read 不 write）
- `agent/usage_pricing.py` 的 cache token 累计 — nano 移到 chain（不算钱）

### 7.6 实际代码量

| 文件 | 行数 | 说明 |
|------|------|------|
| `transports/prompt_caching.py` | ~110 | apply_anthropic_cache_control + _apply_cache_marker |
| `transports/base.py` | +14 | apply_prompt_cache 默认 hook |
| `transports/anthropic.py` | +35 | extract_cache_stats / apply_prompt_cache 重写 / convert_messages 保留 cache_control |
| `transports/chat_completions.py` | +20 | extract_cache_stats |
| `transports/types.py` | +2 | Usage.cache_creation_tokens |
| `transports/chain.py` | +60 | _accumulate_cache_stats / cache_enabled+cache_ttl 参数 / status() 加 cache 字段 |
| **新增** | **~240** | — |
| `agent.py` | +12 | env 解析 + banner + /transport 命令扩展 |

### 7.7 裁剪权衡

源项目复杂度大头（被 nano 删掉）：
- **`usage_pricing.py` 700+ 行** — 把 cache read/write/input token 转换成各家具体单价的钱数估算。教学价值低于"看清命中率怎么算"
- **`native_anthropic` 标志** — 适配 Anthropic 兼容代理（如 OpenRouter 的 anthropic 模式），nano 永远走 native
- **Anthropic 1h TTL 单价区分** — 1h ttl write 比 5m write 贵 2x，read 同价；nano 不算钱，跳过
- **runtime override**（`agent.py:8555` 跨多个 session 切换 cache 开关）— nano 单 env 决定，启动后不改

### 7.8 暴露的下一档问题

- DashScope Qwen Anthropic 兼容端点是否真支持 `cache_control`？盲启可能 400。需要 V20.1 加"启动期一次探测"
- break-even 轮数估算工具？write 比 read 贵 ~12 倍，理论上需要 ≥ 13 轮命中才能回本
- chat_completions 隐式缓存的命中率统计已经累计在 `cache_read_total` 里，但用户可能误以为这是"自己打的 cache_control 生效了"。需要在 `/transport` 输出里区分 explicit vs implicit
- 多轮对话里 prefix 失配（比如 prefetch 的召回内容每轮变化）会让 cache 命中率断崖下跌 — nano 的注入位置（user message 内）会破坏前缀稳定性，需要在 prefetch 之前打 marker 还是之后？

### 7.9 验证方式

- **不变量脚本** `scripts/test_v20_prompt_cache.py`（13 项，全部用纯函数 / fake transport / fake response）：
  - 5 项 prompt_caching 模块：system_and_3 策略 / str→block 升级 / 1h ttl 注入 / 空 messages / 深拷贝不污染原 list
  - 3 项 transport hook：ChatCompletions identity / Anthropic 调底层模块 / convert_messages 保留 cache_control
  - 2 项 extract_cache_stats：Anthropic read+creation / chat_completions cached_tokens
  - 3 项 chain 集成：disabled 不注入 / enabled 注入到 transport 收到的 messages / status 累计 read/write/uncached/hit_rate
- **回归**：V12/V13/V14/V16/V17/V18/V19 现有脚本全过（V15 的 2 项预存在错误同 V19，stash 后 baseline 也是 7/9，与 V20 无关）
- **真跑**：当前 `PROMPT_CACHE_ENABLED=0` 默认行为同 V19；启用后联 DashScope Qwen 真跑命中率需后续真实环境验证
