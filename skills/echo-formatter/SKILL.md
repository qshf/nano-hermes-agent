---
name: echo-formatter
description: "Format text with decorative borders (debug skill for v26.1 readiness backfill observation)."
required_environment_variables:
  - ECHO_FORMATTER_TOKEN
metadata:
  category: debug
  requires_tools:
    - nonexistent-tool
---

# echo-formatter

调试用 skill，用于观察 v26.1 `skill_view` 回填的 `readiness_status` / `missing_env_vars` 是否真的有用。

## 用法（占位）

真实实现会把输入文本转成带边框的格式化输出。
本 nano 演示版不实现 —— 它只是 v26.1 回填逻辑的观测载体。
