"""V15.1 上下文压缩边界修复 — 不变量验证。

修复两个问题：
  P0  _find_tail_boundary 兜底语义 — 当 token 预算大到能覆盖整个会话时，
      旧实现 cut_idx = head_end + 1 让 middle 只剩 1 条假压缩，触发
      anti-thrashing；新实现退化为最大化压缩 cut_idx = n - min_tail。
  P1  _align_boundary 方向 — 旧实现碰到 tool 消息往后走（cut_idx += 1）
      可能拆分 tool_call/result 群；新实现往前对齐到父 assistant 之前，
      整组进 middle 摘要，避免 sanitize 阶段删孤立 result。

四个不变量：
  1. 预算覆盖全会话时，tail 长度 == min_tail（最大化压缩）
  2. tool_call + result 群完整 — 不被切到两侧
  3. 真实场景：tail_start = 4, n=106 的"假压缩" bug 不再复现
  4. V15 原有不变量 — tail 不落在 tool 消息上（回归）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from context_compressor import ContextCompressor


def _make_messages(n: int, char_per_msg: int = 200) -> list[dict]:
    msgs = [{"role": "system", "content": "You are a helpful assistant."}]
    for i in range(1, n):
        role = "user" if i % 2 == 1 else "assistant"
        msgs.append({"role": role, "content": f"Message {i}: " + "x" * char_per_msg})
    return msgs


# ─── Tests ───


def test_1_oversized_budget_falls_back_to_max_compression():
    """预算覆盖全会话时退化为最大化压缩 — middle 拿到所有可摘要消息。

    旧 bug：cut_idx 走到 head_end，兜底 +1 后 middle 只剩 1 条。
    """
    # tail_token_budget 远大于会话总 tokens — 循环会一路走到 head_end
    compressor = ContextCompressor(
        context_window=200000,
        protect_first_n=3,
        tail_token_budget=100000,
    )

    n = 106
    messages = _make_messages(n, char_per_msg=50)  # 总 token 远小于 budget
    head_end = 3

    tail_start = compressor._find_tail_boundary(messages, head_end=head_end)

    middle_size = tail_start - head_end
    tail_size = n - tail_start

    assert middle_size > 50, (
        f"预算超大兜底失败：middle 只剩 {middle_size} 条（应 > 50）。"
        f"tail_start={tail_start}, head_end={head_end}, n={n}"
    )
    assert tail_size == 3, f"最大化压缩时 tail 应为 min_tail=3，实测 {tail_size}"

    print("  [PASS] test_1_oversized_budget_falls_back_to_max_compression")


def test_2_tool_call_group_not_split():
    """tool_call + tool_result 群整组进 middle 或 tail，不被切割。

    cut_idx 落在 tool 消息或父 assistant + tool_calls 处时，对齐到父
    assistant 之前，整组进 middle。
    """
    compressor = ContextCompressor(
        context_window=10000,
        protect_first_n=3,
        tail_token_budget=200,
    )

    # 构造一个 tool 群在 cut_idx 附近会被命中的场景
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},                       # 3 head_end
        {"role": "assistant", "content": "x" * 400},              # 4
        {"role": "user", "content": "x" * 400},                   # 5
        # tool 群：assistant tool_calls + 两条 tool_result —— 不能拆
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "t", "arguments": "{}"}},
            {"id": "c2", "function": {"name": "t", "arguments": ""}},
        ]},                                                        # 6
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},  # 7
        {"role": "tool", "tool_call_id": "c2", "content": "r2"},  # 8
        {"role": "assistant", "content": "done"},                 # 9
        {"role": "user", "content": "ok"},                        # 10
        {"role": "assistant", "content": "bye"},                  # 11
    ]

    tail_start = compressor._find_tail_boundary(messages, head_end=3)

    # tool_call group 必须整组在同一侧（要么全 < tail_start，要么全 >= tail_start）
    group_indices = [6, 7, 8]
    in_middle = [i for i in group_indices if i < tail_start]
    in_tail = [i for i in group_indices if i >= tail_start]

    assert len(in_middle) == 0 or len(in_tail) == 0, (
        f"tool_call 群被拆开：middle 含 {in_middle}，tail 含 {in_tail}。"
        f"tail_start={tail_start}"
    )

    # 进一步：cut_idx 不能落在 tool 消息上
    assert messages[tail_start].get("role") != "tool", (
        f"tail_start 落在 tool 消息上，role={messages[tail_start].get('role')}"
    )

    print("  [PASS] test_2_tool_call_group_not_split")


def test_3_real_world_106_messages_bug_fixed():
    """复现用户报告的 tail_start=4, n=106 假压缩 bug — 必须修好。

    场景：106 条消息，head_end=3，预算估算偏小（_msg_tokens 粗估 chars/4+10）
    导致循环一路走到 head_end。
    """
    compressor = ContextCompressor(
        context_window=200000,
        protect_first_n=3,
        tail_token_budget=4000,  # 默认值
    )

    # 106 条小消息：总粗估 token ~ 106 * (50/4 + 10) ≈ 2438，远小于 4000 budget
    messages = _make_messages(106, char_per_msg=50)
    head_end = 3

    tail_start = compressor._find_tail_boundary(messages, head_end=head_end)

    # 关键：旧 bug 会让 tail_start = 4，middle 只 1 条
    assert tail_start != 4, (
        f"BUG 复现：tail_start={tail_start}，middle 只 1 条假压缩 — 修复未生效"
    )

    # 修复后：middle 应该拿到大部分消息
    middle_size = tail_start - head_end
    assert middle_size >= 50, (
        f"middle 太少：{middle_size} 条（应 >= 50，bug 表现为只 1 条）"
    )

    print("  [PASS] test_3_real_world_106_messages_bug_fixed")


def test_4_v15_regression_tail_not_on_tool():
    """V15 原有不变量回归 — tail 起点不落在 tool 消息上。"""
    compressor = ContextCompressor(
        context_window=10000,
        protect_first_n=3,
        tail_token_budget=200,
    )

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "a" * 400},
        {"role": "assistant", "content": "b" * 400},
        {"role": "user", "content": "c" * 400},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "function": {"name": "test", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "ok"},
        {"role": "assistant", "content": "bye"},
    ]

    tail_start = compressor._find_tail_boundary(messages, head_end=3)
    assert messages[tail_start].get("role") != "tool", (
        f"V15 回归失败：tail 起点落在 tool 消息，"
        f"role={messages[tail_start].get('role')} at idx {tail_start}"
    )

    print("  [PASS] test_4_v15_regression_tail_not_on_tool")


def test_5_format_messages_handles_none_content():
    """_format_messages 对 content=None 的 assistant 消息不能崩。

    OpenAI/Anthropic 协议里 assistant 带 tool_calls 时 content: null 合法。
    旧代码 `content.strip()` 会 AttributeError('NoneType' object has no attribute 'strip')。
    """
    compressor = ContextCompressor(context_window=10000)

    messages = [
        {"role": "user", "content": "run tests"},
        # 真实 OpenAI 响应：tool_calls + content=None
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": None},  # 也有些 provider 给 None
        {"role": "assistant", "content": "done"},
    ]

    # 不抛即通过
    out = compressor._format_messages(messages)
    assert "called terminal" in out, f"格式化输出缺 tool_call 信息：{out!r}"

    print("  [PASS] test_5_format_messages_handles_none_content")


def test_6_build_assistant_history_msg_rescues_reasoning_only():
    """写入侧抢救：content=None 且无 tool_calls 但有 reasoning → 提升 reasoning。

    源头修复：避免脏消息进 history。
    """
    from transports.types import NormalizedResponse, ToolCall, build_assistant_history_msg

    # 场景 1：纯 reasoning（deepseek-v4-flash 偶发把正文塞 reasoning）
    n1 = NormalizedResponse(
        content=None,
        tool_calls=None,
        finish_reason="stop",
        provider_data={"reasoning_content": "这就是真正的回答..."},
    )
    msg1 = build_assistant_history_msg(n1)
    assert msg1["content"] == "这就是真正的回答...", (
        f"reasoning 应被提升为 content，实测 content={msg1['content']!r}"
    )
    assert "tool_calls" not in msg1

    # 场景 2：tool_calls 合法 + content=None — 不动
    n2 = NormalizedResponse(
        content=None,
        tool_calls=[ToolCall(id="c1", name="ls", arguments="{}")],
        finish_reason="tool_calls",
    )
    msg2 = build_assistant_history_msg(n2)
    assert msg2["content"] is None, "带 tool_calls 时 content=None 合法，不应抢救"
    assert msg2["tool_calls"][0]["id"] == "c1"

    # 场景 3：全空 — 占位空格不让消息丢失
    n3 = NormalizedResponse(content=None, tool_calls=None, finish_reason="stop")
    msg3 = build_assistant_history_msg(n3)
    assert msg3["content"] == " ", f"全空兜底应为 ' '，实测 {msg3['content']!r}"

    # 场景 4：DeepSeek thinking padding — 带 tool_calls 但无 reasoning → padding=" "
    msg4 = build_assistant_history_msg(n2)
    assert msg4.get("reasoning_content") == " ", "thinking padding 缺失"

    print("  [PASS] test_6_build_assistant_history_msg_rescues_reasoning_only")


def test_7_convert_messages_sanitizes_dirty_history():
    """读出侧 sanitize：历史里已经存在的"纯 reasoning"脏 assistant 消息被抢救。

    第二道防线：兜旧账。即使写入侧没修，下一轮请求也不会 400。
    """
    from transports.chat_completions import ChatCompletionsTransport

    transport = ChatCompletionsTransport()

    dirty = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        # 真实复现的脏消息：content=None / 无 tool_calls / 有 reasoning_content
        {"role": "assistant", "content": None,
         "reasoning_content": "Now I can see the original structure clearly..."},
        {"role": "user", "content": "next"},
        # 极端：连 reasoning 都没有
        {"role": "assistant", "content": None},
        {"role": "user", "content": "ok"},
    ]

    cleaned = transport.convert_messages(dirty)

    # 第 1 条脏消息：reasoning 提升为 content
    assert cleaned[2]["content"] == "Now I can see the original structure clearly...", (
        f"reasoning 应提升为 content，实测 {cleaned[2]['content']!r}"
    )
    # 第 2 条脏消息（全空）：占位空格
    assert cleaned[4]["content"] == " ", (
        f"全空兜底应为 ' '，实测 {cleaned[4]['content']!r}"
    )
    # 健康消息不被改动
    assert cleaned[0] is dirty[0]
    assert cleaned[1] is dirty[1]
    assert cleaned[3] is dirty[3]
    assert cleaned[5] is dirty[5]

    # 合法的 content=None + tool_calls 不能被错改
    legit = [
        {"role": "user", "content": "run"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "ls", "arguments": "{}"}}
        ]},
    ]
    cleaned2 = transport.convert_messages(legit)
    assert cleaned2[1]["content"] is None, "带 tool_calls 时 content=None 合法，不应被改"
    assert cleaned2[1] is legit[1]

    print("  [PASS] test_7_convert_messages_sanitizes_dirty_history")


def test_8_tail_starts_on_user_turn_boundary():
    """tail 必须从 user 消息开始 — 不能在回合中段(assistant)切割。

    复现 v25 trajectory 报告的真实 bug：cut_idx 落在 `user → assistant` 之间，
    user 被划进 middle 摘要掉，assistant 回复留在 tail，压缩后 summary 紧跟一条
    孤立 assistant（在回答已被摘要掉的问题）。修复后 tail 起点必为 user。
    """
    compressor = ContextCompressor(
        context_window=10000,
        protect_first_n=3,
        tail_token_budget=300,
    )

    # 构造一串干净的 user/assistant 回合，让 budget 自然把 cut_idx 落在某个
    # assistant 上（回合中段）。
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "u1"},                # 3 head_end
        {"role": "assistant", "content": "x" * 400},       # 4
        {"role": "user", "content": "不够吸引人"},          # 5  ← 触发问题
        {"role": "assistant", "content": "x" * 200},       # 6  ← 它的回复，必须和 5 同侧
        {"role": "user", "content": "蛊真人看过没有"},      # 7
        {"role": "assistant", "content": "x" * 50},        # 8
        {"role": "user", "content": "ok"},                 # 9
        {"role": "assistant", "content": "bye"},           # 10
    ]

    tail_start = compressor._find_tail_boundary(messages, head_end=3)

    assert messages[tail_start].get("role") == "user", (
        f"tail 起点必须是 user，实测 role={messages[tail_start].get('role')} "
        f"at idx {tail_start} —— 回合被从中段切开，孤立 assistant 进 tail"
    )

    print("  [PASS] test_8_tail_starts_on_user_turn_boundary")


def test_9_no_orphan_assistant_after_summary():
    """端到端：压缩后 summary（head 之后第一条）的下一条不能是孤立 assistant。

    直接断言修复目标：summary user 之后若紧跟 assistant，则该 assistant 的触发
    user 已丢失。修复后 summary 后第一条应是 user（新回合）或 head 尾本就合法。
    """
    compressor = ContextCompressor(
        context_window=10000,
        protect_first_n=3,
        tail_token_budget=300,
    )

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "x" * 400},
        {"role": "user", "content": "不够吸引人"},
        {"role": "assistant", "content": "你说得对…" + "x" * 200},
        {"role": "user", "content": "蛊真人看过没有"},
        {"role": "assistant", "content": "看过…" + "x" * 50},
        {"role": "user", "content": "ok"},
        {"role": "assistant", "content": "bye"},
    ]

    head_end = 3
    tail_start = compressor._find_tail_boundary(messages, head_end=head_end)
    tail = messages[tail_start:]

    # tail 第一条是 user（回合起点）；它的 assistant 回复也在 tail 内
    assert tail[0].get("role") == "user", (
        f"tail 第一条应为 user，实测 {tail[0].get('role')}"
    )
    # 具体到这个 case：「不够吸引人」和它的回复要么都进 middle，要么都在 tail —— 不拆开
    tail_contents = [m.get("content", "") for m in tail]
    has_trigger = any("不够吸引人" in c for c in tail_contents)
    has_reply = any("你说得对" in c for c in tail_contents)
    assert has_trigger == has_reply, (
        f"「不够吸引人」与其回复被拆到两侧：trigger_in_tail={has_trigger}, "
        f"reply_in_tail={has_reply}"
    )

    print("  [PASS] test_9_no_orphan_assistant_after_summary")


# ─── 运行 ───

if __name__ == "__main__":
    print("=" * 60)
    print("  V15.1 Compress Boundary — Behavior Tests")
    print("=" * 60)

    tests = [
        test_1_oversized_budget_falls_back_to_max_compression,
        test_2_tool_call_group_not_split,
        test_3_real_world_106_messages_bug_fixed,
        test_4_v15_regression_tail_not_on_tool,
        test_5_format_messages_handles_none_content,
        test_6_build_assistant_history_msg_rescues_reasoning_only,
        test_7_convert_messages_sanitizes_dirty_history,
        test_8_tail_starts_on_user_turn_boundary,
        test_9_no_orphan_assistant_after_summary,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {test.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print()
    print(f"  Results: {passed} passed, {failed} failed, {passed + failed} total")
    if failed == 0:
        print("  All tests passed!")
    else:
        sys.exit(1)
