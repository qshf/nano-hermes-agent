"""V25.0 — trajectory：把对话转成 ShareGPT 训练样本落盘（数据飞轮起点）。

这一档解决什么
==============
前 24 档跑过的对话"用完即抛"：v24 让 messages 能 resume（状态回放），但存的是
原始 OpenAI dict，**不是训练格式**。拿去 SFT 要先转 ShareGPT、要脱敏、要把
tool_call 拍平成可解析的 XML —— 这层转换 v24 没做也不该做（v24 决策 4 划清边界）。
v25.0 接住它：每段对话在被丢弃前自动落成一份可直接喂 ``trl``/``axolotl`` 的样本。

与 v24 session-store 的边界（决策 1）
=====================================
| | v24 session-store | v25 trajectory |
|---|---|---|
| 形态 | OpenAI messages dict（SQLite 逐条入行） | ShareGPT {from,value} 对（jsonl 逐行一对话） |
| tool | tool_calls + tool_call_id 完整关联 | 拍平成 <tool_call> XML，丢 tool_call_id |
| 密钥 | 原样（能 replay） | 过 redact 脱敏（内容已变） |
| 用途 | 状态回放 — resume 真能续上 | 数据飞轮 — 喂 SFT，replay 会断 |
两者关注点正交：trajectory 拿去 replay 会丢 tool_call_id（配不上 tool 结果）+
密钥被脱敏 —— 它只配训练，不配恢复。

源项目对照
==========
- ShareGPT 转换：``run_agent.py:4583-4750`` 的 ``_convert_to_trajectory_format``
- 落盘 + 残缺分流：``agent/trajectory.py:30-56`` 的 ``save_trajectory``
nano 砍掉：多模态 base64 图剥离（nano 无图）/ 离线 trajectory_compressor 批处理摘要。

每条 value 写盘前过 ``redact``（决策 3）—— 训练样本不带密钥。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from agent.redact import redact

# 落盘目录：env ``TRAJECTORY_DIR`` 覆盖。``:none:`` 关闭落盘（退化到不写盘，
# 对齐 session_store 的 ``:memory:`` sentinel 思路 —— 一个不落盘、一个落内存）。
_TRAJECTORY_DIR_ENV = os.environ.get("TRAJECTORY_DIR", "trajectories")

# 源项目系统段固定模板（run_agent.py:4602-4614 裁剪）。nano 不动态拼工具 schema
# 进 system —— 教学版只需展示"ShareGPT 第一条是 system + 工具说明"这个形态契约。
_SYSTEM_VALUE = (
    "You are a function calling AI model. You may call one or more functions "
    "to assist with the user query. Tool calls are wrapped in <tool_call></tool_call> "
    "XML tags; results come back in <tool_response></tool_response> tags. "
    "Reasoning is wrapped in <think></think> tags."
)


def _wrap_think(reasoning: str, body: str) -> str:
    """拼 gpt turn 的 value —— reasoning 包 <think>，保证每个 gpt turn 都有 <think>
    块（空也带，对齐源 4670-4673：训练格式一致性）。"""
    head = f"<think>\n{reasoning}\n</think>\n" if reasoning and reasoning.strip() else ""
    content = head + body
    if "<think>" not in content:
        content = "<think>\n</think>\n" + content
    return content


def _flatten_tool_calls(tool_calls: list[dict]) -> str:
    """把 OpenAI ``tool_calls`` 拍平成 ``<tool_call>{name,arguments}</tool_call>`` XML。

    丢 tool_call_id（决策 1：trajectory 不配 replay）。arguments 是 JSON 字符串则
    先 parse 成对象再 dump，让样本里是结构化 args 而非转义字符串。
    """
    out = ""
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except (json.JSONDecodeError, TypeError):
            args = {}
        payload = {"name": fn.get("name", "unknown"), "arguments": args}
        out += f"<tool_call>\n{json.dumps(payload, ensure_ascii=False)}\n</tool_call>\n"
    return out


def to_sharegpt(messages: list[dict], *, system_value: Optional[str] = None) -> list[dict]:
    """OpenAI messages → ShareGPT ``{from, value}`` 对（对齐源 4583-4750）。

    映射规则：
    - 首条 ``{from: "system", value: <工具说明段>}``（system_value 覆盖默认模板）
    - ``user`` → ``{from: "human", value}``
    - ``assistant`` → ``{from: "gpt", value: "<think>..</think><tool_call>..XML"}``
      —— reasoning 包 <think>，tool_calls 拍平成 XML；每个 gpt turn 保证有 <think>
    - 连续多条 ``tool`` 结果合并成一条 ``{from: "tool", value: "<tool_response>.."}``
    - ``system`` 角色（除首条注入外）跳过 —— ShareGPT 的 system 只在开头一条

    nano 适配：源用 ``msg["reasoning"]``，nano OpenAI dict 用 ``reasoning_content``
    （与 session_store messages 表列名一致）。
    """
    convs: list[dict] = [{"from": "system", "value": system_value or _SYSTEM_VALUE}]
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        role = msg.get("role")

        if role == "user":
            convs.append({"from": "human", "value": msg.get("content") or ""})
            i += 1
            continue

        if role == "assistant":
            reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
            body = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []
            value = _wrap_think(reasoning, (body + "\n" if body and tool_calls else body))
            if tool_calls:
                value += _flatten_tool_calls(tool_calls)
            convs.append({"from": "gpt", "value": value.rstrip()})
            # 收集紧随其后的连续 tool 结果，合并成一条
            responses: list[str] = []
            j = i + 1
            while j < n and messages[j].get("role") == "tool":
                tmsg = messages[j]
                content = tmsg.get("content") or ""
                try:
                    if isinstance(content, str) and content.strip().startswith(("{", "[")):
                        content = json.loads(content)
                except (json.JSONDecodeError, ValueError):
                    pass
                responses.append(
                    "<tool_response>\n"
                    + json.dumps({"content": content}, ensure_ascii=False)
                    + "\n</tool_response>"
                )
                j += 1
            if responses:
                convs.append({"from": "tool", "value": "\n".join(responses)})
            i = j
            continue

        # system（非首条）/ 其它角色：跳过
        i += 1
    return convs


def has_incomplete_think(convs: list[dict]) -> bool:
    """任一 value 里 ``<think>`` 开标无闭合 → 残缺（对齐源 has_incomplete_scratchpad）。

    残缺样本分流到 ``failed_trajectories.jsonl`` 而非 ``samples``，避免污染训练集
    （截断的 reasoning 会教模型不闭合 think 块）。
    """
    for c in convs:
        v = c.get("value") or ""
        if "<think>" in v and "</think>" not in v:
            return True
    return False


def save_trajectory(
    convs: list[dict],
    *,
    model: str,
    completed: bool,
    timestamp: str,
    out_dir: Optional[str] = None,
    filename_stem: str = "trajectory",
) -> Optional[Path]:
    """外层包 ``{conversations, timestamp, model, completed}`` 追加进 jsonl。

    - ``completed`` 且不残缺 → ``<stem>_samples.jsonl``，否则 ``<stem>_failed.jsonl``
      （对齐源 trajectory.py:42 的 completed 分流）
    - **每条 value 写盘前过 ``redact``**（决策 3）—— 训练样本不带密钥
    - ``timestamp`` 由调用方传入（不在此处 ``time.time()`` —— 让测试可决定性断言）
    - ``out_dir=None`` 时读 env ``TRAJECTORY_DIR``；值为 ``:none:`` → 不落盘返回 None

    返回写入的文件 Path（关闭时返回 None）。
    """
    target_dir = out_dir if out_dir is not None else _TRAJECTORY_DIR_ENV
    if target_dir == ":none:":
        return None

    redacted = [{"from": c.get("from"), "value": redact(c.get("value") or "")} for c in convs]
    ok = completed and not has_incomplete_think(redacted)
    suffix = "samples" if ok else "failed"
    path = Path(target_dir) / f"{filename_stem}_{suffix}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "conversations": redacted,
        "timestamp": timestamp,
        "model": model,
        "completed": completed,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


def flush_session_trajectory(
    messages: list[dict],
    *,
    model: str,
    completed: bool,
    filename_stem: str = "trajectory",
    out_dir: Optional[str] = None,
) -> Optional[Path]:
    """生产侧薄包装：to_sharegpt → save_trajectory，时间戳此处生成。

    两个生产 hook（compaction 压缩点 / main 退出兜底）共用此函数，避免重复
    "转 + 落 + 打时间戳"三步。``save_trajectory`` 本身保持纯（timestamp 入参）
    让测试可决定性断言；时间戳的副作用（``datetime.now()``）收敛在这一层。

    空 messages（如纯 system 的子早退）→ to_sharegpt 只剩 1 条 system，
    无 human/gpt，落盘也没训练价值 —— 直接跳过返回 None。
    """
    if not messages:
        return None
    convs = to_sharegpt(messages)
    # 至少要有一条 human 或 gpt 才值得落（只有 system 的样本训不出东西）
    if not any(c.get("from") in ("human", "gpt") for c in convs):
        return None
    return save_trajectory(
        convs,
        model=model,
        completed=completed,
        timestamp=datetime.now().isoformat(),
        out_dir=out_dir,
        filename_stem=filename_stem,
    )
