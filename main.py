"""Nano Hermes Agent — 瘦入口（V27.1 重构后）。

历史背景见 git log / docs/decisions：本文件曾是 1000+ 行的"胖 main"，装配与
运行全挤在 ``run_agent`` 一个函数 + 一堆 import-time 副作用 global 里。V27.1
重构把它切成三层：

- ``agent/bootstrap.py``  装配层：``bootstrap_services()`` 一次性组装运行期对象，
  打包成 ``AgentServices``。
- ``agent/turn_loop.py``  运行层：``run_repl(services)`` 跑 REPL + turn loop。
- ``main.py``（本文件）   入口层：解析 ``--cwd`` + chdir，然后 bootstrap → run_repl。

时序硬约束
==========
``--cwd`` 的 ``os.chdir`` 必须在 ``bootstrap_services()`` 之前完成 —— PromptBuilder
的项目上下文段在装配时捕获 ``Path.cwd()``，据此找用户项目根的 nano-hermes-agent.md /
AGENTS.md。所以解析 + chdir 放在 ``main()`` 第一步，早于 bootstrap。

env 开关一览见 .env.example 与 CLAUDE.md（transport / failover / prompt cache /
streaming / memory / voice orchestrator / 上下文压缩）。
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)


def _parse_cli_args() -> argparse.Namespace:
    """V23.2: ``--cwd PATH`` 让 agent 能在任意目录下启动。

    必须在所有"按 cwd 工作"的代码（PromptBuilder 项目上下文段、terminal /
    read_file 工具的相对路径）之前 ``os.chdir``。

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
        default="./",
        metavar="PATH",
        help=(
            "启动后切到此目录工作。terminal / read_file 等工具的相对路径以及 "
            "system prompt 注入的 nano-hermes-agent.md / AGENTS.md 都从这里找。"
            "不传则用当前 shell cwd。"
        ),
    )
    return parser.parse_args()


def main() -> None:
    """瘦入口：解析 --cwd + chdir → 装配 → 跑循环。

    chdir 必须早于 bootstrap（PromptBuilder 项目上下文段依赖 cwd）。装配 /
    运行两层分别在 agent/bootstrap.py 与 agent/turn_loop.py。
    """
    args = _parse_cli_args()
    if args.cwd is not None:
        target = Path(args.cwd).expanduser().resolve()
        if not target.is_dir():
            print(f"  [error] --cwd {args.cwd!r} 不存在或不是目录")
            sys.exit(2)
        os.chdir(target)

    # chdir 之后再 import 装配 / 运行层 —— 两者在 import 期没有副作用，但 bootstrap
    # 装配 PromptBuilder 时捕获 Path.cwd()，所以 import 顺序无所谓、调用顺序才关键。
    from agent.bootstrap import bootstrap_services
    from agent.turn_loop import run_repl

    services = bootstrap_services()
    run_repl(services)


if __name__ == "__main__":
    main()
