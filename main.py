"""
Nano Hermes Agent — V22: 流式输出 + 中断

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
    python main.py
"""

import json
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(override=True)

from model_tools import get_tool_definitions, get_available_tool_names
from tools.registry import registry
from tools.skill_view_tool import set_skill_loader as _inject_skill_loader
from memory import BuiltinMemoryProvider, MemoryManager, RemoteSemanticProvider
from context_compressor import ContextCompressor
from transports.chain import FailoverExhausted, build_chain_from_env
from transports.client_factory import make_llm_client
from transports.streaming import (
    EVENT_DONE,
    EVENT_REASONING_DELTA,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALL_STARTED,
    CancelToken,
    StreamCancelled,
)
from agent import PromptBuilder, SkillLoader
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
# 段顺序固定（骨架 → skill 索引 → memory → 工具列表），空段自动跳过；
# V21.3 起 SkillLoader 实际注入，tier 1 索引段开始填充内容。
prompt_builder = PromptBuilder(
    get_toolset_tool_names=get_available_tool_names,
    enabled_toolsets=ENABLED_TOOLSETS,
    memory_manager=memory_manager,
    skill_loader=skill_loader,
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

    退出条件二选一：读到 Esc / ``stop_event.set()``（主线程在流结束时通知）。
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
                return
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        except (termios.error, OSError):
            pass


def _stream_one_turn(chain, model, messages, tools, cancel_token):
    """跑一次 ``chain.stream_call`` 并实时打印增量 — 返回最终 NormalizedResponse。

    打印策略：
    - ``text_delta`` → 逐 token flush 到 stdout
    - ``reasoning_delta`` → 灰色前缀 ``[think]`` 一次性插入流首（DeepSeek/Kimi）；
      为简洁，nano 不为 reasoning 单独装饰一行 — 只是不让它和正文混淆
    - ``tool_call_started`` → 在文本流之间插入一行 ``[tool] <name>``
    - ``done`` → 拿到完整 ``NormalizedResponse``，return

    cancel：调用方负责在 stream_call 之前重置 token；本函数捕获
    ``StreamCancelled`` 后打印 ``[cancelled]`` 并返回 None，让上层回到 prompt。
    """
    printed_prefix = False  # 是否已经写过 "Agent > "（仅 text 流时写）
    has_text = False
    saw_reasoning = False
    final_resp = None

    # V22: 启动 Esc 监听后台线程（仅在 tty 上有效）
    import threading as _threading
    _stop = _threading.Event()
    _listener = _threading.Thread(
        target=_esc_listener,
        args=(cancel_token, _stop),
        daemon=True,
    )
    _listener.start()

    try:
        for ev in chain.stream_call(
            cancel_token=cancel_token,
            model=model,
            messages=messages,
            tools=tools,
        ):
            if ev.type == EVENT_TEXT_DELTA:
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
                if saw_reasoning and not has_text:
                    # 清掉 [think] ... 占位，让 tool 行从行首开始
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    saw_reasoning = False
                if has_text:
                    sys.stdout.write("\n")
                sys.stdout.write(f"  [tool] {ev.tool_name} (streaming args...)\n")
                sys.stdout.flush()
            elif ev.type == EVENT_DONE:
                final_resp = ev.response
                if has_text:
                    sys.stdout.write("\n\n")
                elif saw_reasoning:
                    sys.stdout.write("\n")
                sys.stdout.flush()
    except StreamCancelled:
        sys.stdout.write("\n  [cancelled] (Esc / Ctrl+C — back to prompt)\n\n")
        sys.stdout.flush()
        return None
    finally:
        # 无论流正常结束 / cancel / 异常，都通知 listener 退出并恢复 termios
        _stop.set()
        _listener.join(timeout=1.0)

    return final_resp


def run_agent():


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
    model = os.environ.get("MODEL", "gpt-4o-mini")

    messages = [{"role": "system", "content": build_system_prompt()}]

    # V8: 通过 manager 拿到内置 provider，用于 banner 和 /memory 命令
    builtin_provider = memory_manager.get_provider("builtin")

    # V14: 跟踪当前 session_id
    current_session_id = os.environ.get("MEMORY_SESSION_ID", "default")

    # V15: 上下文压缩器
    compressor = ContextCompressor()

    # V22: 流式开关 — env 默认开；运行期 /stream on|off 切换
    stream_enabled = os.environ.get("STREAM_ENABLED", "1") not in ("0", "false", "False", "")

    print("=" * 60)
    print("  Nano Hermes Agent v22 — 流式输出 + 中断")
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
    print(f"  Session: {current_session_id}")
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
    print("  Commands: /help to list all (V21.1: dispatched via cli registry)")
    print("  输入 'quit' 退出")
    print("=" * 60)
    print()

    turn_count = 0  # V9: 每轮递增，传给 on_turn_start

    # V22: 全局 cancel token + SIGINT handler
    # prompt 上 SIGINT → 走 KeyboardInterrupt 退出（Python 默认行为）
    # LLM 调用期间 SIGINT → 设置 token，stream_call 内部循环 check 后 raise
    #   StreamCancelled，main 捕获后回到 prompt 不退出
    # 用一个布尔 `streaming_active` 区分两种语境 — handler 只在流式期间
    # 翻译为 cancel；prompt 期间让 KeyboardInterrupt 自然抛出
    cancel_token = CancelToken()
    streaming_active = {"flag": False}

    def _sigint_handler(signum, frame):
        if streaming_active["flag"]:
            cancel_token.cancel()
            # 不抛异常 — stream_call 检查 token 自己 raise StreamCancelled
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
        stream_enabled=stream_enabled,
        cancel_token=cancel_token,
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

            # 收集本轮 assistant 的最终文本响应（不含 tool call），用于 sync
            final_assistant_text = ""

            while True:
                # V15: 压缩检查 — API 调用前判断是否需要压缩上下文
                if compressor.should_compress(messages):
                    print("  [compress] context exceeds threshold, compacting...")
                    head_end = compressor.protect_first_n
                    memory_manager.on_pre_compress_all(messages[head_end:])
                    # V19: 摘要 LLM 调用也走 chain，享受 failover
                    # V21.1: 用 in-place 替换避免局部 messages 与 ctx.messages 引用分歧
                    compacted = compressor.compress(messages, client, model, transport=chain)
                    messages[:] = compacted
                    print(f"  [compress] compacted to {len(messages)} messages")

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
                    if ctx.stream_enabled:
                        cancel_token.reset()
                        streaming_active["flag"] = True
                        try:
                            normalized = _stream_one_turn(
                                chain, model, messages, all_tools_schema, cancel_token,
                            )
                        finally:
                            streaming_active["flag"] = False
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
                except ValueError:
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

                # 把标准化响应回填进对话历史（保持 OpenAI 消息 shape，下一轮 build_kwargs 还能消费）
                assistant_dump: dict = {"role": "assistant", "content": normalized.content}
                if normalized.tool_calls:
                    assistant_dump["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments},
                        }
                        for tc in normalized.tool_calls
                    ]
                # DeepSeek/Kimi/Moonshot thinking mode 要求每条 assistant 消息回传 reasoning_content
                # 否则下一轮请求 400: "The reasoning_content in the thinking mode must be passed back"
                # 对齐源项目 run_agent.py:9621-9635（pad 一个空格避开 DeepSeek V4 Pro 的非空校验）
                rc = normalized.reasoning_content
                if rc is not None:
                    assistant_dump["reasoning_content"] = rc
                elif normalized.tool_calls:
                    assistant_dump["reasoning_content"] = " "
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
                    args = json.loads(tool_call.function.arguments)
                    pretty_args = json.dumps(args, ensure_ascii=False)
                    print(f"  [tool] {name}({pretty_args})")

                    # V8: 路由 — manager 接管的工具走 manager，其余走 registry
                    if memory_manager.has_tool(name):
                        result = memory_manager.handle_tool_call(name, args)
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

            # V9 生命周期：tool loop 结束后持久化对话
            # sync 用原始 user 输入（不含围栏），保持后端记录干净
            memory_manager.sync_all(user_input, final_assistant_text)

            # V13: 预热下一轮的 recall — 用当轮 user input 作为 query
            memory_manager.queue_prefetch_all(user_input)

            # V21.1: 同步本轮 turn_count 到 ctx（messages / session_id 已通过引用共享）
            ctx.turn_count = turn_count
    finally:
        # V10: 释放外部 provider 的 httpx client；builtin 的 shutdown 是 no-op
        memory_manager.shutdown_all()


if __name__ == "__main__":
    run_agent()
