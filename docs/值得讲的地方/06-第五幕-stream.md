# 第五幕 · Stream — 边生成边输出 + 能优雅中断 `[核心]`

> 分支：`stream/v0.22`　核心文件：`transports/streaming.py` / `transports/chain.py` / `transports/chat_completions.py`　核心类：`StreamEvent` / `CancelToken` / `_ChatStreamAccumulator`

---

## 导览 `[核心]`

**【slide 正面】**

> **一档，但有一条贯穿的主轴：给已经证明对的同步路径，并排加一条流式路径，不污染它。**

```
痛点：V17–V20 把 transport 契约做实了，但只用了同步那一半。
      任何一次响应都要等模型整段写完才出现（DeepSeek thinking 5–30s 黑屏）。
      Ctrl+C 落在 chain.call 阻塞期间只能 SIGKILL 整个进程，对话历史全丢。

四个设计点：
  ① 正交路径    流式是独立的 stream_call，不是 call() 里按 callback 分支
  ② 事件模型    StreamEvent 携带语义（text / reasoning / tool / done），不是裸文本流
  ③ 首帧哨兵    首帧前可切家，首帧后必抛 —— 主动放弃同步路径的重试韧性
  ④ 中断信号    CancelToken（threading.Event）+ StreamCancelled 与 KeyboardInterrupt 分家
```

**【讲稿】**
第三幕的主题是"契约扛得住——加模型、组故障链、上缓存，主循环不动"。第五幕是同一主题的另一面:不是"新功能能塞进契约",而是**新功能不能污染已经证明对的路径**。流式是个大改动,但它走在 V17–V21 同步路径的**旁边**,而不是**里面**——代价是每个 transport 写两套调用,换来的是那 105 项同步不变量测试一个字不改。

**【过渡】**
先看这个"正交 vs 内部分支"的选择——它决定了整幕的形状。

---

## 设计点 ① 正交路径 — 800 行 vs 两个短方法 `[核心]`

**【slide 正面】**

> **解决**:把"流式"做成独立入口 `stream_call`,复用 `build_kwargs` / `convert_*`,但产出形态完全不同(迭代器 vs 单值)。

```
源项目 run_agent.py:7484+      nano transports/streaming.py
─────────────────────────     ──────────────────────────
call() 内部按 callback 分支     call()       一进一出,不动
  if stream_callback != None:   stream_call() 独立方法,吐事件
      ...流式状态机...
  else:
      ...同步...
                                两条路径明确:
单方法 800+ 行                   要流式调 stream_call
  · chunk 累积                   要非流式调 call
  · finish_reason 推断           不必看 callback 是不是 None
  · silent fallback
```

**【讲稿 · 金句】**
源项目把流式做成 `call` 的内部分支,按 `stream_callback != None` 切路径,代价是一个 **800+ 行**的方法——同一个函数里既管分片累积、又管 finish_reason 推断、还管静默降级。nano 选正交路径:`call()` 保持"一进一出",流式是独立的 `stream_call`。

代价很诚实:`ChatCompletionsTransport` 现在同时有 `call` / `stream_call` 两条路径(各约 50 / 100 行)。但这点重复换来的是——**V17 到 V21 写过的 105 项同步不变量测试,原样全过**。流式改造只碰 transport / chain / main 三处,agent loop 里"压缩 / memory / sync_turn"那些无关循环节零改动。

> 记忆点:**当一个新维度(流式)横切已有抽象时,优先并排加一条正交路径,而不是在老方法里加分支。分支会让"已经证明对的路径"重新背上未证明的状态。**

**【代价 · 诚实交代】**
正交不是免费的。代价是**每个 transport 子类要写两套 SDK 调用**(`call` + `stream_call`):

| transport | `call` | `stream_call` |
|-----------|--------|---------------|
| ChatCompletions | ~50 行 | ~100 行(含累积器) |
| Anthropic | ~40 行 | ~85 行 |

加第三家(如 Gemini)确实要再写一个 `stream_call`。但要看清这点冗余的**性质**:

