"""
Nano Hermes Agent — V23.2: 项目上下文注入 + --cwd 启动

V23.2 主题"agent 跨项目可用"。V0–V23.1 强约束"必须在 nano 仓库根目录下启动"
（skills/ 路径用 ``Path(__file__).parent``，但 system prompt 没有任何"用户项目"
信息）。本档让 agent 真的能去帮用户写代码：

- ``main.py --cwd /Users/foo/Documents/book`` 启动后 ``os.chdir`` 到该目录 →
  terminal / read_file 等工具的相对路径全在用户项目下生效
- ``PromptBuilder`` 注入 cwd → system prompt 自动注入用户项目根的
  ``nano-hermes-agent.md`` 或 ``AGENTS.md``（首个命中即停，封顶 20000 字符）
- skills/ 仍绑定在 nano 自己源码目录（``Path(__file__).parent / "skills"``），
  跨 cwd 启动也能加载 — 这是源项目 hermes-agent 的同向设计：能力（skills）
  跟 agent 走，规则（AGENTS.md）跟用户项目走
- env ``NANO_IGNORE_RULES=1`` 跳过项目上下文注入（仿源项目 HERMES_IGNORE_RULES）

V22 主题"transport 弧线收尾 + 多 agent 前置"。前 21 档全部同步 ``chain.call``
（一次性 5–30s 等响应），V22 在 transport ABC 上补全流式那一半 + 中断：

- ``ProviderTransport.stream_call(client, cancel_token, **kwargs)`` 默认假流式
- ``ChatCompletionsTransport.stream_call``：``stream=True + include_usage`` 真 SSE
  → emit ``text_delta`` / ``tool_call_started``，最终 ``done`` 携带完整
  ``NormalizedResponse``
- ``AnthropicTransport.stream_call``：``client.messages.stream(...)`` 上下文管理器
  → 解析 ``content_block_delta`` / ``thinking_delta`` / ``tool_use`` block；
  done 复用 ``normalize_response``
- ``TransportChain.stream_call``：**首帧前可切家**，已 yield 过事件后失败禁止
  切家（避免 token 重发）；``StreamCancelled`` 透传不计为失败；done 帧累计
  cache 统计（与同步路径完全一致）
- ``transports/streaming.py`` 提供 ``StreamEvent`` / ``CancelToken`` /
  ``StreamCancelled``；``CancelToken`` 用 ``threading.Event`` 让 V23 多 agent
  能跨线程共享父 token
- main.py 用 ``signal.signal(SIGINT, ...)`` 把 Ctrl+C 翻译成 ``token.cancel()``：
  prompt 上的 SIGINT 走 KeyboardInterrupt 退出，流式期间走 cancel 回到 prompt
- ``/stream on|off`` 命令切换流式与非流式（教学回退路径）

V21 系列（向下保留）：
- V21.4 工具结果协议 ``tool_result()`` / ``tool_error()`` + dispatch 兜底
- V21.3 Skill 系统（progressive disclosure）
- V21.2 三段式 PromptBuilder
- V21.1 slash 命令注册表 + AgentCtx

env 开关（V23.2 新增）：
    NANO_IGNORE_RULES           1/0（默认 0）；1 时跳过 nano-hermes-agent.md /
                                AGENTS.md 注入，便于诊断 prompt 长度问题

env 开关（V22 新增）：
    STREAM_ENABLED              1（默认）/ 0；0 时退化到 V21 同步路径

env 开关（V20 保留）：
    PROMPT_CACHE_ENABLED        1/0（默认 0，启用后链上每个 entry 自决定是否打标记）
    PROMPT_CACHE_TTL            5m（默认）/ 1h（1h 单价更高但 TTL 长）

env 开关（V19）：
    TRANSPORT_CHAIN              chat_completions,anthropic_messages（不设则用 TRANSPORT_MODE 单家）
    FAILOVER_FAILURE_THRESHOLD   断路器打开阈值（默认 3 次连续失败）
    FAILOVER_COOLDOWN_SECONDS    断路器冷却时间（默认 60s）
    FAILOVER_MAX_RETRIES         单 transport RETRYABLE 错误最大重试次数（默认 2）
    FAILOVER_BASE_DELAY          backoff 基数（默认 1.0s）

env 开关（V18 保留）：
    TRANSPORT_MODE              chat_completions（默认）/ anthropic_messages
    ANTHROPIC_API_KEY           Anthropic SDK key（DashScope 等）
    ANTHROPIC_BASE_URL          Anthropic SDK base_url（如 https://dashscope.aliyuncs.com/apps/anthropic）

env 开关:
    CONTEXT_WINDOW              模型上下文窗口大小（默认 32000 tokens）
    CONTEXT_THRESHOLD_PERCENT   压缩阈值比例（默认 0.75）
    CONTEXT_PROTECT_HEAD        保留前 N 条消息（默认 3）
    CONTEXT_TAIL_BUDGET         tail 保留 token 预算（默认 4000）
    MEMORY_SERVICE_URL          外部记忆服务 URL（设了才挂 remote_semantic）
    MEMORY_SESSION_ID           会话隔离 id（默认 default）
    MEMORY_BANK_ID              bank 命名空间（默认 hermes）
    MEMORY_MODE                 context / tools / hybrid（默认 hybrid）
    MEMORY_PREFETCH_METHOD      recall / reflect（默认 recall）
    MEMORY_RECALL_BUDGET        low / mid / high（默认 mid → k=5）
    MEMORY_AUTO_RETAIN          1/0（默认 1）
    MEMORY_AUTO_RECALL          1/0（默认 1）
    MEMORY_RETAIN_TAGS          逗号分隔的 tags（默认空）

启动顺序（另开终端）：
    docker compose down -v && docker compose up -d   # 重建 DB（schema 变了）
    python scripts/mock_memory_server.py
    export MEMORY_SERVICE_URL=http://127.0.0.1:8765
    # V23.2 起：在 nano 目录下也行，在用户项目目录下也行
    python /path/to/nano/main.py --cwd /Users/foo/Documents/book
    # 或者：
    cd /Users/foo/Documents/book && python /path/to/nano/main.py
"""

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(override=True)


