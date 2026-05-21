"""Demo: queue + single writer thread + sentinel graceful shutdown.

Run:
    python scripts/demo_queue_writer_sentinel.py

This mirrors the idea used by RemoteSemanticProvider:
- The main thread only enqueues work, so it returns quickly.
- One background writer thread drains jobs in FIFO order.
- A unique sentinel object tells the writer to exit after queued jobs are done.
"""

from __future__ import annotations

import queue
import threading
import time
import atexit
from collections.abc import Callable


SENTINEL = object()


class AsyncWriter:
    def __init__(self, *, join_timeout: float = 5.0) -> None:
        self._queue: queue.Queue[Callable[[], None] | object] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._shutting_down = threading.Event()
        self._atexit_registered = False
        self._join_timeout = join_timeout

    def submit(self, name: str, seconds: float) -> None:
        """Create a slow job and enqueue it without running it here."""
        if self._shutting_down.is_set():
            print(f"[main] drop {name}: writer is shutting down")
            return

        def job() -> None:
            print(f"[writer] start {name}")
            time.sleep(seconds)
            print(f"[writer] done  {name}")

        self._ensure_writer()
        self._register_atexit()
        self._queue.put(job)
        print(f"[main] queued {name}")

    def shutdown(self) -> None:
        """Stop accepting new jobs, ask writer to exit, then wait for it."""
        if self._shutting_down.is_set():
            return
        self._shutting_down.set()

        if self._thread is not None and self._thread.is_alive():
            print("[main] send sentinel")
            self._queue.put(SENTINEL)
            self._thread.join(timeout=self._join_timeout)
            if self._thread.is_alive():
                print(f"[main] writer still busy after {self._join_timeout:.1f}s, give up waiting")
            else:
                print("[main] writer stopped")

    def _ensure_writer(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        self._thread = threading.Thread(
            target=self._writer_loop,
            name="demo-writer",
            daemon=True,
        )
        self._thread.start()
        print("[main] writer started")

    def _writer_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                if self._shutting_down.is_set():
                    return
                continue

            try:
                if item is SENTINEL:
                    print("[writer] got sentinel, exit")
                    return

                job = item
                try:
                    job()  # type: ignore[operator]
                except Exception as exc:
                    print(f"[writer] job failed: {exc}")
            finally:
                self._queue.task_done()

    def _register_atexit(self) -> None:
        if self._atexit_registered:
            return
        self._atexit_registered = True
        atexit.register(self._atexit_shutdown)

    def _atexit_shutdown(self) -> None:
        if self._shutting_down.is_set():
            return
        try:
            print("[atexit] process is exiting, try graceful shutdown")
            self.shutdown()
        except Exception as exc:
            print(f"[atexit] shutdown failed: {exc}")


def main() -> None:
    writer = AsyncWriter()

    try:
        start = time.monotonic()
        writer.submit("job-1", 0.8)
        writer.submit("job-2", 0.8)
        writer.submit("job-3", 0.8)
        elapsed = time.monotonic() - start

        print(f"[main] submit returned in {elapsed:.3f}s")
        print("[main] main thread can do other work now")
        time.sleep(0.2)
    except KeyboardInterrupt:
        print("[main] interrupted, try graceful shutdown")
    finally:
        print("[main] now shutdown and wait for queued jobs")
        writer.shutdown()



if __name__ == "__main__":
    main()
