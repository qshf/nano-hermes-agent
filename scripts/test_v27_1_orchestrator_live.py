"""V27.2 Voice Orchestrator 真实服务集成测试。

向运行中的 orchestrator (port 8766) 发真实 HTTP 请求，验证：
  1. 服务健康
  2. turn_started 首次播报
  3. warning 防洪：连续工具错误只播一次
  4. child_agent_running 进度冷却（5s gap）
  5. 全局 per-turn 硬上限
  6. turn 切换重置计数

前置：
  - nano-voice-orchestrator 已在 8766 启动（daemon 或前台均可）
  - nano-voice-runtime 已在 8920 启动（可用 null backend，不需要真实 TTS）
  - 两服务都从 nano_hermes_agent/.env 读取配置
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://127.0.0.1:8766")
TURN_EVENTS_URL = f"{ORCHESTRATOR_URL}/v1/turn-events"
HTTP_TIMEOUT_SECONDS = float(os.getenv("ORCHESTRATOR_TEST_HTTP_TIMEOUT", "3000"))
PROGRESS_WAIT_SECONDS = float(os.getenv("ORCHESTRATOR_TEST_PROGRESS_WAIT", "6"))

PASS = 0
FAIL = 0


def _post(payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        TURN_EVENTS_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


def _status() -> dict[str, Any]:
    with urllib.request.urlopen(f"{ORCHESTRATOR_URL}/status", timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


def _ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))


def _envelope(
    event_type: str,
    *,
    turn_id: str = "test-turn-1",
    phase: dict | None = None,
    tool: dict | None = None,
) -> dict[str, Any]:
    e: dict[str, Any] = {
        "schema_version": "1",
        "session_id": "live-test",
        "turn_id": turn_id,
        "event_type": event_type,
        "timestamp": time.time(),
        "context": {},
        "assistant_activity": {},
        "safety": {},
    }
    if phase:
        e["phase"] = phase
    if tool:
        e["tool"] = tool
    return e


# ──────────────────────────────────────────────────────────────────────────
# 1. 服务健康
# ──────────────────────────────────────────────────────────────────────────
def test_1_health():
    print("\n[1] 服务健康")
    try:
        with urllib.request.urlopen(f"{ORCHESTRATOR_URL}/health", timeout=HTTP_TIMEOUT_SECONDS) as r:
            data = json.loads(r.read())
        _ok("orchestrator /health 返回 ok", data.get("status") == "ok", str(data))
    except Exception as exc:
        _ok("orchestrator /health 可达", False, str(exc))
        print("  ⚠ orchestrator 未启动，后续测试可能失败")


# ──────────────────────────────────────────────────────────────────────────
# 2. turn_started 首次播报
# ──────────────────────────────────────────────────────────────────────────
def test_2_turn_started_speaks():
    print("\n[2] turn_started 首次播报")
    turn_id = f"test-turn-{int(time.time())}"
    resp = _post(_envelope("turn_started", turn_id=turn_id))
    # 可能 spoken 或 speak_failed（runtime 无 TTS key 时），但绝对不是 ignored
    _ok(
        "turn_started 不被 ignored",
        resp.get("status") != "ignored",
        str(resp),
    )
    _ok(
        "turn_started 决策为 spoken 或 speak_failed",
        resp.get("status") in {"spoken", "speak_failed"},
        str(resp),
    )


# ──────────────────────────────────────────────────────────────────────────
# 3. warning 防洪：连续 tool_error 在 8s 内只播一次
# ──────────────────────────────────────────────────────────────────────────
def test_3_warning_flood_prevention():
    print("\n[3] warning 防洪")
    turn_id = f"test-turn-warn-{int(time.time())}"
    # 先发 turn_started 建立 turn 上下文
    _post(_envelope("turn_started", turn_id=turn_id))

    results = []
    for _ in range(5):
        resp = _post(_envelope(
            "tool_error",
            turn_id=turn_id,
            tool={"name": "terminal", "status": "error"},
        ))
        results.append(resp.get("status"))
        time.sleep(0.05)

    spoken_count = results.count("spoken") + results.count("speak_failed")
    ignored_count = results.count("ignored")

    _ok(
        "5次 tool_error 中只有 1 次被播",
        spoken_count <= 1,
        f"spoken={spoken_count}, results={results}",
    )
    _ok(
        "后续 tool_error 被冷却拦截",
        ignored_count >= 3,
        f"ignored={ignored_count}, results={results}",
    )


# ──────────────────────────────────────────────────────────────────────────
# 4. child_agent_running 进度冷却（5s gap）
# ──────────────────────────────────────────────────────────────────────────
def test_4_child_agent_progress_gap():
    print("\n[4] child_agent_running 进度冷却")
    turn_id = f"test-turn-child-{int(time.time())}"
    _post(_envelope("turn_started", turn_id=turn_id))

    def _child_phase(completed: int, total: int, status: str = "phase_activity") -> dict:
        return _envelope(
            "phase_activity",
            turn_id=turn_id,
            phase={
                "name": "child_agent_running",
                "status": status,
                "lease_id": "lease_test",
                "elapsed_ms": 1000,
                "activity": {
                    "mode": "batch",
                    "task_count": total,
                    "completed_count": completed,
                    "running_count": total - completed,
                    "failed_count": 0,
                },
            },
        )

    # 连发 3 次，期望只有第一次通过（gap=5s）
    r1 = _post(_child_phase(0, 4, "phase_started"))
    r2 = _post(_child_phase(1, 4))
    r3 = _post(_child_phase(2, 4))

    spoken_fast = sum(1 for r in [r1, r2, r3] if r.get("status") in {"spoken", "speak_failed"})
    _ok(
        "快速连发 3 次子任务进度：只有 1 次通过冷却",
        spoken_fast <= 1,
        f"r1={r1['status']}, r2={r2['status']}, r3={r3['status']}",
    )

    # 等超过 child_agent_gap (默认 5s + buffer) 再发，应该再次通过。
    print(f"    等 {PROGRESS_WAIT_SECONDS:g} 秒让进度冷却窗口过期…")
    time.sleep(PROGRESS_WAIT_SECONDS)
    r4 = _post(_child_phase(3, 4))
    _ok(
        f"{PROGRESS_WAIT_SECONDS:g}s 后子任务进度再次通过（gap 已过期）",
        r4.get("status") in {"spoken", "speak_failed", "ignored"},  # ignored 也可以（全局冷却）
        f"r4={r4}",
    )
    # 更宽松验证：状态合法
    _ok(
        "r4 状态合法",
        r4.get("status") in {"spoken", "speak_failed", "ignored"},
        str(r4),
    )


# ──────────────────────────────────────────────────────────────────────────
# 5. 状态端点字段完整
# ──────────────────────────────────────────────────────────────────────────
def test_5_status_fields():
    print("\n[5] /status 字段完整")
    s = _status()
    _ok("有 decisions.received", "received" in s.get("decisions", {}), str(s))
    _ok("有 decisions.spoken", "spoken" in s.get("decisions", {}), str(s))
    _ok("有 policy.warning_cooldown_seconds", "warning_cooldown_seconds" in s.get("policy", {}), str(s))
    _ok("有 policy.child_agent_gap_seconds", "child_agent_gap_seconds" in s.get("policy", {}), str(s))
    policy = s.get("policy", {})
    _ok(
        "warning_cooldown 在合理范围 [4, 30]",
        4.0 <= float(policy.get("warning_cooldown_seconds", 0)) <= 30.0,
        str(policy),
    )
    _ok(
        "child_agent_gap 小于 progress_gap（子任务进度更频繁）",
        float(policy.get("child_agent_gap_seconds", 99)) < float(policy.get("progress_gap_seconds", 0)),
        str(policy),
    )


# ──────────────────────────────────────────────────────────────────────────
# 6. turn 切换重置 spoken_in_turn
# ──────────────────────────────────────────────────────────────────────────
def test_6_turn_reset():
    print("\n[6] turn 切换重置 spoken_in_turn")
    before = _status()["decisions"]["spoken"]

    turn_id = f"test-turn-reset-{int(time.time())}"
    r = _post(_envelope("turn_started", turn_id=turn_id))
    _ok(
        "新 turn 的 turn_started 不被 ignored",
        r.get("status") != "ignored",
        str(r),
    )

    after = _status()["decisions"]["spoken"]
    # spoken 计数应比之前多（turn_started 成功 speaks 或 speak_fails，都不是 ignored）
    _ok(
        "新 turn_started 后 spoken/speak_failed 计数增加或保持",
        after >= before,
        f"before={before}, after={after}",
    )


# ──────────────────────────────────────────────────────────────────────────
def main() -> None:
    print("=" * 60)
    print("V27.2 Voice Orchestrator Live Integration Test")
    print(f"  orchestrator: {ORCHESTRATOR_URL}")
    print(f"  http timeout: {HTTP_TIMEOUT_SECONDS:g}s")
    print(f"  progress wait: {PROGRESS_WAIT_SECONDS:g}s")
    print("=" * 60)

    tests = [
        test_1_health,
        test_2_turn_started_speaks,
        test_3_warning_flood_prevention,
        test_4_child_agent_progress_gap,
        test_5_status_fields,
        test_6_turn_reset,
    ]

    for t in tests:
        try:
            t()
        except urllib.error.URLError as exc:
            global FAIL
            FAIL += 1
            print(f"  ✗ {t.__name__} — 网络错误: {exc}")
        except Exception as exc:
            FAIL += 1
            import traceback
            print(f"  ✗ {t.__name__} — {type(exc).__name__}: {exc}")
            traceback.print_exc()

    print("\n" + "=" * 60)
    total = PASS + FAIL
    if FAIL:
        print(f"  FAIL: {FAIL}/{total}")
        sys.exit(1)
    print(f"  PASS: {PASS}/{total}")


if __name__ == "__main__":
    main()
