"""V19 — TransportChain：主备 transport 故障切换 + 断路器 + jittered backoff。

教学定位
--------
V18 已经验证 ABC 抽象在两家家族（chat_completions / anthropic_messages）上
都能真跑通，但任一家挂了 agent 就死。V19 让两家组成**主备链**：

主家正常 → 直接返回
主家挂 → 切备家
备家也挂 → 抛出错误

三个核心机制（教学最小集，覆盖源项目 ``run_agent.py:1742-1764`` 的 fallback chain
+ ``error_classifier.py`` 的分类 + ``retry_utils.py`` 的 jittered backoff 三者
合并而成的最小可演示形态）：

1. **错误分类驱动决策** — ``classify_error`` 返回 ``RETRYABLE/FAILOVER/FATAL``，
   chain 不需要硬编码"看到 429 就切" — 分类器一处改全局生效。

2. **断路器自愈** — 一个 transport 连续失败 ``failure_threshold`` 次后被标记为
   "open"，``cooldown_seconds`` 内跳过；冷却期满自动尝试一次（半开探针），
   成功则关闭断路器（标准 circuit-breaker 三态：closed / open / half_open）。

3. **Jittered backoff** — RETRYABLE 错误按 ``base_delay * 2^attempt + uniform jitter``
   等待。jitter 防止多 session 同时重试形成 thundering herd（源项目踩过的坑）。

env 开关：
    TRANSPORT_CHAIN          逗号分隔的 api_mode 列表（默认仅取 TRANSPORT_MODE 单家）
                             例：``chat_completions,anthropic_messages``
    FAILOVER_FAILURE_THRESHOLD  连续失败几次后断路器打开（默认 3）
    FAILOVER_COOLDOWN_SECONDS   断路器冷却时间（默认 60s）
    FAILOVER_MAX_RETRIES        单 transport RETRYABLE 错误最大重试次数（默认 2）
    FAILOVER_BASE_DELAY         backoff 基数（默认 1.0s）

对应源项目（散落实现）:
- ``run_agent.py:1742-1764`` — fallback chain 初始化
- ``run_agent.py:1655-1697`` — fallback 激活逻辑
- ``agent/error_classifier.py`` — 错误分类（被 nano 简化为 3 个 action）
- ``agent/retry_utils.py:19-57`` — jittered_backoff
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from transports import get_transport
from transports.base import ProviderTransport
from transports.error_classifier import ClassifiedError, ErrorAction, classify_error
from transports.streaming import (
    EVENT_DONE,
    CancelToken,
    StreamCancelled,
    StreamIterator,
)
from transports.types import NormalizedResponse

logger = logging.getLogger(__name__)


# ── 断路器状态 ──────────────────────────────────────────────────────────


@dataclass
class _BreakerState:
    """单 transport 的断路器状态。

    closed (默认): 健康，正常调用
    open: 故障，跳过，直到 cooldown 过去
    half_open: 冷却完毕，允许一次探针调用 — 成功则 close，失败则重开
    """

    consecutive_failures: int = 0
    opened_at: float = 0.0  # 0 表示 closed；非 0 是打开时刻的 monotonic 秒
    last_reason: str = ""

    def is_open(self, now: float, cooldown_seconds: float) -> bool:
        if self.opened_at == 0.0:
            return False
        return (now - self.opened_at) < cooldown_seconds


# ── Transport entry ────────────────────────────────────────────────────


@dataclass
class _ChainEntry:
    """链中的一个 transport + 配套的客户端 + 模型 + 断路器状态 + cache 统计。

    ``model`` 是 V19.1 加入 — 每个 entry 携带自己的模型名（仿源项目
    ``run_agent.py:1742-1765`` 的 fallback chain 设计：每条 fallback entry 都是
    自包含的 ``{"provider": "...", "model": "..."}``，不依赖全局 MODEL）。
    None 表示"用调用方传入的 model 参数"，对应源项目 legacy single-dict 兼容路径。

    ``cache_*`` 是 V20 加入 — 累计 prompt cache 命中/写入 token，让 ``/transport``
    能直接展示命中率（cached / (cached + uncached)）。
    """

    api_mode: str
    transport: ProviderTransport
    client: Any
    model: Optional[str] = None
    breaker: _BreakerState = field(default_factory=_BreakerState)
    cache_read_total: int = 0
    cache_write_total: int = 0
    cache_uncached_total: int = 0  # 非 cache 输入 token，用于算命中率分母


# ── TransportChain ──────────────────────────────────────────────────────


class FailoverExhausted(RuntimeError):
    """链中所有 transport 都失败 — 上层应认为这次调用彻底失败。"""

    def __init__(self, attempts: list[ClassifiedError]) -> None:
        self.attempts = attempts
        msg = "; ".join(f"{e.reason}({e.status_code})" for e in attempts)
        super().__init__(f"All transports failed: {msg}")


class TransportChain:
    """按顺序尝试多个 transport，主家挂则切备家。

    单 transport 的链（``len(entries) == 1``）行为退化为"普通 transport.call() +
    重试"，向后兼容 V18。
    """

    def __init__(
        self,
        entries: list[_ChainEntry],
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        max_retries: int = 2,
        base_delay: float = 1.0,
        cache_enabled: bool = False,
        cache_ttl: str = "5m",
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if not entries:
            raise ValueError("TransportChain requires at least 1 entry")
        self.entries = entries
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.cache_enabled = cache_enabled
        self.cache_ttl = cache_ttl
        self._sleep = sleep_fn
        self._clock = clock_fn

    # ── 公共属性 ──────────────────────────────────────────────

    @property
    def primary(self) -> _ChainEntry:
        """链上第一个 entry — agent loop 用它做 messages 转换 / 工具 schema。"""
        return self.entries[0]

    @property
    def primary_transport(self) -> ProviderTransport:
        return self.primary.transport

    @property
    def primary_client(self) -> Any:
        return self.primary.client

    @property
    def primary_model(self) -> Optional[str]:
        """主家自带的模型名（链字符串内联了 ``api_mode:model`` 时非 None）。"""
        return self.primary.model

    # ── 主入口 ────────────────────────────────────────────────

    def call(self, client: Any = None, **kwargs) -> NormalizedResponse:
        """按链顺序尝试 transports，成功则返回，全失败抛 FailoverExhausted。

        签名兼容 ``ProviderTransport.call(client, **kwargs)`` — 调用方可以无差别
        传入 chain 或 transport。``client`` 参数被链忽略，因为每个 entry 自带
        client；保留位置参数让 ``ContextCompressor`` 这种存量调用点零改动。

        kwargs 透传给 ``transport.call()`` (model, messages, tools, ...)。
        """
        del client  # 链版本由 entry.client 承担调用，外部 client 不参与
        attempts: list[ClassifiedError] = []
        # V0.28.0: 与 attempts 平行收集每个 entry 的最后一次原始异常，
        # 链耗尽抛 FailoverExhausted 时挂最后一个作为 __cause__，保住 traceback 因果链。
        errors: list[BaseException] = []
        now = self._clock()

        for entry in self.entries:
            # 断路器打开且未冷却 → 跳过
            if entry.breaker.is_open(now, self.cooldown_seconds):
                logger.info(
                    "skip %s: breaker open (last=%s, %.1fs left)",
                    entry.api_mode,
                    entry.breaker.last_reason,
                    self.cooldown_seconds - (now - entry.breaker.opened_at),
                )
                continue

            # 半开探针：cooldown 过了但 opened_at != 0 — 尝试一次，成功则关闭
            half_open = entry.breaker.opened_at != 0.0

            result = self._try_with_retry(entry, kwargs, attempts, errors)
            if result is not None:
                # 成功 — 关闭断路器
                if half_open or entry.breaker.consecutive_failures > 0:
                    logger.info("breaker closed for %s", entry.api_mode)
                entry.breaker.consecutive_failures = 0
                entry.breaker.opened_at = 0.0
                return result

            # 失败 — 决策已写入 breaker，继续下一家
            now = self._clock()

        raise FailoverExhausted(attempts) from (errors[-1] if errors else None)

    # ── 流式入口 ──────────────────────────────────────────
    def stream_call(
        self,
        client: Any = None,
        cancel_token: Optional[CancelToken] = None,
        **kwargs,
    ) -> StreamIterator:
        """链版流式 — 在**首帧到达前**做故障切换；首帧后只负责吐事件。

        策略（仿源项目 ``run_agent.py:7966-8082`` 的 deltas_were_sent 哨兵）：
        - 主家流挂了但还没 yield 过任何事件 → 走下一家（用户体感是"没反应了一下"）
        - 主家挂了但已 yield 过 ≥1 个增量事件 → **不切家**，把异常往上抛（上层负责
          打印 "[failed mid-stream]"）。理由：切家会导致 token 重发 + 改变响应结构，
          UI 体感非常糟，比一次失败更差
        - ``StreamCancelled`` 不属于 transport 失败 — 直接透传，上层负责回退到 prompt
        - 单 entry RETRYABLE 流式不做内部重试（同上：会破坏已发出的 token 流）
          —— 与 ``_try_with_retry`` 的同步重试策略对齐"流式只切，不重试"

        cache 统计：done 事件携带的 ``NormalizedResponse`` 含完整 usage —
        与同步 ``call`` 调用相同的 ``_accumulate_cache_stats`` 路径。
        """
        del client
        attempts: list[ClassifiedError] = []
        # V0.28.0: 同 call()，平行留存最后一次原始异常用于挂 __cause__。
        errors: list[BaseException] = []
        now = self._clock()

        for entry in self.entries:
            if entry.breaker.is_open(now, self.cooldown_seconds):
                logger.info(
                    "skip %s: breaker open (last=%s, %.1fs left)",
                    entry.api_mode, entry.breaker.last_reason,
                    self.cooldown_seconds - (now - entry.breaker.opened_at),
                )
                continue

            half_open = entry.breaker.opened_at != 0.0
            call_kwargs = dict(kwargs)
            if entry.model:
                call_kwargs["model"] = entry.model
            if self.cache_enabled and "messages" in call_kwargs:
                call_kwargs["messages"] = entry.transport.apply_prompt_cache(
                    call_kwargs["messages"], cache_ttl=self.cache_ttl,
                )

            delivered = False
            try:
                gen = entry.transport.stream_call(
                    entry.client, cancel_token=cancel_token, **call_kwargs,
                )
                for ev in gen:
                    delivered = True
                    yield ev
                    if ev.type == EVENT_DONE and ev.response is not None:
                        self._accumulate_cache_stats(entry, ev.response)
                # 成功 — 关闭断路器并返回
                if half_open or entry.breaker.consecutive_failures > 0:
                    logger.info("breaker closed for %s", entry.api_mode)
                entry.breaker.consecutive_failures = 0
                entry.breaker.opened_at = 0.0
                return
            except StreamCancelled:
                # 用户取消 — 不计为失败，直接透传
                raise
            except Exception as exc:
                classified = classify_error(exc)
                attempts.append(classified)
                errors.append(exc)
                logger.warning(
                    "stream transport=%s failed: action=%s reason=%s status=%s delivered=%s",
                    entry.api_mode, classified.action.value,
                    classified.reason, classified.status_code, delivered,
                )
                if classified.action == ErrorAction.FATAL:
                    raise

                self._record_failure(entry, classified)

                if delivered:
                    # 已 yield 过事件 — 切家会重复 token，禁止
                    raise

                # 没 yield 过 — 切下一家
                now = self._clock()
                continue

        raise FailoverExhausted(attempts) from (errors[-1] if errors else None)

    # ── 单 transport 重试 ─────────────────────────────────────

    def _try_with_retry(
        self,
        entry: _ChainEntry,
        kwargs: dict,
        attempts: list[ClassifiedError],
        errors: list[BaseException],
    ) -> Optional[NormalizedResponse]:
        """对单个 entry 做 RETRYABLE 错误的内部重试。

        返回 NormalizedResponse 表示成功；返回 None 表示该 entry 已被否决（FAILOVER）
        或彻底失败（FATAL，FATAL 会直接 raise）。

        每个 entry 自带 ``model``，覆盖调用方传入的 model（源项目 fallback 链
        每条 entry 都自包含 model 的本地翻译）。entry.model 为 None 时回退到
        kwargs 里的全局 model。

        V20: cache_enabled 时通过 ``transport.apply_prompt_cache`` 注入 cache_control。
        每个 entry 用自己的 transport 注入 — Anthropic 显式打标记，OpenAI 兼容
        identity 直通。返回后从 ``NormalizedResponse.usage.cached_tokens`` 累计统计
        到 entry.cache_*_total。
        """
        # 按 entry 模型覆盖 kwargs（不 mutate 原 dict — 避免链上各 entry 互相污染）
        call_kwargs = dict(kwargs)
        if entry.model:
            call_kwargs["model"] = entry.model

        # cache 启用时让 transport 自己决定怎么标记（Anthropic 打 cache_control，
        # 其他家 identity）。注意 apply_prompt_cache 返回深拷贝，不污染上游 messages。
        if self.cache_enabled and "messages" in call_kwargs:
            call_kwargs["messages"] = entry.transport.apply_prompt_cache(
                call_kwargs["messages"], cache_ttl=self.cache_ttl,
            )

        for attempt in range(self.max_retries + 1):
            try:
                resp = entry.transport.call(entry.client, **call_kwargs)
                # 累计 cache 统计 — usage.cached_tokens 已被 transport 标准化
                self._accumulate_cache_stats(entry, resp)
                return resp
            except Exception as exc:
                classified = classify_error(exc)
                attempts.append(classified)
                errors.append(exc)
                logger.warning(
                    "transport=%s attempt=%d failed: action=%s reason=%s status=%s",
                    entry.api_mode, attempt, classified.action.value,
                    classified.reason, classified.status_code,
                )

                if classified.action == ErrorAction.FATAL:
                    # 用户/输入问题 — 切了也是错，直接抛出
                    raise

                if classified.action == ErrorAction.RETRYABLE and attempt < self.max_retries:
                    delay = self._jittered_backoff(attempt + 1)
                    logger.info("retry %s after %.2fs", entry.api_mode, delay)
                    self._sleep(delay)
                    continue

                # FAILOVER 或 RETRYABLE 用尽 — 这家算 1 次失败
                self._record_failure(entry, classified)
                return None

        # 不会走到这里 — for 循环只有 return 或 continue 出口
        return None

    # ── 失败记账 ──────────────────────────────────────────────

    def _record_failure(self, entry: _ChainEntry, classified: ClassifiedError) -> None:
        entry.breaker.consecutive_failures += 1
        entry.breaker.last_reason = classified.reason
        if entry.breaker.consecutive_failures >= self.failure_threshold:
            entry.breaker.opened_at = self._clock()
            logger.warning(
                "breaker opened for %s (failures=%d, reason=%s)",
                entry.api_mode, entry.breaker.consecutive_failures, classified.reason,
            )

    # ── cache 记账 ────────────────────────────────────────

    def _accumulate_cache_stats(
        self, entry: _ChainEntry, resp: NormalizedResponse,
    ) -> None:
        """累计 prompt cache 命中/写入到 entry。

        ``Usage.cached_tokens`` / ``Usage.cache_creation_tokens`` 已被 transport
        标准化（Anthropic 区分 read/write，OpenAI 兼容只有 read，write=0）。

        命中率分母 = uncached input = ``prompt_tokens - cached - creation``；
        ``prompt_tokens`` 在 Anthropic 侧是 ``input + read + write`` 的总和。
        """
        usage = resp.usage
        if usage is None:
            return
        cached = usage.cached_tokens or 0
        write = usage.cache_creation_tokens or 0
        uncached = max(0, (usage.prompt_tokens or 0) - cached - write)
        entry.cache_read_total += cached
        entry.cache_write_total += write
        entry.cache_uncached_total += uncached

    # ── Jittered backoff ──────────────────────────────────────

    def _jittered_backoff(self, attempt: int) -> float:
        """``base * 2^(attempt-1) + uniform(0, 0.5*base*2^(attempt-1))``。

        jitter 防止多 session 重试同步形成 thundering herd（源项目踩过）。
        """
        exponent = max(0, attempt - 1)
        delay = min(self.base_delay * (2 ** exponent), 60.0)
        jitter = random.uniform(0, 0.5 * delay)
        return delay + jitter

    # ── 健康检查窥视（教学/调试用） ───────────────────────────

    def status(self) -> list[dict]:
        """返回每个 entry 的健康状态快照（供 ``/transport`` 命令展示）。

        V20: 增加 cache_read / cache_write / cache_hit_rate 字段。
        """
        now = self._clock()
        result = []
        for e in self.entries:
            is_open = e.breaker.is_open(now, self.cooldown_seconds)
            total_input = e.cache_read_total + e.cache_uncached_total
            hit_rate = (e.cache_read_total / total_input) if total_input > 0 else 0.0
            result.append({
                "api_mode": e.api_mode,
                "model": e.model,
                "state": "open" if is_open else ("half_open" if e.breaker.opened_at else "closed"),
                "consecutive_failures": e.breaker.consecutive_failures,
                "last_reason": e.breaker.last_reason,
                "cooldown_left": (
                    max(0.0, self.cooldown_seconds - (now - e.breaker.opened_at))
                    if e.breaker.opened_at else 0.0
                ),
                "cache_read": e.cache_read_total,
                "cache_write": e.cache_write_total,
                "cache_uncached": e.cache_uncached_total,
                "cache_hit_rate": hit_rate,
            })
        return result


# ── 工厂 ────────────────────────────────────────────────────────────────


def build_chain_from_env(
    chain_env: str,
    client_factory: Callable[[str], Any],
    *,
    failure_threshold: int = 3,
    cooldown_seconds: float = 60.0,
    max_retries: int = 2,
    base_delay: float = 1.0,
    cache_enabled: bool = False,
    cache_ttl: str = "5m",
) -> TransportChain:
    """从字符串构建链。

    每段语法：``api_mode[:model]``，逗号分隔。例：
        ``"chat_completions:deepseek-chat,anthropic_messages:qwen3.6-plus"``
        ``"chat_completions"``  — 无内联 model，运行时 fallback 到全局 ``MODEL`` env

    设计来自源项目 ``run_agent.py:1742-1765`` 的 fallback chain — 每条 entry
    自包含 ``{provider, model}`` 不依赖全局变量。本地翻译用 ``:`` 分隔保持
    单 env 字符串。

    ``client_factory`` 是 ``transports.client_factory.make_llm_client``，
    注入是为了让测试能传一个 fake factory。
    """
    segments = [s.strip() for s in chain_env.split(",") if s.strip()]
    if not segments:
        raise ValueError("TRANSPORT_CHAIN is empty")

    entries: list[_ChainEntry] = []
    for seg in segments:
        if ":" in seg:
            mode, _, model = seg.partition(":")
            mode = mode.strip()
            model = model.strip() or None
        else:
            mode = seg
            model = None

        transport = get_transport(mode)
        if transport is None:
            raise RuntimeError(f"Transport not registered: {mode!r}")
        client = client_factory(mode)
        entries.append(_ChainEntry(api_mode=mode, transport=transport, client=client, model=model))

    return TransportChain(
        entries,
        failure_threshold=failure_threshold,
        cooldown_seconds=cooldown_seconds,
        max_retries=max_retries,
        base_delay=base_delay,
        cache_enabled=cache_enabled,
        cache_ttl=cache_ttl,
    )
