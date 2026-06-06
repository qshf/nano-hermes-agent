"""V26.4: 代码级语音心跳 —— 长任务防"挂机"感知。

为什么需要（与 V26.3 的关系）
============================
V26.3 用 prompt 软指令命令模型「每 20 秒主动播一次语音心跳」。问题不是措辞弱，
而是**结构性做不到**：

- 模型在 ``terminal`` 工具的 ``subprocess.run`` 阻塞期间物理上无法插播——而这正是
  最像"挂机"的时刻；
- 模型在自己生成 token 期间也不能动作；
- 模型只在「轮与轮之间」有机会，且经常忘。

所以本档把「存活心跳」从模型手里拿走，交给 agent 主进程的一个后台守护线程：它直连
``nano_voice_kit`` 的 ``VoiceClient``（httpx → Runtime FastAPI），在主线程被工具卡住
的那几十秒里照常存活、照常报活。模型只保留**有语义**的播报（起手 / 阶段 / 风险 /
完成），那些需要理解任务才能产出，只能模型做。

设计要点
========
- **软依赖 + 全程 fail-safe**：``VoiceClient`` try-import；Runtime 启动探活 ``/health``
  不通 → 心跳整个不启。任何运行期异常（Runtime 中途挂）被循环 try 吃掉，记一次
  warning 后继续，绝不冒泡进 main loop、绝不反复刷错。
- **静默超时才播**：只有 ``agent_busy.is_set()`` **且** 距上次心跳超过阈值时才推
  一条 ``progress``。模型正常播报时，voice kit 的 progress 队列替换策略消化偶发重叠。
- **状态独立于 AgentRuntime**：心跳是父主进程独有、与 delegate 子 agent 无关，不进
  父子共享的 ``AgentRuntime``（避免污染 ``cancel_token`` / ``session_tokens`` 语义）。
- **线程信号清晰**：``agent_busy`` 是 main.py 传入的 ``threading.Event``；set 表示
  正在跑 tool loop，clear 表示回到 prompt。``last_beat`` 心跳线程自写、main loop
  轮始重置，竞争窗口最坏多/少播一拍，无害。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 心跳默认沉默阈值（秒）；env VOICE_HEARTBEAT_SECONDS 覆盖，0 / 负数 → 关闭心跳。
DEFAULT_HEARTBEAT_SECONDS = 25.0
# 后台线程轮询节拍（秒）—— 远小于阈值，保证超时后最多迟 1 拍触发。
_TICK_SECONDS = 1.0
# 存活心跳文案（固定、极短、无内容 —— 只为"别以为挂了"）。
_HEARTBEAT_TEXT = "还在处理，稍等"
_HEARTBEAT_INTENT = "progress"


def _resolve_threshold() -> float:
    """读 env ``VOICE_HEARTBEAT_SECONDS``；非法 / 缺省 → 默认值。

    返回 ``<= 0`` 表示「关闭心跳」，由 ``VoiceHeartbeat.start`` 据此短路不起线程。
    """
    raw = os.environ.get("VOICE_HEARTBEAT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_HEARTBEAT_SECONDS
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid VOICE_HEARTBEAT_SECONDS=%r, using default", raw)
        return DEFAULT_HEARTBEAT_SECONDS


def _make_default_client() -> Optional[Any]:
    """try-import ``nano_voice_kit`` 的 VoiceClient，失败返回 None（软依赖降级）。

    nano 主线对 voice kit 是软依赖：包没装 / 版本不兼容 → 心跳整个不启，主任务零
    影响。把 import 收在函数里（而非模块顶层），让没装 voice kit 的环境也能正常
    ``import agent.voice_heartbeat``（单测注入 fake client 时根本不碰真包）。
    """
    try:
        from nano_voice_kit.client import VoiceClient  # 延迟 import：软依赖
    except Exception as exc:  # noqa: BLE001 — 包缺失/导入错都按"不可用"降级
        logger.info("nano_voice_kit not importable, heartbeat disabled: %r", exc)
        return None
    return VoiceClient()


class VoiceHeartbeat:
    """后台守护线程：主线程被工具阻塞时，定时推一条存活语音，防"挂机"感知。

    用法（main.py）::

        hb = VoiceHeartbeat.create_if_available(agent_busy, client=...)
        if hb:
            hb.start()
        ...
        # tool loop 轮始：agent_busy.set() 之后
        hb.mark_active()      # 刷新基准时间戳，避免刚进 loop 立刻补播
        ...
        # 退出
        hb.stop()

    Parameters
    ----------
    agent_busy :
        main.py 持有的 ``threading.Event`` —— 整轮 tool loop（含 delegate 子 agent）
        期间为 set 状态（见 main.py V23.4）。心跳仅在 set 时触发。
    client :
        ``VoiceClient`` 实例（或 duck-typed：需有 ``speak_intent(intent, text)``）。
        ``None`` → 心跳不可用。单测注入 fake client 走这里。
    threshold_seconds :
        沉默多久后补播。``<= 0`` → 关闭。缺省读 env。
    """

    def __init__(
        self,
        agent_busy: threading.Event,
        *,
        client: Optional[Any],
        threshold_seconds: Optional[float] = None,
    ) -> None:
        self._agent_busy = agent_busy
        self._client = client
        self._threshold = (
            threshold_seconds if threshold_seconds is not None else _resolve_threshold()
        )
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 上次「任意心跳」时间戳 —— 心跳线程写、main loop 轮始 mark_active 重置。
        self._last_beat = time.monotonic()
        # 连续发送失败计数 —— 只在首次失败记 warning，避免 Runtime 挂掉后刷屏。
        self._warned_send_failure = False

    # ── 构造门控 ────────────────────────────────────────────────────────

    @classmethod
    def create_if_available(
        cls,
        agent_busy: threading.Event,
        *,
        client: Optional[Any] = None,
        threshold_seconds: Optional[float] = None,
        probe_health: bool = True,
    ) -> "Optional[VoiceHeartbeat]":
        """按可用性门控构造 —— 不可用时返回 None（不抛），调用方据此决定是否 start。

        门控顺序（任一不过即返回 None）：

        1. 阈值 ``<= 0`` → 用户显式关闭（env VOICE_HEARTBEAT_SECONDS=0）。
        2. ``client`` 不可用（未注入则 try-import VoiceClient，import 失败 → None）。
        3. ``probe_health`` 时 ``client.health()`` 不通 → Runtime 没起，不启。

        注意：voice-runtime skill 的 ``is_fully_available``（DASHSCOPE_API_KEY +
        terminal）门控由 **main.py** 在调用本方法前判断 —— 本类只管"客户端 +
        Runtime 这条链通不通"，不重复 skill 元信息逻辑。
        """
        threshold = (
            threshold_seconds if threshold_seconds is not None else _resolve_threshold()
        )
        if threshold <= 0:
            logger.info("voice heartbeat disabled (threshold<=0)")
            return None

        if client is None:
            client = _make_default_client()
        if client is None:
            return None

        if probe_health:
            try:
                client.health()
            except Exception as exc:  # noqa: BLE001 — Runtime 没起按"不可用"降级
                logger.info("voice Runtime health probe failed, heartbeat off: %r", exc)
                return None

        return cls(agent_busy, client=client, threshold_seconds=threshold)

    # ── 生命周期 ────────────────────────────────────────────────────────

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
        """main loop 轮始调用：刷新基准时间戳。

        让"刚进 tool loop"不会立刻补播 —— 心跳从进入那刻起重新计沉默时长。
        """
        self._last_beat = time.monotonic()

    def stop(self, join_timeout: float = 2.0) -> None:
        """通知线程退出并 join（幂等）。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
            self._thread = None

    # ── 线程主体 ────────────────────────────────────────────────────────

    def _should_beat(self, now: float) -> bool:
        """三条件全满足才补播：busy + 超过阈值。Runtime 可达性已在构造期探过。"""
        if not self._agent_busy.is_set():
            return False
        return (now - self._last_beat) >= self._threshold

    def _send_beat(self) -> None:
        """推一条存活 progress；失败不冒泡（只首次记 warning）。"""
        try:
            self._client.speak_intent(_HEARTBEAT_INTENT, _HEARTBEAT_TEXT)
            self._warned_send_failure = False
        except Exception as exc:  # noqa: BLE001 — Runtime 中途挂不应炸主任务
            if not self._warned_send_failure:
                logger.warning("voice heartbeat send failed: %r", exc)
                self._warned_send_failure = True

    def _run(self) -> None:
        """守护循环：每 tick 醒来检查，满足条件就补播并刷新基准。"""
        while not self._stop_event.is_set():
            # wait 既是节拍也是退出信号：stop() set 后立即返回 True 跳出。
            if self._stop_event.wait(_TICK_SECONDS):
                break
            now = time.monotonic()
            if self._should_beat(now):
                self._send_beat()
                self._last_beat = now
