# nano_hermes_agent — 子系统实现先后顺序考量

> 本文档解释：在 V0–V16 已经把"工具系统 + 记忆系统"两条主线打通后，**剩下哪些子系统值得做**、**为什么这个顺序**、**每个系统的最小切片是什么**。
>
> 受众：未来的自己 / 协作 LLM（跨会话保持决策一致）。
> 维护规则：每开一个新版本前回来 review；新增子系统插入到对应优先级；落地后把"状态"列改成 ✅。

---

## 0. 已完成的两条主线

| 主线 | 范围 | 终点版本 | 核心抽象 |
|------|------|---------|---------|
| 工具系统 | V0 → V5 | V5 MCP 接入 | `Tool` 注册表 / toolsets / tool calling schema / MCP 跨进程协议 |
| 记忆系统 | V6 → V16 | V16 多跳 + 时间衰减 | `MemoryProvider` ABC / `MemoryManager` 编排 / 围栏 / 知识图谱 / 读写双异步 / 会话切换 / 上下文压缩 |

**共同设计模式**（已经反复出现，下一阶段可继续复用）：
- ABC + 多实现：`Tool` / `MemoryProvider` 都是接口与实现分离
- 单一集成点：tool registry / MemoryManager 都是 agent loop 的"门面"
- 生命周期钩子：on_turn_start / prefetch / sync_turn / on_session_switch / on_pre_compress
- HTTP 边界 + 服务端独立演化：mock_memory_server 验证了"两端独立演化"
- 错误隔离 + 围栏 + 启动期 fail-fast

---

## 1. 候选子系统全景（源项目对照）

源项目 hermes-agent 中**还没在 nano 里复现的**主要子系统：

| 子系统 | 源项目位置 | 行数（参考） | 一句话定位 | 教学价值 | 复杂度 |
|--------|-----------|-------------|-----------|---------|-------|
| **Transports（多 LLM 适配）** | `agent/transports/*.py` + `agent/anthropic_adapter.py` + `agent/bedrock_adapter.py` | 5300+ | 把"对话后端的格式差异"抽成 ABC，让 agent loop 不依赖具体 LLM 家族 | ★★★★★ | 中 |
| **Multi-Agent（delegate）** | `tools/delegate_tool.py` + `tools/mixture_of_agents_tool.py` | 3300+ | 父 agent spawn 子 agent，受限 toolset + 独立 context + 并发汇总 | ★★★★ | 中-高 |
| **Skills（动态技能加载）** | `agent/skill_commands.py` + `skills/` + `optional-skills/` | ~1500 | 运行时扫描技能目录、按 trigger 注入 system prompt | ★★★ | 中 |
| **Curator + Trajectory** | `agent/curator.py` + `agent/trajectory.py` | 1837 | 结构化记录每轮对话用于回放 / 学习 / 调试 | ★★★ | 中 |
| **Gateway（多平台分发）** | `gateway/*` | 1500+ | 同一 agent 跑在 Telegram / Discord / Slack | ★★ | 高 |
| **ACP（Agent Comm Protocol）** | `acp_adapter/` + `acp_registry/` | 1500+ | agent 之间的通信协议（不仅父子，还有跨进程） | ★★ | 高 |
| **Plugin 发现机制** | `plugins/__init__.py` + `plugin.yaml` | ~300 | 扫描 plugin 目录 + yaml 元数据动态加载 | ★★★ | 中 |

---

## 2. 决策：先做 Transports，再做 Multi-Agent

### 2.1 当前外部约束

> **现实**：手头只有 DeepSeek 的 OpenAI 兼容 API，没有 Anthropic / Bedrock / Gemini 的 key。

这个约束直接影响排序。下面是利弊分析。

### 2.2 为什么不先做 Multi-Agent

直觉上 multi-agent 比 transports"更有产品感"，但放在第一位会有一个隐藏代价：

```
现状：agent.py 直接 new OpenAI() + client.chat.completions.create()
      ↓
先做 multi-agent：父 agent 和子 agent 都硬编码 OpenAI 调用
      ↓
后做 transports：父子两套调用点都要改，子 agent spawn 逻辑也要重写一遍
```

