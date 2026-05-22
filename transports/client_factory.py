"""V17 — LLM 客户端工厂。

教学定位
--------
``make_llm_client(api_mode)`` 把"哪个 SDK 该用什么 client 类"这件事从 agent.py
搬到这里。V17 阶段只支持 ``"chat_completions"`` → ``OpenAI(api_key, base_url)``。
V18 起会增加 ``"anthropic_messages"`` → ``anthropic.Anthropic(api_key, base_url)``，
agent.py 一行不动。

为什么不直接用 transport 自己暴露 client？
- transport 的职责是 **格式转换**（messages/tools/response），不该耦合 SDK 实例化。
- 客户端的 endpoint / api_key / 超时配置等是部署关注点，应该独立于 transport。

对应源项目: ``hermes-agent/agent/agent_factory.py`` 中按 ``api_mode`` 路由 client
实例化的逻辑（散落，nano 收敛成一个函数）。
"""

from __future__ import annotations

import os
from typing import Any


def make_llm_client(api_mode: str) -> Any:
    """按 api_mode 创建对应 SDK 的 client。

    支持的 api_mode:
        "chat_completions" — OpenAI 兼容（DeepSeek/Qwen OpenAI 端点 等）
        "anthropic_messages" — Anthropic Messages API（Qwen DashScope 端点 等）
    """
    if api_mode == "chat_completions":
        from openai import OpenAI

        return OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL"),
        )

    if api_mode == "anthropic_messages":
        import anthropic

        return anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            base_url=os.environ.get("ANTHROPIC_BASE_URL"),
        )

    raise ValueError(
        f"Unsupported api_mode: {api_mode!r} "
        f"(supported: 'chat_completions', 'anthropic_messages')"
    )
