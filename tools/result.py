"""V21.4: 工具结果格式辅助函数 — 对齐源项目 hermes-agent 的协议约定。

源项目 ``tools/registry.py:537-548`` 提供 ``tool_result()`` / ``tool_error()``，
统一所有工具的返回格式：

- 永远返回 JSON 字符串（不是 dict、不是裸 string）
- 错误用 ``{"error": "..."}``（独占语义，不与 output 共存）
- 主输出用 ``{"output": "..."}``（terminal/code_execution/skill_view）
- 文件类内容用 ``{"content": "..."}``（read_file —— 与源项目一致）
- 多字段并列时直接 ``{...}``（execute_code: status/output/exit_code/...）

V21.4 之前每个工具自己 ``json.dumps({...}, ensure_ascii=False)``，重复 6 处
且 ``mcp_client.py:160`` 直接 ``return "\n".join(parts)`` 漏掉了 JSON 包装，
让 message 协议出现裂缝。本模块收口这件事。

为什么仍然保留 JSON 包装而不是裸 markdown
=========================================
用户曾问 "skill_view 返回的 markdown 被 ``\\n`` 转义看着难受，要不要去掉 JSON 包装"。
对照源项目后否决：source-of-truth 是 ``{"output": str}`` 的 JSON，模型完全能
解析，多花的几个 token 换来"工具结果协议统一可机械解析"，是值得的代价。
真正解决长内容浪费 context 的方法是 *沙箱持久化*（见 docs/system-roadmap.md），
不是降级协议。
"""

from __future__ import annotations

import json
from typing import Any


def tool_result(data: Any = None, **kwargs: Any) -> str:
    """构造成功结果的 JSON 字符串。

    两种用法：

    >>> tool_result({"output": "hello"})
    '{"output": "hello"}'
    >>> tool_result(output="hello", exit_code=0)
    '{"output": "hello", "exit_code": 0}'

    第一种用法用于已经构造好 dict 的场景；第二种用于直接命名参数构造。
    优先用第一种 —— 与源项目 ``tool_result(data)`` 保持一致。
    """
    payload = data if data is not None else kwargs
    return json.dumps(payload, ensure_ascii=False)


def tool_error(message: str, **extra: Any) -> str:
    """构造错误结果的 JSON 字符串。

    >>> tool_error("File not found")
    '{"error": "File not found"}'
    >>> tool_error("Timeout", timeout=30)
    '{"error": "Timeout", "timeout": 30}'

    错误字段独占语义：调用方看到 ``"error"`` key 就知道失败，不必再判断
    其他字段。``extra`` 用于补充上下文（超时秒数、相关路径等）。
    """
    payload = {"error": str(message)}
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)
