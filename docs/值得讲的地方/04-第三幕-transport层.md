# 第三幕 · Transport 层 — 把"跟模型对话"抽象出来

> 分支：`transport/v0.17 → v0.20`（4 档）　核心类：`ProviderTransport` / `NormalizedResponse` / `TransportChain`

---

## 导览 `[核心]`

**【slide 正面】**

> **一条线四档，每档解决上一档暴露的问题：**

```
v0.17  抽象：ProviderTransport 契约 —— 把"跟一家模型对话"拆成固定五步
v0.18  验证：加 AnthropicTransport —— 第二家来了，主循环改了几行？
v0.19  韧性：TransportChain —— 多家组主备链，一家挂了自动切换
v0.20  省钱：Prompt Cache —— 显式标记缓存断点 + 命中率统计
```

> 记忆点：**契约只有在第二个实现出现时才被证明。** v0.17 抽的是猜想，v0.18 才是事实。

**【讲稿】**
记忆那条线讲的是"换存储后端不动主循环"，Transport 这条线讲的是同一个道理换一个维度：**换模型厂商不动主循环**。最自然但最致命的写法，是在主循环里写 `if 是 OpenAI 就这样、else if 是 Anthropic 就那样`——每接一家新模型，主循环就被污染一次。这一幕就是把"跟一家模型对话"这件事抽成契约，然后用四档证明这个契约扛得住：加第二家、组故障链、上缓存，主循环几乎不动。

**【过渡】**
先看 v0.17：在只有一家模型时，先把契约立起来。

---

## v0.17 — ProviderTransport 契约 `[核心]`

**【slide 正面】**

> 分支：`transport/v0.17`　核心文件：`transports/base.py` / `transports/types.py`
>
> **解决**：把"跟一家模型对话"拆成固定五步，agent loop 只认标准化结果。

```python
class ProviderTransport(ABC):
    @property
    @abstractmethod
    def api_mode(self) -> str: ...                       # 该 transport 处理的模式名

    @abstractmethod
    def convert_messages(self, messages, **kw): ...      # OpenAI 格式 → 各家原生
    @abstractmethod
    def convert_tools(self, tools): ...                  # 工具定义 → 各家原生
    @abstractmethod
    def build_kwargs(self, model, messages, tools=None, **p): ...  # 组装 SDK kwargs
    @abstractmethod
    def normalize_response(self, response, **kw) -> NormalizedResponse:
        """原生 SDK 响应 → 标准结果，agent loop 之后只读它。"""

    # 三个可选 hook，默认实现 —— 给后面几档留的扩展点
    def validate_response(self, response) -> bool: return True
    def extract_cache_stats(self, response): return None         # v0.20 用
    def apply_prompt_cache(self, messages, cache_ttl="5m"): return messages  # v0.20 用
```

**【讲稿 · 金句】**
这个契约的 docstring 里写了一句我特别喜欢的话：它列的不是"我负责什么"，而是"**我不负责什么**"——client 构造、streaming、缓存、重试、中断，统统不归我管。**明确写下边界外的东西，比写边界内的更能防止一层抽象慢慢腐烂成大杂烩。**

配套还抽了一个 `NormalizedResponse` 数据类，**它是 transport 层唯一暴露给 agent loop 的返回类型**——主循环不再消费 OpenAI 的 `ChatCompletion` 或 Anthropic 的 `Message`，只读这一份。但有个很克制的兼容设计：

```python
@dataclass
class ToolCall:
    id: str | None
    name: str
    arguments: str  # JSON string
    provider_data: dict[str, Any] | None = field(default=None, repr=False)
    # 旧代码读 tc.function.name / tc.type，让 .function 返回 self 即可零改动
    @property
    def function(self) -> "ToolCall": return self
    @property
    def type(self) -> str: return "function"
```

> 跨家族通用的字段（content / tool_calls / finish_reason / usage）升到 top-level；少数家族独有的（DeepSeek 的 `reasoning_content`、Anthropic 的 `reasoning_details`）塞进 `provider_data` 逃生口，不污染共享接口。

