# V24 — 会话状态持久化（SQLite 会话子系统 + 真 resume + 压缩链）

> 主轴：**会话与压缩**（v14 会话切换 → v15 压缩 → **v24 持久化**）。它是 v14 的直接续作 + v15 的缺口补齐。
> 编号沿革：最初当成 v14 小补丁（拟名 v14.2），但方案涨到 ~620 行 + 独立 SQLite 子系统 + 压缩链，已非补丁量级，promote 到 **v24**。原 v24（trajectory + insights 数据飞轮）整体后移 **v25**。
> **拆 2 档交付**（见 §0）：**v24.0** 全量重写 store + 真 resume + `/sessions`（修掉假 resume，可独立上线）→ **v24.1** 迁 append-only + 压缩链分裂 + 重定向/折叠（加"压缩前可回溯"，接 v15）。
> 源项目对照：[hermes-agent/hermes_state.py](https://github.com/qshf/hermes-agent/blob/main/hermes_state.py)（SQLite 会话存储核心）+ `cli.py:5645-5757`（/resume）。
> 状态：**计划（未实现）**。落地后转 [docs/decisions/v24.md](decisions/) 并补"实现日期"。

---

## 0. 拆档：v24.0 → v24.1

为什么拆：append-only 与压缩链**绑死**（append-only 下一旦 in-place 压缩，游标与变短的 messages 错位，必须靠会话分裂化解 —— 决策 2/7），所以它俩进不了第一档。但"SQLite store + 真 resume"用**全量重写**就能独立修掉假 resume 这个 bug、先交付。切割面干净。

| 档 | 内容 | 行数 | 耦合 v15? | 独立交付? |
|----|------|------|----------|----------|
| **v24.0** | `SessionStore`（全量删重插）+ 启动 load + 轮末/退出写 + `/resume` 真 load + `/sessions` 列表。**修掉假 resume** | ~355 | 否 | ✅ |
| **v24.1** | store 写入迁 **append-only** + 压缩点会话分裂（`end_session`/`create_session`）+ `resolve_resume_tip` 重定向 + `list_sessions(fold_chains)` 折叠。**加压缩前可回溯** | ~295 | 是 | 依赖 v24.0 |

下文决策按**最终形态**（v24.1 完成后）描述；决策 2/7 标注了"v24.0 用全量重写过渡、v24.1 迁 append-only"的演进。拆档代价：v24.1 要把 v24.0 的全量重写改成 append-only，比一次做完多 ~30 行返工 —— 换来 v24.0 能先独立上线、里程碑清晰、风险低。

> **每档决策日志各自建档**：`docs/decisions/v24.0.md`（store 形态 + 真 resume，引用决策 1/3/4/5/6/8）、`docs/decisions/v24.1.md`（append-only 迁移 + 压缩链，引用决策 2/7 + 为何从全量重写改起）。

---

## 1. 这一档解决什么

**痛点：v14 的 `/resume` 是假的。**

v14 只落了 `on_session_switch` 生命周期钩子（给 memory manager 切 bank key + drain buffer 用），**从没把对话 messages 存过盘**。导致 [cli/commands/session.py:53-63](../cli/commands/session.py#L53-L63) 的 `/resume`：

```python
def cmd_resume(args: str, ctx: AgentCtx) -> None:
    target = args.strip()
    ctx.memory_manager.on_session_switch_all(target, reset=False)
    ctx.current_session_id = target
    ctx.messages[:] = [{"role": "system", "content": _rebuild_system_prompt(ctx)}]  # ← 只剩 system prompt
    ctx.turn_count = 0
```

命令描述写 "Resume an existing session by id"，实际**只换了 memory bank 的 key，把对话清成只剩 system prompt**。`/resume <任意 id>` 拿不回任何历史——这是 latent bug，不只是功能缺失。进程一退，整段对话蒸发。

**本档目标**：让 `messages` 成为可持久化的真实实体。
- 每轮末 / 压缩分裂时 / 退出时 append 到 SQLite（`sessions/state.db`）
- `/resume <id>` 真正 load 回历史对话
- 压缩链：压缩前完整历史封存在旧 session，可回溯（决策 7，v24.1）
- 新增 `/sessions` 列出已存会话 + 压缩链结构
- 顺带为 **v25 trajectory** 立住"session 是持久实体"这个前提

**纠一处文档错误**：[system-roadmap.md](system-roadmap.md) 的 V24 小节（"简化掉的"）写 "不做 SQLite 持久化 —— **v14 的 SQLite 已够用**"。前提错误：nano 的 v14 从来没有 SQLite（全项目搜 `sqlite` 只命中 mock memory server 的 pgvector，与会话无关）。本档落地时一并修正该句，并把 roadmap 的 V24 重定义为"会话持久化"、数据飞轮顺延 V25。

---

## 2. 源项目对照（生产级设计）

源项目 `hermes_state.py` 是 SQLite 驱动的会话存储（`~/.hermes/state.db`，SCHEMA_VERSION=11）：

| 维度 | 源项目做法 | 文件:行号 |
|------|-----------|----------|
| 后端 | SQLite + WAL 模式 + FTS5 全文搜索（三元组支持 CJK） | `hermes_state.py:309-426` |
| 会话表 | `sessions`（id / source / model / system_prompt / parent_session_id / 6 维 token / cost / title / ended_at / end_reason …30+ 字段） | `hermes_state.py:185-251` |
| 消息表 | `messages` **逐条一行**（role / content / tool_call_id / tool_calls / reasoning_* …） | 同上 |
| 写入 | **append-only**：`_last_flushed_db_idx` 游标只追加新消息、绝不删旧（nano 复现，决策 2） | `run_agent.py:4469-4527` |
| Resume | `get_messages_as_conversation()` 还原 OpenAI 格式；`resolve_resume_session_id` 沿链重定向到 tip（产品决策，非补 flush；nano 复刻，决策 7） | `cli.py:5645-5757` |
| 压缩 | **session 分裂**：`end_session(old,"compression")` + `create_session(new,parent=old)` + 游标归零，旧消息不删；列表默认折叠到 tip（nano 复刻，决策 7） | `run_agent.py:10078-10120` |
| 并发 | WAL 多读单写 + `BEGIN IMMEDIATE` 写锁 + 15 次重试抖动 + 每 50 写 checkpoint | `hermes_state.py:309-426` |
| CLI | `sessions list/browse/export/delete/prune/rename/stats` + TUI `/resume` `/branch` `/history` `/title` | `hermes_cli/main.py:10920-11100` |

---

## 3. 关键设计决策（nano 简化版）

### 决策 1：用 SQLite（逐条入行），不是松散 JSON 文件

**选**：单库 `sessions/state.db`（nano 根，env `SESSION_DB_PATH` 覆盖），WAL 模式，**一条消息一行**。用 Python stdlib `sqlite3`，无第三方依赖。

schema（源项目 30+ 字段裁到必要的 + 压缩链 3 字段）：

```sql
CREATE TABLE sessions (
    session_id    TEXT PRIMARY KEY,
    created_at    REAL,
    updated_at    REAL,
    turn_count    INTEGER,
    model         TEXT,
    input_tokens  INTEGER,   -- runtime.session_tokens 4 维直接摊平
    output_tokens INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    parent_session_id  TEXT,  -- 压缩链：指向被压缩封存的父会话（决策 7）
    ended_at      REAL,       -- 非空 = 已封存（压缩分裂 / 主动结束）
    end_reason    TEXT,       -- "compression" 等
    title         TEXT        -- 预留，本档可不填
);
CREATE TABLE messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    seq           INTEGER NOT NULL,        -- 会话内顺序（append-only 游标基准）
    role          TEXT NOT NULL,
    content       TEXT,                    -- 可空（assistant 有 tool_calls 时）
    tool_call_id  TEXT,                    -- 仅 role=tool
    tool_calls    TEXT,                    -- JSON 字符串，仅 role=assistant 且有调用
    reasoning_content TEXT                 -- DeepSeek/Kimi thinking，可空
);
CREATE INDEX idx_messages_session ON messages(session_id, seq);
CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
```

**为什么逐条入行而不是把整段 messages JSON 塞一列**：
- blob 塞 SQLite = 只把 SQLite 当文件用，相比 JSON 文件**没多买到任何东西**（不能按 message 查、不能 FTS、列表还得整段反序列化）
- 逐条入行对齐源项目 `messages` 表，教"把 OpenAI 消息规范化进表 + 读时重建 shape"这个真实生产模式（源 `get_messages_as_conversation`）
- append-only（决策 2）只对逐条入行成立 —— blob 没有"游标之后"可言
- 为后续可选 FTS（`/sessions` 搜内容）留地基

**读时重建 OpenAI shape**：`SELECT ... WHERE session_id=? ORDER BY seq`，逐行还原 dict —— `tool_calls` 非空才 `json.loads` 挂上、`tool_call_id` 仅 role=tool 挂上、`content`/`reasoning_content` 非空才带。**不存 system 行**（决策 3，resume 时重建）。

**元数据取自现成运行期状态**：`turn_count`（[main.py:544](../main.py#L544)）、`session_tokens`（[agent/runtime.py:40](../agent/runtime.py#L40) 4 维 dict 摊平进 4 列）、`model`。时间戳 `time.time()`。

**messages 各字段都可平凡序列化**（已核实）：assistant 的 `tool_calls[].function.arguments` 已是 JSON 字符串，tool 消息是 `{role,tool_call_id,content}`，无 SDK response 对象等不可序列化值。

### 决策 2：append-only 写入（游标 = 已存 max(seq)），不全量重写【最终形态，v24.1】

> **拆档演进**：v24.0 先用**全量删重插**（`DELETE FROM messages WHERE session_id=?` + 重插），简单、能独立修掉假 resume；v24.1 迁到下面的 append-only —— 因为引入压缩链后，全量重写会让压缩后旧消息被覆盖丢失。这一迁移是 v24.1 的核心改动（~30 行返工）。

**最终选**：完整复现源项目的 append-only 语义 —— 每次 `append` 只追加"游标之后"的新消息，**绝不 DELETE 旧消息**。游标无状态：`SELECT COUNT(*) FROM messages WHERE session_id=?` 即已写条数，append 索引 ≥ 该值的（剔除 system 后的）消息。

```python
def append(self, session_id, messages, *, turn_count, model, session_tokens):
    persistable = [m for m in messages if m.get("role") != "system"]
    already = self._count(session_id)          # 已写条数 = 游标
    with self.conn:                            # 一个事务
        for seq in range(already, len(persistable)):
            self._insert_message(session_id, seq, persistable[seq])
        self._upsert_session(session_id, turn_count, model, session_tokens)  # created_at 保旧值
```

**为什么 v24.1 必须从全量重写改成 append-only**：
- 全量删重插会让**压缩后旧消息被覆盖丢失** —— 与"可回溯压缩前历史"（决策 7）直接冲突
- append-only 是源项目"压缩前历史永久留旧 session"的**根因**（[run_agent.py:4484](../hermes-agent/run_agent.py#L4484) `flush_from = max(start_idx, _last_flushed_db_idx)`）

**append-only 与 nano in-place 压缩的冲突，靠会话分裂化解**（决策 7 详述）：
in-place 压缩（`messages[:] = compacted`）让运行时 messages 整个变短，游标与新列表错位。源项目唯一解法 = 压缩时换 `session_id` + 游标归零（新 session COUNT=0）。所以 append-only **必然绑定决策 7 的会话分裂** —— 二者一体不可拆，这正是它俩同进 v24.1、进不了 v24.0 的原因。每个 `session_id` 的 messages 只增不减，游标语义干净。

### 决策 3：system prompt 不进存档（resume 时重建）

**选**：存档 `messages` 时**剔除首条 system message**，resume 时用 `ctx.prompt_builder.build()` 重新生成首条 system，再拼接历史的 user/assistant/tool 消息。

**为什么不存 system**：
- system prompt 含 skill 索引 / 工具列表 / 项目上下文（v23.2 的 `--cwd` 注入）—— 这些**随启动环境变化**。存旧的 system 再 resume，会让会话带着过期的工具列表 / 错误的项目上下文跑
- 与 `/new` `/resume` 现有"重建 system prompt"逻辑（[session.py:18-21](../cli/commands/session.py#L18-L21) 的 `_rebuild_system_prompt`）一致，复用而非另起
- 源项目存 `system_prompt` 字段是为跨平台 / 审计回放；nano 教学版不需要，简化掉

**代价**：resume 后的对话用的是"当前"system prompt，不是存档时的。对教学版是正确取舍（用户改了 skill / 换了 cwd，理应生效）。决策日志记一笔。

### 决策 4：session-store 和 v25 trajectory 是两个独立 store

这是本档**最重要的边界**，避免 v25 启动时返工：

| | **本档 session-store** | **v25 trajectory** |
|---|---|---|
| 形态 | append-only 入库（每 session_id 只增）+ 压缩分裂 | append-only、逐 turn 追加 |
| 内容 | 完整运行时 `messages`（含 tool_call_id / 压缩前全文留旧 session） | `from/value` SFT 训练格式（有损、脱敏） |
| 目的 | **状态回放** — resume 真能续上 + 压缩前可回溯 | **数据飞轮** — 喂 SFT / 统计 |
| 读写 | 读 + 写 | 只写 |
| 触发 | 每轮末 / 压缩分裂 / 退出 | `on_turn_end` |

**两者仍不合并**（即便都是 append-only）：trajectory 是有损训练格式（脱敏 + `from/value` 转换），拿它 replay 会丢 tool_call_id；session-store 存的是可直接喂回 transport 的原始 OpenAI messages。关注点正交（roadmap §1 已把"状态恢复 vs 数据飞轮"分列）。v25 落地时 trajectory recorder 反而**依赖** session 是持久实体 —— 本档（v24）是 v25 隐性前置。

### 决策 5：写入时机 = 每轮末 + 压缩分裂前 + 退出，同步写，不上后台线程

**选**：三个 append 点，全部同步：
- 轮末（[main.py:777](../main.py#L777) `sync_all` 之后）：append 当轮新消息
- 压缩分裂前（[main.py:651](../main.py#L651) compress 之后、`messages[:]=compacted` 之前）：把旧 session 压缩前全文 flush 到底（决策 7 的关键步）
- 退出兜底（[main.py:784](../main.py#L784) finally 内、`shutdown_all()` 前）：append 残余 + `close()`

**为什么不学 v12/v13 的后台 writer 线程**：
- append-only 每次只插增量几条，同步 `INSERT` 耗时 < 5ms，无感
- 后台线程要处理"退出时 join / 写一半进程被 kill / 压缩分裂的顺序依赖"，对教学版收益为负
- 与 memory 的后台 writer（那个有真实 2-5s LLM + embedding 延迟）形成对照：**该异步的异步，不该异步的别异步**，是个好教学点

**为什么轮末 + 退出都写**：轮末写保证 crash 也只丢当轮；退出写覆盖"`/resume` 切走前把旧会话最后状态固化"的边界。

### 决策 6：并发写保护 = WAL + busy_timeout，防物理损坏不防逻辑覆盖

上 SQLite 后多窗口共享 `state.db` 成为现实场景（开多个终端各跑一个 `main.py`）。源项目那套（WAL + `BEGIN IMMEDIATE` 写锁 + 15 次重试抖动 + 每 50 写 PASSIVE checkpoint）是 gateway 级别需求（多平台会话 + 后台 curator 并发写）。nano 取**最小够用**：

```python
conn.execute("PRAGMA journal_mode=WAL")    # 读不挡写
conn.execute("PRAGMA busy_timeout=5000")   # 第二个写者等 5s 再报错（手写重试循环的内置替身）
```

跳过手写重试循环和定期 checkpoint（WAL 自动 checkpoint 够用）。

**必须讲清的边界（教学点）**：SQLite 文件锁防的是**物理损坏 / 写半截**，**防不了逻辑冲突**。两个窗口若 `/resume` 同一个 `session_id` 各聊各的，事务都成功，但**最后 commit 的全量重写覆盖先 commit 的**（last-write-wins，丢前者对话）。

nano **不引入会话级锁**解决逻辑冲突 —— 单用户教学版不值当。约定用法：每个窗口用不同 `MEMORY_SESSION_ID`，物理上不撞同一会话。决策日志记明这条边界，避免误以为"上了 SQLite 就多窗口安全"。

### 决策 7：复刻压缩链 —— 会话分裂 + resume 重定向到 tip + 列表折叠【v24.1】

**源项目的压缩链**（[run_agent.py:10078-10120](../hermes-agent/run_agent.py#L10078)）：压缩时"会话分裂"——`end_session(旧,"compression")` 只打标不删消息，换 `session_id` + `create_session(parent=旧)` + 游标归零，压缩后视图写新会话。`parent_session_id` 串成链。

**关键事实（修正本计划早先的错误论断）**：源项目**每轮**就 flush（[run_agent.py:4314](../hermes-agent/run_agent.py#L4314) `_persist_session`），所以压缩发生时旧 session 行里**已有压缩前完整消息**——压缩前历史本就可回溯。它额外做两件事**不是为了补 flush，而是产品决策**：
- **resume 重定向**（[hermes_state.py:1621](../hermes-agent/hermes_state.py#L1621) `resolve_resume_session_id`，3 个 resume 入口都调）：resume 旧 id → 沿 `parent_session_id` 走到有消息的最新 tip。让"一条逻辑对话"默认恢复到连续状态的最新点，而非停在压缩断点。
- **列表折叠**（[hermes_state.py:1184](../hermes-agent/hermes_state.py#L1184) `project_compression_tips=True`）：一条压缩链在 `sessions list` 只显示一个条目（投影到 tip），压缩前 root 默认不单列。

**nano 复刻这套语义**（用户选定）：数据层保留压缩前全文（可回溯），产品层默认折叠 + 重定向（默认看不到/切不回旧节点，与源项目一致）。nano 压缩点 [main.py:651-653](../main.py#L651-L653)：

```python
compacted = compressor.compress(messages, client, model, transport=chain)
store.append(current_session_id, messages, turn_count=..., ...)   # 旧 session 压缩前全文落底
store.end_session(current_session_id, "compression")
old_sid = current_session_id
current_session_id = f"{old_sid}-c{compressor.compression_count}"
store.create_session(current_session_id, parent_session_id=old_sid, model=model)
memory_manager.on_session_switch_all(current_session_id, reset=False)  # 对齐源 run_agent.py:10148
messages[:] = compacted
ctx.current_session_id = current_session_id   # 同步局部变量回 ctx（易错点）
```

需要在 SessionStore 实现两个方法对齐源项目：
- `resolve_resume_tip(session_id)`：沿 `parent_session_id` 往子代走到有消息的最新 tip（源 `resolve_resume_session_id` 的简化版，深度上限 32 防环）。`/resume` 调它。
- `list_sessions(fold_chains=True)`：默认把压缩链投影到 tip，只返回一行；`fold_chains=False` 给 debug 看 raw root。

**代价（诚实标注）**：选复刻语义后，本档比"全列+可切回"那版多 `resolve_resume_tip` + 列表折叠递归（~+40 行），且**与 v15 压缩深度耦合**。本档不再是"轻档"。压缩前节点默认对用户隐藏 —— 想回溯需 debug 路径（`load(root_id)` 直读或 `/sessions --all`，列入 todo）。

### 决策 8：子 agent 结果本档自动落盘，原始中间步归 v25

**本档对 delegate 子 agent 零新增**，因为 altitude 已经对齐（v23.0 隔离设计 + v23.4 结构化结果）：

- 父 messages 里子 agent 只体现为 **1 条 `delegate_task` tool_call + 1 条 tool_result**，后者 content 是 v23.4 的结构化 JSON（`{"results":[{summary,status,tokens,tool_trace,duration_seconds,...}]}`）。这条 tool message 本就在父 messages 里 —— **持久化父会话时，子的结构化摘要 + tool_trace 一起免费落盘**
- 子 token 已在 v23.4 聚合进 `runtime.session_tokens`，本档存它时一并落
- 子 agent **内部原始 messages**（自己的 read_file 等中间步）从不进父 messages（v23.0 隔离），属观测数据 —— 归 **v25 trajectory**（每个子独立 trajectory 文件 + 父记 `child_trajectory_path`）

这正好印证决策 4 边界：**session-store = 父会话状态回放**（含子的结构化摘要，够 resume 后父 LLM 理解"上次委派过什么"）；**trajectory = 含子原始步的观测/训练数据**。两个 store 各取所需，本档不碰子的内部步。

---

## 4. 最小可教学切片

> **档归属**：🟢 = v24.0（全量重写就能做）；🔵 = v24.1（append-only + 压缩链增量）。同一文件跨两档演进时分别标注。

### A. 持久化核心 `agent/session_store.py`（v24.0 ~150 行 → v24.1 +~100 行）

🟢 v24.0 形态（全量重写 + 真 resume，不含压缩链）：
```python
DB_PATH = Path(os.environ.get("SESSION_DB_PATH", "sessions/state.db"))

class SessionStore:
    def __init__(self, db_path=DB_PATH):
        # mkdir parent；connect；PRAGMA journal_mode=WAL + busy_timeout=5000；建表（IF NOT EXISTS）
    def save(self, session_id, messages, *, turn_count, model, session_tokens) -> None:
        """🟢 v24.0：一个事务 DELETE + 重插（全量）。🔵 v24.1 改名 append() 走游标增量。"""
    def load(self, session_id) -> dict | None:
        """SELECT session 行 + messages ORDER BY seq；重建 OpenAI shape（不含 system）。无则 None。"""
    def list_sessions(self) -> list[dict]:
        """🟢 v24.0：各 session 元数据 + msg 数 + 预览，按 updated_at 倒序。"""
    def delete(self, session_id) -> bool: ...
    def close(self) -> None: ...
```

🔵 v24.1 增量（迁 append-only + 压缩链）：
```python
    def append(self, session_id, messages, *, turn_count, model, session_tokens) -> None:
        """游标 = COUNT(*) WHERE session_id。只插剔除 system 后、索引 ≥ 游标的新消息。绝不 DELETE。（替代 save）"""
    def end_session(self, session_id, reason) -> None:          # 打标 ended_at+end_reason，不动消息
    def create_session(self, session_id, *, parent_session_id, model) -> None:   # 新 session，游标=0
    def resolve_resume_tip(self, session_id) -> str:
        """沿 parent_session_id 走到有消息的最新 tip（深度上限 32 防环）。对齐源 resolve_resume_session_id。"""
    # list_sessions 加 fold_chains=True 参数：默认投影压缩链到 tip；False 返回 raw root（debug）
```

要点：
- 🔵 `append` 游标无状态（每次 `COUNT(*)` 算），不在内存存 `_last_flushed_idx` —— 重启 / resume 后游标自动正确
- 重建 shape：`tool_calls` 列非空才 `json.loads` 挂上、`tool_call_id` 仅 role=tool、`content`/`reasoning_content` 非空才带（与 [main.py:710-711](../main.py#L710-L711) `build_assistant_history_msg` 产物对称）
- `sqlite3` 连接非线程安全 → 父 main loop 单线程用，`check_same_thread` 默认即可；delegate 子线程**不碰** store（子结果已在父 messages 里）
- session_id 是列值不是文件名，无目录穿越；仍校验非空
- schema：🟢 v24.0 即建全部列（含 `parent_session_id`/`ended_at`/`end_reason`），v24.1 才往里写 —— 避免 v24.1 改表

### B. main.py 接入（v24.0 ~25 行 → v24.1 +~20 行）

1. 🟢 **启动建 store + load**（[main.py:481-487](../main.py#L481-L487) 附近）：`store = SessionStore()`；若 `store.load(current_session_id)` 非空，挂历史到 `messages`（system 重建在前 + 历史在后）、恢复 `turn_count` / `runtime.session_tokens`。banner `Session:` 行标注 `(resumed N msgs)` / `(new)`。
2. 🟢 **轮末写**（[main.py:777](../main.py#L777) `sync_all` 之后）：v24.0 `store.save(...)`，v24.1 改 `store.append(...)`。
3. 🟢 **退出兜底写 + close**（[main.py:784](../main.py#L784) finally 内、`shutdown_all()` 前）。
4. 🟢 store 挂进 `ctx`（新增 `ctx.session_store` 字段）供 slash 命令用。
5. 🔵 **压缩分裂回调**（[main.py:651-653](../main.py#L651-L653)，决策 7 核心 —— v24.1 才加）：compress 之后、`messages[:]=compacted` 之前，串起 `store.append(旧, 压缩前全文)` → `store.end_session(旧,"compression")` → 换 `current_session_id` → `store.create_session(新, parent=旧)` → `memory_manager.on_session_switch_all(新, reset=False)`。注意 `current_session_id` 是局部变量，改完要同步 `ctx.current_session_id`（易错点）。

### C. slash 命令（cli/commands/session.py 改造 + 新增 `/sessions`，v24.0 ~50 行 → v24.1 +~15 行）

1. **`/resume` 改真 load**（[session.py:53-63](../cli/commands/session.py#L53-L63)）：
   ```python
   tip = ctx.session_store.resolve_resume_tip(target)   # 🔵 v24.1：旧 id 重定向到压缩链最新 tip（v24.0 直接用 target）
   data = ctx.session_store.load(tip)
   if data is None:
       print(f"  [session] No saved session: {target}"); return
   ctx.session_store.append(ctx.current_session_id, ctx.messages, ...)  # 🟢 先固化旧会话（v24.0 用 save）
   ctx.memory_manager.on_session_switch_all(tip, reset=False)
   ctx.current_session_id = tip
   ctx.messages[:] = [{"role": "system", "content": _rebuild_system_prompt(ctx)}] + data["messages"]
   ctx.turn_count = data["turn_count"]
   # 恢复 runtime.session_tokens
   if tip != target:   # 🔵 v24.1
       print(f"  [session] Resumed: {tip} (redirected from {target} — compacted)")
   else:
       print(f"  [session] Resumed: {tip} ({len(data['messages'])} msgs, turn {data['turn_count']})")
   ```
   🟢 v24.0 已能真 load + 续聊；🔵 v24.1 加 `resolve_resume_tip` 重定向（旧压缩 id → 跳 tip）。
2. 🟢 **`/new` 切走前固化旧会话**（[session.py:38-44](../cli/commands/session.py#L38-L44)）：清空前先 `save`/`append`。
3. 🟢 **新增 `/sessions`**（抄 `/memory` 列表风格，category="session"）：列 `list_sessions()`，每行 `<id>  <turn>turns  <n>msgs  <updated 相对时间>  <preview>`。
4. 🔵 **`/sessions --all` 折叠开关**（v24.1）：默认 `fold_chains=True` 压缩链折叠成 tip 一行；`--all` 走 `fold_chains=False` 展开看压缩前节点（debug）。
5. **可选 `/session delete <id>`**：扩展位，本档可不做（todo 标注）。

### D. 测试（v24.0 `scripts/test_v24_0_session_store.py` 10 项 + v24.1 `scripts/test_v24_1_compaction_chain.py` 6 项，均用临时 db）

🟢 v24.0（10 项）：
1. `save` → `load` round-trip，messages 内容一致
2. 存档/load **不含** system message（由调用方重建）
3. tool_calls（assistant）/ tool_call_id（tool）round-trip 重建正确
4. `created_at` 跨多次 save 不变，`updated_at` 递增
5. in-place 压缩前的全量重写：messages 变短后 save，load 回来 = 当前视图（v24.0 不留旧消息，符合预期）
6. `list_sessions` 倒序 + preview 截断 + msg 数正确
7. 事务原子性：mock 第二条 INSERT 抛异常，整次 save 回滚（不留半截）
8. `load` 不存在的 id 返回 `None`
9. session_tokens 4 维 round-trip
10. `delete` 删行 + 返回值；WAL 随 `close` 收尾

🔵 v24.1（6 项，验证迁移 + 压缩链）：
11. **append-only 游标**：两次 append（第二次是第一次超集）只插增量，旧行 id 不变、不重复
12. **压缩链核心**：append 旧 N 条 → `end_session` → `create_session(new,parent=old)` → append 新 M 条；旧 session 仍 `load` 出 N 条原文、新 session 出 M 条、`parent_session_id` 正确
13. **resume 重定向**：`resolve_resume_tip(旧 id)` 跳 tip；旧 id 有消息/无子代时原样返回；链深 >1 跳最末 tip；构造环不死循环（上限 32）
14. **列表折叠**：`fold_chains=True` 一条链只出一行（tip）；`False` 展开所有节点（含 root）
15. `end_session` 只打标不删消息（标记后仍 load 出全文）
16. **迁移兼容**：v24.0 用 save 写的旧库，v24.1 的 append 能在其上正确续写（游标从已有 COUNT 起算，不重复插）

---

## 5. 验证

🟢 **v24.0**：
- `save→load` round-trip 单测全过（10 项）
- 真跑：起 agent 聊 3 轮 → `quit` → 重启同 `MEMORY_SESSION_ID` → 历史 3 轮还在，`turn_count` 续上（**今天做不到的，v24.0 即修好**）
- `/new` 开新会话 → `/sessions` 看到两个会话 → `/resume <旧 id>` 把旧对话拉回，继续聊接得上
- `/resume` 不存在的 id → 友好报错，不崩、不清空当前对话
- 关掉持久化（`SESSION_DB_PATH=:memory:` 或 env 开关）→ 退化到 v14 行为，旧测零回归
- 跨档零回归：v14_session_switch / v15_compress / v23_* 全过

🔵 **v24.1**：
- append-only + 压缩链单测全过（6 项）
- **压缩链真跑**：把 `CONTEXT_WINDOW` 调小逼出压缩 → 压缩后打 `[session] split: <旧> → <旧>-c1 (N msgs archived)` → `/sessions` 默认**只显示 tip 一行**（链折叠）→ `/resume <旧 id>` 提示 `redirected from <旧> — compacted` 并恢复到 tip → `/sessions --all` 才看得到压缩前 root 节点
- **迁移验证**：v24.0 用 `save` 写的旧库，升 v24.1 后 `append` 在其上正确续写（不重复插）
- 多窗口边界：两个窗口 resume 同一 session_id 各聊 → append-only 下两者消息交错入同一 session（决策 6 已注明非目标，约定用不同 `MEMORY_SESSION_ID`）
- 跨档零回归：**v15_compress（压缩回调改动重点验**）/ v23_* 全过

---

## 6. 简化掉的（vs 源项目 ~2100 行 hermes_state.py）

- **复刻压缩链含产品语义**（决策 7）：会话分裂 + `parent_session_id` + `resolve_resume_tip` 重定向 + 列表折叠（`project_compression_tips` 的简化版）；压缩前节点默认隐藏，`/sessions --all` 才展开
- **不存 system_prompt**（决策 3）：resume 时重建
- **不做 FTS5 全文搜索 / 三元组 CJK 索引** —— `/sessions` 只列不搜（逐条入行已留地基）
- **不做 cost 估算字段**（estimated/actual_cost_usd + pricing_version）—— 留给 v25 insights；只存 4 维原始 token
- **不做 source / handoff / 跨平台字段** —— nano 只有 CLI 单一入口
- **不做 export / prune / rename / stats 子命令** —— 只做 list + resume + (可选) delete
- **不做手写重试循环 / 定期 checkpoint**（决策 6）—— 用 WAL + `busy_timeout` 内置兜底；不做会话级锁（多窗口约定用不同 session_id）

---

## 7. 预估规模（按档拆）

| 部分 | v24.0 | v24.1 增量 |
|------|-------|-----------|
| `agent/session_store.py` | ~150（全量重写 store + load + list + delete） | +~100（append 迁移 + end/create/resolve_tip + fold） |
| `main.py` 接入 | ~25（建 store + load + 轮末/退出写 + ctx） | +~20（压缩分裂回调） |
| `cli/commands/session.py` + `/sessions` | ~50（真 resume + 列表） | +~15（重定向 + `--all` 折叠） |
| `cli/context.py` 加 `session_store` 字段 | ~3 | — |
| 测试 | ~125（`test_v24_0_session_store.py` 10 项） | ~125（`test_v24_1_compaction_chain.py` 6 项 + 迁移） |
| **小计** | **~355**（核心 ~230 + 测试 ~125） | **~265**（核心 ~140 + 测试 ~125） |

**两档合计 ~620 行**（核心 ~370 + 测试 ~250）。对这个项目偏大（多数档 150–400 行），故拆 2 档。

> 注：v24.1 与 v15 压缩深度耦合（压缩点回调 store + 联动 memory bank 轮换）。v24.0 不碰压缩、可独立交付。

---

## 8. 落地后要同步的文档（system-roadmap.md §5 维护规则）

1. CLAUDE.md 进度表加 v24.0 / v24.1 两行；第 3 节"下一档候选"更新（v24 候选项移除，飞轮挪 v25）
2. `docs/decisions/v24.0.md`（store 形态 + 真 resume，决策 1/3/4/5/6/8）+ `docs/decisions/v24.1.md`（append-only 迁移 + 压缩链，决策 2/7）各建档 + decisions/README.md 索引加 2 行
3. **修正 system-roadmap.md V24 小节**：原句 "不做 SQLite —— v14 的 SQLite 已够用" 前提错误（nano v14 从无 SQLite）。把 roadmap 的 **V24 重定义为"会话持久化（SQLite 会话子系统）"**，**数据飞轮（trajectory + insights）顺延 V25**，并注明"v25 复用 v24 的 session_id 关联但独立存训练数据"
4. CLAUDE.md 验活 cheatsheet 测试清单加 `v24_0_session_store` / `v24_1_compaction_chain`；env 区加 `SESSION_DB_PATH`
5. `.gitignore` 加 `sessions/`（会话 db + WAL/SHM 不入库，与 builtin memory 落盘同理）
6. docs/todo.md 加 v24 块（真模型烟测 / 多窗口 last-write-wins 验证 / 可选 delete 命令 / 将来 FTS / v25 飞轮依赖本档 等待办）
7. `docs/Multi-agent-system/iteration-plan.md` 版本表：标注 v24=会话持久化、v25=数据飞轮（原 v23.5 嵌套 delegate 仍保留，归多智能体主轴）

---

## 9. 排序依据（为什么 v24 先于 v25 飞轮）

1. **修真 bug**：`/resume` 当前是假的，v24.0 即可独立修掉，符合 nano "每档解决一个具体痛点"的节奏
2. **v25 隐性前置**：v25 trajectory 计划挂在 `on_session_switch` flush，但当前 session 是 ephemeral 的，trajectory 文件会变孤儿 —— 先让 session 成为持久实体，v25 才有归属
3. **store 边界先立清**（决策 4）：避免 v25 想"顺手用 trajectory 兼做回放"造成耦合返工
4. **压缩链补齐 v15 的缺口**：v15 in-place 压缩当前把压缩前历史永久丢弃；v24.1 让它在磁盘上可回溯，是 v15 的自然续作（也因此与 v15 耦合，见 §7）

> 档内排序：v24.0（全量重写 + 真 resume，不碰压缩、低风险）先行并可独立上线；v24.1（append-only + 压缩链，耦合 v15）后续。二者不可合并跳过 v24.0 直接 append-only —— 但 v24.0 单独交付已能消除"假 resume"这个用户可感知的 bug，是干净的里程碑切点。

