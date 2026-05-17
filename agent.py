"""
Nano Hermes Agent — V2: Toolsets + check_fn

架构变化（相比 V1）：
- 引入 toolsets.py：工具按组管理，agent 只需指定 toolset 名
- 引入 model_tools.py：薄包装层，串联 toolsets 展开 + registry 过滤
- registry 新增 check_fn：运行时判断工具是否可用
- 新增 docker_exec 工具演示 check_fn（Docker 未装则自动隐藏）

运行方式：
    python agent.py
"""

import json
import os

from dotenv import load_dotenv
from openai import OpenAI

from model_tools import get_tool_definitions, get_available_tool_names
from tools.registry import registry

# ─── 配置 ────────────────────────────────────────────────────────────────────
ENABLED_TOOLSETS = ["core"]

SYSTEM_PROMPT = """You are a helpful coding assistant. You have access to the following tools:
{tool_list}

When the user asks you to do something, use the appropriate tool.
Always explain what you're doing before and after tool use.
Respond in the same language as the user."""


def build_system_prompt() -> str:
    tool_names = get_available_tool_names(ENABLED_TOOLSETS)
    tool_list = "\n".join(f"- `{name}`" for name in tool_names)
    return SYSTEM_PROMPT.format(tool_list=tool_list)


def run_agent():
    load_dotenv()

    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL"),
    )
    model = os.environ.get("MODEL", "gpt-4o-mini")

    tools_schema = get_tool_definitions(ENABLED_TOOLSETS)
    available = get_available_tool_names(ENABLED_TOOLSETS)

    messages = [{"role": "system", "content": build_system_prompt()}]

    print("=" * 60)
    print("  Nano Hermes Agent v2 — Toolsets + check_fn")
    print(f"  Model: {model}")
    print(f"  Toolsets: {ENABLED_TOOLSETS}")
    print(f"  Available tools: {', '.join(available)}")
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

        while True:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools_schema,
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
