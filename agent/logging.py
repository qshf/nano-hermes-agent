"""V25.1 — 结构化日志：每条 record 注入 ``[session_id]`` + 滚动文件 + 脱敏 formatter。

这一档解决什么
==============
前 24 档的日志要么 ``print`` 到 stdout（跟对话输出混在一起、进程退出即丢），要么
压根没有结构化记录。排查"哪个会话在哪轮调了哪个 transport 失败"时无据可查。
v25.1 加一个**单文件 + 滚动 + 脱敏**的结构化日志层：

- 每条 record 自动带 ``[session_id]`` 前缀（thread-local 注入，子线程各自带）
- ``RotatingFileHandler``（``agent.log``，5MB × 3）—— 防日志无限膨胀
- ``RedactingFormatter`` 挂在 handler 上 —— **日志落盘前过 redact**（复用 v25.0 的
  ``agent.redact.redact``）—— 日志文件不落密钥（transport 每次调用都带
  ``Authorization`` 头，print 出来就泄漏，这里挡在写盘前）

为什么 redact 挂在 formatter 而非调用点（决策 3）
=================================================
源项目 ``hermes_logging.py`` 的 ``RedactingFormatter`` 挂在**所有 handler** 上 ——
脱敏是"最后一道写盘前的闸"，不依赖调用方记得脱敏。这样无论哪段代码 ``log.info(...)``
带了密钥，落盘前都会被掩。与 v25.0 trajectory 写盘前过 redact 是同一道防线的两处出口
（trajectory 喂训练 / 日志供排查），共用 ``redact()``。

为什么配 root logger 而非私有 named logger（决策 9，v25.1 接线时修正）
====================================================================
handler 必须挂在 **root logger**（``logging.getLogger()`` 无参），不能挂私有
``nano`` logger。因为代码库已有 8 个模块（``transports.chain`` failover /
``tools.delegate_tool`` / ``memory.manager`` / ``tools.registry`` …）早就在用
``logging.getLogger(__name__)`` —— 它们的 record 沿 logger 树 **propagate 到 root**。
只有把 handler + ``_SessionFilter`` 挂 root，这些已有日志（尤其 docstring 承诺要
排查的 transport failover）才会流经 ``RedactingFormatter`` 脱敏。若挂私有 ``nano`` +
``propagate=False``，那 8 个模块一条都收不到，密钥照漏。源项目 ``hermes_logging.py``
同样挂 root（``root = logging.getLogger()``）。

源项目对照：``hermes-agent/hermes_logging.py``（390 行：``agent.log`` / ``errors.log``
双文件 + 组件路由 + NixOS chmod + 多 handler 分级）。nano 取最小切片：单 ``agent.log``
+ session 注入 + RedactingFormatter。砍掉的留 v25+ 待办。
"""

from __future__ import annotations

import logging
import os
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from agent.redact import redact

# 日志文件路径：env ``LOG_FILE`` 覆盖；``:none:`` 关闭文件日志（只留 stderr）。
_LOG_FILE_ENV = os.environ.get("LOG_FILE", "logs/agent.log")
# 级别：env ``LOG_LEVEL`` 控制（DEBUG/INFO/WARNING/ERROR），缺省 INFO。
_LOG_LEVEL_ENV = os.environ.get("LOG_LEVEL", "INFO").upper()

_MAX_BYTES = 5 * 1024 * 1024  # 5MB
_BACKUP_COUNT = 3

# thread-local 存当前 session_id —— 父 main loop 与 delegate 子线程各自独立。
# 子线程不设则取 default，不会串到父的 session。
_local = threading.local()

_LOGGER_NAME = "nano"
_configured = False


def set_log_session(session_id: str) -> None:
    """绑定当前线程的 session_id —— 之后该线程所有 log record 带 ``[session_id]``。

    main loop 在 ``/new`` / ``/resume`` 切会话后调一次；delegate 子线程可在
    spawn 时各自设自己的子 id（thread-local 天然隔离，互不污染）。
    """
    _local.session_id = session_id


def _current_session() -> str:
    return getattr(_local, "session_id", "-")