def _parse_cli_args() -> argparse.Namespace:
    """V23.2: ``--cwd PATH`` 让 agent 能在任意目录下启动。

    必须在所有"按 cwd 工作"的代码（PromptBuilder 项目上下文段、terminal /
    read_file 工具的相对路径）之前 ``os.chdir``。所以解析+chdir 都在模块顶层
    跑，早于 PromptBuilder 构造。

    --cwd 不传 → 保留 ``os.getcwd()``（V0–V23.1 行为，向下兼容）。
    --cwd 传了但目录不存在 → 立刻报错退出（fail-fast，避免后续诡异路径错）。
    """
    parser = argparse.ArgumentParser(
        description="Nano Hermes Agent — 教学版多智能体 AI Agent",
        add_help=True,
    )
    parser.add_argument(
        "--cwd",
        type=str,           
        default='./', #'/Users/qshf/Documents/book',
        metavar="PATH",
        help=(
            "启动后切到此目录工作。terminal / read_file 等工具的相对路径以及 "
            "system prompt 注入的 nano-hermes-agent.md / AGENTS.md 都从这里找。"
            "不传则用当前 shell cwd。"
        ),
    )
    return parser.parse_args()


_cli_args = _parse_cli_args()
if _cli_args.cwd is not None:
    _target = Path(_cli_args.cwd).expanduser().resolve()
    if not _target.is_dir():
        print(f"  [error] --cwd {_cli_args.cwd!r} 不存在或不是目录")
        sys.exit(2)
    os.chdir(_target)

from model_tools import get_tool_definitions, get_available_tool_names
from tools.registry import registry
from tools.skill_view_tool import set_skill_loader as _inject_skill_loader
from tools.delegate_tool import (
    DelegateContext,
    set_delegate_context as _inject_delegate_context,
)
from memory import BuiltinMemoryProvider, MemoryManager, RemoteSemanticProvider
from context_compressor import ContextCompressor
from transports.chain import FailoverExhausted, build_chain_from_env
from transports.client_factory import make_llm_client
from transports.streaming import (
    EVENT_DONE,
    EVENT_REASONING_DELTA,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_ARGUMENTS_DELTA,
    EVENT_TOOL_ARGUMENTS_FINISHED,
    EVENT_TOOL_CALL_STARTED,
    CancelToken,
    StreamCancelled,
)
from transports.types import build_assistant_history_msg
from agent import (
    PHASE_ASSISTANT_GENERATING_TEXT,
    PHASE_ASSISTANT_GENERATING_TOOL_ARGUMENTS,
    PHASE_TOOL_EXECUTING,
    PromptBuilder,
    SkillLoader,
    VoiceEventSink,
    build_turn_event_envelope,
    preview_tool_result,
)
from agent.compaction import apply_compaction
from agent.logging import setup_logging, set_log_session, get_logger
from agent.runtime import AgentRuntime, SESSION_TOKEN_KEYS
from agent.session_store import SessionStore
import cli  # 触发 cli/commands 下所有命令的装饰器注册

# V22 输入体验：用 prompt_toolkit 的 PromptSession 取代内建 ``input()``。
# 内建 input() 在 macOS 默认走 libedit，对 CJK 多字节字符的退格按"字节"
# 而非"字符"删 — 视觉上字消失但 stdin buffer 残留半截 UTF-8 字节，回车
# 送出去就是乱串或空串。PromptSession 正确处理多字节 / IME / 退格 / 历史
# 上下键，与源项目 hermes-agent CLI 同向。
#
# 多行输入（Esc → Enter）：
#   ``multiline=True`` 时所有 Enter 都换行，不会送出。我们要 "默认 Enter
#   送出 + Esc-Enter 换行"，所以 ``multiline=False`` + 自定义键绑定让
#   Esc-Enter 主动插入换行符。
#
# Ctrl+C / Ctrl+D 仍按 input() 语义抛 KeyboardInterrupt / EOFError，
# 与现有 try/except 代码兼容（prompt_toolkit 自己拦截，不走我们的 SIGINT
# handler；流式期间的 SIGINT handler 由 V22 cancel_token 路径接管）。
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

_HISTORY_PATH = Path.home() / ".nano_hermes_history"

_kb = KeyBindings()


@_kb.add("escape", "enter")
def _insert_newline(event):
    """Esc-Enter（先按 Esc 松开再按 Enter）→ 在缓冲里插入 ``\\n``。

    注意 macOS Terminal.app 下 Option-Enter 不会发送 Esc 序列；如需要
    Option-Enter，自行去 Terminal → Settings → Profiles → Keyboard
    勾选 "Use Option as Meta key"。iTerm2 / Alacritty 默认就支持。
    """
    event.current_buffer.insert_text("\n")


_input_session = PromptSession(
    history=FileHistory(str(_HISTORY_PATH)),
    key_bindings=_kb,
    multiline=False,
)

# ─── 配置 ────────────────────────────────────────────────────────────────────
ENABLED_TOOLSETS = ["core"]

# V8: 通过 manager 编排 provider；V10: 按环境变量加挂外部 provider
memory_manager = MemoryManager()
memory_manager.add_provider(BuiltinMemoryProvider())

# V10/V10.1: 按需注册远端语义记忆 provider（mock 服务见 scripts/mock_memory_server.py）
# V11: 支持 memory_mode / prefetch_method / bank_id / retain_tags 配置
_remote_url = os.environ.get("MEMORY_SERVICE_URL", "").strip()
if _remote_url:
    _tags_raw = os.environ.get("MEMORY_RETAIN_TAGS", "").strip()
    _retain_tags = [t.strip() for t in _tags_raw.split(",") if t.strip()] if _tags_raw else []

    _remote = RemoteSemanticProvider(
        base_url=_remote_url,
        bank_id=os.environ.get("MEMORY_BANK_ID", "hermes"),
        budget=os.environ.get("MEMORY_RECALL_BUDGET", "mid"),
        memory_mode=os.environ.get("MEMORY_MODE", "hybrid"),
        prefetch_method=os.environ.get("MEMORY_PREFETCH_METHOD", "recall"),
        auto_retain=os.environ.get("MEMORY_AUTO_RETAIN", "1") not in ("0", "false", "False", ""),
        auto_recall=os.environ.get("MEMORY_AUTO_RECALL", "1") not in ("0", "false", "False", ""),
        retain_tags=_retain_tags,
        retain_every_n_turns=max(1, int(os.environ.get("MEMORY_RETAIN_EVERY_N_TURNS", "1"))),
    )
    if _remote.is_available():
        memory_manager.add_provider(_remote)
    else:
        print(f"  [warn] MEMORY_SERVICE_URL set but {_remote_url}/healthz unreachable — skipping")
        _remote.shutdown()