- 真正会重复的逻辑**已经抽走了**——字段抽取(content/tool_calls/usage)只定义在 `normalize_response` 一处,流式把分片黏成同形态假对象**喂回它**,两边共用。剩下的"两套"只是各家 SDK 入口那几行(`stream=True` vs `messages.stream`),本来就该各写一遍。
- 冗余是**横向、隔离**的:每家一个独立文件,加 Gemini 不碰 agent loop、不碰 chain、不碰其它 transport,改动 O(1)。对比内部分支:加一家要去那个 800 行热点函数里塞特判,**改的是所有厂商共享、状态纠缠的代码**,谁改谁担风险。前者加法,后者乘法。
- 会不会脱节?`call` 的 105 项 + `stream_call` 的 13 项不变量测试互为防线,任一边改坏立刻红。

**什么时候这个选择会翻盘**:厂商极多(几十家)且流式逻辑高度雷同 → 横向冗余累积,该抽流式基类模板;或产品根本不需要保留同步路径 → 留两套就是纯负担。nano 两家、要保 `/stream off` 退路、教学要可读,正交是对的;源项目几十家 + 复杂 reasoning UI,选内部分支吃下 800 行也有它的道理。**这本身是个权衡,不是非黑即白。**

**【过渡】**
独立路径产出的是事件流。下一个问题:事件里装什么。

---

## 设计点 ② 事件模型 — 让事件携带语义,不让消费方去猜 `[核心]`

**【slide 正面】**

> 分支:`stream/v0.22`　核心文件:`transports/streaming.py`

```python
# 四种增量事件 —— transport 与 agent loop 之间的最小契约
EVENT_TEXT_DELTA       = "text_delta"        # 正文增量
EVENT_REASONING_DELTA  = "reasoning_delta"   # DeepSeek/Kimi thinking 增量
EVENT_TOOL_CALL_STARTED= "tool_call_started" # 第一次见到完整 tool name(仅一次)
EVENT_DONE             = "done"              # 流尾,response 给完整 NormalizedResponse

@dataclass
class StreamEvent:
    type: str
    text: str = ""
    tool_name: Optional[str] = None
    response: Optional[NormalizedResponse] = None   # 仅 done 帧带
```

**【第一段 · nano 现状:一个 `for` 循环拉所有语义】**
V22 的流式契约就一个类型 `StreamEvent` + 一个 `for` 循环。agent loop 拉事件,按 `type` 分流:

```python
# main.py:_stream_one_turn —— 唯一的消费者,边拉边处理
for ev in chain.stream_call(cancel_token=ct, model=m, messages=msgs, tools=ts):
    if   ev.type == EVENT_TEXT_DELTA:        sys.stdout.write(ev.text)                    # 正文,直接打
    elif ev.type == EVENT_REASONING_DELTA:   render_thinking(ev.text)                     # thinking,灰字分区
    elif ev.type == EVENT_TOOL_CALL_STARTED: sys.stdout.write(f"[tool] {ev.tool_name}")   # 工具开始
    elif ev.type == EVENT_DONE:              final_resp = ev.response                     # 完整 NormalizedResponse
```

三件省心事,都在这段里:

```text
① type 自带语义   → 文本/reasoning/工具/结束,if/elif 直接分流,不猜、不翻累加器
② done 帧给成品   → response 一次给齐 content/tool_calls/usage/finish_reason,loop 不拼分片
③ 脏活封内部     → SSE 分片怎么重建是 transport 的事(见 ②.5),loop 只见成品事件
```

**【先厘清 · push 与 pull 是"谁主动"】**
后面反复出现 push / pull,先一句话定死——指的是**生产者(吐 token 的 transport)和消费者(显示/TTS)之间,谁主动**:

```text
push(推) │ 消费者先登记回调,生产者主动 cb(data) 喂过来  │ 控制权在上游 │ "别打给我们,我们打给你"
pull(拉) │ 生产者做成迭代器,消费者 for 循环主动取        │ 控制权在下游 │ "你要的时候自己来取"
```

```python
# push:我登记函数,撒手;生产者说了算,它来调我          # pull:我拿着循环,我说了算
cb = my_fn                  # ← 登记                      for ev in stream:        # ← 主动拉
for cb in callbacks: cb(delta)   # 生产者主动喂              if ev.type==...: ...  # 想停就 break
```

谁主动,决定了三件事的难易:

```text
广播  push 顺 │ 登记 N 个回调,生产者挨个 cb,天生多消费者
中断  pull 顺 │ for 里直接 break 就停;push 要在回调里抛异常往上钻才能打断生产者
背压  pull 顺 │ 消费者拉慢生产者自然等;push 是硬塞,塞快了消费者得自己开队列扛
```

