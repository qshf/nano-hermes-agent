"""V25.0 trajectory + redact 行为验证脚本。

验证 10 个关键不变量（行为契约，非实现细节）：
 1. to_sharegpt 形态：首条 system，user→human，assistant→gpt，连续 tool 合并成一条
 2. <think> 包裹：assistant 的 reasoning_content 进 <think>；无 reasoning 也补空 <think>
 3. tool_call 拍平：assistant.tool_calls → <tool_call>{name,arguments}</tool_call> XML，
    arguments 反序列化成对象（非转义字符串），且不带 tool_call_id（决策 1）
 4. has_incomplete_think：<think> 无闭合 → True；闭合或无 think → False
 5. completed 分流：completed=True 且不残缺 → *_samples.jsonl；
    残缺或 completed=False → *_failed.jsonl（决策：截断 think 不污染训练集）
 6. redact 脱敏：sk-* / Bearter 头 / KEY=value / 私钥块 / DB 连接串 都被掩码，
    且 save_trajectory 落盘内容已脱敏（决策 3）
 7. redact 默认开 + import 快照：_ENABLED 反映 import 时的 env，运行期改 env 无效
 8. mask 策略：短 token(<18) 全掩 ***，长 token 留首6末4
 9. :none: 关闭落盘：TRAJECTORY_DIR=:none: → save 返回 None，不建文件
10. flush_session_trajectory：空 messages / 纯 system → None（无训练价值不落）；
    含 human/gpt → 落盘且 timestamp/model/completed 外层字段齐全

零外部依赖：用 tmp 目录，秒级完成，不需要 pytest。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.trajectory import (
    to_sharegpt,
    has_incomplete_think,
    save_trajectory,
    flush_session_trajectory,
)
from agent.redact import redact, mask

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        print(f"  ✗ {name}  {detail}")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ── 不变量 1-3: to_sharegpt 形态 + <think> + tool_call 拍平 ──────────────
def test_to_sharegpt_shape() -> None:
    print("[1] to_sharegpt 形态 + 连续 tool 合并")
    messages = [
        {"role": "system", "content": "ignored sys"},
        {"role": "user", "content": "查一下天气"},
        {"role": "assistant", "content": "", "reasoning_content": "我该调工具",
         "tool_calls": [
             {"id": "c1", "function": {"name": "weather", "arguments": '{"city":"北京"}'}},
         ]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"output":"晴"}'},
        {"role": "tool", "tool_call_id": "c2", "content": '{"output":"25C"}'},
        {"role": "assistant", "content": "北京晴 25 度"},
    ]
    convs = to_sharegpt(messages)
    froms = [c["from"] for c in convs]
    check("首条是 system", convs[0]["from"] == "system", froms)
    check("user→human", froms[1] == "human", froms)
    check("assistant→gpt", froms[2] == "gpt", froms)
    check("连续两条 tool 合并成一条", froms.count("tool") == 1 and froms[3] == "tool", froms)
    check("末条 assistant→gpt", froms[-1] == "gpt", froms)
    check("非首 system 角色被忽略(只有 1 条 system)", froms.count("system") == 1, froms)


def test_think_wrap() -> None:
    print("[2] <think> 包裹")
    convs = to_sharegpt([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "答", "reasoning_content": "想了想"},
    ])
    gpt = next(c for c in convs if c["from"] == "gpt")
    check("reasoning 进 <think>", "<think>\n想了想\n</think>" in gpt["value"], gpt["value"])
    # 无 reasoning 也补空 think
    convs2 = to_sharegpt([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "答"},
    ])
    gpt2 = next(c for c in convs2 if c["from"] == "gpt")
    check("无 reasoning 也带 <think>", "<think>" in gpt2["value"] and "</think>" in gpt2["value"], gpt2["value"])


def test_tool_call_flatten() -> None:
    print("[3] tool_call 拍平成 XML + 反序列化 args + 丢 tool_call_id")
    convs = to_sharegpt([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "abc", "function": {"name": "f", "arguments": '{"k":"v"}'}}]},
    ])
    gpt = next(c for c in convs if c["from"] == "gpt")
    check("含 <tool_call> 标签", "<tool_call>" in gpt["value"], gpt["value"])
    check("args 反序列化成对象", '"arguments": {"k": "v"}' in gpt["value"], gpt["value"])
    check("不带 tool_call_id", "abc" not in gpt["value"], gpt["value"])


# ── 不变量 4: has_incomplete_think ───────────────────────────────────────
def test_incomplete_think() -> None:
    print("[4] has_incomplete_think")
    check("开标无闭合→True",
          has_incomplete_think([{"from": "gpt", "value": "<think>\n半截"}]) is True)
    check("闭合→False",
          has_incomplete_think([{"from": "gpt", "value": "<think>\nok\n</think>x"}]) is False)
    check("无 think→False",
          has_incomplete_think([{"from": "human", "value": "纯文本"}]) is False)


# ── 不变量 5: completed 分流 ─────────────────────────────────────────────
def test_completed_routing() -> None:
    print("[5] completed 分流 samples vs failed")
    with tempfile.TemporaryDirectory() as d:
        good = [{"from": "human", "value": "q"}, {"from": "gpt", "value": "<think>\n</think>a"}]
        p_ok = save_trajectory(good, model="m", completed=True, timestamp="T", out_dir=d, filename_stem="s")
        check("completed+完整→_samples", p_ok.name == "s_samples.jsonl", p_ok)

        bad = [{"from": "gpt", "value": "<think>\n截断"}]
        p_bad = save_trajectory(bad, model="m", completed=True, timestamp="T", out_dir=d, filename_stem="s")
        check("残缺 think→_failed", p_bad.name == "s_failed.jsonl", p_bad)

        p_inc = save_trajectory(good, model="m", completed=False, timestamp="T", out_dir=d, filename_stem="s")
        check("completed=False→_failed", p_inc.name == "s_failed.jsonl", p_inc)


# ── 不变量 6: redact 脱敏 + 落盘内容已脱敏 ───────────────────────────────
def test_redact_patterns() -> None:
    print("[6] redact 脱敏各 pattern")
    cases = {
        "sk-proj-abcdefghij1234567890": "sk-",
        "Authorization: Bearer eyJhbGciOiJIUzI1Nitoken12345": "Bearer ",
        "OPENAI_API_KEY=sk-secretvalue1234567890": "OPENAI_API_KEY=",
        "postgresql://nano:supersecretpw@host:5432/db": "postgresql://nano:",
    }
    for raw, keep_prefix in cases.items():
        out = redact(raw)
        check(f"脱敏 {keep_prefix.strip()}", "***" in out or "..." in out, out)
        check(f"  保留语境前缀 {keep_prefix!r}", keep_prefix in out, out)
    pk = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    check("私钥块整体替换", redact(pk) == "[REDACTED PRIVATE KEY]", redact(pk))


def test_save_redacts() -> None:
    print("[6b] save_trajectory 落盘内容已脱敏")
    with tempfile.TemporaryDirectory() as d:
        convs = [{"from": "human", "value": "key is sk-proj-abcdefghij1234567890"},
                 {"from": "gpt", "value": "<think>\n</think>ok"}]
        p = save_trajectory(convs, model="m", completed=True, timestamp="T", out_dir=d, filename_stem="s")
        raw_text = p.read_text(encoding="utf-8")
        check("落盘不含原始密钥", "sk-proj-abcdefghij1234567890" not in raw_text, raw_text[:120])


# ── 不变量 7: redact 默认开 + import 快照 ───────────────────────────────
def test_redact_import_snapshot() -> None:
    print("[7] redact 默认开 + import 时快照")
    import agent.redact as rd
    check("默认 _ENABLED=True", rd._ENABLED is True)
    os.environ["NANO_REDACT_SECRETS"] = "0"
    try:
        check("运行期改 env 不影响已 import 的 _ENABLED", rd._ENABLED is True)
        check("redact 仍生效(快照未变)", "***" in rd.redact("sk-proj-abcdefghij1234567890")
              or "..." in rd.redact("sk-proj-abcdefghij1234567890"))
    finally:
        os.environ.pop("NANO_REDACT_SECRETS", None)


# ── 不变量 8: mask 策略 ──────────────────────────────────────────────────
def test_mask_strategy() -> None:
    print("[8] mask 短全掩 / 长留首尾")
    check("短 token(<18) 全掩", mask("short123") == "***", mask("short123"))
    long = "sk-proj-abcdefghij1234567890"
    m = mask(long)
    check("长 token 留首6", m.startswith(long[:6]), m)
    check("长 token 留末4", m.endswith(long[-4:]), m)
    check("长 token 中段省略", "..." in m, m)


# ── 不变量 9: :none: 关闭落盘 ────────────────────────────────────────────
def test_none_disables() -> None:
    print("[9] TRAJECTORY_DIR=:none: 关闭落盘")
    convs = [{"from": "human", "value": "q"}, {"from": "gpt", "value": "<think>\n</think>a"}]
    p = save_trajectory(convs, model="m", completed=True, timestamp="T", out_dir=":none:", filename_stem="s")
    check(":none: → 返回 None 不落盘", p is None, p)


# ── 不变量 10: flush_session_trajectory ──────────────────────────────────
def test_flush_wrapper() -> None:
    print("[10] flush_session_trajectory 空/纯system跳过 + 外层字段齐全")
    check("空 messages → None", flush_session_trajectory([], model="m", completed=True) is None)
    # 纯 system（无 user/assistant）→ to_sharegpt 只剩 system 一条 → 跳过
    only_sys = flush_session_trajectory([{"role": "system", "content": "x"}], model="m", completed=True)
    check("纯 system → None", only_sys is None, only_sys)
    with tempfile.TemporaryDirectory() as d:
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
        p = flush_session_trajectory(msgs, model="deepseek", completed=True, filename_stem="sess1", out_dir=d)
        check("含 human/gpt → 落盘", p is not None and p.exists(), p)
        entry = _read_jsonl(p)[0]
        check("外层有 conversations", "conversations" in entry)
        check("外层有 timestamp", bool(entry.get("timestamp")))
        check("外层 model 正确", entry.get("model") == "deepseek", entry.get("model"))
        check("外层 completed=True", entry.get("completed") is True)


def main() -> int:
    for fn in [
        test_to_sharegpt_shape, test_think_wrap, test_tool_call_flatten,
        test_incomplete_think, test_completed_routing, test_redact_patterns,
        test_save_redacts, test_redact_import_snapshot, test_mask_strategy,
        test_none_disables, test_flush_wrapper,
    ]:
        fn()
    print(f"\n{'='*50}\n  passed={_passed}  failed={_failed}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
