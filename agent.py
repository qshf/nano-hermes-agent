"""
Nano Hermes Agent — V1: 注册表版本

架构变化（相比 V0）：
- 引入 ToolRegistry，工具通过 register() 自注册
- agent.py 不再需要逐个 import 工具的 schema 和 handler
- 新增工具只需创建 *_tool.py 文件，无需改 agent.py
- 自动发现机制：import tools 时扫描所有 *_tool.py 并触发注册

运行方式：
    python agent.py
"""

import json
import os

from dotenv import load_dotenv
from openai import OpenAI

# 一行 import 触发所有工具的自动发现和注册
import tools  # noqa: F401
from tools.registry import registry

# ─── 系统提示词（根据已注册工具动态生成）────────────────────────────────────
SYSTEM_PROMPT = """You are a helpful coding assistant. You have access to the following tools:
{tool_list}

When the user asks you to do something, use the appropriate tool.
Always explain what you're doing before and after tool use.
Respond in the same language as the user."""


def build_system_prompt() -> str:
    """根据注册表中的工具动态构建系统提示词。"""
    tool_list = "\n".join(f"- `{name}`" for name in registry.tool_names)
    return SYSTEM_PROMPT.format(tool_list=tool_list)


def run_agent():
    """Agent 主循环：对话 → LLM → 工具调用 → 循环。"""
    load_dotenv()

    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL"),
    )
    model = os.environ.get("MODEL", "gpt-4o-mini")

    messages = [{"role": "system", "content": build_system_prompt()}]

    print("=" * 60)
    print("  Nano Hermes Agent v1 — 注册表版本")
    print(f"  Model: {model}")
    print(f"  Tools: {', '.join(registry.tool_names)}")
    print("  输入 'quit' 退出")
    print("=" * 60)
    print()

    while True:
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

        # Agent 循环
        while True:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=registry.get_openai_tools(),
            )

            choice = response.choices[0]
            assistant_message = choice.message
            messages.append(assistant_message.model_dump())

            if not assistant_message.tool_calls:
                print(f"\nAgent > {assistant_message.content}\n")
                break

            for tool_call in assistant_message.tool_calls:
                name = tool_call.function.name
                args = json.loads(tool_call.function.arguments)
                print(f"  [tool] {name}({tool_call.function.arguments})")

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


if __name__ == "__main__":
    run_agent()