memory_manager.initialize_all(session_id=os.environ.get("MEMORY_SESSION_ID", "default"))


# V21.3: skill 系统 — progressive disclosure tier 1
# 一级目录约定 ``skills/<name>/SKILL.md``；scan 失败的单个 skill 会被跳过且打印
# warning，不影响 agent 启动。skill_loader 同时注入到 PromptBuilder（tier 1
# 索引段）和 tools/skill_view_tool（tier 2 工具回调），保证两条路径看到的是
# 同一份 metadata 缓存。
skill_loader = SkillLoader(Path(__file__).parent / "skills")
skill_loader.scan()
_inject_skill_loader(skill_loader)


# V21.2: 三段式 PromptBuilder 替换 V4 的 SYSTEM_PROMPT.format(...)
# 段顺序固定（骨架 → 项目上下文 → skill 索引 → memory → 工具列表），空段自动跳过；
# V21.3 起 SkillLoader 实际注入，tier 1 索引段开始填充内容。
# V23.2 起 cwd 注入 → system prompt 自动加用户项目根的 nano-hermes-agent.md /
# AGENTS.md。``Path.cwd()`` 在 ``--cwd`` 已 chdir 之后捕获，所以等价用户传入值。
prompt_builder = PromptBuilder(
    get_toolset_tool_names=get_available_tool_names,
    enabled_toolsets=ENABLED_TOOLSETS,
    memory_manager=memory_manager,
    skill_loader=skill_loader,
    cwd=Path.cwd(),
)


def build_system_prompt() -> str:
    """V4→V21.1 兼容入口；V21.2 起 thin wrapper 委托给 prompt_builder.build()。

    保留模块函数是为了兼容 V21.1 期间 ``AgentCtx.build_system_prompt`` 字段
    的 callable 类型 — 单测里 mock ctx 时直接传 ``lambda: "..."`` 即可。
    """
    return prompt_builder.build()


# ─── V22 流式辅助 ─────────────────────────────────────────────────────────


def _esc_listener(cancel_token, stop_event):
    """流式期间在后台读 stdin 单键 — Esc 触发取消。

    源项目 (cli.py:11372 ``handle_ctrl_c``) 把 agent 跑后台线程、prompt_toolkit
    Application 始终活跃，KeyBinding 直接调 ``agent.interrupt()``。nano 主循环
    单线程，agent 阻塞在 ``stream_call`` 时 PromptSession 不在场，stdin 无人管。

    所以 V22 在流式期间临时切 cbreak 模式 + 后台线程读单字节：见到 ``\\x1b``
    (Esc) 就 ``cancel_token.cancel()``，与 SIGINT handler 同样的"语义层取消"
    路径汇合。Ctrl+C (SIGINT) 仍保留作为备用通路。

    V23.4: listener 生命周期上提到整个 tool loop（含 delegate 子 agent
    执行期间）。命中 Esc **不退出**循环 —— 因为后续可能还有多轮 LLM /
    工具调用，每次 cancel 后 main loop 会 ``token.reset()`` 让下一轮可中断。
    退出唯一靠 ``stop_event.set()``。

    退出时还原 termios，否则 prompt_toolkit 下一次 prompt 会继承 cbreak 模式。

    非 tty 环境（pytest / 管道 / CI）跳过 — ``termios.tcgetattr`` 抛
    ``termios.error`` / ``OSError``，被外层 try 吃掉。
    """
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    try:
        old_attrs = termios.tcgetattr(fd)
    except (termios.error, OSError):
        return  # 非 tty，安静退出

    try:
        tty.setcbreak(fd)
        while not stop_event.is_set():
            # 0.1s 超时让循环每帧检查 stop_event；select 在 stop_event.set
            # 之前不会自然唤醒，必须靠 timeout 轮询
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            try:
                ch = os.read(fd, 1)
            except OSError:
                break
            if not ch:
                break
            if ch == b"\x1b":
                cancel_token.cancel()
                # 不 return —— tool loop 可能多轮，每轮前 main reset token，
                # 再次按 Esc 仍可中断下一轮。退出 listener 由 stop_event 控制。
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        except (termios.error, OSError):
            pass