nano 选 pull(单消费者 + 好中断,配 V22 `CancelToken`),原项目选 push(一份流广播给显示+TTS)。下面三段就是沿这条线展开。

**【第二段 · 迭代1:给 pull 流加第二个消费者(同步广播)】**
现状只有一个消费者(终端打印)。要加 TTS 朗读,本质是**给同一个事件流挂第二个消费者**。pull 流没有"注册回调"机制,所以在下游把"一个循环"重构成"一组 handler",自己重建广播:

```python
# 每个消费者 = 一个 handle(ev) 闭包;StreamEvent 自带 type,各取所需
def make_printer():
    def handle(ev):
        if   ev.type == EVENT_TEXT_DELTA:        sys.stdout.write(ev.text)
        elif ev.type == EVENT_TOOL_CALL_STARTED: sys.stdout.write(f"\n  [tool] {ev.tool_name}\n")
    return handle

def make_tts(tts_queue):
    def handle(ev):
        if   ev.type == EVENT_TEXT_DELTA: tts_queue.put(ev.text)  # 只挑文本朗读
        elif ev.type == EVENT_DONE:       tts_queue.put(None)     # 本轮结束 → 冲刷缓冲
    return handle                                                  # reasoning/tool 自动忽略

# TTS 跑在独立线程,不阻塞打印(仿源项目 cli.py 的 text_queue 线程)
tts_queue = queue.Queue()
def _tts_worker():
    while True:
        text = tts_queue.get()
        if text is None: continue            # 流结束哨兵,不朗读
        speak(text)                          # pyttsx3 / edge-tts / 云 API
threading.Thread(target=_tts_worker, daemon=True).start()

# 消费循环:一个事件广播给所有 handler —— 这就是 nano 版的 "callbacks 名单"
consumers = [make_printer(), make_tts(tts_queue)]
for ev in chain.stream_call(cancel_token=ct, model=m, messages=msgs, tools=ts):
    for handle in consumers:
        handle(ev)
```

改动量:

```text
现状  → 一个 for,打印写死在分支里
加TTS → 往 consumers append 一项,消费循环一字不改;TTS handler 按 ev.type 自取,
        reasoning_delta / tool_call_started / done 全自动跳过,不需要任何哨兵约定
```

> 落地顺序:先方案 A(在现有 `text_delta` 分支直接加一行 `tts_queue.put(ev.text)`,半小时验证引擎出声)→ 再重构成上面的多 handler。纯输出朗读够用;要做**语音对话**(语音输入)还得在输入侧接 STT,属另一条线。

**【第二段·迭代2 · 异构消费者背压隔离:让慢消费者扛自己的债】**
迭代1 的 `for h in consumers: h(ev)` 是**同步串行**的,埋了三个生产级故障——而这正是原项目早已解决、迭代1 还没补的:

```text
① 慢消费者反压生产者
   tts handle 里若直接 speak()(同步播 2 秒),这 2 秒 for 循环卡死
   → 拉不了下一个 SSE 事件 → 模型连接闲置/超时 → 连"显示"都跟着卡
② 一个消费者崩,全流死
   make_tts 抛异常(引擎没装)→ 整个 for ev in stream 栈穿 → 显示也没了
③ 队列无界涨内存 / 有界满了又退回故障①
```

"异构"是关键词:显示是**微秒级**(write 到终端),TTS 是**秒级**(合成音频+等播完)。同一个流喂两个速度差千倍的消费者,**慢的那个不能拖垮快的,也不能反向卡住生产者**。迭代2 的解法:**每个消费者 = 自己的线程 + 自己的有界队列 + 自己的丢弃策略**。

```python
# 每个消费者一个 (queue, thread, 背压策略);生产者只管非阻塞分发
class Consumer:
    def __init__(self, name, handle, *, maxsize, on_full):
        self.name, self.handle, self.on_full = name, handle, on_full
        self.q = queue.Queue(maxsize=maxsize)     # 有界:堆到上限触发策略
        threading.Thread(target=self._run, daemon=True).start()

    def offer(self, ev):                           # 生产者调它,绝不阻塞
        try:
            self.q.put_nowait(ev)
        except queue.Full:
            self._on_full(ev)                      # 满了按策略丢/合并,不卡生产者

    def _run(self):                                # 独立线程,慢就慢自己
        while True:
            ev = self.q.get()
            try:
                self.handle(ev)                    # ② per-consumer try:崩了只崩自己
            except Exception as e:
                log_consumer_error(self.name, e)   # 不往上抛,其他消费者无感
```

