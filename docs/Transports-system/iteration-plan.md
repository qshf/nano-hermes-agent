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
| **V19**（候选） | Failover + 健康检查 | 主家挂切备家 / 连续失败计数 / 健康恢复探针 | DeepSeek + Qwen 联合演示主备切换 | 散落在 `run_agent.py` retry/fallback 路径 |
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
| V19 | 推荐 | 落地 multi-agent（V21+）后失败容错才有真实价值 |
| V20 | 推荐 | 真实使用中发现重复发送相同 system prompt 才有动机 |

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