**【遗留问题】**
此刻只有 `ChatCompletionsTransport` 一家实现。**一个实现的抽象只是猜想**——你没法证明这五个方法切得对。得等第二家完全不同的协议来撞一下。

**【过渡】**
v0.18 把 Anthropic 接进来——这是给 v0.17 的契约做的第一次实弹测试。

---

## v0.18 — AnthropicTransport：契约的第一次实弹测试 `[核心]`

**【slide 正面】**

> 分支：`transport/v0.18`　核心文件：`transports/anthropic.py`
>
> **解决**：第二家协议接进来，所有格式差异关在 anthropic.py 里，**agent loop 零改动**。

Anthropic 跟 OpenAI 的三个核心结构差异，全在 transport 内部消化：

```python
class AnthropicTransport(ProviderTransport):
    # ① system 是顶层参数，不是 messages 里的一条 → convert_messages 返回元组
    def convert_messages(self, messages, **kw) -> tuple[str, list]:
        ...
        ...
        return system_str, anthropic_messages

    # ② assistant 的工具调用是 tool_use content block；
    #    tool 结果是 user 消息里的 tool_result content block（不是 OpenAI 的顶层 tool_calls）
    #        {"type": "tool_use",   "id": ..., "name": ..., "input": {...}}
    #        {"type": "tool_result","tool_use_id": ..., "content": ...}

    # ③ stop_reason 词汇不同 → 映射回 OpenAI 标准
    _STOP_MAP = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length"}

    def convert_tools(self, tools):              # parameters → input_schema
        return [{"name": fn["name"], "description": fn.get("description", ""),
                 "input_schema": fn.get("parameters", {...})} for ...]
```

**【讲稿】**
这一档真正的主角不是 anthropic.py 写了什么，而是 **agent.py 的 diff**。两家协议差到——system 位置不同、工具调用建模不同、连"模型为什么停下"的词都不同（`end_turn` vs `stop`）——但这些差异一行都没漏进主循环。主循环唯一的改动是按 `transport.api_mode` 选 SDK 方法（`chat.completions.create` vs `messages.create`），其余照旧读 `NormalizedResponse`。

还兑现了 v0.17 埋的两个扩展点：transport **注册表 + 自动发现**（新文件末尾 `register_transport(...)` 就能被发现，agent.py 一行不改），以及 `make_llm_client(api_mode)` 工厂多一个 elif 分支。**加一家新模型的成本是 O(1)。**

**【遗留问题】**
真踩了个坑：DashScope 的 Anthropic 端点要求显式传 `thinking={"type":"disabled"}`，不传直接 400。这种 provider 私有约束只能塞进 `build_kwargs` 默认值。另外此刻只有"单 transport"——一家挂了整个会话就停。生产环境需要主备。

**【演示】**
`git diff transport/v0.17 transport/v0.18 -- agent.py`：加了一整家协议，主循环的改动小到可以逐行念完。**这就是抽象成功的实证时刻**——v0.17 的猜想在这里变成事实。

**【过渡】**
能接多家了，下一步让它们组成主备链：一家挂了自动切到下一家。v0.19。

---

## v0.19 — TransportChain + 断路器 `[核心]`

**【slide 正面】**

> 分支：`transport/v0.19`　核心文件：`transports/chain.py` / `transports/error_classifier.py`
>
> **解决**：多 transport 顺序故障切换，把"主流"和"故障路径"解耦出主循环。

```python
def classify_error(exc) -> ErrorAction:    # 三分类，不是源项目的 14 种
    # RETRYABLE  500/502/504/408、timeout  → 同 transport 等待后重试 N 次
    # FAILOVER   429/401-403/503/529、rate_limit/auth → 直接切下一家
    # FATAL      400+context_overflow、413        → 切了也错，直接抛

class _BreakerState:                       # 两个字段表达断路器三态
    consecutive_failures: int
    opened_at: float                       # ==0 closed；未过 cooldown open；过了 half_open

class TransportChain:
    def call(self, client=None, **kwargs) -> NormalizedResponse: ...
    def _try_with_retry(self, entry, **kwargs): ...   # RETRYABLE 在本家重试
    def _jittered_backoff(self, attempt):             # base*2^n + jitter，防惊群
    def status(self) -> list[dict]: ...               # /transport 命令读它
```