关键在**每个消费者的背压策略可以不同**——这才叫"异构"隔离:

```python
consumers = [
    Consumer("display", printer_handle, maxsize=0,  on_full=None),       # 显示:无界必达,本来就快
    Consumer("tts",     tts_handle,     maxsize=32, on_full="coalesce"), # TTS:有界,落后就合并文本
]
for ev in stream:
    for c in consumers:
        c.offer(ev)        # 非阻塞:显示瞬间收下,TTS 慢就在它自己队列里扛
```

```text
显示 display │ 必达不丢字 │ 队列无界,它本来就快,堆不起来
TTS  tts     │ 可丢可合并 │ 落后了把队列里几段文本拼成一句再念,
             │            │ 宁可少几个换气停顿,也不让音频延迟越拉越大
gateway 转发 │ 可丢老的   │ on_full="drop_oldest",客户端只要最新状态
```

`coalesce`(合并)对 TTS 尤其合适——队列满时不是丢字,而是把相邻文本拼成一段,队列长度不增:

```python
def _on_full(self, ev):
    if self.on_full == "coalesce" and ev.type == EVENT_TEXT_DELTA:
        old = self.q.get_nowait()                              # 取出队头
        self.q.put_nowait(StreamEvent(EVENT_TEXT_DELTA,
                                      text=old.text + ev.text)) # 合并塞回 → 长度不变
    elif self.on_full == "drop_oldest":
        self.q.get_nowait(); self.q.put_nowait(ev)
    # drop_new:啥也不做,新事件直接丢
```

到这一版,nano 的多消费者不再是"用 pull 硬模拟 push",而是**比原项目的固定回调多了一层 per-consumer 背压策略**。代价:线程/队列/丢弃策略的复杂度——**单消费者场景完全用不上**,所以这是"真长出多消费者需求才做"的迭代,不是现在就加(否则违背 nano 教学简化的定位)。

**【第三段 · 迭代2 vs 原项目 Hermes:补齐后,差异收敛到哪】**
原项目**不是**只有一个 `Callable[[str], None]`——`run_agent` 构造器挂了 **11 个具名回调**,reasoning / 工具开始**各有专属通道**(`reasoning_callback` / `tool_gen_callback`),TTS 也早就跑在独立 `text_queue` 线程、每个回调 `try` 包住。所以迭代2 补的"背压+崩溃隔离",**原项目生产侧本就有**。

"专属通道"不是空话,就是这俩 `_fire_*`(`run_agent.py:7453+` 真身)——一个语义一个独立回调:

```python

def _on_text(text):
    _fire_first()
    self._fire_stream_delta(text)
    deltas_were_sent["yes"] = True

def _on_tool(name):
    _fire_first()
    self._fire_tool_gen_started(name)

def _on_reasoning(text):
    _fire_first()
    self._fire_reasoning_delta(text)
    
def _fire_reasoning_delta(self, text):        # reasoning 专属:只喂 thinking,不和正文混
    cb = self.reasoning_callback              # ← 独立于 stream_delta_callback(正文)
    if cb is not None:
        try: cb(text)
        except Exception: pass

def _fire_tool_gen_started(self, tool_name):  # 工具开始专属:喂结构化 name,不是文本
    cb = self.tool_gen_callback               # ← 又一个独立回调;docstring:给 TUI 显示 spinner
    if cb is not None:                         #    免得用户盯着 45KB write_file 生成时冻屏
        try: cb(tool_name)
        except Exception: pass

# 流式现场按语义分流到不同回调 —— 这就是 push"一个语义一个通道"的实证:
stream_converse_with_callbacks(raw_response,
    on_text_delta=_on_text,            # 正文      → stream_delta_callback
    on_tool_start=_on_tool,            # 工具开始  → tool_gen_callback
    on_reasoning_delta=_on_reasoning)  # reasoning → reasoning_callback
```

对照 nano:**原项目"加一种语义 = 加一个 `_fire_xxx` + 一个回调参数",nano"加一种语义 = 加一个 `EVENT_XXX` type 值"——功能等价,聚合方式不同。** 两边其实是同一件事的两种聚合:**原项目 push、nano pull。** 把 TTS 那条线摆并排:

```python
# ── 原项目 Hermes:push 模型,框架级背压+隔离 ──────────────────
callbacks = [self.stream_delta_callback,        # 显示
             self._stream_callback]             # ← TTS;跑在独立 text_queue 线程
for cb in callbacks:
    try: cb(delta.content)                      # 每个 cb 各自 try,崩了不连坐
    except Exception: pass
# 段结束怎么通知?没有专属通道 → 借 None 当哨兵:
self.stream_delta_callback(None)                # 显示侧:None = "关显示框"
# tts_tool.py: if delta is None: flush()        # TTS 侧:None = "全文读完"
#              ↑ 同一个 None,两消费者理解相反 → 14499 得手动跳过 TTS + 加注释

# ── nano 迭代2:pull 模型,下游 Consumer 自带背压+隔离 ─────────
consumers = [Consumer("display",...), Consumer("tts", maxsize=32, on_full="coalesce")]
for ev in stream:
    for c in consumers: c.offer(ev)             # 非阻塞分发,各队列各线程各策略
# 段结束?ev.type == EVENT_DONE 一等事件 → 各 Consumer 自取,不借 None
```

```text
                  原项目 Hermes(push)            nano 迭代2(pull + StreamEvent)
加 TTS          │ 注册第三个 Callable            │ append 一个 Consumer
广播            │ 框架天生支持(push 给名单)     │ 下游 for c.offer 派发(已封进 Consumer)
慢消费者隔离    │ text_queue 线程,框架级        │ 每 Consumer 独立线程+有界队列   ← 持平
崩溃隔离        │ 每个 cb 各自 try              │ 每 Consumer _run 里 try         ← 持平
背压策略        │ 固定(线程+丢弃,基本一套)     │ per-consumer:coalesce/drop_*   ← nano 更细
段结束控制信号  │ 无通道 → 借 None,显示/TTS 冲突 │ EVENT_DONE 一等事件,零歧义     ← nano 更干净
reasoning       │ reasoning_callback 专属        │ EVENT_REASONING_DELTA           ← 功能等价
工具开始        │ tool_gen_callback(name) 专属   │ EVENT_TOOL_CALL_STARTED         ← 功能等价
中断/取消       │ 回调里抛异常往上钻            │ for 循环里 break(配 CancelToken)← nano 更顺
语义扩展        │ 加一个构造器回调参数          │ 加一个 type 枚举(改 transport) ← 原项目改动面更小
```

**一句话**:迭代2 把"背压+崩溃隔离"补齐后,两边在**多消费者隔离上打平**,各自多出一点对方没有的:nano 多了 per-consumer 背压策略(coalesce/drop)和 `EVENT_DONE`/可中断的干净;原项目多了"加语义只改构造器、不碰流式核心"的低成本扩展。**唯一站得住的设计异味仍只有原项目的 `None` 哨兵**——而它是"push 模型没给控制信号开具名通道"的纪律问题(补个 `segment_end_callback` 即可),不是"没做 StreamEvent"。结论没变:**push 为多消费者+多语义优化、控制信号靠自律;pull 为单消费者+可中断优化、控制信号靠类型。两边对各自场景都合身,nano 选 pull 是因为它就是 pull 的场景。**

**【过渡】**
第一段说"分片重建封死在 transport 内部",听起来轻巧。但 OpenAI 的 SSE 分片有两个反直觉的坑,封装它的 `_ChatStreamAccumulator` 才是脏活所在。

---

## 设计点 ②.5 累积器 — 两个 provider quirk 的真实踩坑 `[扩展]`

**【slide 正面】**

> 核心文件:`transports/chat_completions.py` 的 `_ChatStreamAccumulator`

```python
fn = getattr(tcd, "function", None)
if fn is not None:
    if fn.name:
        entry["name"] = fn.name        # ← 赋值,不是 +=
    if fn.arguments:
        entry["arguments"] += fn.arguments   # ← 必须 concat
```

**【讲稿】**
同样是工具调用的分片,两个字段的累积规则**相反**,这是真踩出来的:

- `arguments` 必须 `+=`:OpenAI spec 规定 arguments 是逐片下发的,得拼起来才是完整 JSON。
- `name` 必须**赋值**:本来以为 name 只在第一帧来一次,直到接 MiniMax M2.7(via NVIDIA NIM)发现它**每帧都重发完整 name**——用 `+=` 会拼成 `"read_fileread_file"`,工具名直接废掉。

还有一个 quirk:`usage` 不在正文帧里,而在最后一个 `choices=[]` 的空帧带——必须显式传 `stream_options.include_usage` 才有。