**根因**：multi-agent 是 transports 的"应用层" — 它需要 spawn 出一个能调 LLM 的子 AIAgent，这个子 agent 必然依赖 LLM 调用接口。如果接口还没抽好就先做 multi-agent，将来会出现"为了支持第二家 LLM 把 multi-agent 也改一遍"的回炉。

### 2.3 为什么 transports 值得先做（即使只有 DeepSeek）

**transports 的教学价值不全部依赖"真换一家"**。它的核心模式有三层：

| 模式层 | 单 DeepSeek 能体现吗 | 说明 |
|-------|--------------------|------|
| 1. 接口与实现分离（ABC + 多实现） | ✅ 能 | 抽出 `ChatTransport` ABC，先实现 `ChatCompletionsTransport`，agent loop 不再 import openai |
| 2. 格式差异点的归一化 | ⚠️ 部分 | `convert_messages` / `convert_tools` / `normalize_response` 的"形状"先立起来；具体差异留 stub |
| 3. 真适配第二家（Anthropic / Bedrock） | ❌ 暂无 | 等有 key 或本地起 vLLM/Qwen 时再加，**但接口已经在那等着** |

**真正的取舍**：transports V17 做到第 1+2 层就有教学价值 — 让读者理解"为什么这一层抽象是必要的"。第 3 层（真适配）作为后续档（V18+）按需补。

源项目 `transports/base.py` 只有 89 行，五个核心方法的 ABC 已经把模式说清楚了。**抽出 ABC 比写完所有适配器更重要**。

### 2.4 顺序结论

```
V17  Transports ABC + ChatCompletionsTransport（DeepSeek/OpenAI 兼容）
     ├─ 抽出 ProviderTransport ABC（5 核心方法）
     ├─ agent.py 不再直接调 openai SDK
     └─ 留好 Anthropic / Bedrock 适配器的 stub 接口

V18  多 LLM 真适配（条件：有 Anthropic key 或本地 vLLM/Ollama）
     ├─ AnthropicTransport（system 字段位置 / tool_use 结构 / cache_control）
     └─ failover：主家挂了自动切备家

V19  Delegate tool（单子任务）
     ├─ delegate_task tool：spawn 子 AIAgent（goal + 受限 toolset + context 注入）
     ├─ 子 agent 复用 V17 transport，不绑特定 LLM 家族
     ├─ 父只看 summary，子的中间 tool calls 不污染父 context
     └─ DELEGATE_BLOCKED_TOOLS：no recursive delegate / no shared memory write

V20  Delegate batch + 并发
     ├─ ThreadPoolExecutor 并发 N 个子任务
     ├─ 每个子 agent 独立 task_id（独立终端 session、文件操作缓存）
     ├─ 父子 memory 隔离（子用临时 bank_id 或纯 builtin）
     └─ 超时 + 异常隔离

V21  Mixture-of-Agents（可选）
     └─ 同题异问多 LLM 家族 → LLM 聚合答案（依赖 V18 已有多家 transport）
```

### 2.5 如果改主意先做 multi-agent 会怎样？

可以做，但要主动接受"V19 之后回来重构 agent loop 把 LLM 调用收敛进 transport"的代价。这不是死路 — 只是把一个干净的演进顺序变成了"做了 → 重构 → 再做"。**对教学项目而言**，干净的演进比快速产出更重要。

---

## 3. Memory-system 还能迭代什么（次要档）

> 详细对照见同目录 [Memory-system/memory-nano-vs-source.md](Memory-system/memory-nano-vs-source.md) 末尾"未来迭代候选"章节。

V11–V16 已经把 Hindsight 的核心生产能力 1:1 落地了。**剩下的全部是次要打磨项**，按教学价值排序：

| 候选 | 教学价值 | 复杂度 | 一句话 |
|------|---------|-------|-------|
| 实体消歧 + 别名合并 | 中（NLP pipeline） | 中 | "小明" / "XiaoMing" 合并为同一实体节点 |
| Bank 模板渲染（多维隔离） | 中（多租户） | 低 | `{user_id}_{platform}` 动态 bank_id |
| Plugin 发现机制 | 中（约定优于配置） | 中 | 扫描 `plugins/memory/*/plugin.yaml` 自动加载 |
| 注入扫描 | 小（安全） | 小 | 12 条 regex 防止 prompt injection 写入记忆 |
| Schema 迁移工具 | 小（运维） | 中 | 替代当前 `docker compose down -v` 重建 |
| 流式围栏 scrubber | 小 | 小 | 跨 chunk 状态机；前提是先做流式 UI |

