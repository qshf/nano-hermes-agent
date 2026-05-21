"""V12 端到端体感测试 — 用真实 mock_memory_server 验证主循环不被 retain 阻塞。

对比指标：
- sync_turn() 调用本身的耗时（V11 = 整个 /retain 链路；V12 ≈ 0）
- writer 后台 drain 完整链路的耗时（不变 — 但已不阻塞主循环）
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.remote_semantic import RemoteSemanticProvider


def main() -> int:
    provider = RemoteSemanticProvider(
        base_url="http://127.0.0.1:8765",
        bank_id="v12_e2e_test",
        memory_mode="hybrid",
        auto_retain=True,
    )
    if not provider.is_available():
        print("FAIL: mock server not reachable at http://127.0.0.1:8765")
        return 1
    provider.initialize(session_id="v12_e2e")

    samples = [
        ("我叫李雷，在做 nano_hermes_agent 项目", "好的，已记住你叫李雷"),
        ("我用 Python 3.13", "Python 3.13 收到"),
        ("项目部署在 macOS 上", "已记录部署平台为 macOS"),
    ]

    enqueue_durations: list[float] = []
    for u, a in samples:
        t0 = time.monotonic()
        provider.sync_turn(u, a)
        enqueue_durations.append(time.monotonic() - t0)

    enqueue_total = sum(enqueue_durations) * 1000
    print(f"主循环 sync_turn × {len(samples)} 总耗时: {enqueue_total:.1f}ms "
          f"（人均 {enqueue_total/len(samples):.2f}ms — 仅入队）")

    print("等待后台 writer drain（这就是 V11 时阻塞主循环的部分）...")
    t0 = time.monotonic()
    provider.shutdown()
    drain_elapsed = (time.monotonic() - t0) * 1000
    print(f"后台 drain {len(samples)} 个 retain 耗时: {drain_elapsed:.0f}ms")
    print(f"\n体感对比:")
    print(f"  V11 主循环阻塞: {drain_elapsed:.0f}ms （retain 同步阻塞主循环）")
    print(f"  V12 主循环阻塞: {enqueue_total:.1f}ms （仅入队，HTTP 在后台）")
    print(f"  减少: {drain_elapsed - enqueue_total:.0f}ms ({(1 - enqueue_total/drain_elapsed)*100:.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
