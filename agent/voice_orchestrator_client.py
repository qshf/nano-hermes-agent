"""V27.1 — fail-safe client/sink for an external Voice Orchestrator.

The host sends bounded facts. The orchestrator owns scheduling, speech counts,
phrase planning, and calls to nano_voice_kit.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import urllib.error
import urllib.request
from typing import Any, Protocol

from agent.env import env_bool, env_float, env_int
from agent.turn_events import TurnEventEnvelope

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 0.5
DEFAULT_QUEUE_SIZE = 128


class VoiceOrchestratorClient(Protocol):
    def submit(self, envelope: dict[str, Any]) -> None:
        """Submit one envelope to the external orchestrator."""


class HttpVoiceOrchestratorClient:
    """Tiny stdlib HTTP client: POST one JSON envelope and ignore response body."""

    def __init__(self, url: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.url = url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def submit(self, envelope: dict[str, Any]) -> None:
        body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:  # noqa: S310 - configured local/owned URL
            resp.read(0)


class RecordingVoiceOrchestratorClient:
    """Test helper that stores envelopes in memory."""

    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.envelopes: list[dict[str, Any]] = []

    def submit(self, envelope: dict[str, Any]) -> None:
        if self.raises:
            raise RuntimeError("orchestrator unavailable")
        self.envelopes.append(envelope)


class VoiceEventSink:
    """Queue-backed sender that never lets voice side-channel failures escape."""

    def __init__(
        self,
        client: VoiceOrchestratorClient,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        async_send: bool = True,
    ) -> None:
        self._client = client
        self._async_send = async_send
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=max(1, queue_size))
        self._thread: threading.Thread | None = None
        self._warned = False

    @classmethod
    def create_from_env(cls) -> "VoiceEventSink | None":
        if not env_bool("VOICE_ORCHESTRATOR_ENABLED", False):
            return None
        url = os.environ.get("VOICE_ORCHESTRATOR_URL", "").strip()
        if not url:
            log.warning("VOICE_ORCHESTRATOR_ENABLED=1 but VOICE_ORCHESTRATOR_URL is empty")
            return None
        timeout = env_float("VOICE_ORCHESTRATOR_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
        queue_size = env_int("VOICE_ORCHESTRATOR_QUEUE_SIZE", DEFAULT_QUEUE_SIZE)
        return cls(HttpVoiceOrchestratorClient(url, timeout_seconds=timeout), queue_size=queue_size)

    def start(self) -> None:
        if not self._async_send:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="voice-orchestrator-sink", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._async_send:
            return
        if self._thread is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=1.0)
        self._thread = None

    def submit(self, envelope: TurnEventEnvelope | dict[str, Any]) -> None:
        """入口 submit：主线程调用，只入队（async）或就地 _safe_submit（sync）。

        注意区分两个 submit：本方法是 sink 的入口，负责把 envelope 交给队列、
        立刻返回、绝不阻塞主流程；真正发出网络请求的是 _safe_submit 里调用的
        ``self._client.submit``（出口 submit），跑在后台 daemon 线程。
        """
        data = envelope.to_dict() if hasattr(envelope, "to_dict") else dict(envelope)
        if not self._async_send:
            self._safe_submit(data)
            return
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            return

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._safe_submit(item)
            finally:
                self._queue.task_done()

    def _safe_submit(self, envelope: dict[str, Any]) -> None:
        # 出口 submit：self._client.submit 是真正发 HTTP 的地方（与入口 submit 区分）。
        # 这里把所有异常吞掉——语音是 side channel，失败绝不能影响主流程。
        try:
            self._client.submit(envelope)
            self._warned = False
        except (OSError, urllib.error.URLError, RuntimeError, ValueError) as exc:
            if not self._warned:
                log.warning("voice orchestrator submit failed: %r", exc)
                self._warned = True
        except Exception as exc:  # noqa: BLE001 - side channel must not affect main flow
            if not self._warned:
                log.warning("voice orchestrator submit failed: %r", exc)
                self._warned = True
