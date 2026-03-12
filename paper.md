# Computed Skills: When the Prompt Is a Program's Output

*Jounes Deblauwe — March 2026*

---

## The Problem

AI agent skills are markdown files. You write instructions, the LLM follows them. Simple, readable, works great — until it doesn't.

Here's what breaks:

**Same instructions, wildly different contexts.** Your code review skill fires the same checklist whether you changed 2 auth files or 40 config files. The LLM gets "check for security issues, check for correctness, check for consistency" regardless. It can't prioritize because *you* didn't prioritize — you wrote one-size-fits-all instructions.

**LLM doing bookkeeping.** I run a personal AI agent (Reef) that tracks learnings, errors, and corrections across sessions. 87 entries in 3 markdown files. Every session, the LLM re-reads ~1200 lines of structured entries to check for duplicate Pattern-Keys before logging a new one. That's parsing, counting, and string matching — stuff Python does in milliseconds — burning LLM tokens and context window.

**No memory between runs.** A static skill can't remember that it reviewed the same auth module yesterday with a safety focus. It can't know that the last 3 reviews missed edge cases in error handling. Every invocation starts from zero.

## The Idea

What if the skill file wasn't the instructions — it was a program that *generates* the instructions?

```
Before:  SKILL.md (static markdown) ──────────────────→ LLM
After:   SKILL.md → python script → generated markdown → LLM
```

The LLM still receives markdown. That's its native format, what it was trained on. But the markdown is *computed* — tailored to the current context, informed by past runs, stripped of data the LLM doesn't need to parse.

The SKILL.md becomes 5 lines:

```yaml
---
name: smart-review
description: Context-aware code review
---

!`python3 ${CLAUDE_SKILL_DIR}/scripts/generate.py $ARGUMENTS`
```

The `!`command`` syntax (supported by Claude Code and OpenClaw) runs the script before the prompt reaches the LLM. The script's stdout becomes the instructions. That's it. No framework, no dependencies, no platform changes needed.

## What We Built

### Code Review — Adaptive Strategy

A Python script that reads git state and generates a different review prompt every time.

**What the script does:**
1. Reads `git diff` — which files, how many, additions vs deletions
2. Detects patterns — security-sensitive paths? config-heavy? test-only? scripts?
3. Weights the review lenses — Safety at 50% for auth files, Consistency at 35% for config
4. Picks depth — line-by-line for 2 files, architecture-first for 20+
5. Checks state file — used the same strategy last time? Inject a "Fresh Eyes" pass

**What the LLM receives:**

For a 2-file auth change:
```
## Review Strategy (auto-selected)
Depth: deep — Small change (2 files, ~15 lines)
Signals: [security] Security-sensitive files: src/auth/login.js

Lens weights:
- Safety    ██████████░░░░░░░░░░ 50%  ← PRIMARY
- Correctness ██████░░░░░░░░░░░░░░ 30%
- Robustness  ███░░░░░░░░░░░░░░░░░ 15%
- Consistency █░░░░░░░░░░░░░░░░░░░ 5%

⚠ Security Alert
- Hardcoded secrets are Critical severity, always.
- Check .env files are not committed
```

For a 22-file config overhaul:
```
## Review Strategy (auto-selected)
Depth: architectural — Massive change (22 files)
Signals: [config] Config-heavy (22/22 files), [large-change] possible refactor

Lens weights:
- Consistency ███████░░░░░░░░░░░░░ 35%  ← PRIMARY
- Correctness ██████░░░░░░░░░░░░░░ 30%
- Robustness  ████░░░░░░░░░░░░░░░░ 20%
- Safety      ███░░░░░░░░░░░░░░░░░ 15%
```

Same skill. Completely different instructions. The static version would have given the same generic 4-lens checklist both times.

### Self-Improve — 5-Mode Learning System

This is the bigger example. Reef (the agent) tracks corrections, errors, and capability gaps in structured markdown files. The static skill was 214 lines of instructions telling the LLM how to parse entries, check for duplicates, count recurrences, find promotion candidates...

All bookkeeping. All things Python does better.

The Python brain has 5 modes:

**`always-on`** (every session) — Parses all 87 entries, builds a Pattern-Key hash index, computes next sequence numbers, surfaces anything that needs attention. The LLM gets a pre-digested summary instead of 1200 lines to parse.

**`status`** (`/self-improve` command) — Full dashboard: counts by status/priority/area, entries due for promotion (recurrence >= 2), stale entries (>21 days old, never recurred), cross-file duplicates.

