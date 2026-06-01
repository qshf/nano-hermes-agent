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

## 多智能体系统（V23）

- [ ] V23.0 真模型烟测缺失 — 10/10 fake-chain 不变量过了，但还需在真 DeepSeek 上手测：父让子做"读 README 第一行" → 子返回简短 summary、父 messages 里只多了 1 条 tool_call + 1 条 tool_result（不混入子的 read_file 中间步）。
- [ ] V23.0 父在 STREAM_ENABLED=1 下调 delegate 时，子内部仍走同步 chain.call，父 UI 表现为"等子时一片空白" — 这是预期行为（V23.2 才接流式中继），但需要在真跑里观察一下"空白时长 vs 子任务长度"是否符合直觉。
- [ ] V23.1 真模型烟测缺失 — 9/9 fake-chain 不变量过了，但还需在真 DeepSeek 上验证："父让子并行分析 3 个文件" → 父 messages 里只多 1 条 tool_call + 1 条 JSON 数组的 tool_result（实测耗时 < 串行 3 倍）。
- [ ] V23.1 父 LLM 看到批量返回的 JSON 数组字符串后是否会**自动 json.loads**？需要在真模型上观察 — 如果不会，下一档要在 system prompt 里加一句 hint（"the output field of a batch delegate result is a JSON array string; parse it before reasoning over individual tasks"）。
- [ ] V23 后续档计划已写在 [docs/Multi-agent-system/iteration-plan.md](Multi-agent-system/iteration-plan.md)：V23.2 流式中继 + cancel 桥接 / V23.3 结构化结果 / V23.4 嵌套（可选）。优先级：V23.2 → V23.3 → V24 trajectory，V23.4 仅在 V24 完成且有需求时启动。

## 压缩系统（V15 修复主线）

- [x] ~~`tail_start = 4 / n = 106` 假压缩 — 兜底语义错误导致 middle 1 条假压缩，触发 anti-thrashing 后整个 V15 流水线躺平~~ — v15.1 修复：兜底反向走最大化压缩。
- [x] ~~`/compress` 报 NoneType.strip 崩 — `_format_messages` 没兜 content=None~~ — v15.1 修复：`content = msg.get("content") or ""` + 类型守卫。
- [x] ~~assistant content=None 且无 tool_calls 时下一轮 400 — deepseek-v4-flash 把可见正文塞 reasoning_content~~ — v15.1 修复：写入侧 `build_assistant_history_msg` 抢救 + 读出侧 `convert_messages` sanitize 双道防线。
- [ ] **v15.2 候选**：`_ensure_last_user_message_in_tail`（防活跃任务消失，源项目 #10896）/ `soft_ceiling = budget * 1.5`（超大 tool 输出稳健）/ Prefill retry + `_empty_terminal_sentinel` + `_drop_trailing_empty_response_scaffolding`（接管 reasoning-only 抢救的正确语义，替代 v15.1 的"reasoning 提升 content"）/ Post-tool nudge（防 `tool→user` 非法序列）/ V15 旧测 test_5/test_6 修 transport 签名（V18 重构遗债）。
- [ ] V15.1 真模型烟测：在真 DeepSeek 长会话（≥ 100 turn）上跑 `/compress` 验证：① middle 不是 1 条假压缩；② tool 群完整不被拆；③ deepseek-v4-flash reasoning-only 响应不再触发 400；④ `/compress` 输出 token 节省百分比看着合理（≥ 50%）。

## 会话持久化（V24）