**【讲稿】**
故障切换涉及四件事：错误分类、断路器状态、backoff、半开探针。把它们混进主循环，主流和故障路径就耦死了。所以抽一个 `TransportChain`，主循环从 `transport.call(...)` 改成 `chain.call(...)`——**签名兼容**，链长 1 时退化成 v0.18 行为，旧部署 env 一个字不用改。

下面把两个最值得讲的设计点各配一段真实代码。

### 设计点 ① 断路器：两个字段表达三态

教科书的断路器是"closed / open / half_open"三态，最直觉的写法是一个 enum 加一台状态机。这里偏不——**两个字段就够了**：`consecutive_failures` 记连续失败数，`opened_at` 记打开时刻（0 表示没打开）。三态全从这两个数推出来。

```python
@dataclass
class _BreakerState:
    """closed (默认): 健康，正常调用
       open:        故障，跳过，直到 cooldown 过去
       half_open:   冷却完毕，允许一次探针调用 — 成功则 close，失败则重开"""
    consecutive_failures: int = 0
    opened_at: float = 0.0    # 0 表示 closed；非 0 是打开时刻的 monotonic 秒
    last_reason: str = ""

    def is_open(self, now: float, cooldown_seconds: float) -> bool:
        if self.opened_at == 0.0:           # 从没打开过 → closed
            return False
        return (now - self.opened_at) < cooldown_seconds   # 还在冷却窗口内 → open
```

三态怎么读出来，看 `status()` 里这一行就懂了——**没有第三个字段，half_open 是"opened_at 非 0 但 is_open 已返回 False"推出来的**：

```python
"state": "open" if is_open else ("half_open" if e.breaker.opened_at else "closed"),
```

调用循环里三态各自的动作（这是 `call()` 遍历每个 entry 的真实片段）：

```python
for entry in self.entries:
    # ① open 且未冷却 → 跳过这家
    if entry.breaker.is_open(now, self.cooldown_seconds):
        logger.info("skip %s: breaker open (last=%s, %.1fs left)",
                    entry.api_mode, entry.breaker.last_reason,
                    self.cooldown_seconds - (now - entry.breaker.opened_at))
        continue

    # ② 半开探针：cooldown 过了但 opened_at != 0 — 尝试一次，成功则关闭
    half_open = entry.breaker.opened_at != 0.0

    result = self._try_with_retry(entry, kwargs, attempts)
    if result is not None:
        # ③ 成功 — 关闭断路器（清零两个字段）
        if half_open or entry.breaker.consecutive_failures > 0:
            logger.info("breaker closed for %s", entry.api_mode)
        entry.breaker.consecutive_failures = 0
        entry.breaker.opened_at = 0.0
        return result

    # 失败 — 决策已写入 breaker，继续下一家
    now = self._clock()
```

> 失败累加在另一处：`consecutive_failures += 1`，一旦 `>= failure_threshold` 就 `opened_at = self._clock()`——断路器打开。**整套三态机就靠"给 opened_at 赋值/清零"驱动，没有显式状态枚举要维护。**

### 设计点 ② jittered backoff：抖动防惊群

RETRYABLE 错误（500/超时）在本家重试，重试间隔是指数退避。但**纯指数退避有个隐患**：多个 session 同时撞上同一家故障，它们的重试时刻会精确对齐，冷却到点的瞬间一起重试，把刚恢复的服务再打挂——这就是惊群（thundering herd）。源项目踩过这个坑。

```python
def _jittered_backoff(self, attempt: int) -> float:
    """base * 2^(attempt-1) + uniform(0, 0.5 * base * 2^(attempt-1))"""
    exponent = max(0, attempt - 1)
    delay = min(self.base_delay * (2 ** exponent), 60.0)   # 指数主体，封顶 60s
    jitter = random.uniform(0, 0.5 * delay)                # 0~50% 的随机抖动
    return delay + jitter
```

它在 `_try_with_retry` 的重试循环里被调用——**只有 RETRYABLE 且没到上限才退避重试**，FATAL 直接抛、FAILOVER 当场记一次失败切下一家：

