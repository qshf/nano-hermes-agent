"""V26.2: SKILL.md 安全 token 替换。

把 SKILL.md 正文里的 ``${SKILL_DIR}`` / ``${SESSION_ID}`` 替换成运行期具体值。
白名单固定两个 token，**不做内联 shell**。设计理由（为何不移植源项目的
``!`cmd``` 内联 shell、为何用白名单）见 docs/decisions/v26.2.md。
"""

from __future__ import annotations

import re
from pathlib import Path

# 白名单：只认这两个 token，其余 ${...}（含密钥环境变量名）一律不动。
_TOKEN_RE = re.compile(r"\$\{(SKILL_DIR|SESSION_ID)\}")


def substitute_tokens(
    content: str,
    skill_dir: Path | None,
    session_id: str | None,
) -> str:
    """把 ``${SKILL_DIR}`` / ``${SESSION_ID}`` 替换成具体值。

    只替换有具体值的 token；无值的（如无会话时的 ``${SESSION_ID}``）原样保留。
    白名单外的 token 不在捕获范围内，天然保留。``content`` 为空时原样返回。
    """
    if not content:
        return content

    skill_dir_str = str(skill_dir) if skill_dir else None

    def _replace(match: re.Match) -> str:
        token = match.group(1)
        if token == "SKILL_DIR" and skill_dir_str:
            return skill_dir_str
        if token == "SESSION_ID" and session_id:
            return str(session_id)
        return match.group(0)

    return _TOKEN_RE.sub(_replace, content)
