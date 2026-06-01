"""V25.0 — 密钥脱敏（trajectory 写盘前 + 日志 formatter 双挂钩）。

为什么要它
==========
trajectory 是要喂 SFT 的训练样本（决策 1/2），而对话里随处可能混进密钥 ——
transport 每次调用都带 ``Authorization: Bearer ...``，工具回显可能带 ``OPENAI_API_KEY=...``，
用户也可能直接把 ``sk-...`` 贴进对话。这些原样落进 jsonl 就成了**泄漏在训练集里的
凭证**。所以 trajectory 每条 value 落盘前都过一遍 ``redact``（决策 3）。

源项目对照：``hermes-agent/agent/redact.py``（404 行、~35 条 pattern + Telegram /
Discord / phone / URL query param / form body 等）。nano 取**最常见的 ~10 条**：
``sk-*`` / ``ghp_*`` / ``AKIA*`` / JWT(``eyJ*``) / ``Bearer`` 头 / ``KEY=value`` ENV /
私钥块 / DB 连接串 userinfo。砍掉的留 v25+ 待办（见 docs/todo.md）。

掩码策略对齐源（``redact.py:_mask_token``）：
- 短 token（< 18 字符）→ 全掩 ``***``（留首尾反而泄漏太多熵）
- 长 token → 留首 6 末 4（``sk-pro...7890``）—— 保留可调试性，但中段隐藏

默认开 + import 时快照（决策 3）
================================
``NANO_REDACT_SECRETS=0`` 才关。``_ENABLED`` 在 **import 时**取一次快照 ——
对齐源 ``redact.py:67`` 的 ``_REDACT_ENABLED``。为什么不每次读 env：防"运行期某段
代码 ``os.environ["NANO_REDACT_SECRETS"]="0"`` 把脱敏偷偷关掉"这种被绕过的场景
（安全默认不该被运行期 mutate 动摇）。代价：测试要验"关闭"行为得 reimport 模块
（见 test_v25_0 第 8 项），这是安全换来的可测试性代价，决策日志记一笔。
"""

from __future__ import annotations

import os
import re

# import 时快照 —— 之后改 env 无效（决策 3）。
_ENABLED = os.environ.get("NANO_REDACT_SECRETS", "1") != "0"

# 短 token 全掩阈值 + 长 token 留首尾位数 —— 对齐源 ``_mask_token``。
_FLOOR = 18
_HEAD = 6
_TAIL = 4


def mask(token: str) -> str:
    """把单个 token 掩成展示态 —— 短全掩、长留首 6 末 4。

    对齐源 ``redact.py:_mask_token``（floor=18 / head=6 / tail=4）。空串返回
    ``***``（历史行为：宁可多掩不漏）。
    """
    if not token:
        return "***"
    if len(token) < _FLOOR:
        return "***"
    return f"{token[:_HEAD]}...{token[-_TAIL:]}"


# ── pattern 表（~10 条，每条都标命中目标）─────────────────────────────────
# 前缀类密钥：匹配"前缀 + 连续 token 字符"，左右用边界防把相邻文字吃进来。
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",        # OpenAI / Anthropic(sk-ant-*) / DeepSeek
    r"ghp_[A-Za-z0-9]{10,}",         # GitHub PAT (classic)
    r"AKIA[A-Z0-9]{16}",             # AWS Access Key ID
    r"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_=-]{4,}){0,2}",  # JWT(header[.payload[.sig]])
]
# 编进一条 alternation，左右负向边界防截断相邻标识符。
_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(_PREFIX_PATTERNS) + r")(?![A-Za-z0-9_-])"
)

# Authorization: Bearer <token> —— 保留 "Bearer " 前缀，只掩 token。
_BEARER_RE = re.compile(r"(Bearer\s+)([A-Za-z0-9._\-]{8,})", re.IGNORECASE)

# ENV 赋值：KEY=value，KEY 含 secret 语义词。保留 KEY= 只掩 value。
_ENV_NAMES = r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)"
_ENV_RE = re.compile(
    rf"([A-Z0-9_]{{0,40}}{_ENV_NAMES}[A-Z0-9_]{{0,40}}\s*=\s*)(['\"]?)(\S+)\2"
)

# 私钥块：-----BEGIN ... PRIVATE KEY----- ... -----END ... PRIVATE KEY-----
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)

# DB 连接串：protocol://user:PASSWORD@host —— 只掩 password 段。
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:/\s]+:)([^@/\s]+)(@)",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """遍历 pattern 替换密钥为掩码态；``_ENABLED=False`` 时原样返回。

    顺序：先整块（私钥）→ 再结构化（ENV / Bearer / DB）→ 最后裸前缀 token。
    先掩结构化（带 KEY=/Bearer 前缀的）能让裸前缀正则不必重复处理它们，
    且保留 ``KEY=`` / ``Bearer `` / ``user:`` 这些非密钥语境，可读性更好。
    """
    if not _ENABLED or not text:
        return text
    text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    text = _ENV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{mask(m.group(3))}{m.group(2)}", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)}{mask(m.group(2))}", text)
    text = _DB_CONNSTR_RE.sub(lambda m: f"{m.group(1)}{mask(m.group(2))}{m.group(3)}", text)
    text = _PREFIX_RE.sub(lambda m: mask(m.group(1)), text)
    return text
