# v26.4 代码级语音心跳 —— 知识点

## 一、背景

v26.3 用 `inject_directive` 把「必须主动语音播报」的纪律注入 system prompt 常驻段，让 agent 每轮都看得到。实测 agent 确实会主动播报了——起手一句 `info`、完成一句 `done`。

但 directive 里最初还有一条指令：

> 维持约 20 秒一次的语音心跳，agent 在长时间 silence 时主动播"还在处理"

这条指令模型**物理上做不到**。不是因为措辞弱，而是有三个结构性的死穴：

```
死穴 1: terminal 阻塞期模型不能动
  tools/terminal_tool.py → subprocess.run(timeout=30)
  主线程卡在 subprocess 里，模型想插播但此时根本不在执行
  而这恰恰是"最像挂机"的时刻——用户看到几十秒死寂

死穴 2: 模型生成 token 期间也不能插播
  模型输出长文本需要时间，这期间也不能分心调工具

死穴 3: 模型只在轮与轮之间有机会动作，且经常忘
  "定时"变得不可靠——有时 20s 播一次，有时 80s 没动静
```

结论：**让模型负责「定时报活」是把任务派给了它最不擅长、甚至物理上做不到的环节。**

v26.4 把纯「存活心跳」从模型手里拿走，交给 agent 主进程的一个后台守护线程。它独立于模型、独立于 tool loop，在主线程被 `subprocess.run` 卡住的那几十秒里照常存活、照常报活。

---

## 二、职责重划

| 谁 | 负责什么 | 为什么 |
|----|---------|--------|
| **模型保留** | 有语义的播报：起手 `info`、阶段切换 `progress`、风险 `warning`、完成 `done` | 需要理解任务才能产出，只能模型做 |
| **代码接管** | 纯「我还在，别以为挂了」的存活心跳 | 机械、无内容、且能在阻塞期触发 |

模型只背它背得动的部分。那条做不到的「定时报活」现在是代码层的保证。

---

## 三、VoiceHeartbeat 类

> 📂 [`agent/voice_heartbeat.py`](../../../agent/voice_heartbeat.py)（新文件，225 行）

### 3.1 整体设计

一个独立于 `AgentRuntime` 的小对象，持有自己的 `last_beat` / `stop_event` / `VoiceClient` 句柄 / 阈值。它从 agent 主进程起一个 `daemon=True` 的后台线程，每 1 秒醒来检查：agent 在忙且沉默够久了 → 推一条存活语音。

为什么**不进 AgentRuntime**：`AgentRuntime` 是父子（delegate）共享的真理源，承载 `cancel_token` / `session_tokens`。心跳是父主进程独有、与子 agent 无关，放进去会污染语义。

### 3.2 构造参数