整个累积器的收尾很漂亮:把分片黏完后,构造一个和非流式 `ChatCompletion` **同形态**的 `SimpleNamespace` 假对象,喂回 `normalize_response`。这样"字段抽取"(content / tool_calls / usage)只在一处定义,流式和同步共用。

> 教训:**流式协议的脏活不在"拼字符串",在"每家 provider 的分片语义都不一样"。把它关进一个累积器类,主循环就只看见干净的 `NormalizedResponse`。**

**【过渡】**
事件能流出来了,接下来是这一幕真正的高潮:流式失败了怎么办。答案出人意料——**它主动放弃了第三幕辛苦建立的重试韧性。**

---

## 设计点 ③ 首帧哨兵 — 流式语义逼出来的硬约束 `[核心]`

**【slide 正面】**

> 核心文件:`transports/chain.py` 的 `stream_call`(仿源项目 `run_agent.py:7966-8082` 的 `deltas_were_sent` 哨兵)

```python
delivered = False
try:
    for ev in entry.transport.stream_call(entry.client, cancel_token=..., **kw):
        delivered = True          # ← 只要吐过一帧就翻 True
        yield ev
        if ev.type == EVENT_DONE: self._accumulate_cache_stats(entry, ev.response)
    return                        # 成功,关断路器
except StreamCancelled:
    raise                         # 用户取消,不计失败,透传
except Exception as exc:
    ...
    if delivered:
        raise                     # ← 已吐过 token → 禁止切家
    continue                      # ← 还没吐过 → 切下一家
```

**【先纠一个常见误解】**
这不是"nano 砍掉了源项目的重试"。恰恰相反——**这个逻辑是源项目先有的(`deltas_were_sent` 哨兵),nano 把它照搬过来了**(`delivered` 就是同一个东西换了名)。它更不是某个项目的偷懒,而是**任何做流式 failover 的人都会撞到的同一面墙**。正因为连生产级源项目都被逼着主动放弃重试,这个点才更有说服力。

**【讲稿】**
回忆第三幕:同步路径的 `_try_with_retry` 对 RETRYABLE 错误(500/超时)做 jittered-backoff,在本家重试好几次,扛得住抖动。**流式路径故意把这套全关了。** 就靠一个 `delivered` 哨兵分三种情况:

| 场景 | 行为 | 用户体感 |
|------|------|---------|
| 首帧**之前**挂 | 切下一家 | "卡了一下后正常出字"(无痕) |
| 首帧**之后**挂 | 不切,异常上抛 | 半截真实内容 + 一个干净的失败提示 |
| `StreamCancelled` | 透传,不动断路器 | 回到 prompt |

那面墙到底是什么?**重试的前提是"失败可以无痕重来"。**

- 同步路径:模型整段写完才一次性显示,失败了在后台重试,用户**根本看不见**中间过程——重试是纯收益。
- 流式路径:每吐一小块就**立刻泼到屏幕上**,泼出去就收不回。这时候切到另一家,新模型从第一个字重新生成,但用户屏幕上已经有前半段了——新内容要么和旧的重复、要么对不上,**没有干净的办法把已显示的收回再重来**。

所以"首帧后必抛"不等于"整轮丢光":前半段已经显示、也已在 transport 内部累积,上层把这半截保留 + 标记中断即可,而且**对话历史还在**(这正是 V22 要解决的痛点——以前 SIGKILL 整进程会丢光历史)。**半截真实内容 + 干净的失败提示,胜过一段自我冲突的乱码。**

> 决策日志原话:"V22 流式不做单 entry 的 RETRYABLE 内部重试……**这是流式的硬约束,不是简化。**"

**【slide · 金句】**
分量在"硬约束 vs 简化"的区分。nano 砍源项目大多是教学简化(够用就行),**这一处却是连源项目都没法简化的硬约束**——流式语义本身决定了重试有害。同步路径重试是优点,流式路径重试是缺点:**同一个容错机制,换个上下文就从资产变成负债。** 差别只在一件事——**内容有没有泼到屏幕上。** 没泼出去之前失败可以装作没发生;泼出去之后,只能诚实面对。这也呼应一条工程原则:**报告结果要忠实——失败了就说失败、带上已有的部分,而不是用一次赌博式重试去掩盖。**