```python
for attempt in range(self.max_retries + 1):
    try:
        resp = entry.transport.call(entry.client, **call_kwargs)
        self._accumulate_cache_stats(entry, resp)          # V20 顺手累计 cache 命中
        return resp
    except Exception as exc:
        classified = classify_error(exc)                   # 三分类
        if classified.action == ErrorAction.FATAL:
            raise                                          # 切了也错 → 直接抛
        if classified.action == ErrorAction.RETRYABLE and attempt < self.max_retries:
            delay = self._jittered_backoff(attempt + 1)    # ← 抖动退避，本家再试
            self._sleep(delay)
            continue
        self._record_failure(entry, classified)            # FAILOVER / 重试用尽 → 记失败
        return None                                        # 返回 None，call() 切下一家
```

> 两个细节：指数主体 `min(..., 60.0)` **封顶 60 秒**，防止 attempt 大了退避到几分钟；jitter 取 `[0, 0.5*delay]` 把各 session 的重试时刻**散开半个量级**，对齐的尖峰就被抹平成一段平缓的重试流。注意退避只发生在 RETRYABLE 分支——这正是 `classify_error` 三分类的价值：**只对"等一下可能就好"的错误退避重试，对"切了也错"的直接抛、对"换家可能好"的立刻切。**

**【遗留问题 / 真实踩坑】**
第一版让链上各家共享同一个 `MODEL` env，写完才发现：DeepSeek 切到 Qwen 时会带着 `deepseek-chat` 这个错模型名调过去，立即 400。修复是让**每个 chain entry 自带 model**，链语法升级成 `api_mode:model`：

```bash
TRANSPORT_CHAIN=chat_completions:deepseek-chat,anthropic_messages:qwen3.6-plus
```

> 教训：**fallback 链的每个 entry 必须自包含**——模型名、甚至 client，不能依赖全局共享状态。源项目把 entry 做成 `list[dict]` 各自带 `{provider, model, base_url, api_key}`，就是这个原因。

**【过渡】**
能扛故障了，最后一档解决一个看不见但烧钱的问题：prompt cache。v0.20。

---

## v0.20 — Prompt Cache 控制 `[核心]`

**【slide 正面】**

> 分支：`transport/v0.20`　核心文件：`transports/prompt_caching.py`
>
> **解决**：显式给 Anthropic 打缓存断点 + 累计命中率统计；ChatCompletions 走 identity 不动。

```python
# transports/base.py 的 hook（v0.17 就埋好的扩展点），只有 Anthropic 重写
def apply_prompt_cache(self, messages, cache_ttl="5m"):
    return messages                        # 默认 identity：DeepSeek/OpenAI 隐式缓存 prefix

@dataclass
class Usage:
    cached_tokens: int = 0          # read：命中，约 1/10 input 价
    cache_creation_tokens: int = 0  # write：写入，约 1.25x input 价 —— 故意拆开，别合并
```

**【讲稿】**
prompt cache 是省钱的关键：每轮不变的开头（system + 历史前缀）可以缓存命中，read 价只有 input 的约 1/10。但 Anthropic 要**显式**在 message 上打 `cache_control` 标记才缓存。这件事该谁做？答案是 transport 自己——**复用 v0.17 埋的 `apply_prompt_cache` hook**，只有 `AnthropicTransport` 重写它，chat_completions 走 identity 路径，messages 一字节不变。

下面是这档最核心的三段代码。

### 核心代码 ① Anthropic 重写 hook

默认 hook 是 identity，所以 OpenAI/DeepSeek 兼容接口完全不用关心 `cache_control`。只有 AnthropicTransport 把这个 hook 接到 `prompt_caching.py`：

```python
def apply_prompt_cache(self, messages, cache_ttl="5m"):
    """Anthropic prompt cache 显式标记（system_and_3 策略）。"""
    from transports.prompt_caching import apply_anthropic_cache_control
    return apply_anthropic_cache_control(messages, cache_ttl=cache_ttl)
```