> 📂 [`agent/voice_heartbeat.py:107-125`](../../../agent/voice_heartbeat.py#L107)

```python
class VoiceHeartbeat:
    def __init__(self, agent_busy: dict, *, client, threshold_seconds):
        self._agent_busy = agent_busy       # main.py 的 {"flag": bool} 字典引用
        self._client = client               # VoiceClient 实例
        self._threshold = threshold_seconds # 沉默多久后补播（默认 25s）
        self._stop_event = threading.Event()
        self._last_beat = time.monotonic()  # 上次心跳时间戳
        self._warned_send_failure = False   # 发送失败只告警一次
```

几个关键设计意图：

- **`agent_busy` 是引用不是拷贝**。main.py 和心跳线程共享同一个字典。main loop 里 `agent_busy["flag"] = True/False`，心跳线程读取——Python dict 的 bool 读写原子，不需要锁。
- **`_last_beat` 心跳线程自己写、main loop 轮始 `mark_active()` 重置**。两个线程可能同时碰它，竞争窗口最坏后果是多播或少播一条——无害，比引入锁的复杂度划算得多。
- **`_warned_send_failure`**。Runtime 中途挂了只记一条 warning，不会每秒刷一条错误日志。

### 3.3 构造门控：三层防线

> 📂 [`agent/voice_heartbeat.py:128-168`](../../../agent/voice_heartbeat.py#L128)

```python
@classmethod
def create_if_available(cls, agent_busy, *, client=None,
                         threshold_seconds=None, probe_health=True):
    """按可用性门控构造 —— 不可用时返回 None（不抛异常）。

    门控顺序（任一不过即返回 None）：
    1. 阈值 <= 0 → 用户显式关闭
    2. client 不可用 → try-import VoiceClient 失败
    3. probe_health 时 client.health() 不通 → Runtime 没起
    """
    threshold = (threshold_seconds if threshold_seconds is not None
                 else _resolve_threshold())
    if threshold <= 0:                              # ← 防线 1
        return None

    if client is None:
        client = _make_default_client()             # ← try-import
    if client is None:                              # ← 防线 2
        return None

    if probe_health:
        try:
            client.health()                         # ← /health 探活
        except Exception:                           # ← 防线 3
            return None

    return cls(agent_busy, client=client, threshold_seconds=threshold)
```

三层各解决不同场景：

```
场景 A：VOICE_HEARTBEAT_SECONDS=0
  → 防线 1 触发 → 返回 None，心跳关闭

场景 B：没装 nano_voice_kit 包
  → _make_default_client() try-import 失败 → 返回 None
  → agent 正常干活，只是没心跳

场景 C：装了包但 Runtime 进程挂了
  → client.health() 抛异常 → 返回 None
  → 不启线程去撞一堵墙

场景 D：全通
  → 返回 VoiceHeartbeat 实例 → start() 起线程
```

**注意**：这之前 main.py 还有一层门控——voice-runtime skill 的 `is_fully_available()`（DASHSCOPE_API_KEY 已设 + terminal 工具在）要先通过。`VoiceHeartbeat` 自身不管 skill 元信息，只管「客户端链路上不上得通」。

### 3.4 触发判定：静默超时才播

> 📂 [`agent/voice_heartbeat.py:199-203`](../../../agent/voice_heartbeat.py#L199)

```python
def _should_beat(self, now: float) -> bool:
    """三条件全满足才补播：busy + 超过阈值。"""
    if not self._agent_busy.get("flag"):
        return False                           # agent 没在忙 → 不播
    return (now - self._last_beat) >= self._threshold  # 沉默够久了 → 播
```

两个条件必须**同时满足**：

- agent 空闲时（用户还在打字 / agent 在等下一轮）不会莫名其妙播"还在处理"。
- 是「静默超时」而不是「固定间隔」。模型正常播报时，voice kit 的 `progress` 队列策略（新的 progress 替换还没播的旧 progress）自然吸收偶发重叠——用户听到模型的声音，心跳不触发。

### 3.5 守护线程主体

> 📂 [`agent/voice_heartbeat.py:215-224`](../../../agent/voice_heartbeat.py#L215)

```python
def _run(self) -> None:
    """守护循环：每 tick 醒来检查，满足条件就补播并刷新基准。"""
    while not self._stop_event.is_set():
        if self._stop_event.wait(_TICK_SECONDS):  # tick=1s，同时是退出信号
            break                                   # stop() 被调用 → 跳出
        now = time.monotonic()
        if self._should_beat(now):
            self._send_beat()
            self._last_beat = now                  # 刷新基准
```

`stop_event.wait(1.0)` 既是节拍器也是退出信号。`stop()` 被调用时，`wait` 立即返回 `True`，循环干净退出——不需要额外的 `time.sleep` + `flag` 检查。

线程是 `daemon=True`，即使 `stop()` 忘了调，进程退出时也被强制回收——不阻塞关机。

### 3.6 发送失败处理

> 📂 [`agent/voice_heartbeat.py:205-213`](../../../agent/voice_heartbeat.py#L205)

```python
def _send_beat(self) -> None:
    """推一条存活 progress；失败不冒泡（只首次记 warning）。"""
    try:
        self._client.speak_intent(_HEARTBEAT_INTENT, _HEARTBEAT_TEXT)
        self._warned_send_failure = False           # 成功 → 重置标记
    except Exception as exc:
        if not self._warned_send_failure:            # ← 只记一次
            logger.warning("voice heartbeat send failed: %r", exc)
            self._warned_send_failure = True
```

Runtime 中途挂了怎么办？第一次失败记 warning 告知用户，之后所有失败静默。Runtime 恢复后下一次成功自动重置标记。全程不冒泡进 main loop。

### 3.7 生命周期方法

```python
def start(self) -> None:
    """起后台守护线程（幂等：已起则忽略）。"""
    if self._thread is not None and self._thread.is_alive():
        return
    self._stop_event.clear()
    self._last_beat = time.monotonic()
    self._thread = threading.Thread(
        target=self._run, name="voice-heartbeat", daemon=True
    )
    self._thread.start()

def mark_active(self) -> None:
    """main loop 轮始调用：刷新基准时间戳。"""
    self._last_beat = time.monotonic()

def stop(self, join_timeout: float = 2.0) -> None:
    """通知线程退出并 join（幂等）。"""
    self._stop_event.set()
    if self._thread is not None:
        self._thread.join(timeout=join_timeout)
        self._thread = None
```

`mark_active()` 的必要性：刚进入新一轮 tool loop（`agent_busy=True` 刚设），上一轮结束到本轮开始之间可能已经过了好几秒。如果不刷新 `last_beat`，可能刚进 loop 就触发心跳——用户会听到一句来路不明的"还在处理"。`mark_active()` 让沉默计时从**此刻**重新开始。

---

## 四、main.py 集成

> 📂 [`main.py`](../../../main.py)

整个集成只动了三个位置，最小侵入。

### 4.1 启动时：两层门控 + 构造

```python
# V26.4: 代码级语音心跳
# 门控两层：
#   1. skill 层：voice-runtime 必须 is_fully_available
#   2. 链路层：VoiceHeartbeat.create_if_available 再探 VoiceClient + /health
voice_heartbeat = None
try:
    _vmeta = skill_loader.get("voice-runtime")
    _voice_tools = sorted(
        set(get_available_tool_names(ENABLED_TOOLSETS))
        | set(memory_manager.get_all_tool_names())
    )
    if _vmeta is not None and _vmeta.is_fully_available(_voice_tools):
        voice_heartbeat = VoiceHeartbeat.create_if_available(agent_busy)
except Exception as exc:
    log.warning("voice heartbeat init skipped: %r", exc)
    voice_heartbeat = None
if voice_heartbeat is not None:
    voice_heartbeat.start()
    log.info("voice heartbeat on (threshold=%ss)", voice_heartbeat._threshold)
```

两层门控叠加：

```
skill 层：skill_loader.get("voice-runtime")
  └─ is_fully_available(available_tools)
       ├─ DASHSCOPE_API_KEY 设了？
       └─ terminal 工具在？
            └─ ✅ → 进链路层

链路层：VoiceHeartbeat.create_if_available(agent_busy)
  ├─ threshold > 0？
  ├─ VoiceClient import 成功？
  └─ Runtime /health 通？
       └─ ✅ → start() 起线程
```

任一层不过 → `voice_heartbeat = None`，后续所有代码看到 None 就跳过。全程 `try` 包裹——任何异常都不阻 agent 启动。

### 4.2 每轮 tool loop：刷新沉默基准

```python
agent_busy["flag"] = True
# V26.4: 轮始刷新心跳基准
if voice_heartbeat is not None:
    voice_heartbeat.mark_active()
```

### 4.3 退出时：停止线程

```python
# V26.4: 停心跳守护线程
if voice_heartbeat is not None:
    voice_heartbeat.stop()
```

虽然 daemon 线程即便不 `stop` 进程退出也会回收，但显式 `stop` 让 Ctrl+D 退出更干净——不在退出瞬间多吐一条心跳。

---

## 五、完整运行时序

以「用户说 *帮我跑 npm install*」为例，假设安装耗时 55 秒：

```
T0       agent 收到任务
         ├─ 播 info "开始安装依赖"
         ├─ agent_busy["flag"] = True
         ├─ mark_active()                  ← last_beat = T0
         └─ 调用 terminal: npm install     ← 主线程阻塞在这！！！

T0+1s    后台线程醒来 → busy=True 但才过 1s < 25s → 不播
T0+2s    后台线程醒来 → 2s < 25s → 不播
...      （每秒醒来，都不满足）
T0+25s   后台线程醒来 → busy=True 且过 25s >= 25s
         → speak_intent("progress", "还在处理，稍等")
         → last_beat = T0+25s              ← 用户听到第一声心跳

T0+26s   后台线程醒来 → 距上次心跳才 1s < 25s → 不播
...      
T0+50s   后台线程醒来 → 距上次心跳过 25s >= 25s
         → 又播一条 "还在处理，稍等"       ← 用户听到第二声心跳
         → last_beat = T0+50s

T0+55s   npm install 完成，主线程恢复
         ├─ agent 拿到工具返回值
         ├─ 播 done "安装完成"
         └─ agent_busy["flag"] = False     ← 心跳线程此后不再触发
```

用户全程听到的：*"开始安装依赖" → 25 秒沉默 → "还在处理，稍等" → 25 秒 → "还在处理，稍等" → "安装完成"*。

如果**不加 v26.4**，用户听到的：*"开始安装依赖" → 55 秒死寂 → "安装完成"*。中间那 55 秒大概率以为 agent 挂了然后 Ctrl+C。

---

## 六、SKILL.md directive 瘦身

> 📂 [`skills/voice-runtime/SKILL.md`](../../../skills/voice-runtime/SKILL.md)

v26.4 对 directive 做了一处减法：删掉模型做不到的那条。

### 删掉的内容（v26.3 初版）

```
维持约 20 秒一次的语音心跳，agent 在长时间 silence 时
主动播一小段语音（如"还在处理"）以示存活。
```

### 替换为（v26.4）

```
存活心跳由 Runtime 自动维持（v26.4）：长命令阻塞、多步推进期间，宿主后台会自动
播"还在处理"的存活进度，你**不需要**自己定时播心跳。你只负责在**状态真正变化**时播报。
```

同时在正文「何时播报」一节补充：

```markdown
> **存活心跳由 Runtime 自动维持（v26.4）。** 你**不需要**自己定时播"我还在"那类
> 心跳——长命令阻塞、多步推进期间，agent 宿主的后台守护线程会直连 Runtime 自动播
> 存活进度（沉默约 25 秒触发，env `VOICE_HEARTBEAT_SECONDS` 可调）。这恰好补上了你
> 物理上做不到的环节：工具阻塞期 / 生成 token 期你无法插播。你只管在**状态真正变化**
> 时播有语义的 `info`/`progress`/`warning`/`done`。
```

---

## 七、关键设计决策

| 决策 | 选项 | 没选 | 原因 |
|------|------|------|------|
| 心跳触发者 | 代码层后台线程 | 模型自觉（prompt 软指令） | 模型在 terminal 阻塞期、token 生成期物理上不能动 |
| 连接方式 | `VoiceClient` httpx 直连 | subprocess 调 CLI | 心跳每 tick 都可能触发，fork 进程太重；httpx 请求开销极小 |
| 触发策略 | 静默超时才播 | 固定间隔硬播 | 固定间隔会跟模型自己的播报打架；静默超时意味着"模型在说话 → 不算沉默 → 自然不触发" |
| 状态归属 | 独立 `VoiceHeartbeat` 对象 | 放 `AgentRuntime` | AgentRuntime 是父子共享的；心跳父进程独有，放进去污染语义 |
| 依赖方式 | 软依赖 try-import | 硬依赖 import | 没装 voice kit 的环境主任务零影响，只是没心跳 |
| 线程安全 | 不持锁 | `threading.Lock` 保护 `last_beat` | bool 原子读写；`last_beat` 竞争窗口无害（最坏多/少播一拍），比引入锁的复杂度 + 死锁风险划算 |
| directive | 瘦身（删心跳段） | 保留（让模型继续试） | 把做不到的硬要求从模型肩上卸掉；模型只背有语义播报，遵守率反而回升 |
| 存活文案 | 固定"还在处理，稍等" | 动态读最近 tool 名 | 动态文案要心跳线程看到 tool loop 动作——增加耦合；固定文案足够消除"挂机"感知，动态是增量增强 |

---

## 八、线程安全分析

这里不持锁是一个有意识的取舍：

```
竞争窗口：
  心跳线程写 _last_beat（_send_beat 成功后 L224）
  main loop 读+写 _last_beat（mark_active L188）

同时发生的后果：
  - 心跳刚写完，main loop 立刻覆盖 → 多播一条心跳
  - main loop 刚写完，心跳立刻读旧值 → 少播一条心跳

影响：一条存活"还在处理，稍等"的 ±1 偏差
      用户体感完全不可感知
      远远小于加锁引入的复杂度（死锁风险、lock contention）
```

`agent_busy["flag"]` 的情况更简单——Python dict 的 bool 读写是 CPython GIL 下原子的，不会有"读到半个 bool"的问题。

---

## 九、与 v26.3 的关系

```
v26.3: prompt 层 —— "让纪律每轮可见"
  inject_directive → system prompt 常驻段
  agent 知道必须主动播报起手/阶段/风险/完成
  但做不到定时心跳（物理限制）

v26.4: 代码层 —— "让纪律里做不到的由代码兜底"
  后台线程直连 VoiceClient → 阻塞期照常存活报活
  directive 瘦身 → 模型只背有语义的播报
```

| 维度 | v26.3 | v26.4 |
|------|-------|-------|
| 机制 | prompt 注入 | 后台守护线程 |
| 解决什么 | agent 不知道要用语音 | agent 知道要用但物理上做不到 |
| 播报内容 | 有语义的（起手/阶段/风险/完成） | 机械的（"还在处理，稍等"） |
| 触发者 | 模型自己决定 | 代码判断静默超时 |
| 阻塞期可播 | ❌ | ✅ |
| 依赖 | `inject_directive` frontmatter | `VoiceClient` + Runtime `/health` |

两者互补，不是替代。v26.3 的 `inject_directive` 仍然负责让 agent 主动播有语义的内容；v26.4 只抢走了「定时报活」这一条模型确实做不到的。

---

## 附录：涉及文件一览

| 文件 | 涉及内容 |
|------|----------|
| [`agent/voice_heartbeat.py`](../../../agent/voice_heartbeat.py) | **新文件**：`VoiceHeartbeat` 类 —— 构造参数（L107-125）、`create_if_available` 三层门控（L128-168）、`_should_beat` 触发判定（L199-203）、`_send_beat` fail-safe（L205-213）、`_run` 守护循环（L215-224）、`start`/`mark_active`/`stop` 生命周期 |
| [`main.py`](../../../main.py) | 启动时两层门控 + 构造 + `start()`；tool loop 轮始 `mark_active()`；退出时 `stop()` |
| [`agent/__init__.py`](../../../agent/__init__.py) | 导出 `VoiceHeartbeat` |
| [`skills/voice-runtime/SKILL.md`](../../../skills/voice-runtime/SKILL.md) | frontmatter `inject_directive` 删心跳段 → 「存活心跳由 Runtime 自动维持」；正文「何时播报」同步更新 |
| [`docs/decisions/v26.4.md`](../../../docs/decisions/v26.4.md) | **新文件**：决策文档 —— 6 项决策 + 落地表 + 线程模型 + 验证计划 |
| [`scripts/test_v26_4_voice_heartbeat.py`](../../../scripts/test_v26_4_voice_heartbeat.py) | **新文件**：10 项不变量（全用 fake VoiceClient 注入）—— 门控 4 + 触发判定 3 + 线程行为 3 |
| [`docs/decisions/README.md`](../../../docs/decisions/README.md) | 追加 v26.4 索引行 |
| [`CLAUDE.md`](../../../CLAUDE.md) | 当前阶段更新为 v26.4；env `VOICE_HEARTBEAT_SECONDS` 说明 |

### 不变量测试覆盖（10 项）

| 类别 | 测试 | 验证内容 |
|------|------|----------|
| 门控 | `threshold<=0` → None | 显式关闭路径 |
| 门控 | client import 失败 → None | 软依赖降级 |
| 门控 | `health()` 抛异常 → None | Runtime 没起 |
| 门控 | `health()` 通 → 返回实例 | 正常构造 |
| 触发 | busy + 超阈值 → True | 核心判定正确 |
| 触发 | 非 busy → False | 空闲时不播 |
| 触发 | busy 未超时 → False | 不提前播 |
| 行为 | 后台线程真推一条 progress | 端到端链路 |
| 行为 | `mark_active` 重置计时 | 基准刷新正确 |
| 行为 | speak 抛错不冒泡 + stop 后线程退出 | fail-safe + 生命周期 |