def _stream_one_turn(chain, model, messages, tools, cancel_token, runtime: AgentRuntime | None = None):
    """跑一次 ``chain.stream_call`` 并实时打印增量 — 返回最终 NormalizedResponse。

    打印策略：
    - ``text_delta`` → 逐 token flush 到 stdout
    - ``reasoning_delta`` → 灰色前缀 ``[think]`` 一次性插入流首（DeepSeek/Kimi）；
      为简洁，nano 不为 reasoning 单独装饰一行 — 只是不让它和正文混淆
    - ``tool_call_started`` → 在文本流之间插入一行 ``[tool] <name>``
    - ``done`` → 拿到完整 ``NormalizedResponse``，return

    cancel：调用方负责在 stream_call 之前重置 token；本函数捕获
    ``StreamCancelled`` 后打印 ``[cancelled]`` 并返回 None，让上层回到 prompt。

    V23.4: Esc/Ctrl+C 监听器的启停**不**在本函数内 —— 由外层 tool loop 统一
    管理（覆盖 LLM 流式 + tool 执行的整段时间，含 delegate_task 子 agent）。
    """
    printed_prefix = False  # 是否已经写过 "Agent > "（仅 text 流时写）
    has_text = False
    saw_reasoning = False
    final_resp = None
    text_phase = None
    args_phase = None
    _args_activity_last_chars = 0
    _ARGS_ACTIVITY_THRESHOLD = int(os.environ.get("VOICE_ORCHESTRATOR_ARGUMENT_DELTA_MIN_CHARS", "512"))

    try:
        for ev in chain.stream_call(
            cancel_token=cancel_token,
            model=model,
            messages=messages,
            tools=tools,
        ):
            if ev.type == EVENT_TEXT_DELTA:
                if runtime is not None and text_phase is None:
                    text_phase = runtime.phase_tracker.start(PHASE_ASSISTANT_GENERATING_TEXT)
                if text_phase is not None:
                    text_phase.activity_event(delta_chars=len(ev.text))
                if not printed_prefix:
                    sys.stdout.write("\nAgent > ")
                    printed_prefix = True
                sys.stdout.write(ev.text)
                sys.stdout.flush()
                has_text = True
            elif ev.type == EVENT_REASONING_DELTA:
                # 不实时回显 reasoning（避免和正文交错）；保留在 NormalizedResponse
                # 里供下一轮回传给 DeepSeek/Kimi 的 thinking 模式。
                # 提示一次"模型在思考"足够。
                if not saw_reasoning:
                    sys.stdout.write("  [think] ...")
                    sys.stdout.flush()
                    saw_reasoning = True
            elif ev.type == EVENT_TOOL_CALL_STARTED:
                if runtime is not None and text_phase is not None:
                    runtime.phase_tracker.close(text_phase)
                    text_phase = None
                if runtime is not None:
                    args_phase = runtime.phase_tracker.start(
                        PHASE_ASSISTANT_GENERATING_TOOL_ARGUMENTS,
                        tool_name=ev.tool_name or "",
                        tool_call_id=ev.tool_call_id or "",
                    )
                    _args_activity_last_chars = 0
                if saw_reasoning and not has_text:
                    # 清掉 [think] ... 占位，让 tool 行从行首开始
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    saw_reasoning = False
                if has_text:
                    sys.stdout.write("\n")
                sys.stdout.write(f"  [tool] {ev.tool_name} (streaming args...)\n")
                sys.stdout.flush()
            elif ev.type == EVENT_TOOL_ARGUMENTS_DELTA:
                if runtime is not None and args_phase is None:
                    args_phase = runtime.phase_tracker.start(
                        PHASE_ASSISTANT_GENERATING_TOOL_ARGUMENTS,
                        tool_name=ev.tool_name or "",
                        tool_call_id=ev.tool_call_id or "",
                    )
                    _args_activity_last_chars = 0
                total = ev.total_chars or 0
                if args_phase is not None and (total - _args_activity_last_chars) >= _ARGS_ACTIVITY_THRESHOLD:
                    _args_activity_last_chars = total
                    args_phase.activity_event(
                        tool_name=ev.tool_name or "",
                        tool_call_id=ev.tool_call_id or "",
                        argument_field=ev.argument_field or "",
                        delta_chars=ev.delta_chars,
                        total_chars=total,
                    )
            elif ev.type == EVENT_TOOL_ARGUMENTS_FINISHED:
                if runtime is not None and args_phase is not None:
                    runtime.phase_tracker.close(
                        args_phase,
                        tool_name=ev.tool_name or "",
                        tool_call_id=ev.tool_call_id or "",
                        argument_field=ev.argument_field or "",
                        total_chars=ev.total_chars,
                    )
                    args_phase = None
            elif ev.type == EVENT_DONE:
                if runtime is not None and args_phase is not None:
                    runtime.phase_tracker.close(args_phase)
                    args_phase = None
                if runtime is not None and text_phase is not None:
                    runtime.phase_tracker.close(text_phase)
                    text_phase = None
                final_resp = ev.response
                if has_text:
                    sys.stdout.write("\n\n")
                elif saw_reasoning:
                    sys.stdout.write("\n")
                sys.stdout.flush()
    except StreamCancelled:
        if runtime is not None and args_phase is not None:
            runtime.phase_tracker.close(args_phase, status="cancelled")
        if runtime is not None and text_phase is not None:
            runtime.phase_tracker.close(text_phase, status="cancelled")
        sys.stdout.write("\n  [cancelled] (Esc / Ctrl+C — back to prompt)\n\n")
        sys.stdout.flush()
        return None

    return final_resp


def _voice_stream_only() -> bool:
    return os.environ.get("VOICE_ORCHESTRATOR_STREAM_ONLY", "1") not in ("0", "false", "False", "")


def _accumulate_parent_turn_tokens(runtime: AgentRuntime, usage) -> None:
    """把父 turn LLM usage 累加到 ``runtime.session_tokens``。

    与 ``tools/delegate_tool.py:_accumulate_runtime_tokens`` 镜像 —— 同一份
    runtime 字典，由父 turn 路径 + 子 worker 路径并发累加；后者已用模块级
    lock 保护，父 turn 单线程不需要再 lock（main loop 唯一线程）。

    ``usage`` 为 None / 缺字段 时静默跳过 —— 容忍 partial NormalizedResponse。
    """
    if usage is None:
        return
    field_map = {
        "input": "prompt_tokens",
        "output": "completion_tokens",
        "cache_read": "cached_tokens",
        "cache_write": "cache_creation_tokens",
    }
    for key in SESSION_TOKEN_KEYS:
        v = getattr(usage, field_map[key], 0) or 0
        if isinstance(v, (int, float)):
            runtime.session_tokens[key] += int(v)