**`check <key>`** (before logging) — Instant Pattern-Key lookup. Exact match: "Don't create new, increment existing, this will trigger auto-promote." Fuzzy match via word-overlap: "Did you mean `memory-cadence-slippage`?" No match: "Safe to create."

**`drift`** (heartbeat cycle) — Loads SOUL.md principles, scans last 3 daily logs for keyword drift. "Over-explaining detected 3 times in recent logs → possible drift from 'be concise' principle."

**`triage`** (monthly) — Ages all entries, buckets into stale (21-45 days, close candidate) and very stale (>45 days, probably one-off).

### Check-Pattern — Skill-to-Skill Pipeline

A hidden skill (`user-invocable: false`) that the agent can invoke silently mid-conversation. It calls the same self-improve Python backend with `check` mode. The always-on prompt references it:

```
Two ways to check:
1. Scan the Known Pattern-Keys list above — fast, covers all known keys
2. Use check-pattern skill — invoke with the candidate key for exact + fuzzy matching
```

This is a skill calling a skill calling Python. The pipeline:

```
Correction detected → self-improve (always-on) triggers
→ agent picks candidate Pattern-Key
→ invokes check-pattern skill
→ Python does exact + fuzzy lookup
→ returns "increment existing" or "safe to create"
→ agent writes the entry
```

## The Separation of Concerns

This is the core design principle:

| Python does | LLM does |
|---|---|
| Parse structured data | Make judgment calls |
| Index, count, filter | Decide severity and priority |
| Age-calculate, bucket | Decide what to promote |
| Exact + fuzzy matching | Decide if patterns are related |
| Select strategy from signals | Apply the strategy with nuance |
| Track state across runs | Adapt to conversation context |

Python handles what it's good at. The LLM handles what it's good at. Markdown is the interface.

## When To Use This

Not every skill needs Python. The decision is straightforward:

**Keep static markdown** when instructions are stable and context-independent. A style guide, a deploy checklist, a commit message format — these don't change based on what you're deploying.

**Use computed skills** when:
- The skill works with structured data (logs, entries, git diffs)
- Strategy should change based on context
- The LLM is burning tokens on parsing instead of thinking
- You want the skill to remember past invocations
- The "what to do" depends on "what's happening right now"

## How To Build One

1. Write the skill normally in static markdown first. Get the instructions right.
2. Identify what changes between invocations. What context matters? What data does the LLM parse that Python could pre-digest?
3. Write a Python script that analyzes context and generates the markdown.
4. Replace the SKILL.md body with `!`python3 ${CLAUDE_SKILL_DIR}/scripts/generate.py $ARGUMENTS``
5. Add a state file if you want memory across runs.
6. Keep the static backup (`SKILL.md.static-backup`) in case you need to revert.

The script writes to stdout. Whatever it prints becomes the prompt. No framework, no SDK, no dependencies beyond Python stdlib.

## Levels

We see a spectrum:

| Level | What | Example |
|---|---|---|
| 0 | Static markdown | Style guide, deploy checklist |
| 1 | Markdown + `!`command`` data injection | Review skill that injects `git diff --stat` |
| 2 | Program generates entire prompt | Smart review, self-improve (this paper) |
| 3 | Program tracks outcomes, adjusts templates | State files with `hits`/`misses` (scaffolded, not active) |
| 4 | Skills invoke other skills, share state | check-pattern + self-improve pipeline |
| 5 | Skills that generate other skills | Speculative |

We're at Level 2-4 in production. Level 3 is structurally ready — the state files have the arrays, just need the feedback loop wired. Level 5 is the question: can a meta-skill observe recurring patterns and create new computed skills to address them?

## What Exists Today vs What's New

Anthropic calls it "context engineering" — curating the right tokens at inference time. DSPy (Stanford) optimizes static prompts through compilation. The `!`command`` syntax in skill systems enables data injection.

All of these are pieces. Nobody we found is doing the specific thing: **the entire skill is a program's output, with persistent state, strategy selection, and skill-to-skill coordination.** The pieces exist. The architecture of putting them together this way appears to be new.

## Limitations

**Harder to edit.** Static markdown is readable by anyone. Computed skills require reading Python to understand what the LLM will see. We mitigate this by having the generated prompt include a "strategy explanation" section.

**Failure modes.** If the script crashes, the skill produces nothing. Static skills always produce output. Write defensively, handle missing files, fall back to generic instructions.

**Over-engineering risk.** A 10-line style guide doesn't need a Python backend. Don't compute what doesn't change.

**Platform-specific.** The `!`command`` syntax isn't universal. But any agent framework that supports running a shell command and injecting its output into the prompt can implement this pattern.

---

*Examples and code: see the `examples/` directory.*