def get_log_session() -> str:
    """读当前线程绑定的 session_id（未绑定返回 ``"-"``）。

    供延迟结算类日志用：某操作在会话 A 发起、效果到会话 B 才结算时，调用方可在
    发起时 ``get_log_session()`` 快照 A，结算时用 ``log_session_scope(A)`` 临时切回，
    让日志的 ``[session_id]`` 归因到真正发生该操作的会话，而非结算时刻的会话。
    """
    return _current_session()


class log_session_scope:
    """``with log_session_scope(sid):`` 临时把当前线程 session 切到 ``sid``，退出还原。

    用于延迟结算日志的正确归因（见 ``get_log_session``）。线程内嵌套安全：
    保存进入前的值，``__exit__`` 无条件还原。"""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._saved: str | None = None

    def __enter__(self) -> "log_session_scope":
        self._saved = getattr(_local, "session_id", None)
        _local.session_id = self._session_id
        return self

    def __exit__(self, *exc) -> None:
        if self._saved is None:
            if hasattr(_local, "session_id"):
                del _local.session_id
        else:
            _local.session_id = self._saved


class _SessionFilter(logging.Filter):
    """把 thread-local 的 session_id 塞进每条 record，供 formatter 取用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = _current_session()
        return True


class RedactingFormatter(logging.Formatter):
    """格式化后整行过 ``redact`` —— 日志落盘前掩掉密钥（决策 3）。

    挂在所有 handler 上：无论哪段代码 log 了带密钥的字符串，写盘前都被掩。
    与 v25.0 trajectory 写盘前过 redact 共用同一个 ``redact()``，是同一道防线
    的两处出口。
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(*, log_file: str | None = None, level: str | None = None) -> logging.Logger:
    """配置 **root logger** 并返回 nano 埋点 logger（幂等：重复调只配一次）。

    - handler + ``_SessionFilter`` 挂 **root 的 handler 上**（决策 9）—— 让已有 8 个用
      ``getLogger(__name__)`` 的模块（transports.chain failover / delegate /
      memory …）的日志 propagate 到 root 后统一过脱敏 + session 注入
    - ``[session_id]`` 注入（``_SessionFilter`` 挂 **handler**，不挂 logger —— 见下方
      实现注释，logger.filter 不过滤 propagate 上来的 record）
    - ``RotatingFileHandler``（5MB × 3）；``log_file=":none:"`` 跳过文件 handler
    - ``RedactingFormatter`` 挂所有 handler —— 写盘前脱敏
    - ``LOG_LEVEL`` env 控制级别
    - 返回的是私有 ``nano`` logger（供主动埋点 ``get_logger()`` 用），它
      ``propagate=True`` 冒泡到 root，同样享受 root 的脱敏 handler
    """
    global _configured
    nano_logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        return nano_logger

    lvl = (level or _LOG_LEVEL_ENV)
    level_const = getattr(logging, lvl, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level_const)

    fmt = RedactingFormatter(
        "%(asctime)s [%(levelname)s] [%(session_id)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # _SessionFilter 必须挂在 **handler** 上，不能挂 logger 上。Python logging 的
    # logger.filter 只过滤"直接 log 到该 logger"的 record，**不过滤 propagate 上来的**
    # —— 而 transports.chain 等模块的 record 正是 propagate 到 root 的。挂 handler 则
    # 对所有到达该 handler 的 record（含 propagate 来的）都注入 session_id，否则
    # formatter 引用 %(session_id)s 会 KeyError。
    session_filter = _SessionFilter()

    target = log_file if log_file is not None else _LOG_FILE_ENV
    if target != ":none:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            target, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.addFilter(session_filter)
        root.addHandler(fh)

    # nano 埋点 logger：propagate=True（默认），record 冒泡到 root 过脱敏 handler
    nano_logger.setLevel(level_const)

    _configured = True
    return nano_logger


def get_logger() -> logging.Logger:
    """取已配置的 nano logger（未 setup 则返回未配置的同名 logger，调用方自配）。"""
    return logging.getLogger(_LOGGER_NAME)