def run_agent():


    # V25.1: 日志子系统开机 —— 必须在 chain 构建 / 任何 transport 调用之前，
    # 否则那些早期 record 收不到。setup_logging 配 root（让 transports.chain /
    # delegate / memory 等已有 getLogger(__name__) 模块的日志都流经 RedactingFormatter
    # 脱敏），幂等。LOG_FILE=:none: 关闭文件日志只留 stderr。
    setup_logging()
    log = get_logger()

    # V19: 构建 transport 链 — TRANSPORT_CHAIN 优先（多家），否则退化为
    # TRANSPORT_MODE 单家（行为同 V18）。chain 长度 1 时仍带 RETRYABLE 错误重试，
    # 但不会切换 — 链长 ≥ 2 才有 failover 价值。
    chain_env = os.environ.get("TRANSPORT_CHAIN", "").strip()
    if not chain_env:
        chain_env = os.environ.get("TRANSPORT_MODE", "chat_completions")

    chain = build_chain_from_env(
        chain_env,
        client_factory=make_llm_client,
        failure_threshold=int(os.environ.get("FAILOVER_FAILURE_THRESHOLD", "3")),
        cooldown_seconds=float(os.environ.get("FAILOVER_COOLDOWN_SECONDS", "60")),
        max_retries=int(os.environ.get("FAILOVER_MAX_RETRIES", "2")),
        base_delay=float(os.environ.get("FAILOVER_BASE_DELAY", "1.0")),
        cache_enabled=os.environ.get("PROMPT_CACHE_ENABLED", "0") not in ("0", "false", "False", ""),
        cache_ttl=os.environ.get("PROMPT_CACHE_TTL", "5m"),
    )

    # 兼容存量代码路径 — 主家用于 client 引用（仅作为 compressor.compress 第三个
    # 位置参数兼容签名传入；真正的 LLM 调用统一走 chain.call()）。
    client = chain.primary_client
    # V25.1 修复：model 优先取 chain 主家自带的内联模型名（TRANSPORT_CHAIN 里的
    # ``api_mode:model``），而非直接读 MODEL env。此前直接 ``os.environ["MODEL"]``
    # 在只配 TRANSPORT_CHAIN（未单设 MODEL）时回退到硬编码 ``gpt-4o-mini``，导致
    # save_session 把一个从未真正调用过的假模型名写进 sessions 表 —— v25.1 insights
    # 第一次把 model 字段聚合成报表才暴露出这个 v24 持久化 bug（详见 decisions/v25.1.md
    # 决策 8）。chain.primary_model 为 None（legacy 单家未内联）时才回退到 env。
    model = chain.primary_model or os.environ.get("MODEL", "gpt-4o-mini")

    # V25.1: 启动埋点 —— chain 配置进结构化日志（排查"哪家 model / 链怎么配的"有据）
    log.info(
        "agent boot: model=%s chain=[%s] failover(threshold=%s cooldown=%ss)",
        model,
        ",".join(f"{e.api_mode}:{e.model or '-'}" for e in chain.entries),
        chain.failure_threshold,
        chain.cooldown_seconds,
    )

    # V22: 流式开关 — env 默认开；运行期 /stream on|off 切换
    stream_enabled = os.environ.get("STREAM_ENABLED", "1") not in ("0", "false", "False", "")

    # V22: 全局 cancel token + SIGINT handler — V23.3 起也注入到 delegate
    # 上下文，让父子共享同一个 token（父 Ctrl+C → 所有子下一帧退出）
    # prompt 上 SIGINT → 走 KeyboardInterrupt 退出（Python 默认行为）
    # LLM 调用期间 SIGINT → 设置 token，stream_call 内部循环 check 后 raise
    #   StreamCancelled，main 捕获后回到 prompt 不退出
    # 用一个布尔 `streaming_active` 区分两种语境 — handler 只在流式期间
    # 翻译为 cancel；prompt 期间让 KeyboardInterrupt 自然抛出
    cancel_token = CancelToken()
    runtime = AgentRuntime(
        stream_enabled=stream_enabled,
        cancel_token=cancel_token,
    )
    voice_event_sink = VoiceEventSink.create_from_env()
    if voice_event_sink is not None:
        runtime.voice_event_sink = voice_event_sink
        voice_event_sink.start()
        log.info("voice orchestrator sink on")
    registry.set_runtime(runtime)
    # V23.4: agent_busy 覆盖整轮 tool loop（含 delegate 子 agent 执行期间），
    # 取代仅在父流式期间为 True 的 streaming_active。SIGINT / Esc listener 据此
    # 判断"现在按取消是取消 agent，还是退出 prompt"。
    agent_busy = threading.Event()

    # V23.0: 注入 delegate_task 工具的运行期上下文 —— chain 构建完才注入。
    # 父全集 = registry 注册过的 + memory_manager 暴露的；黑名单（delegate_task /
    # memory_*）由 ``_resolve_child_toolset`` 自己过滤。check_fn 在注入完成后
    # 才让 delegate_task 暴露给父 LLM。
    # V23.3: delegate 注入共享 runtime —— 子 agent 与父共享同一个 cancel_token；
    # stream_enabled 也动态读取，让 /stream on|off 能同时影响父 loop 和子 loop。
    _parent_full_toolset = sorted(
        set(registry.tool_names) | set(memory_manager.get_all_tool_names())
    )
    _inject_delegate_context(DelegateContext(
        chain=chain,
        model=model,
        parent_toolset_names=set(_parent_full_toolset),
        runtime=runtime,
    ))

    messages = [{"role": "system", "content": build_system_prompt()}]

    # V8: 通过 manager 拿到内置 provider，用于 banner 和 /memory 命令
    builtin_provider = memory_manager.get_provider("builtin")

    # V14: 跟踪当前 session_id
    current_session_id = os.environ.get("MEMORY_SESSION_ID", "default")

    # V15: 上下文压缩器
    compressor = ContextCompressor()

    # V24.0: 会话持久化 store —— 启动即建库（SESSION_DB_PATH 覆盖路径，
    # ":memory:" 关闭落盘退化到 v14 ephemeral 行为）。若当前 session_id 已有
    # 存档，把历史挂回 messages（system 重建在前 + 历史在后），并恢复
    # turn_count / runtime.session_tokens —— 这一步就是 v24.0 修掉假 resume 的
    # 核心：进程重启后同一 MEMORY_SESSION_ID 能续上对话。
    # V24.1: 启动先 resolve_resume_tip —— 若该 session 曾被压缩分裂（成了链 root），
    # 跳到压缩后最新 tip，避免重载压缩前超长 root 后下一轮立刻重压。
    session_store = SessionStore()
    resumed_note = "new"
    current_session_id = session_store.resolve_resume_tip(current_session_id)
    _saved = session_store.load(current_session_id)
    if _saved is not None and _saved["messages"]:
        messages.extend(_saved["messages"])
        turn_count = _saved["turn_count"]
        for _k in SESSION_TOKEN_KEYS:
            runtime.session_tokens[_k] = _saved["session_tokens"].get(_k, 0)
        resumed_note = f"resumed {len(_saved['messages'])} msgs, turn {turn_count}"

    # V25.1: session_id 已 resolve（含 resume 重定向到压缩链 tip），绑定到 thread-local
    # —— 之后主循环所有 log record 带 [session_id]。/new /resume 切会话与压缩分裂时
    # 会重新绑定（见下方两处 set_log_session）。
    set_log_session(current_session_id)
    log.info("session ready: %s (%s)", current_session_id, resumed_note)

    print("=" * 60)
    print("  Nano Hermes Agent v23.4 — 多智能体结构化结果 + 父子成本聚合")
    print(f"  Default MODEL (entries 不内联时回退到此): {model}")
    chain_modes = " → ".join(
        f"{e.api_mode}({e.model})" if e.model else e.api_mode
        for e in chain.entries
    )
    print(f"  Transport chain: {chain_modes}")
    print(f"    failure_threshold={chain.failure_threshold}, "
          f"cooldown={chain.cooldown_seconds}s, "
          f"max_retries={chain.max_retries}, base_delay={chain.base_delay}s")
    if chain.cache_enabled:
        print(f"    prompt_cache: enabled (ttl={chain.cache_ttl}, "
              f"strategy=system_and_3, applies to anthropic_messages only)")
    else:
        print(f"    prompt_cache: disabled (set PROMPT_CACHE_ENABLED=1 to enable)")
    print(f"  Streaming: {'on' if stream_enabled else 'off'} (toggle: /stream on|off)")
    print(f"  Session: {current_session_id} ({resumed_note})")
    print(f"  Toolsets: {ENABLED_TOOLSETS}")
    print(f"  Memory providers: {[p.name for p in memory_manager.providers]}")
    if builtin_provider is not None:
        store = builtin_provider.store
        print(f"    file: {store.file_path}")
        print(f"    entries: {len(store.entries)}, "
              f"usage: {store.char_count()}/{store.char_limit} chars")
    print(f"  Compression: threshold={compressor.threshold_tokens}tok, "
          f"tail_budget={compressor.tail_token_budget}tok, "
          f"protect_head={compressor.protect_first_n}")
    print(f"  Available tools: {', '.join(get_available_tool_names(ENABLED_TOOLSETS))}")
    print(f"  Memory tools: {', '.join(sorted(memory_manager.get_all_tool_names()))}")
    skill_names = skill_loader.names()
    if skill_names:
        print(f"  Skills: {len(skill_names)} loaded ({', '.join(skill_names)})")
    else:
        print(f"  Skills: 0 loaded (skills/ empty or absent)")
    # V23.2: 显示 cwd + 命中的项目上下文文件（NANO_IGNORE_RULES=1 时跳过）
    _cwd_now = Path.cwd()
    _ignore_rules = os.environ.get("NANO_IGNORE_RULES", "0") not in ("0", "false", "False", "")
    if _ignore_rules:
        print(f"  Working dir: {_cwd_now} (project context: skipped via NANO_IGNORE_RULES)")
    else:
        from agent.prompt_builder import PROJECT_CONTEXT_FILE_NAMES as _PC_NAMES
        _hit = next((n for n in _PC_NAMES if (_cwd_now / n).is_file()), None)
        if _hit:
            print(f"  Working dir: {_cwd_now} (project context: {_hit})")
        else:
            print(f"  Working dir: {_cwd_now} (project context: none — tried {', '.join(_PC_NAMES)})")
    print("  Commands: /help to list all (V21.1: dispatched via cli registry)")
    print("  输入 'quit' 退出")
    print("=" * 60)
    print()

    turn_count = 0  # V9: 每轮递增，传给 on_turn_start

    def _send_turn_event(
        event_type: str,
        *,
        assistant_text: str = "",
        reasoning_activity: str = "",
        next_tool_name: str = "",
        tool=None,
        phase=None,
    ) -> None:
        if runtime.voice_event_sink is None:
            return
        if _voice_stream_only() and not runtime.stream_enabled:
            return
        # envelope 构建在主线程同步执行（redaction 正则 / str() 任意内容 / asdict），
        # 网络发送的 fail-safe 在 sink 内部，保护不到这里。任何构建期异常都不能掀翻
        # 用户的主 turn —— 语音是旁路（v27.1 review #4）。
        try:
            envelope = build_turn_event_envelope(
                event_type=event_type,
                session_id=current_session_id,
                turn_id=f"turn-{turn_count}",
                messages=messages,
                assistant_text=assistant_text,
                reasoning_activity=reasoning_activity,
                next_tool_name=next_tool_name,
                tool=tool,
                phase=phase,
            )
            runtime.voice_event_sink.submit(envelope)
        except Exception:  # noqa: BLE001 — 语音旁路绝不影响主 turn
            log.debug("voice turn-event build/submit failed", exc_info=True)

    runtime.phase_tracker.set_listener(lambda status, phase: _send_turn_event(status, phase=phase))

    # V22 cancel_token / agent_busy 已在 delegate 注入前提前构造（见上方）。
    # 这里仅注册 SIGINT handler —— 用 agent_busy.is_set() 区分两种语境：
    # - prompt 期间 SIGINT → 还原默认行为，让 input() 抛 KeyboardInterrupt 退出
    # - tool loop 期间 SIGINT → cancel_token.cancel()，stream_call / 子 agent
    #   下一帧 check 后 raise StreamCancelled，main 捕获后回到 prompt 不退出
    # V23.4: 把判定从"父正在流式"扩到"父在跑 tool loop"，覆盖 delegate 子 agent
    # 执行期间的 Ctrl+C —— 否则按下后会走 KeyboardInterrupt 退出整个进程。
    def _sigint_handler(signum, frame):
        if agent_busy.is_set():
            cancel_token.cancel()
            # 不抛异常 — stream_call / 子 loop 检查 token 自己 raise StreamCancelled
        else:
            # prompt 期间 — 还原默认 SIGINT 行为，让 input() 抛 KeyboardInterrupt
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)


    # V21.1: slash handler 共享的运行期上下文
    ctx = cli.AgentCtx(
        messages=messages,
        current_session_id=current_session_id,
        turn_count=turn_count,
        chain=chain,
        client=client,
        model=model,
        memory_manager=memory_manager,
        builtin_provider=builtin_provider,
        compressor=compressor,
        registry=registry,
        enabled_toolsets=ENABLED_TOOLSETS,
        build_system_prompt=build_system_prompt,
        prompt_builder=prompt_builder,
        skill_loader=skill_loader,
        runtime=runtime,
        session_store=session_store,
    )

    try:
        while True:
            try:
                # V22: PromptSession 替代 input() — 修 macOS libedit 的 CJK
                # 退格 byte-vs-char bug；保留 Ctrl+C → KeyboardInterrupt 语义
                user_input = _input_session.prompt("You > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break

            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                print("Bye!")
                break

            # V21.1: slash 命令统一通过 cli.dispatch 路由到 cli/commands/*.py
            # handler 通过 ctx 直接 mutate 状态：
            #   - messages 与 ctx.messages 共享同一 list 引用（in-place ops）
            #   - session_id / turn_count 由 handler 改 ctx.* 后此处同步回局部
            if cli.dispatch(user_input, ctx):
                # V25.1: /new /resume 等命令可能切了会话 —— 重新绑定 thread-local
                # session_id，让后续日志归到新会话。仅在真变化时记一条。
                if ctx.current_session_id != current_session_id:
                    set_log_session(ctx.current_session_id)
                    log.info(
                        "session switched: %s -> %s (via slash command)",
                        current_session_id,
                        ctx.current_session_id,
                    )
                current_session_id = ctx.current_session_id
                turn_count = ctx.turn_count
                continue

            # V9 生命周期：每轮开始通知 + prefetch 召回
            # 在 user message 入队前完成，prefetch 结果用围栏包裹后注入
            memory_manager.on_turn_start_all(turn_count, user_input)
            recalled = memory_manager.prefetch_all(user_input)
            turn_count += 1

            # prefetch 结果与原始 user 输入合并到同一条 user message
            # 注入位置选 user message（不是 system prompt）有两个原因：
            #   1. 召回内容每轮不同，放进 system prompt 会破坏前缀缓存
            #   2. 围栏 + 系统注释明确告诉模型"这是召回内容，不是新输入"
            if recalled:
                user_message_content = f"{recalled}\n\n{user_input}"
            else:
                user_message_content = user_input

            messages.append({"role": "user", "content": user_message_content})
            _send_turn_event("turn_started")

            # 收集本轮 assistant 的最终文本响应（不含 tool call），用于 sync
            final_assistant_text = ""

            # V23.4: 整个 tool loop 期间挂 Esc 监听 + 把 agent_busy 置 True ——
            # 这样 Ctrl+C / Esc 在父流式、tool 执行、delegate 子 agent 跑任何
            # 阶段都走 cancel_token.cancel()（不退出进程）。
            _esc_stop = threading.Event()
            _esc_thread = threading.Thread(
                target=_esc_listener,
                args=(cancel_token, _esc_stop),
                daemon=True,
            )
            cancel_token.reset()
            agent_busy.set()
            _esc_thread.start()

            try:
                while True:
                    # V15: 压缩检查 — API 调用前判断是否需要压缩上下文
                    # V24.1: 压缩 + 会话分裂统一走 agent.compaction.apply_compaction
                    # （与手动 /compress 共用同一实现，避免两条路径行为漂移 / 丢数据）。
                    # apply_compaction 全程读写 ctx：调用前把局部 session_id/turn_count
                    # 同步进 ctx，调用后读回（auto 路径的 turn_count 此刻已 +1，ctx 直到
                    # 轮末才同步，所以这里手动对齐一次）。
                    if compressor.should_compress(messages):
                        print("  [compress] context exceeds threshold, compacting...")
                        ctx.current_session_id = current_session_id
                        ctx.turn_count = turn_count
                        did = apply_compaction(ctx)
                        current_session_id = ctx.current_session_id
                        if did:
                            # V25.1: 压缩分裂换了 session id —— 重新绑定 thread-local
                            set_log_session(current_session_id)
                            log.info(
                                "compaction split -> %s (%d msgs live)",
                                current_session_id,
                                len(messages),
                            )
                            print(
                                f"  [compress] split → {current_session_id} "
                                f"({len(messages)} msgs live, pre-compaction archived)"
                            )
                        else:
                            print("  [compress] skipped (no effective compaction)")

                    # 每轮重新计算：check_fn 结果可能变化（如用户中途装了 Docker）
                    tools_schema = get_tool_definitions(ENABLED_TOOLSETS)
                    # V8: 通过 manager 收集所有 provider 的 tool schema
                    provider_schemas = memory_manager.get_all_tool_schemas()
                    all_tools_schema = tools_schema + [
                        {"type": "function", "function": s} for s in provider_schemas
                    ]

                    # V19: 统一 LLM 调用走 chain — 主家失败自动切备家。
                    # V22: ctx.stream_enabled=True 走流式路径 — 实时打印 token、可中断
                    #      False 退化到 V21 行为 — 一次性返回后整段打印
                    # ValueError 仍按"响应不合法"处理，FailoverExhausted 表示链全挂。
                    normalized = None
                    streamed_text_already = False  # 流式路径已打印过 final text，不再二次打印
                    try:
                        if runtime.stream_enabled:
                            cancel_token.reset()
                            normalized = _stream_one_turn(
                                chain, model, messages, all_tools_schema, cancel_token, runtime,
                            )
                            if normalized is None:
                                # StreamCancelled — 用户取消，跳出 tool loop 回到 prompt
                                # 注意：messages 还没 append assistant，对话历史保持干净
                                break
                            # 标记：本轮如果是纯文本响应，已经在 _stream_one_turn 里打印过
                            streamed_text_already = not normalized.tool_calls
                        else:
                            normalized = chain.call(
                                model=model,
                                messages=messages,
                                tools=all_tools_schema,
                            )
                    except FailoverExhausted as e:
                        print(f"  [error] all transports failed: {e}")
                        break
                    except ValueError as exc:
                        print("  [warn] invalid response shape, skipping turn")
                        break

                    # 用 API 返回的真实 token 数更新压缩器（下一轮触发判断用）
                    if normalized.usage and normalized.usage.prompt_tokens:
                        '''
                        prompt_tokens — 输入（所有 messages + tools schema）的 token 数
                        completion_tokens — 本次 assistant 生成的 token 数
                        total_tokens — 两者之和
                        '''
                        compressor.update_usage(normalized.usage.prompt_tokens)

                    # 累加父 turn 用量到 runtime.session_tokens（独立于
                    # compressor —— 后者只用 prompt_tokens 做压缩阈值）
                    _accumulate_parent_turn_tokens(runtime, normalized.usage)

                    # 把标准化响应回填进对话历史（保持 OpenAI 消息 shape，下一轮 build_kwargs 还能消费）
                    # V15.1: 抢救 content=None + 无 tool_calls 的脏 assistant 消息（reasoning 提升为 content）
                    # 对齐源项目 run_agent.py:9621-9635（DeepSeek/Kimi/Moonshot thinking padding）
                    assistant_dump = build_assistant_history_msg(normalized)
                    messages.append(assistant_dump)

                    if not normalized.tool_calls:
                        final_assistant_text = normalized.content or ""
                        if not streamed_text_already:
                            # 同步路径或流式但从未拿到 text_delta（罕见 — provider 把
                            # 整段塞 done 帧）— 在 break 前补打一次
                            print(f"\nAgent > {final_assistant_text}\n")
                        break

                    for tool_call in normalized.tool_calls:
                        name = tool_call.function.name
                        try:
                            args = json.loads(tool_call.function.arguments)
                        except json.JSONDecodeError as exc:
                            from tools.result import tool_error
                            result = tool_error(
                                f"invalid tool arguments JSON: {exc}",
                                raw_arguments=tool_call.function.arguments[:500],
                            )
                            print(f"  [tool] {name}(<invalid JSON>)")
                            print(f"  [error] {exc} — asking LLM to retry")
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": result,
                            })
                            continue
                        pretty_args = json.dumps(args, ensure_ascii=False)
                        print(f"  [tool] {name}({pretty_args})")

                        # V8: 路由 — manager 接管的工具走 manager，其余走 registry。
                        # V27.1: memory tools 不经过 registry，但也只报告通用 tool phase
                        # 和安全 envelope；不在 main 里判断语音策略。
                        if memory_manager.has_tool(name):
                            _mem_started_at = time.monotonic()
                            _mem_phase = runtime.phase_tracker.start(PHASE_TOOL_EXECUTING, tool_name=name, tool_category="memory")
                            try:
                                result = memory_manager.handle_tool_call(name, args)
                            except Exception as exc:  # noqa: BLE001 — 保持原异常语义，但先关 phase
                                runtime.phase_tracker.close(_mem_phase, status="error", tool_name=name, error_type=type(exc).__name__)
                                raise
                            _duration_ms = int((time.monotonic() - _mem_started_at) * 1000)
                            try:
                                _mem_parsed = json.loads(result)
                                _mem_result_kind = "error" if isinstance(_mem_parsed, dict) and "error" in _mem_parsed else "ok"
                            except json.JSONDecodeError:
                                _mem_result_kind = "non_json"
                            runtime.phase_tracker.close(_mem_phase, tool_name=name, result_kind=_mem_result_kind)
                            _send_turn_event(
                                "tool_finished" if _mem_result_kind != "error" else "tool_error",
                                tool=preview_tool_result(name, _mem_result_kind, result, duration_ms=_duration_ms),
                            )
                        else:
                            result = registry.dispatch(name, args)

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result,
                        })

                        try:
                            parsed = json.loads(result)
                            if "error" in parsed:
                                print(f"  [error] {parsed['error']}")
                            elif "output" in parsed:
                                output = parsed["output"]
                                if len(output) > 200:
                                    output = output[:200] + "..."
                                print(f"  [result] {output}")
                            else:
                                print(f"  [result] (ok)")
                        except json.JSONDecodeError:
                            print(f"  [result] {result[:100]}")
            finally:
                # V23.4: 收回 busy 状态 + 通知 Esc listener 退出、恢复 termios。
                # 放 finally 是因为 tool loop 中途任何 break / 异常都得保证 termios
                # 被还原 —— 否则 prompt_toolkit 下一次 prompt 会继承 cbreak 模式。
                agent_busy.clear()
                _esc_stop.set()
                _esc_thread.join(timeout=1.0)

            _send_turn_event("turn_finished", assistant_text=final_assistant_text)

            # V9 生命周期：tool loop 结束后持久化对话
            # sync 用原始 user 输入（不含围栏），保持后端记录干净
            memory_manager.sync_all(user_input, final_assistant_text)

            # V13: 预热下一轮的 recall — 用当轮 user input 作为 query
            memory_manager.queue_prefetch_all(user_input)

            # V21.1: 同步本轮 turn_count 到 ctx（messages / session_id 已通过引用共享）
            ctx.turn_count = turn_count

            # V24.1: 轮末持久化 —— append-only 只追加游标之后的新消息（剔除
            # system）。放在 sync_all 之后，保证即使进程随后 crash 也只丢"未走到
            # 这里"的当轮。current_session_id 可能被压缩分裂改过（见 tool loop 内
            # 的分裂回调），用 ctx 上的当前值最稳妥。append 取代 v24.0 的 save：
            # 只增不删，压缩前全文永久留在被分裂封存的旧 session 里。
            session_store.append(
                ctx.current_session_id,
                messages,
                turn_count=turn_count,
                model=model,
                session_tokens=runtime.session_tokens,
            )
    finally:
        # V24.1: 退出兜底 —— append 当前会话残余 + 关库。覆盖"轮中途按 quit /
        # Ctrl+D 退出"的边界（轮末写没走到）。append 幂等：已写的不重插，游标自动
        # 续上。close() 让 WAL checkpoint 回主库。包 try 防 save 失败遮蔽 memory
        # shutdown（两者都要尽力跑完）。
        try:
            session_store.append(
                ctx.current_session_id,
                messages,
                turn_count=ctx.turn_count,
                model=model,
                session_tokens=runtime.session_tokens,
            )
        except Exception as exc:
            print(f"  [warn] session save on exit failed: {exc!r}")
        finally:
            session_store.close()
        # V25.0: 退出兜底 trajectory flush —— 把整段会话转 ShareGPT 落一份训练样本。
        # 定调"整段 flush"而非轮末增量：ShareGPT 是"一行一个完整对话"，轮末
        # append 增量会产生半截对话碎片，与该语义冲突。压缩点已 flush 被分裂走的
        # 旧段（compaction.py），这里 flush 退出时的最终段，两点合起来覆盖全程。
        # completed=True：正常退出 = 这段跑完了；若中途 quit 留下未闭合 <think>，
        # has_incomplete_think 会把它分流到 *_failed.jsonl，不污染训练集。
        try:
            from agent.trajectory import flush_session_trajectory
            flush_session_trajectory(
                messages, model=model, completed=True,
                filename_stem=ctx.current_session_id,
            )
        except Exception as exc:  # noqa: BLE001 — trajectory 落盘永不阻断退出
            print(f"  [warn] trajectory flush on exit failed: {exc!r}")
        # V10: 释放外部 provider 的 httpx client；builtin 的 shutdown 是 no-op
        memory_manager.shutdown_all()
        # V27.1: 语音 orchestrator sink 是纯 side-channel，退出时尽力收尾。
        if runtime.voice_event_sink is not None:
            runtime.voice_event_sink.stop()


if __name__ == "__main__":
    run_agent()