- [x] ~~V24.0 全量删重插会丢压缩前历史~~ — V24.1 已迁 append-only（只增不删）+ 压缩点会话分裂修复。
- [x] ~~手动 `/compress` 漏接会话分裂导致 append-only 游标卡死、压缩后新对话静默丢失~~ — 已抽 `agent/compaction.py::apply_compaction`，自动压缩与 `/compress` 共用，不变量 17/17b 覆盖。
- [x] ~~V24.1 真模型烟测~~ — 2026-06-01 在真实压缩链数据（`default → default-c1 → default-c1-c1`，多级链）上手测 5 项全过：① 列表折叠（`/sessions` 链只显 tip 一行）；② `/sessions --all` 展开见压缩前 root（215 msgs 原文可回溯）；③ preview 取链 root 首问"你好"非摘要；④ `/resume default` 提示 `redirected from default — compacted` 跳到 tip；⑤ 启动 `MEMORY_SESSION_ID=default` 自动 resolve 到 tip。多级链印证 append-only 游标连续压缩不卡死、每级旧全文完整保留。
- [ ] V24.1 last-write-wins 未解（决策 6 沿用 v24.0）— 多窗口共享同一 `state.db` 且 resume 同一会话各聊各的会互相覆盖。nano 约定每窗口用不同 `MEMORY_SESSION_ID` 物理隔离；v24.2 候选引入会话级锁或乐观版本号。
- [ ] V24.1 `resolve_resume_tip` 深度上限 32 是猜测值 — 正常压缩链不该有 32 级，但若哪天支持手动 fork 出树状链，线性走子代（`ORDER BY created_at DESC LIMIT 1`）只会跟最新一支，其余分支被忽略。树状场景需要重新设计走法。
- [ ] V24.1 偏离计划文档：resume 重定向取 `get_compression_tip` 的无条件到 tip 语义，而非计划写的 `resolve_resume_session_id`（root 有消息则短路）。计划文本未更新，以 [decisions/v24.1.md](decisions/v24.1.md) 决策 3 为准；后续若回写计划文档需对齐。
- [ ] V24.x 候选：append-only 之上接 trajectory 数据飞轮（导出"压缩前完整链 → 训练数据"）/ FTS5 全文检索跨会话搜历史 / 会话标题自动生成（`title` 列已预留，当前不写值）。

## 数据飞轮（V25）

- [x] ~~V25.0 trajectory 导出 + 脱敏~~ — ShareGPT 格式 + ~10 pattern redact + 三 flush 点（压缩点/退出/delegate 子轨迹），见 [decisions/v25.0.md](decisions/v25.0.md)。
- [x] ~~V25.1 insights 离线分析 + 结构化日志~~ — `InsightsEngine` 读 v24 SQLite 出 token/成本/tool top-N/失败率，`RedactingFormatter` 写盘前脱敏 + session 注入。决策 4：数据源是 SQLite 不是 jsonl（纠正原 roadmap），见 [decisions/v25.1.md](decisions/v25.1.md)。
- [ ] V25.1 成本估算 hardcode deepseek+qwen 单价，官方调价后会过时（决策 5 已知简化）。V26 候选：多家 pricing 表 + pricing_version + actual_cost 对账。
- [ ] V25.1 insights 砍掉的报表块（platform/skill breakdown、活动模式 day/hour/streak、top sessions、gateway markdown）留作 insights 扩展档。
- [ ] V25.1 失败率只认 `role=tool` 内容里的 `{"error":...}`（V21.4 协议）；`end_reason` 当前只写 `compression`，没有"会话因报错中止"的信号。若将来 end_reason 扩展（如 `error`/`cancelled`），insights 可加会话级失败维度。
- [x] ~~V25.1 结构化日志只接了 `agent/logging.py` 基础设施，尚未在 transport / agent loop / delegate 各调用点广泛埋点~~ — review 时发现 `setup_logging()` **从没被调用**（死模块）+ handler 挂错私有 logger（已有 8 个 `getLogger(__name__)` 模块收不到）。已修（决策 9）：handler 改挂 **root** + filter 挂 handler 级，`main.py` 启动开机 + boot/session/failover 埋点。`transports.chain` failover 等已有日志现自动流经脱敏。
- [ ] V25.1 主动埋点仍可扩 —— 目前靠"挂 root 让 8 个模块自动接入" + main.py 三处（boot / session 切换 / 压缩分裂）。agent loop 每轮 turn / tool dispatch 耗时 / memory recall 命中等更细的主动 `get_logger()` 埋点留后续按需补。


## 文档
- [x] ~~CLAUDE.md 已超 250 行硬规则上限，下一档完成后应把决策日志按版本拆到 `docs/decisions/v<N>.md`，本文件只留索引。~~ — 已拆分（决策日志移到 [docs/decisions/](decisions/)，待办移到本文件）。
