"""V20 — Anthropic prompt caching（``system_and_3`` 策略）。

教学定位
--------
多轮对话每轮把整个 system prompt + 历史消息重新发给 LLM 是浪费 — 这部分前缀
几乎不变，理应被后端缓存。Anthropic 提供 ``cache_control: ephemeral`` 标记，
显式告诉服务端"这段 prefix 请缓存"，下一轮命中可省 ~75% input token 计费。

与 OpenAI/DeepSeek 隐式 cache 的区别在于：
- DeepSeek/OpenAI: prefix 自动匹配缓存，调用方什么都不用做（命中率写在 usage.prompt_tokens_details.cached_tokens）
- Anthropic: 必须在 message 上 ``显式`` 打 ``cache_control`` 标记才会被缓存

本模块只关心后者 — Anthropic 显式标记。"system_and_3"是源项目验证过的策略：

    1 breakpoint @ system prompt（每轮都稳定，命中率最高）
  + 3 breakpoints @ 最后 3 条非 system 消息（滚动窗口）
  = 4 breakpoints（Anthropic 上限）

随着对话推进，最后 3 条会变，但前面的 prefix 已经被 cache 过，下一轮调用前
N-3 条都吃 cache_read 价（约为 input 价的 1/10）。

设计与裁剪
----------
源项目 ``agent/prompt_caching.py`` 72 行几乎照搬。只删 ``native_anthropic``
布尔标志（nano 不走 Anthropic 兼容代理这种边缘情况，永远是 native）。

env 开关:
    PROMPT_CACHE_ENABLED   1/0（默认 0，不启用）
    PROMPT_CACHE_TTL       5m / 1h（默认 5m，1h 比 5m 单价高但 TTL 长）

对应源项目: ``hermes-agent/agent/prompt_caching.py``。
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List


# Anthropic 单次请求最多支持 4 个 cache_control breakpoint
_MAX_BREAKPOINTS = 4


def _apply_cache_marker(msg: dict, marker: dict) -> None:
    """在单条 message 上打一个 cache_control 标记。

    针对不同 content 形态分流：

    - role=tool / 空 content: 直接在 message 顶层挂 cache_control（Anthropic
      原生 messages.create 接受这种形态 — 走 SDK 的"消息级"breakpoint）
    - content 是 str: 升级为 [{"type":"text", "text":..., "cache_control":...}]
      list（每个 block 独立带 cache_control）
    - content 是 list: 在最后一个 block 上挂 cache_control（cache 边界落在
      block 末尾，前面所有 block 都被缓存）
    """
    role = msg.get("role", "")
    content = msg.get("content")

    if role == "tool":
        # tool result 的标准做法是顶层挂 — 在 anthropic_adapter 转换后会变成
        # tool_result content block 的 cache_control
        msg["cache_control"] = marker
        return

    if content is None or content == "":
        msg["cache_control"] = marker
        return

    if isinstance(content, str):
        # str → list[block] 升级；cache_control 落在 block 上
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": marker}
        ]
        return

    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = marker


def apply_anthropic_cache_control(
    api_messages: List[Dict[str, Any]],
    cache_ttl: str = "5m",
) -> List[Dict[str, Any]]:
    """``system_and_3`` 策略：在 system + 最后 3 条非 system 消息上打 cache_control。

    总共最多 4 个 breakpoint — Anthropic API 单次请求上限。

    Args:
        api_messages: OpenAI 格式 messages（``[{"role":..., "content":...}, ...]``）
        cache_ttl: ``"5m"`` (默认) 或 ``"1h"``。1h 单价更高但 TTL 长。

    Returns:
        ``api_messages`` 的深拷贝，注入 cache_control 后的版本。原 list 不变（避免
        跨轮污染）。
    """
    messages = copy.deepcopy(api_messages)
    if not messages:
        return messages

    marker: Dict[str, Any] = {"type": "ephemeral"}
    if cache_ttl == "1h":
        marker["ttl"] = "1h"

    breakpoints_used = 0

    # 1. system prompt（如果存在且在第一条）
    if messages[0].get("role") == "system":
        _apply_cache_marker(messages[0], marker)
        breakpoints_used += 1

    # 2. 最后 (4 - breakpoints_used) 条非 system 消息
    remaining = _MAX_BREAKPOINTS - breakpoints_used
    if remaining <= 0:
        return messages

    non_sys_indices = [
        i for i in range(len(messages)) if messages[i].get("role") != "system"
    ]
    for idx in non_sys_indices[-remaining:]:
        _apply_cache_marker(messages[idx], marker)

    return messages
