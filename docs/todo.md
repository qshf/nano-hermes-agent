# 待办 / 已知问题

> CLAUDE.md 的"待办"区从这里被托管。新增项追加到下方对应版本块；解决的项保留划线版本，不删（保留"曾经踩过这个坑"的历史）。

## 仓库杂务
- [ ] 仓库根有几个无关临时文件（`1.txt` / `2.txt` / `MCP_CLIENT_EXPLAINED.md`），不在 git 跟踪范围，需要时再清。
- [ ] `docs/` 下迭代规划是 `iteration-plan-v10.1.md`，但 skill 模板期望 `iteration-plan.md`（聚合所有版本）— 后续应建一份合并版规划。
- [x] `docs/memory-nano-vs-source.md` 已完成（记忆系统全景对比）。

## Memory 系统（V11 / V16）
- [ ] V11 知识图谱抽取 prompt 需要根据实际使用效果调优（当前是通用版）。
- [ ] V11 事实去重阈值 `FACT_DEDUP_THRESHOLD=0.92` 需要实测验证。
- [x] ~~V11 `/retain` 含 LLM 调用 + 多次 embedding，单次约 2-5s~~ — V12 已通过后台 writer 解决（主循环 0 阻塞）。
- [x] ~~V11 图遍历目前只做 1-hop，复杂场景可能需要 2-hop。~~ — V16 已通过 `RECALL_HOPS` 实现 N-hop BFS（默认 2）。
- [x] ~~V12 后只剩 prefetch 同步阻塞（200-500ms/轮）~~ — V13 已通过后台 prefetch 预热解决（第 2 轮起近零阻塞）。
- [x] ~~仍未实现 `on_session_switch`：切 session 时 buffer 没 flush~~ — V14 已通过 on_session_switch 生命周期钩子解决（drain + 清缓存 + 轮转）。
- [ ] V16 `RECALL_HOPS=2` 的实测召回质量需要在真实 bank 上验证（教学示例可能数据量太小看不出差异）。
- [ ] V16 `DECAY_HALF_LIFE_DAYS=30` 是猜测值，需要根据实际记忆使用周期调优；用户能不能"显式重要"标记免衰减？

## Transports 系统（V17-V22）
- [x] ~~V17 `transports/` 只有 `chat_completions` 一家，ABC 价值在 V18 加 Anthropic 时才会真正显现~~ — V18 加 Anthropic 验证 ABC，V19 加 Chain 进一步验证"抽出来的边界能复用"。
- [ ] V19 断路器 `cooldown_seconds=60` 是猜测值，需要根据实际 provider 恢复时间调优；不同 reason 应不应该有不同 cooldown？
- [x] V19 真实多家 provider 联跑测试缺失 — 当前只有 fake transport 的不变量测试，需要在两家真 endpoint 上验证（比如故意把 OPENAI_API_KEY 改错触发 401，观察 chain 是否切到 Anthropic）。
- [x] V20 真实 cache 命中率验证缺失 — 需要在 DashScope Anthropic 端点上跑多轮对话，观察 `cache_read_input_tokens` 是否真有上升（DashScope 可能不实现 cache_control，盲启可能直接 400）。
- [ ] V20 break-even 轮数估算 — 单次 cache write 比 read 贵 ~12 倍，理论上需要 ≥ 13 轮命中才能回本；nano 没暴露 pricing 估算工具。
- [ ] V22 流式真跑验证缺失 — fake stream 不变量过了 13 项，但 `signal.SIGINT` + 真 DeepSeek 流式中 Ctrl+C 是否当帧停止、`/stream off` vs `on` 的 usage / cache 命中是否完全一致，都需要在真 endpoint 上手测一遍。
- [ ] V22 流式 + 工具调用混合：本档主要测了纯文本流和纯工具流，但 "先吐一段文字解释、再调工具" 的混合形态（tool_call_started 在 text_delta 之间穿插）UX 表现没专门 case；上 V23 之前补一个集成测试。
- [ ] V22 reasoning 实时回显被故意省略 — 仅打个 `[think] ...` 占位。如果要做完整 reasoning box（DeepSeek/Kimi 的 CoT 用户希望看到），延后到 V25/V26 再补。

## 文档
- [x] ~~CLAUDE.md 已超 250 行硬规则上限，下一档完成后应把决策日志按版本拆到 `docs/decisions/v<N>.md`，本文件只留索引。~~ — 已拆分（决策日志移到 [docs/decisions/](decisions/)，待办移到本文件）。