**【暗线 · 呼应第三幕】**
`classify_error` 三分类在流式里行为收窄了:FATAL 还是直接抛,但 RETRYABLE 不再触发重试(降级成"切家或抛"),FAILOVER 只在首帧前有效。**同一套错误分类,在流式约束下三条路收成两条。** 这是 transport→stream 之间的一条暗线。

**【过渡】**
最后一个设计点:用户怎么"按下停止",以及为什么这个开关现在就该考虑多线程。

---

## 设计点 ④ 中断信号 — 一个开关,为下一档预留 `[核心]`

**【slide 正面】**

> 核心文件:`transports/streaming.py` 的 `CancelToken`

```python
class CancelToken:
    """main loop 设置,stream_call 检查。"""
    def __init__(self): self._event = threading.Event()   # 不是裸 bool
    def cancel(self):  self._event.set()      # SIGINT handler 调,幂等
    def reset(self):   self._event.clear()    # 每轮对话开始前
    def check(self):                          # stream_call 每帧调(hot path)
        if self._event.is_set(): raise StreamCancelled(...)
```

**【讲稿】**
一个线程安全的"取消开关",串起三个角色:**main.py 的 SIGINT handler 按下** `cancel()` → **`stream_call` 每吐一帧 `check()`** → 命中就抛 `StreamCancelled`,干净退出流,被 main loop 接住回到 prompt。两个设计选择值得讲:

**为什么是 `threading.Event` 而不是裸 bool?** V22 单 agent 单线程,裸 bool 够。提前用 Event 是为**下一档 V23 多智能体**——父 token 会被多个子 agent 线程同时 check/set,裸 bool 在多线程下读写没有原子性保证。Event 天然原子、零额外成本,避开"V23 时回头改"的回炉。**为下一档预留抽象**,正是这条演进主轴特有的叙事,直接勾到第六幕。

**为什么叫 `check()` 不叫 `raise_if_cancelled()`?** 这是 hot path,每个 SSE 数据块调一次。短名字让那段高频循环代码视觉负担更低。一个小但真实的命名权衡。

**StreamCancelled 与 KeyboardInterrupt 为什么分家?** main.py 用一个 `streaming_active` 旗标分流同一个 Ctrl+C:

```python
def _sigint_handler(signum, frame):
    if streaming_active["flag"]:
        cancel_token.cancel()      # 流式期间 → 只取消本轮,回到 prompt
    else:
        raise KeyboardInterrupt    # prompt 上 → 退出 agent
```

同一个按键,两种语义:在 prompt 上按是"我要走了"(退出),在流式途中按是"这轮跑偏了"(只取消当前响应)。把它们建模成两种异常,语义就不会串。

**【真实踩坑】**
- **Anthropic 的 `input_json_delta` 不必自己累**:第一稿想给 tool_use 的 input 做 partial-JSON 累积(仿 chat_completions),后来发现 `stream.get_final_message()` 返回的 `input` 已经是完整 dict——SDK 内部累过了。**一行 SDK 调用省掉 50 行手写状态机。** 教训:先看 SDK 给什么,再决定要不要自己累。
- **硬编码"命令集合"的不变量测试**:V22 加 `/stream` 后,V21.1 那个"启动期注册的命令清单等于固定集合"测试立即红——expected 写死了。每加一个命令/工具/钩子都要回头同步,V23 加 `/agents` 时也要记得。

**【演示】**
真 DeepSeek API:thinking 模式下 5+ 字流式中按 Ctrl+C,当帧停止 + 回到 prompt 不杀进程;`/stream off` 跑一轮,确认 usage / cache_hit_rate 与 `on` 一致。

---

## 这一幕证明了什么 `[核心]`

**【slide 正面】**

```
第三幕:契约扛得住 —— 加模型/组链/上缓存,主循环不动
第五幕:契约不被污染 —— 加流式这个新维度,走在同步路径旁边而非里面
        · 正交路径    → 105 项同步不变量零改动
        · 事件模型    → 语义在事件里,不在消费方
        · 首帧哨兵    → 流式语义逼出的硬约束:首帧后不重试(源项目同款,非简化)
        · 中断信号    → threading.Event 为 V23 多线程预留
```

**【过渡】**
单个 Agent 现在能边想边说、还能随时打断。但有些任务想拆开并行——第六幕,让它派出分身。注意 `CancelToken` 已经用 `threading.Event` 给多线程留好了位置:这一幕埋的线,下一幕就接上。重点也从"功能"转向"安全"。
