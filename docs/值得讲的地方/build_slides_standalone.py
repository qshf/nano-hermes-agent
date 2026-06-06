#!/usr/bin/env python3
"""Embed slides-content.md into slides-standalone.html."""

from __future__ import annotations

import argparse
import html
import re
from pathlib import Path


# ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTENT = Path("/Users/qshf/my-project/nano_hermes_agent/docs/值得讲的地方/slides-content.md")
DEFAULT_STANDALONE = Path("/Users/qshf/my-project/nano_hermes_agent/docs/值得讲的地方/slides-standalone.html")


def replace_template(standalone_html: str, escaped_markdown: str) -> str:
    pattern = re.compile(
        r"(<textarea data-template>\n)(.*?)(\n\s*</textarea>)",
        re.DOTALL,
    )
    updated, count = pattern.subn(
        lambda match: f"{match.group(1)}{escaped_markdown}{match.group(3)}",
        standalone_html,
        count=1,
    )
    if count != 1:
        raise RuntimeError("Could not find exactly one <textarea data-template> block")
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Regenerate docs/值得讲的地方/slides-standalone.html from slides-content.md.",
    )
    parser.add_argument(
        "--content",
        type=Path,
        default=DEFAULT_CONTENT,
        help=f"Markdown source file, default: {DEFAULT_CONTENT}",
    )
    parser.add_argument(
        "--standalone",
        type=Path,
        default=DEFAULT_STANDALONE,
        help=f"Standalone HTML file, default: {DEFAULT_STANDALONE}",
    )
    args = parser.parse_args()

    markdown = args.content.read_text(encoding="utf-8")
    escaped = html.escape(markdown, quote=False)

    standalone = args.standalone.read_text(encoding="utf-8")
    updated = replace_template(standalone, escaped)
    args.standalone.write_text(updated, encoding="utf-8")

    print(f"Updated {args.standalone} from {args.content}")


if __name__ == "__main__":
    main()
