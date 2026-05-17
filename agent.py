"""
Nano Hermes Agent — V0: 最小可用版本

架构特征：
- 无注册表，agent.py 直接 import 每个工具的 schema 和 handler
- 分发用字典硬编码
- 新增工具必须改 agent.py（这就是 V1 要解决的问题）

运行方式：
    export OPENAI_API_KEY="sk-..."
    python agent.py
"""

import json
import os

from dotenv import load_dotenv
from openai import OpenAI

# ─── 直接 import 每个工具的 schema 和 handler ───────────────────────────────
from tools.terminal_tool import TERMINAL_SCHEMA, terminal_handler
from tools.read_file_tool import READ_FILE_SCHEMA, read_file_handler

# ─── 手动组装 tools 列表（每加一个工具就要在这里加一行）─────────────────────
TOOLS = [
    {"type": "function", "function": TERMINAL_SCHEMA},
    {"type": "function", "function": READ_FILE_SCHEMA},
]

# ─── 手动分发表（每加一个工具就要在这里加一个条目）──────────────────────────
DISPATCH = {
    "terminal": terminal_handler,
    "read_file": read_file_handler,
}

# ─── 系统提示词 ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a helpful coding assistant. You can:
1. Execute shell commands via the `terminal` tool
2. Read file contents via the `read_file` tool

When the user asks you to do something, use the appropriate tool.
Always explain what you're doing before and after tool use.
Respond in the same language as the user."""


def dispatch_tool_call(tool_call) -> str:
    """根据 tool_call 找到 handler 并执行。"""
    name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)

    handler = DISPATCH.get(name)
    if handler is None:
        return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)

    return handler(args)


def run_agent():
    """Agent 主循环：对话 → LLM → 工具调用 → 循环。"""
    load_dotenv()

    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL"),
    )
    model = os.environ.get("MODEL", "gpt-4o-mini")

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    print("=" * 60)
    print("  Nano Hermes Agent v0 — 最小可用版本")
    print(f"  Model: {model}")
    print("  输入 'quit' 退出")
    print("=" * 60)
    print()

    while True:
        # ─── 用户输入 ───────────────────────────────────────────────────
        try:
            user_input = input("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Bye!")
            break

        messages.append({"role": "user", "content": user_input})

        # ─── Agent 循环：调 LLM，如果有 tool_calls 就执行，再调 LLM ────
        while True:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
            )

            choice = response.choices[0]
            assistant_message = choice.message

            # 把 assistant 消息加入历史
            messages.append(assistant_message.model_dump())

            # 如果没有 tool_calls，说明 LLM 给出了最终回复
            if not assistant_message.tool_calls:
                print(f"\nAgent > {assistant_message.content}\n")
                break

            # 有 tool_calls → 逐个执行
            for tool_call in assistant_message.tool_calls:
                print(f"  [tool] {tool_call.function.name}({tool_call.function.arguments})")

                result = dispatch_tool_call(tool_call)

                # 把工具结果加入历史
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })

                # 打印工具结果摘要
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


if __name__ == "__main__":
    run_agent()
