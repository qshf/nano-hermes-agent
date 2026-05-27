# 决策日志索引

每一档版本的"为什么这样选 / 没那样选 / 真实踩坑 / 验证方式"。
新版本完成后在此追加一行 + 创建 `v<N>.md`。

| 版本 | 标题 | 文件 |
|------|------|------|
| v7 | 抽 MemoryProvider ABC | [v7.md](./v7.md) |
| v9 | prefetch / sync_turn 钩子 | [v9.md](./v9.md) |
| v10 | HTTP 边界 | [v10.md](./v10.md) |
| v10.1 | pgvector + OpenAI 范式 embedding | [v10.1.md](./v10.1.md) |
| v11 | 知识图谱记忆（Hindsight 1:1 复现） | [v11.md](./v11.md) |
| v12 | 异步 retain（后台 writer 线程） | [v12.md](./v12.md) |
| v13 | 后台 prefetch 预热（两阶段 recall） | [v13.md](./v13.md) |
| v14 | 会话切换（on_session_switch 生命周期钩子） | [v14.md](./v14.md) |
| v16 | retain 批量 + 多跳图遍历 + 时间衰减 | [v16.md](./v16.md) |
| v17 | Transport ABC + ChatCompletionsTransport | [v17.md](./v17.md) |
| v18 | AnthropicTransport + Registry | [v18.md](./v18.md) |
| v19 | TransportChain + 断路器（多 transport 故障切换） | [v19.md](./v19.md) |
| v20 | Prompt Cache 控制（Anthropic ephemeral system_and_3） | [v20.md](./v20.md) |
| v21.3 | Skill 系统（progressive disclosure tier 1 + tier 2） | [v21.3.md](./v21.3.md) |

> 注：V8 / V15 的决策记录散落在对应的 commit message 与 [docs/Memory-system/](../Memory-system/) 子目录下，未来如有补录需求再回填本目录。
