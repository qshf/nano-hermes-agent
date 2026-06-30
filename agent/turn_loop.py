"""V27.1 重构 — 运行层（REPL + turn loop），从 ``main.py`` 抽出。

为什么独立成文件
================
配套 ``agent/bootstrap.py``（装配层）。装配产物打包成 ``AgentServices`` 后，
本模块的 ``run_repl(services)`` 负责真正的运行：prompt 循环 → slash 分发 →
memory prefetch → LLM turn loop（流式 / 同步）→ tool 分发 → 压缩 → 会话持久化。

原本这些和装配挤在 ``main.py:run_agent`` 一个 558 行的函数里。切开后 main.py
退回瘦入口（解析 --cwd + chdir + 调 run_repl），装配看 bootstrap，运行看这里。
纯结构搬运，零行为改动。

线程 / 取消模型（搬运前后不变）
==============================
- ``_esc_listener`` 后台读 stdin 单键，Esc → ``cancel_token.cancel()``。
- ``_sigint_handler`` 用 ``agent_busy`` 区分 prompt 期（KeyboardInterrupt 退出）
  与 tool loop 期（cancel 回到 prompt）。
- 两者都在 ``run_repl`` 内注册 / 启停，覆盖父流式 + tool 执行 + delegate 子 agent。
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading

import cli
from agent.bootstrap import ENABLED_TOOLSETS, AgentServices
from agent.compaction import apply_compaction
from agent.env import env_bool, env_int
from agent.logging import set_log_session
from agent.runtime import AgentRuntime, SESSION_TOKEN_KEYS
from agent import (
    PHASE_ASSISTANT_GENERATING_TEXT,
    PHASE_ASSISTANT_GENERATING_TOOL_ARGUMENTS,
    PHASE_TOOL_EXECUTING,
    build_turn_event_envelope,
    tool_span,
)
from model_tools import get_tool_definitions
from tools.registry import registry
from transports.chain import FailoverExhausted
from transports.streaming import (
    EVENT_DONE,
    EVENT_REASONING_DELTA,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_ARGUMENTS_DELTA,
    EVENT_TOOL_ARGUMENTS_FINISHED,
    EVENT_TOOL_CALL_STARTED,
    StreamCancelled,
)
from transports.types import build_assistant_history_msg


logger = logging.getLogger(__name__)


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
    _ARGS_ACTIVITY_THRESHOLD = env_int("VOICE_ORCHESTRATOR_ARGUMENT_DELTA_MIN_CHARS", 512)
    # text delta 同样节流 —— 否则每个 token 都触发一次 phase_activity → 重建一份
    # envelope（遍历 messages + 逐条 redact），在热路径上做无谓重活（v27.1 review #7）。
    _text_activity_total = 0
    _text_activity_last_chars = 0
    _TEXT_ACTIVITY_THRESHOLD = env_int("VOICE_ORCHESTRATOR_TEXT_DELTA_MIN_CHARS", 512)

    def _open_args_phase(ev):
        """开一把"正在生成工具参数"span 并复位节流游标。

        TOOL_CALL_STARTED 与 TOOL_ARGUMENTS_DELTA 两条分支都可能首开 args span
        （provider 谁先发不定），逻辑一字不差 —— 收口到一处，避免 tool_name /
        tool_call_id 字段在两处各写一遍漂移（v27.1 review #9）。
        """
        nonlocal args_phase, _args_activity_last_chars
        args_phase = runtime.phase_tracker.start(
            PHASE_ASSISTANT_GENERATING_TOOL_ARGUMENTS,
            tool_name=ev.tool_name or "",
            tool_call_id=ev.tool_call_id or "",
        )
        _args_activity_last_chars = 0

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
                    _text_activity_total += len(ev.text)
                    if (_text_activity_total - _text_activity_last_chars) >= _TEXT_ACTIVITY_THRESHOLD:
                        _text_activity_last_chars = _text_activity_total
                        text_phase.activity_event(delta_chars=_text_activity_total)
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
                    # 多工具一轮内会连发多个 STARTED；上一把 args span 若没被
                    # ARGUMENTS_FINISHED 关掉（provider 不发该事件，或并行 tool call），
                    # 这里先关旧的再开新的，否则旧 span 永不 close —— orchestrator
                    # 会一直看到一个"正在生成参数"的孤儿 span（v27.1 review #5）。
                    if args_phase is not None:
                        runtime.phase_tracker.close(args_phase)
                    _open_args_phase(ev)
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
                    _open_args_phase(ev)
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
    return env_bool("VOICE_ORCHESTRATOR_STREAM_ONLY", True)


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

def run_repl(services: AgentServices) -> None:
    """跑 REPL + turn loop —— 拿 bootstrap 装配好的 ``AgentServices`` 直接运行。

    装配时序由 ``bootstrap_services()`` 保证；本函数只解包运行期对象后进入
    prompt 循环。纯运行逻辑，无任何 import-time 副作用。
    """
    log = services.log
    chain = services.chain
    client = services.client
    model = services.model
    runtime = services.runtime
    cancel_token = services.cancel_token
    agent_busy = services.agent_busy
    memory_manager = services.memory_manager
    builtin_provider = services.builtin_provider
    compressor = services.compressor
    session_store = services.session_store
    skill_loader = services.skill_loader
    prompt_builder = services.prompt_builder
    _input_session = services.input_session
    messages = services.messages
    current_session_id = services.current_session_id
    # turn_count 恒从 0 起（resume 的轮号只进 banner）—— 对齐重构前行为。
    turn_count = services.turn_count

    def _send_turn_event(
        event_type: str,
        *,
        assistant_text: str = "",
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
                phase=phase,
            )
            runtime.voice_event_sink.submit(envelope)
        except Exception:  # noqa: BLE001 — 语音旁路绝不影响主 turn
            log.debug("voice turn-event build/submit failed", exc_info=True)

    # v2: phase span 事件经此 listener 归一成 activity_* 事件。activity 的 user_goal
    # 仍由 build_turn_event_envelope 单次 last-user 反查补上（轻量，可挂高频 progress）。
    runtime.phase_tracker.set_listener(
        lambda status, phase: _send_turn_event(status, phase=phase)
    )

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
        build_system_prompt=prompt_builder.build,
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
                        # v0.28.0: exc_info=True 让 traceback 展开 __cause__
                        # （链耗尽时挂的最后一个原始异常），落进 v25.1 结构化日志，
                        # 给用户的 print 仍只给一行聚合摘要。
                        logger.error("all transports failed: %s", e, exc_info=True)
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
                            # tool_span 收口 phase 生命周期：异常自动 error-close 后
                            # 重新抛出（保持原 raise 语义），正常退出按 finish。
                            # v2: 不再额外手写 tool_finished 事件 —— memory 与普通
                            # registry 工具一视同仁走 tool_span，结果预览由 span.finish
                            # 的 result= 带出（在 turn_events 边界裁成 ≤200 安全预览）。
                            with tool_span(runtime, PHASE_TOOL_EXECUTING, tool_name=name, tool_category="memory") as _mem_span:
                                result = memory_manager.handle_tool_call(name, args)
                                try:
                                    _mem_parsed = json.loads(result)
                                    _mem_result_kind = "error" if isinstance(_mem_parsed, dict) and "error" in _mem_parsed else "ok"
                                except json.JSONDecodeError:
                                    _mem_result_kind = "non_json"
                                _mem_span.finish(tool_name=name, result_kind=_mem_result_kind, result=result)
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
