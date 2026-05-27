---
name: plan
description: "Plan mode: write a markdown plan instead of executing. No code edits, no mutating commands."
platforms: [linux, macos, windows]
---

# Plan Mode

Use this skill when the user wants a plan instead of execution.

## Core behavior

For this turn, you are planning only.

- Do not implement code.
- Do not edit project files except the plan markdown file.
- Do not run mutating terminal commands, commit, push, or perform external actions.
- You may inspect the repo or other context with read-only tools when needed.
- Your deliverable is a markdown plan saved under `.nano/plans/`.

## Output requirements

Write a markdown plan that is concrete and actionable. Include, when relevant:

- **Goal** — what success looks like in one sentence
- **Current context / assumptions** — what you read, what you're taking on faith
- **Proposed approach** — the shape of the solution and why it fits
- **Step-by-step plan** — ordered, each step independently verifiable
- **Files likely to change** — exact paths
- **Tests / validation** — what proves the change works
- **Risks, tradeoffs, and open questions** — what could go wrong, what you'd want to confirm

If the task is code-related, include exact file paths, likely test targets, and verification steps.

## Save location

Save the plan with the file-write tool under:

```
.nano/plans/YYYY-MM-DD_HHMMSS-<slug>.md
```

Treat that as relative to the active working directory. If the runtime provides a specific target path, use that exact path.

## Interaction style

- If the request is clear, write the plan directly.
- If no explicit instruction accompanies `/plan`, infer the task from conversation context.
- If genuinely underspecified, ask one brief clarifying question instead of guessing.
- After saving, reply briefly with what you planned and the saved path.

## Why a plan first

Plans force the trade-offs into the open before code is written. The cost of a bad plan is a few minutes of typing; the cost of a bad implementation is hours of unwinding. When the task spans more than 2-3 files or has reversibility concerns, plan first.
