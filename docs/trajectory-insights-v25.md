# V25 — Trajectory + Insights（日志追踪 + 训练数据 / 数据飞轮）

> 主轴：**数据飞轮**。它是 v24 会话持久化的直接续作 —— v24 让 session 成为持久实体，v25 在这个前提上接住"对话 → 训练数据 + 统计视图"。
> 编号沿革：原 roadmap 把本档定为 **v24**，但实际做 v24 时发现"trajectory 落盘的前置是会话本身能落盘"，而 v14 标称的会话持久化是假的 —— 于是 v24 被会话持久化占用（拆 v24.0/v24.1），**数据飞轮整体后移 v25**（见 [system-roadmap.md §2.4 路线偏离记录](system-roadmap.md)）。
> **拆 2 档交付**（见 §0）：**v25.0** trajectory + redact（写路径 / 数据飞轮核心，含子独立轨迹 + 压缩点 flush，可独立上线）→ **v25.1** insights + 结构化日志（读路径 / 统计视图，读 v24 SQLite）。
> 源项目对照：[hermes-agent/agent/trajectory.py](https://github.com/qshf/hermes-agent/blob/main/agent/trajectory.py) + `agent/insights.py`（931 行）+ `agent/redact.py`（404 行）+ `hermes_logging.py`（390 行）+ `run_agent.py:4583-4750`（ShareGPT 转换）。
> 状态：**计划（未实现）**。落地后转 [docs/decisions/v25.0.md](decisions/) + [docs/decisions/v25.1.md](decisions/) 并补"实现日期"。

---

## 0. 拆档：v25.0 → v25.1

为什么拆：**写路径与读路径关注点正交**。trajectory（写训练数据）和 insights（读统计视图）在源项目里就是两个独立文件、两套数据通路 —— trajectory 写 jsonl，insights 读 SQLite，互不调用。把它们塞进一档会让"喂训练 vs 看报表"两条叙事杂糅。切割面天然干净，且 v25.0 能独立交付"数据飞轮起点"这个里程碑。

| 档 | 内容 | 行数 | 耦合 v23/v24.1? | 独立交付? |
|----|------|------|----------------|----------|
| **v25.0** | `agent/trajectory.py`（ShareGPT 转换 + jsonl 落盘）+ `agent/redact.py`（脱敏）+ 轮末/压缩点/子 agent 三个 flush 点。**含子独立轨迹 + 压缩前 flush** | ~330 | 是（hook v23 child_loop + v24.1 apply_compaction） | ✅ |
| **v25.1** | `agent/insights.py`（读 v24 SQLite 跨会话聚合 + 终端报表）+ `agent/logging.py`（结构化日志 + 滚动 + redact formatter）+ `/insights` `/trajectory` 命令 | ~290 | 否（只读 v24 store） | 依赖 v24（不依赖 v25.0） |

> **注意一个反直觉点**：v25.1（insights）**不依赖 v25.0**（trajectory），只依赖 **v24 的 SQLite store**。因为 insights 的数据源是 v24 的 `sessions`/`messages` 表，不是 trajectory 的 jsonl。理论上 v25.1 可以先于 v25.0 做。这里按"写在前、读在后"的自然叙事排 v25.0 → v25.1，但二者无硬依赖（决策 4 详述）。

> **每档决策日志各自建档**：`docs/decisions/v25.0.md`（trajectory 形态 + redact + 子轨迹 + 压缩 flush，引用决策 1/2/3/6/7）、`docs/decisions/v25.1.md`（insights 读 SQLite + 结构化日志，引用决策 4/5 + 为何 insights 读 store 而非 jsonl）。

---

## 1. 这一档解决什么

**痛点：前 24 档跑过的对话"用完即抛"，喂不了模型、看不了统计。**

- v24 让 messages 能 resume（**状态回放**），但存的是原始 OpenAI dict，**不是训练格式** —— 拿去 SFT 要先转 ShareGPT、要脱敏、要把 tool_call 拍平成可解析的 XML。这层转换 v24 没做也不该做（v24 决策 4 划清了边界）。
- 没有跨会话统计视图：累计 token / 估算成本 / tool 调用 top-N / 失败率 / 平均轮长全都看不到。
- 日志可能泄漏 API key（transport 每次调用都带 Authorization）。

**本档目标**：让每轮对话在被丢弃前，自动落成两份资产 —— 训练样本（v25.0）+ 可聚合的统计（v25.1，复用 v24 store）。

**纠一处文档错误**：[system-roadmap.md §3 V24 小节](system-roadmap.md) 的"简化掉的"写 "不做 SQLite 持久化（用 jsonl 文件存）—— v14 的 SQLite 已够用"。**前提已被 v24 推翻**：v14 从无 SQLite，而 v24 真建了 SQLite session store。所以 insights 的正确做法是**读 v24 的 SQLite**（与源项目 `insights.py` 一致），不是另存 jsonl。本档落地时一并修正该句，并把 roadmap 的 V24 小节标注为"已演变为会话持久化，数据飞轮顺延 V25"。

---

## 2. 源项目对照（生产级设计）

| 维度 | 源项目做法 | 文件:行号 |
|------|-----------|----------|
| trajectory 格式 | ShareGPT `{from, value}` 对：system / human / gpt / tool；tool_call 拍平成 `<tool_call>` XML、reasoning 包 `<think>`、tool 结果包 `<tool_response>` | `run_agent.py:4583-4750` |
| trajectory 落盘 | 成功进 `trajectory_samples.jsonl`，残缺（`<think>` 没闭合）进 `failed_trajectories.jsonl`；外层带 `timestamp/model/completed` | `agent/trajectory.py:30-56` |
| trajectory 时机 | **loop 末**存最终态（[run_agent.py:14996](https://github.com/qshf/hermes-agent/blob/main/run_agent.py#L14996)）—— **只存压缩后**，压缩前历史丢（nano 决策 7 超越：压缩点 flush） | 同上 |
| 子 agent | delegate spawn 子时 `child = AIAgent(...)` **不传** `save_trajectories` → 吃默认 `False`（`run_agent.py:1066`）→ **子的中间步直接丢弃**（nano 决策 6 超越：子独立成文件） | `delegate_tool.py:1090`（child 构造）+ `run_agent.py:1066`（默认值） |
| redact | ~35 条前缀（`sk-*`/`ghp_*`/`AKIA*`/`eyJ*` JWT…）+ ENV 赋值 + JSON 字段 + Bearer 头 + 私钥块 + DB 连接串；短 token(<18) 全掩、长 token 留首 6 末 4 | `agent/redact.py:70-242` |
| insights 数据源 | **读 SQLite `sessions`/`messages` 表**（不是读 trajectory jsonl）；按时间窗聚合 | `agent/insights.py:180-205` |
| insights 报表 | overview（token/cost/duration）+ model/platform/tool/skill breakdown + activity 模式 + top sessions；`format_terminal()` 画框线柱状图 | `agent/insights.py:411-864` |
| 结构化日志 | `agent.log`/`errors.log`，`[session_id]` 注入每条 record，RotatingFileHandler，RedactingFormatter 挂所有 handler | `hermes_logging.py:46-259` |

---

## 3. 关键设计决策（nano 简化版）

### 决策 1：trajectory 是有损训练格式，与 v24 session-store 两个独立 store【v25.0】

这是本档**最重要的边界**，直接继承 v24 决策 4（v24 已为此预留）：

| | **v24 session-store** | **本档 v25 trajectory** |
|---|---|---|
| 形态 | OpenAI messages dict（SQLite 逐条入行） | ShareGPT `{from, value}` 对（jsonl 逐行一对话） |
| tool 调用 | `tool_calls[].function` + `tool_call_id` 关联完整 | 拍平成 `<tool_call>` XML 字符串，**丢 tool_call_id** |
| 密钥 | 原样（能 replay） | 过 redact 脱敏（**改了内容**，决策 3） |
| 用途 | **状态回放** — resume 真能续上 | **数据飞轮** — 喂 SFT，replay 会断 |
| 读写 | 读 + 写 | 只写 |

**为什么不合并**：trajectory 拿去 replay 会丢 `tool_call_id`（配不上 tool 结果）+ 密钥被脱敏（内容已变）—— 它只配训练，不配恢复。两者关注点正交，v24 决策 4 已把"状态恢复 vs 数据飞轮"分列。**v25.0 是 v24 决策 8 那段预留的兑现**。

### 决策 2：ShareGPT `from/value` 格式 + XML 包裹（对齐源项目）【v25.0】

**选**：复刻源项目 `_convert_to_trajectory_format`（[run_agent.py:4583-4750](https://github.com/qshf/hermes-agent/blob/main/run_agent.py)）的 ShareGPT 转换：

- `{from: "system", value: <工具列表 + 首个 user query 前的系统段>}`
- `{from: "human", value: <user 消息>}`
- `{from: "gpt", value: "<think>...</think><tool_call>{name, args}</tool_call>"}` —— reasoning 包 `<think>`、tool_calls 拍平成 XML；**每个 gpt turn 保证有 `<think>` 块**（空也带，对齐源 4670-4673）
- `{from: "tool", value: "<tool_response>{结果}</tool_response>"}` —— 连续多条 tool 结果合并成一条

**为什么选 from/value 而非直接存 OpenAI messages**：ShareGPT 是 transformers SFT 的事实标准格式，直接喂 `trl`/`axolotl`。决策日志记一笔"为何不存 messages 格式"（答：messages 格式训练前还要转，trajectory 的职责就是产出可直接训练的样本）。

**残缺样本分流**：`<think>` 开标无闭合（`has_incomplete_scratchpad`）→ 写 `failed_trajectories.jsonl` 而非 `samples`，避免污染训练集（对齐源 `trajectory.py:23-27`）。

### 决策 3：redact 脱敏 —— transport 层 + logger 层双挂钩【v25.0】

**选**：`agent/redact.py`，nano 取源项目 ~35 条 pattern 里**最常见的 ~10 条**：`sk-*` / `ghp_*` / `AKIA*`（AWS）/ `eyJ*`（JWT）/ `Bearer <token>`（Auth 头）/ `KEY=value`（ENV）/ 私钥块 / DB 连接串 userinfo。掩码策略对齐源：短 token(<18 字符)全掩 `***`，长 token 留首 6 末 4（`sk-pro...7890`）。

**挂两处**：
- **trajectory 写盘前**：每条 value 过 `redact` —— 训练样本不带密钥（核心，`grep "sk-[a-z0-9]\{40\}"` 应零命中）
- **logger formatter**（v25.1）：`RedactingFormatter` 挂所有 file handler —— 日志文件不落密钥

**默认开 + import 时快照**：`NANO_REDACT_SECRETS=0` 才关；快照在 import 时取（防运行期被改关，对齐源 `redact.py:67`）。决策日志记"为何 import 时快照而非运行期读 env"。

### 决策 4：insights 读 v24 SQLite，不读 trajectory jsonl【v25.1】

**选**：`agent/insights.py` 的数据源是 **v24 的 `sessions`/`messages` 表**（对齐源 `insights.py:180-205`），**不是** trajectory jsonl。

**为什么**（纠正原 roadmap 的错误前提）：
- 原 roadmap V24 小节写"用 jsonl 存统计 —— v14 SQLite 已够用"，前提是"v14 有 SQLite"。**v24 推翻了这个前提**（v14 从无 SQLite，v24 才真建）。现在 v24 store 有现成的 `input_tokens/output_tokens/cache_read/cache_write`（4 列）+ `turn_count`/`model`/`created_at`/`updated_at`/`ended_at`/`end_reason`/`parent_session_id` —— insights 要的字段**全在 v24 表里**。
- jsonl 是有损训练格式（脱敏 + 丢 tool_call_id），拿它算 token/成本会因脱敏 + 格式转换失真。SQLite 存的是原始计量。
- 关注点正交：trajectory 写训练、insights 读计量，各取所需。

**因此 v25.1 只依赖 v24，不依赖 v25.0**（§0 已注）。insights 可在没有 trajectory 的情况下独立工作。

**报表内容**（nano 裁剪版）：overview（累计 4 维 token / 估算成本 / 会话数 / 平均轮长）+ tool 调用 top-N（从 `messages.tool_calls` JSON 解析）+ per-model breakdown + 失败率。`format_terminal()` 输出。砍掉源项目的 platform breakdown（nano 只有 CLI）/ skill breakdown / activity 模式 / top sessions（留 todo）。

### 决策 5：成本估算 hardcode 当前两家价格，不做多家 pricing 表【v25.1】

**选**：`estimate_cost(model, tokens)` 内嵌 deepseek + qwen 当前单价（input/output/cache_read 三档），按 4 维 token 算。源项目的多家 pricing 表 + pricing_version + actual_cost 对账归 v26+。决策日志记"价格会过时，是已知简化；真要准确对账再开档"。

### 决策 6：子 agent 独立 trajectory + 父记 child_trajectory_path —— 超越源项目【v25.0】

**源项目反而不做**：delegate spawn 子时构造 `child = AIAgent(...)`（[delegate_tool.py:1090](https://github.com/qshf/hermes-agent/blob/main/tools/delegate_tool.py#L1090)）**不传** `save_trajectories`，吃 `AIAgent.__init__` 默认 `save_trajectories=False`（[run_agent.py:1066](https://github.com/qshf/hermes-agent/blob/main/run_agent.py#L1066)）—— **子的中间步直接丢弃**，父轨迹里子只剩一句 delegate summary。

> **不要引 `batch_runner.py:331` 当证据**（前一版本误引）：那行确实有 `save_trajectories=False`，但紧接着 [batch_runner.py:357](https://github.com/qshf/hermes-agent/blob/main/batch_runner.py#L357) 调 `_convert_to_trajectory_format(result["messages"], ...)` 把完整轨迹捕获进 batch 输出 —— 它恰恰是"捕获了完整轨迹"的反例。delegate 路径才是真丢中间步的地方。

**nano 选择超越源项目**：每个子 agent 独立落一个 trajectory 文件，父轨迹的 delegate tool_response 里多记一个 `child_trajectory_path` 字段串联。

**为什么超越**（三条理由）：
1. **训练价值密度最高的恰恰是子的 tool-use 链** —— "list_dir → read_file×8 → 算出答案"这种完整多步工具推理是 SFT 最想要的样本，塞回父轨迹只剩一句 summary 就压没了。
2. **隔离边界天然对齐**：v23.0 已让子 agent 跑独立 messages、不继承父历史（[agent/child_loop.py](../agent/child_loop.py)）。子轨迹独立成文件 = 把这个隔离一路贯彻到落盘层，不用在父轨迹里硬塞嵌套结构。
3. **v24 决策 8 白纸黑字预留了**："子 agent 内部原始 messages 从不进父 messages（v23.0 隔离），归 v25 trajectory（每个子独立 trajectory 文件 + 父记 `child_trajectory_path`）"。选独立 = 兑现承诺；不选 = v24 那段设计落空、回头看 v24 像过度设计。

**hook 点**：[tools/delegate_tool.py:435-458](../tools/delegate_tool.py#L435)（子 result 构建处，`_accumulate_runtime_tokens` 之后）—— 拿到子的最终 messages，转 ShareGPT 落 `trajectories/<session>-child-<id>.jsonl`，把路径塞进返回父的结构化结果。子 token 已由 v23.4 聚合进 `runtime.session_tokens`，trajectory 只额外落"步"。

> **前置改动（必做，否则实现卡死）**：当前 child loop 出口 [`_build_result`](../agent/child_loop.py#L129) 返回的 dict **只有** `summary/exit_reason/iterations/tokens/tool_trace/duration_seconds`，**没有 `messages`**；`_run_single_child`（[delegate_tool.py:447-458](../tools/delegate_tool.py#L447)）拿到的 `child_result` 同样没有。ShareGPT 转换需要完整 messages（human/gpt/tool 四类），光靠 `tool_trace`（轻量摘要：`tool/args_preview/result_bytes/status`）**转不出合格训练样本**。所以 v25.0 必须先给 `_build_result` 加一个 `messages` 字段（7 处 return 路径共用同一构造函数，改一处即可 —— child_loop.py:138 注释已说明这点）。这是决策 6 的**隐藏前置成本**：+1 个返回字段，否则 hook 点拿不到数据。

**代价（诚实标注）**：+耦合 v23 的 child_loop 出口（`_build_result` 多带 `messages` 字段，见上方前置改动）+ 父结构化结果多一个 `child_trajectory_path` 关联字段，约 +50 行。是 nano 相对源项目的教学增量，不是复刻 —— 决策日志须写明"源项目丢，nano 接，理由是数据飞轮要在数据被丢弃前接住它"。

### 决策 7：压缩点 flush 压缩前轨迹 —— 超越源项目【v25.0】

**源项目只在 loop 末存压缩后最终态**（[run_agent.py:14996](https://github.com/qshf/hermes-agent/blob/main/run_agent.py#L14996)），**压缩前的多轮原始对话不进 trajectory**（它另有离线 `trajectory_compressor.py` 做批处理摘要，但不是 live 捕获）。

**nano 选择超越**：在 v24.1 的会话分裂点 flush 压缩前完整对话进 trajectory，避免训练样本随 in-place 压缩蒸发。

**hook 点**：[agent/compaction.py:62-66](../agent/compaction.py#L62)，紧挨 v24.1 已有的 `store.append(old_sid, ...)`（压缩前全文落 SQLite 那一步）—— **同一时机同一份 `ctx.messages`，顺手转 ShareGPT 落 trajectory**。这是个干净的复用：v24.1 已经把"压缩前全文"在这里抓出来落了 session-store，v25.0 只是在同一行旁边多落一份训练格式。

**为什么这是 v24.1 的自然续作**：v24.1 决策 7 让压缩前历史在 SQLite 可回溯（状态层），v25.0 让它在 trajectory 可训练（数据层）—— 同一份数据的两个去向，正是决策 1/4 那条"状态 vs 训练"边界在压缩点的具体落地。

**代价**：+耦合 v24.1/v15，约 +30 行。决策日志记"源项目 loop 末存丢压缩前，nano 在分裂点接住"。

---

## 4. 最小可教学切片

> **档归属**：🟢 = v25.0（trajectory + redact，写路径）；🔵 = v25.1（insights + 日志，读路径）。

### A. trajectory 核心 `agent/trajectory.py`（🟢 v25.0 ~120 行）

```python
def to_sharegpt(messages, *, system_value) -> list[dict]:
    """OpenAI messages → ShareGPT from/value 对。
    reasoning→<think>、tool_calls→<tool_call> XML、连续 tool 结果合并成 <tool_response>。
    每个 gpt turn 保证有 <think>（空也带）。"""

def has_incomplete_think(convs) -> bool:
    """<think> 开标无闭合 → 残缺，分流到 failed。"""

def save_trajectory(convs, *, model, completed, out_dir) -> Path:
    """外层包 {conversations, timestamp, model, completed}；
    completed 且不残缺 → samples.jsonl，否则 failed_trajectories.jsonl。
    写盘前每条 value 过 redact（决策 3）。"""
```
要点：`timestamp` 不能用 `time.time()` 直接拼进决定性测试（测试传固定值）；落盘目录 `trajectories/`（env `TRAJECTORY_DIR` 覆盖，`:none:` 关闭 → 退化到不落盘）。

> **sentinel 约定区分**：`TRAJECTORY_DIR=:none:` 是 nano 自创 sentinel，与已有的 `SESSION_DB_PATH=:memory:`（[session_store.py:48](../agent/session_store.py#L48)）**不同语义** —— `:memory:` 是"落到内存库"（SQLite 原生语义，数据仍在、进程退出才丢），`:none:` 是"完全不落盘"（trajectory 没有内存退化态，关了就是不写）。§8 文档同步须在 env 区点明这层区分，避免与 `:memory:` 混淆。

### B. redact `agent/redact.py`（🟢 v25.0 ~90 行）

```python
_PATTERNS = [...]   # ~10 条：sk-* / ghp_* / AKIA* / eyJ* / Bearer / KEY=val / 私钥块 / DB userinfo
_ENABLED = os.environ.get("NANO_REDACT_SECRETS", "1") != "0"   # import 时快照（决策 3）

def mask(token) -> str:   # 短(<18)全掩 ***；长留首6末4
def redact(text) -> str:  # 遍历 pattern 替换；_ENABLED=False 时原样返回
```

### C. 三个 flush 点接入（🟢 v25.0 ~70 行）

1. **退出兜底**（[main.py:833](../main.py#L833) `finally` 里 `session_store.append` 之后）：把整段会话 messages 转 ShareGPT 落一份 trajectory。**定调：轮末不单独落**（[main.py:820](../main.py#L820) 轮末 append 处不挂 trajectory）—— ShareGPT 是"一行一个完整对话"（决策 2），轮末增量 append 会产生半截对话碎片，与该语义冲突。trajectory 的自然粒度是"整段 flush"：要么压缩点 flush 被分裂走的那段（点 2），要么退出时 flush 最终段（本点），对齐源项目"loop 末存最终态"。
2. **压缩点**（[agent/compaction.py:62-66](../agent/compaction.py#L62)，决策 7）：紧挨 `store.append(old_sid,...)` 落压缩前全文 trajectory。
3. **子 agent**（[tools/delegate_tool.py:435-458](../tools/delegate_tool.py#L435)，决策 6）：子 result 构建处落 `<session>-child-<id>.jsonl`，路径塞回父结构化结果。

### D. insights `agent/insights.py`（🔵 v25.1 ~160 行）

```python
class InsightsEngine:
    def __init__(self, store):   # 直接吃 v24 SessionStore（决策 4）
    def generate(self, *, days=30) -> dict:
        """读 sessions 表（时间窗过滤）+ messages 表（tool_calls JSON 解析）。
        overview（4 维 token / estimate_cost / 会话数 / 平均轮长）+ tool top-N + per-model + 失败率。"""
    def format_terminal(self, report) -> str:   # 框线 + 柱状，抄 /memory 列表风格
```
读现成列：`input_tokens/output_tokens/cache_read_tokens/cache_write_tokens`（[session_store.py:52](../agent/session_store.py#L52) `_TOKEN_COLUMNS`）+ `turn_count/model/created_at/updated_at/end_reason`。

### E. 结构化日志 `agent/logging.py`（🔵 v25.1 ~80 行）

`setup_logging()`：`[session_id]` 注入每条 record（thread-local）+ RotatingFileHandler（`agent.log`，5MB×3）+ `RedactingFormatter`（复用 D 的 `redact`，挂所有 handler）。`LOG_LEVEL` env 控制。

### F. slash 命令（🔵 v25.1 ~30 行）

- `/insights [--days 30]`：`InsightsEngine(ctx.session_store).generate()` → `format_terminal`。
- `/trajectory list`：列 `trajectories/` 下文件（session / 条数 / completed）。
- 可选 `/trajectory show <id>`：扩展位，本档可不做（todo）。

### G. 测试（🟢 `scripts/test_v25_0_trajectory.py` ~10 项 + 🔵 `scripts/test_v25_1_insights.py` ~6 项）

🟢 v25.0（沿用 v24 测试范式：直接 `assert` + `:memory:` / tmp 目录，不依赖 pytest）：
1. `to_sharegpt`：system/human/gpt/tool 四类 round-trip；tool_calls 拍平成 `<tool_call>` XML
2. 每个 gpt turn 有 `<think>`（空也带）
3. 连续多条 tool 结果合并成一条 `<tool_response>`
4. reasoning_content → `<think>` 包裹
5. `has_incomplete_think` 检出残缺 → 落 `failed_trajectories.jsonl`
6. **redact 核心**：含 `sk-`+40 位 / JWT / Bearer 的 value 落盘后 `grep` 零命中
7. redact 短 token 全掩、长 token 留首 6 末 4
8. `NANO_REDACT_SECRETS=0` 关闭后原样（验证 import 快照行为）
9. **子轨迹**（决策 6）：子 result 落独立文件，父结构化结果含 `child_trajectory_path`
10. **压缩点 flush**（决策 7）：apply_compaction 后，压缩前 N 条原始对话在 trajectory 可读

🔵 v25.1（6 项，读 v24 store）：
11. `generate` 读 v24 store：4 维 token 累计正确
12. tool 调用 top-N 从 `messages.tool_calls` JSON 解析正确
13. `estimate_cost` 按 model 选价、4 维算账正确
14. 时间窗 `days` 过滤：窗外 session 不计
15. **insights 不依赖 trajectory**（决策 4）：没有任何 jsonl 时 `/insights` 仍出报表
16. redact formatter：日志行含密钥 → 文件里被掩

---

## 5. 验证

🟢 **v25.0**：
- trajectory + redact 单测全过（10 项）
- 真跑：聊 5 轮 → `trajectories/` 出现样本，`cat *.jsonl | python -c "import json,sys;[json.loads(l) for l in sys.stdin]"` 解析通过，格式可直接喂 transformers SFT
- `grep -rE "sk-[a-zA-Z0-9]{20,}" trajectories/` 零命中
- **子轨迹真跑**（决策 6）：delegate 1 个子 → 子独立 jsonl 含其完整 tool-use 链，父 jsonl 的 delegate 结果含 `child_trajectory_path`
- **压缩点真跑**（决策 7）：调小 `CONTEXT_WINDOW` 逼出压缩 → 压缩前多轮在 trajectory 留存（不随 in-place 压缩蒸发）
- 关闭（`TRAJECTORY_DIR=:none:`）→ 退化到不落盘，旧测零回归
- 跨档零回归：v23_*（delegate 改动重点验）/ v24_*（compaction 改动重点验）全过

🔵 **v25.1**：
- insights + 日志单测全过（6 项）
- 真跑：聊几轮 → `/insights --days 7` 显示总 4 维 token / 估算成本 / tool top-N / 失败率
- `/trajectory list` 列出 v25.0 落的样本
- **insights 独立性**（决策 4）：删光 `trajectories/` 后 `/insights` 仍正常（证明读的是 SQLite 不是 jsonl）
- 日志文件 `agent.log` 含 `[session_id]` 前缀且无密钥
- 跨档零回归：v24_*（store 被 insights 读，重点验不破坏）全过

---

## 6. 简化掉的（vs 源项目）

- **trajectory**（vs 57 行 `trajectory.py` + `run_agent.py:4583-4750`）：不做多模态 base64 图剥离（nano 无图）/ 不做离线 `trajectory_compressor` 批处理摘要
- **insights**（vs 931 行）：不做 platform breakdown（nano 只 CLI）/ skill breakdown / activity 模式（day/hour/streak）/ top sessions / gateway markdown 输出 —— 只做 overview + tool top-N + per-model + 失败率
- **redact**（vs 404 行 ~35 条 pattern）：取 ~10 条最常见；不做 Telegram/Discord/phone/URL query param 脱敏
- **成本**（决策 5）：hardcode deepseek+qwen 单价，不做多家 pricing 表 / pricing_version / actual_cost 对账
- **日志**（vs 390 行）：不做 gateway.log 组件路由 / NixOS chmod / 多 handler 分级 —— 单 `agent.log` + RedactingFormatter

---

## 7. 预估规模（按档拆）

| 部分 | v25.0 | v25.1 |
|------|-------|-------|
| `agent/trajectory.py` | ~120 | — |
| `agent/redact.py` | ~90 | （v25.1 复用） |
| 三个 flush 点接入（main / compaction / delegate） | ~70 | — |
| `agent/insights.py` | — | ~160 |
| `agent/logging.py` | — | ~80 |
| slash 命令（`/insights` `/trajectory`） | — | ~30 |
| 测试 | ~120（`test_v25_0_trajectory.py` 10 项） | ~120（`test_v25_1_insights.py` 6 项） |
| **小计** | **~400**（核心 ~280 + 测试 ~120） | **~390**（核心 ~270 + 测试 ~120） |

**两档合计 ~790 行**（核心 ~550 + 测试 ~240）。roadmap 原估核心 500-700 行，吻合偏上（因 nano 选了超越源项目的子轨迹 + 压缩 flush，+~80 行）。

---

## 8. 落地后要同步的文档

1. CLAUDE.md 进度表加 v25.0 / v25.1 两行；第 3 节"下一档候选"更新（飞轮项移除）
2. `docs/decisions/v25.0.md`（trajectory + redact + 子轨迹 + 压缩 flush，决策 1/2/3/6/7）+ `docs/decisions/v25.1.md`（insights 读 SQLite + 日志，决策 4/5）各建档 + decisions/README.md 索引加 2 行
3. **修正 system-roadmap.md**：§3 V24 小节"不做 SQLite（用 jsonl）—— v14 SQLite 已够用"前提已被 v24 推翻 —— 标注"已演变为会话持久化，insights 改读 v24 SQLite，数据飞轮顺延 V25"；§2.4 路线图把 v25 从"Mixture-of-Agents"改为"Trajectory + Insights"，MoA 顺延 v26
4. CLAUDE.md 验活 cheatsheet 测试清单加 `v25_0_trajectory` / `v25_1_insights`；env 区加 `TRAJECTORY_DIR` / `NANO_REDACT_SECRETS` / `LOG_LEVEL`
5. `.gitignore` 加 `trajectories/` + `*.log`（训练样本 + 日志不入库）
6. docs/todo.md 加 v25 块（真模型烟测 / `/trajectory show` / insights 加 activity 模式 / 成本 pricing 表过时风险 / redact pattern 扩充 等待办）
7. `docs/Multi-agent-system/iteration-plan.md` 版本表：标注 v25=数据飞轮（trajectory + insights）

---

## 9. 排序依据（为什么 v25 在 v24 之后）

1. **依赖顺序**：trajectory 落盘的前置是 session 是持久实体（v24 已立）；insights 的数据源是 v24 的 SQLite 表 —— 两件都建在 v24 上，v24 是 v25 的硬前置（v24 决策 4/8 已预留边界）。
2. **收集层先完整再做飞轮**：必须先有 v22 流式（capture 增量帧）+ v23 多 agent（子轨迹）+ v24 持久化（数据源），收集层才完整。先于它们做会回炉改格式。
3. **数据飞轮起点**：做完后 nano 每次实验自动落训练样本 + 统计，v26+ 的迭代都有数据。
4. **补 v15/v23 的数据缺口**：v15 in-place 压缩丢压缩前历史、v23 子 agent 丢中间步 —— v25 在数据层把这两处接住（决策 6/7），是它俩的自然续作。
