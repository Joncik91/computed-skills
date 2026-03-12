# Computed Skills

**When the prompt is a program's output, not a file you wrote.**

AI agent skills are markdown files. You write instructions, the LLM follows them. Computed skills flip this: a Python program analyzes context and *generates* a tailored markdown prompt for each invocation.

```
Static:    SKILL.md (same every time) ────────────────→ LLM
Computed:  SKILL.md → Python (analyzes context) → Markdown → LLM
```

The LLM still receives markdown — that's its native format. But the markdown is *computed*, not *written*.

No framework. No dependencies. Just Python's stdlib and the `!`command`` syntax that skill systems already support.

## Why

Static skills break when:
- The **same checklist fires** for 2 auth files and 40 config files
- The **LLM burns tokens** parsing structured data it could receive pre-digested
- You want the skill to **remember** what happened last time
- The "what to do" depends on "what's happening right now"

## How It Works

A computed skill's SKILL.md is a thin shell:

```yaml
---
name: smart-review
description: Context-aware code review
---

!`python3 ${CLAUDE_SKILL_DIR}/scripts/generate.py $ARGUMENTS`
```

The `!`command`` syntax runs the script *before* the prompt reaches the LLM. Stdout becomes the instructions. The Python script analyzes context, consults persistent state, picks a strategy, and generates tailored markdown.

## Examples

### smart-review

A code review skill that reads git state and generates a different review prompt every time:

- 2 auth files → deep line-by-line review, Safety lens at 50%
- 22 config files → architectural review, Consistency lens at 35%
- Same change reviewed twice → adds "Fresh Eyes" pass to avoid blind spots

The SKILL.md is 5 lines. The Python brain is 400 lines.

### self-improve

A learning capture system with 5 modes. Manages 87 structured entries across 3 files — the kind of data an LLM would waste tokens re-parsing every session:

| Mode | Python does | LLM does |
|---|---|---|
| `always-on` | Parse all entries, build pattern index, surface alerts | Make judgment calls on what to log |
| `status` | Dashboard: counts, promotion queue, stale entries | Present to user, execute actions |
| `check <key>` | Exact + fuzzy Pattern-Key lookup | Decide increment vs create new |
| `drift` | Scan daily logs against principles for drift signals | Deeper semantic check |
| `triage` | Age-calculate entries, bucket stale vs very-stale | Decide resolve/close/keep per entry |

### check-pattern

A hidden skill (`user-invocable: false`) the agent invokes silently before logging new entries. Calls the self-improve backend with `check` mode. Returns "increment existing," "did you mean [similar key]?", or "safe to create."

This is a skill-to-skill pipeline — one computed skill referencing another.

## When To Use

**Keep static markdown** when instructions don't change. Style guides, deploy checklists, commit formats.

**Use computed skills** when:
- Strategy should adapt based on live context
- The skill works with structured data (logs, entries, diffs)
- The LLM wastes tokens parsing instead of thinking
- You want memory across invocations

## The Writeup

See [paper.md](paper.md) for the full developer writeup — the problem, what we built, the separation of concerns, computation levels (0-5), and limitations.

## Requirements

- Python 3.8+
- An agent skill system that supports `!`command`` or equivalent shell injection
- Tested with [Claude Code](https://claude.ai/claude-code) and [OpenClaw](https://openclaw.com)

## License

MIT
