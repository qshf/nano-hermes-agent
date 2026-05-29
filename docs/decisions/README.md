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
| v15.1 | 压缩边界修复 + assistant 消息合法性兜底 | [v15.1.md](./v15.1.md) |
| v16 | retain 批量 + 多跳图遍历 + 时间衰减 | [v16.md](./v16.md) |
| v17 | Transport ABC + ChatCompletionsTransport | [v17.md](./v17.md) |
| v18 | AnthropicTransport + Registry | [v18.md](./v18.md) |
| v19 | TransportChain + 断路器（多 transport 故障切换） | [v19.md](./v19.md) |
| v20 | Prompt Cache 控制（Anthropic ephemeral system_and_3） | [v20.md](./v20.md) |
| v21.3 | Skill 系统（progressive disclosure tier 1 + tier 2） | [v21.3.md](./v21.3.md) |
| v21.4 | 工具结果协议收口（tool_result/tool_error + dispatch 兜底） | [v21.4.md](./v21.4.md) |
| v22 | 流式输出 + 中断（stream_call / CancelToken / chain failover-before-first-event） | [v22.md](./v22.md) |
| v23.0 | 多智能体最小可用版（delegate_task / 子 loop / 工具黑名单） | [v23.0.md](./v23.0.md) |
| v23.1 | 批量并行 + 工具子集白名单（tasks 数组 + ThreadPoolExecutor + 白名单交集） | [v23.1.md](./v23.1.md) |
| v23.2 | 项目上下文注入 + --cwd 启动（nano-hermes-agent.md / AGENTS.md / 跨项目可用） | [v23.2.md](./v23.2.md) |

> 注：V8 的决策记录散落在对应的 commit message 与 [docs/Memory-system/](../Memory-system/) 子目录下，未来如有补录需求再回填本目录。V15 主决策（5 阶段压缩流水线）当时未单独建档，本目录从 v15.1 开始接力补完。