这里的设计点是：**缓存策略属于 provider 差异，不属于 agent loop**。主循环不知道 Anthropic 要在 block 上挂 `cache_control`，也不知道 OpenAI/DeepSeek 是隐式 prefix cache；它只调用统一 hook。

### 核心代码 ② system_and_3 策略

Anthropic 单次请求最多 4 个 breakpoint，上限刚好够放一个稳定点 + 三个滚动点：

```python
_MAX_BREAKPOINTS = 4

def apply_anthropic_cache_control(api_messages, cache_ttl="5m"):
    messages = copy.deepcopy(api_messages)     # 深拷贝，避免污染原始历史消息
    marker = {"type": "ephemeral"}             # 省略 ttl 时 Anthropic 默认 5m
    if cache_ttl == "1h":
        marker["ttl"] = "1h"

    breakpoints_used = 0

    # 1 breakpoint @ system：每轮最稳定的前缀，命中率最高
    if messages and messages[0].get("role") == "system":
        _apply_cache_marker(messages[0], marker)
        breakpoints_used += 1

    # 其余 breakpoint @ 最后几条非 system 消息：滚动窗口
    remaining = _MAX_BREAKPOINTS - breakpoints_used
    non_sys_indices = [
        i for i in range(len(messages))
        if messages[i].get("role") != "system"
    ]
    for idx in non_sys_indices[-remaining:]:
        _apply_cache_marker(messages[idx], marker)

    return messages
```

`_apply_cache_marker()` 负责处理不同 content 形态：字符串会升级成 text block，list content 就把标记挂到最后一个 block，tool/空 content 走 message 顶层标记。这样缓存断点总是落在一段 prefix 的末尾：**断点之前的内容都算可缓存前缀**。

为什么是 `system + 最后 3 条非 system`？system prompt 几乎每轮不变，是最稳定、最值得缓存的前缀；最后 3 条非 system 消息是滚动窗口，保证对话继续推进时，上一轮靠前的内容已经被写过 cache，下一轮就有机会按 cache read 价格命中。

### 核心代码 ③ Chain 统一注入 + 累计统计

注入由 chain 在 `_try_with_retry` 里做，每个 entry 用自己的 transport hook：

```python
call_kwargs = dict(kwargs)
if entry.model:
    call_kwargs["model"] = entry.model

if self.cache_enabled and "messages" in call_kwargs:
    call_kwargs["messages"] = entry.transport.apply_prompt_cache(
        call_kwargs["messages"], cache_ttl=self.cache_ttl,
    )

resp = entry.transport.call(entry.client, **call_kwargs)
self._accumulate_cache_stats(entry, resp)
```

这样 failover 切到不同家时，新 transport 自己决定怎么处理：Anthropic 显式打标记，ChatCompletions identity 直通。**这是 v0.19"chain 是统一调用入口"的复利。**

`Usage` 故意把 read 和 write 拆成两个字段：两者计费完全不同，合并就丢了"省了多少 vs 花了多少写入"的关键信息。命中率累计在 chain 上，`/transport` 命令展示——单次数字噪音太大，累计才稳定可读。

```python
def _accumulate_cache_stats(self, entry, resp):
    usage = resp.usage
    if usage is None:
        return
    cached = usage.cached_tokens or 0
    write = usage.cache_creation_tokens or 0
    uncached = max(0, (usage.prompt_tokens or 0) - cached - write)
    entry.cache_read_total += cached
    entry.cache_write_total += write
    entry.cache_uncached_total += uncached
```

**【遗留问题】**
默认 `PROMPT_CACHE_ENABLED=0` 不启用：短对话（<10 轮）开 cache 反而亏（write 贵 read 便宜，要过 break-even 轮数才划算），且 Anthropic 兼容代理不一定支持 cache_control。把开关交给用户显式选。

**【演示】**
开 `PROMPT_CACHE_ENABLED=1` 跑十几轮，`/transport` 看命中率从 0 爬上去；对比关闭时的 input token 计费。

**【过渡】**
Transport 这一幕证明了：**契约一旦立住，加模型、组故障链、上缓存，主循环几乎不动。** 接下来第四幕，一个关于"省上下文"的巧思——Skill 按需加载。