**我的判断**：memory 主线收尾即可，不再单独开档。其中"实体消歧 + bank 模板"如果以后做 multi-agent + 多用户场景时会自然需要，到时再补一档"V22 多租户记忆增强"。

---

## 4. 各档最小切片（草稿，启动时再细化）

### V17 Transports — 抽 LLM 调用 ABC

**核心问题**：agent.py 当前直接 import openai 并 new OpenAI()，agent loop 与 OpenAI SDK 强耦合。即使 DeepSeek 兼容 OpenAI 协议，"看起来兼容"和"接口边界划清楚"是两件事。

**最小可教学切片**：
1. 新增 `transports/base.py`：`ProviderTransport` ABC（5 方法，照源项目 89 行版本）
2. 新增 `transports/chat_completions.py`：`ChatCompletionsTransport` 实现（包装 OpenAI SDK，覆盖 DeepSeek / OpenAI / 任何兼容端点）
3. 改 agent.py：通过 `transport.build_kwargs()` + `client.chat.completions.create(**kwargs)` 调用，agent loop 不直接见 openai 类型
4. 留好 Anthropic adapter 的"占位 stub"（构造函数 + 抛 NotImplementedError），写一段 README 说明"加 Anthropic key 时只需补这个文件"

**验证**：
- 现有 V16 测试全绿（agent loop 行为零变化）
- 增一个 `test_v17_transport.py`：构造一个 fake transport（直接返回固定响应），确认 agent loop 不依赖 OpenAI SDK

**简化掉的**（vs 源项目）：
- 不做 streaming（源项目 `_stream_consumer` 状态机）
- 不做 prompt caching 控制（源项目 `cache_control: ephemeral`）
- 不做 reasoning/thinking 字段
- 不做 SigV4 签名（Bedrock 专属）

### V19 Delegate — 父子 agent 架构

**核心问题**：当前所有任务都在主 agent 的 context 里执行，长任务会吃掉前缀缓存 + 污染对话历史。

**最小可教学切片**：
1. 新增 `tools/delegate_tool.py`：`delegate_task(goal, toolset, context)` tool schema
2. spawn 子 AIAgent：构造 fresh messages + 受限 toolset + 注入 goal 为 system prompt
3. 子 agent 跑完一轮 tool loop（最多 N 步），把 final assistant text 当 summary 返回
4. 父 agent 只看到 `delegate_task` tool 的返回（summary），不看到子 agent 的中间 tool calls
5. `DELEGATE_BLOCKED_TOOLS = {"delegate_task", "memory"}`（防递归 + 防共享记忆污染）

**验证**：
- 单元：子 agent 的 messages 不包含父 agent 的对话历史
- 集成：父让子 agent "把当前目录所有 .py 文件名列出来"，父 context 只看到一个 tool call + 一段 summary，没有 list_dir 等中间步

**简化掉的**（vs 源项目 2767 行）：
- 不做 batch 并发（V20 再加）
- 不做子 agent 自己的 task_id / 独立终端（共享父的 tool registry）
- 不做超时 / 中断
- 不做 mixture-of-agents

---

## 5. 维护规则

1. **顺序变更需要在这里留记录**：如果将来真要"先 multi-agent 后 transports"，把决策理由写在第 2.5 节末尾。
2. **新子系统插入位置**：候选表（第 1 节）按教学价值/复杂度排，新增的 ACP / 流式 / 观测等都进这里。
3. **状态字段**：每完成一档，"教学价值"列前面加 ✅ 标记，并把切片细化记录回到 CLAUDE.md 的进度表。
4. **跨文档同步**：本文档第 3 节是 memory 候选的索引，详细列在 [Memory-system/memory-nano-vs-source.md](Memory-system/memory-nano-vs-source.md) 末尾"未来迭代候选"。两边保持一致。
