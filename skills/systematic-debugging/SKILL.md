---
name: systematic-debugging
description: "4-phase root-cause debugging: investigate before fixing. No fix without understanding why."
platforms: [linux, macos, windows]
---

# Systematic Debugging

## Overview

Random fixes waste time and create new bugs. Quick patches mask underlying issues.

**Core principle:** ALWAYS find root cause before attempting fixes. Symptom fixes are failure.

## The Iron Law

```
NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST
```

If you haven't completed Phase 1, you cannot propose fixes.

## When to Use

For ANY technical issue: test failures, production bugs, unexpected behavior, performance problems, build failures, integration issues.

**Especially when:**
- Under time pressure (emergencies make guessing tempting)
- "Just one quick fix" seems obvious
- You've already tried multiple fixes
- Previous fix didn't work
- You don't fully understand the issue

**Don't skip when:**
- Issue seems simple (simple bugs have root causes too)
- You're in a hurry (rushing guarantees rework)
- Someone wants it fixed NOW (systematic is faster than thrashing)

## The Four Phases

You MUST complete each phase before proceeding to the next.

---

## Phase 1: Root Cause Investigation

**BEFORE attempting ANY fix:**

### 1. Read Error Messages Carefully

- Don't skip past errors or warnings — they often contain the exact solution
- Read stack traces completely
- Note line numbers, file paths, error codes

### 2. Reproduce Consistently

- Can you trigger it reliably? What are the exact steps?
- Does it happen every time?
- If not reproducible → gather more data, don't guess

```bash
pytest tests/test_module.py::test_name -v --tb=long
```

### 3. Check Recent Changes

```bash
git log --oneline -10
git diff
git log -p --follow src/problematic_file.py | head -100
```

### 4. Gather Evidence in Multi-Component Systems

When the system has multiple components (API → service → database, CI → build → deploy), **before proposing fixes** add diagnostic instrumentation:

For each component boundary:
- Log what data enters
- Log what data exits
- Verify environment/config propagation
- Check state at each layer

Run once to gather evidence showing WHERE it breaks. Then analyze evidence to identify the failing component. Then investigate that specific component.

### 5. Trace Data Flow

When the error is deep in the call stack:

- Where does the bad value originate?
- What called this function with the bad value?
- Keep tracing upstream until you find the source
- Fix at the source, not at the symptom

### Phase 1 Completion Checklist

- [ ] Error messages fully read and understood
- [ ] Issue reproduced consistently
- [ ] Recent changes identified and reviewed
- [ ] Evidence gathered (logs, state, data flow)
- [ ] Problem isolated to specific component/code
- [ ] Root cause hypothesis formed

**STOP:** Do not proceed to Phase 2 until you understand WHY it's happening.

---

## Phase 2: Pattern Analysis

### 1. Find Working Examples

Locate similar working code in the same codebase. What works that's similar to what's broken?

### 2. Compare Against References

If implementing a pattern, read the reference implementation COMPLETELY. Don't skim. Understand the pattern fully before applying.

### 3. Identify Differences

What's different between working and broken? List every difference, however small. Don't assume "that can't matter".

### 4. Understand Dependencies

What other components does this need? What settings, config, environment? What assumptions does it make?

---

## Phase 3: Hypothesis and Testing

### 1. Form a Single Hypothesis

State clearly: "I think X is the root cause because Y." Be specific, not vague.

### 2. Test Minimally

- Make the SMALLEST possible change to test the hypothesis
- One variable at a time
- Don't fix multiple things at once

### 3. Verify Before Continuing

- Did it work? → Phase 4
- Didn't work? → Form NEW hypothesis. Don't pile fixes on top.

### 4. When You Don't Know

Say "I don't understand X." Don't pretend to know. Ask the user, or research more.

---

## Phase 4: Implementation

### 1. Create Failing Test Case

Simplest possible reproduction. Automated test if possible. MUST have before fixing. Use the `test-driven-development` skill.

### 2. Implement Single Fix

- Address the root cause identified
- ONE change at a time
- No "while I'm here" improvements
- No bundled refactoring

### 3. Verify Fix

```bash
pytest tests/test_module.py::test_regression -v
pytest tests/ -q   # full suite — no regressions
```

### 4. If Fix Doesn't Work — The Rule of Three

- **STOP.**
- Count: how many fixes have you tried?
- If < 3: return to Phase 1, re-analyze with new information
- **If ≥ 3: STOP and question the architecture (step 5)**

### 5. If 3+ Fixes Failed: Question Architecture

**Pattern indicating an architectural problem:**
- Each fix reveals new shared state/coupling in a different place
- Fixes require "massive refactoring" to implement
- Each fix creates new symptoms elsewhere

**STOP and question fundamentals:** is this pattern fundamentally sound? Are we sticking with it through inertia? Should we refactor the architecture vs continue fixing symptoms?

Discuss with the user before attempting more fixes. This is NOT a failed hypothesis — this is a wrong architecture.

---

## Red Flags — STOP and Follow Process

If you catch yourself thinking:
- "Quick fix for now, investigate later"
- "Just try changing X and see if it works"
- "Add multiple changes, run tests"
- "Skip the test, I'll manually verify"
- "It's probably X, let me fix that"
- "I don't fully understand but this might work"
- "Here are the main problems: [lists fixes without investigation]"
- **"One more fix attempt" (when already tried 2+)**
- **Each fix reveals a new problem in a different place**

ALL of these mean: STOP. Return to Phase 1.

If 3+ fixes failed: question the architecture (Phase 4 step 5).

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "Issue is simple, don't need process" | Simple issues have root causes too. Process is fast for simple bugs. |
| "Emergency, no time for process" | Systematic debugging is FASTER than guess-and-check thrashing. |
| "Just try this first, then investigate" | First fix sets the pattern. Do it right from the start. |
| "I'll write test after confirming fix works" | Untested fixes don't stick. Test first proves it. |
| "Multiple fixes at once saves time" | Can't isolate what worked. Causes new bugs. |
| "Reference too long, I'll adapt the pattern" | Partial understanding guarantees bugs. Read it completely. |
| "I see the problem, let me fix it" | Seeing symptoms ≠ understanding root cause. |
| "One more fix attempt" (after 2+ failures) | 3+ failures = architectural problem. Question the pattern, don't fix again. |

## Quick Reference

| Phase | Key Activities | Success Criteria |
|-------|---------------|------------------|
| **1. Root Cause** | Read errors, reproduce, check changes, gather evidence, trace data flow | Understand WHAT and WHY |
| **2. Pattern** | Find working examples, compare, identify differences | Know what's different |
| **3. Hypothesis** | Form theory, test minimally, one variable at a time | Confirmed or new hypothesis |
| **4. Implementation** | Create regression test, fix root cause, verify | Bug resolved, all tests pass |

## With test-driven-development

When fixing bugs:
1. Write a test that reproduces the bug (RED)
2. Debug systematically to find root cause
3. Fix the root cause (GREEN)
4. The test proves the fix and prevents regression

**No shortcuts. No guessing. Systematic always wins.**
