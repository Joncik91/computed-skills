# Computed Skills

**When the prompt is a program's output, not a file you wrote.**

Traditional AI agent skills are static markdown files — the same instructions every time. Computed skills flip this: a Python program analyzes context (git state, past outcomes, structured data) and *generates* a tailored markdown prompt for each invocation.

```
Traditional:  SKILL.md (static) ────────────────────→ LLM
Computed:     SKILL.md → Python (analyzes context) → Markdown → LLM
```

The LLM still receives markdown. But the markdown is *computed*, not *written*.

## Why

| Static skills | Computed skills |
|---|---|
| Same instructions every time | Different prompt per invocation |
| LLM parses structured data | Python pre-digests data, LLM judges |
| No memory across runs | Persistent state, feedback loops |
| Can't adapt strategy | Picks strategy based on context |

## Examples

### smart-review

A code review skill that adapts based on what changed:

- 2 auth files → deep review, Safety lens at 50%
- 22 config files → architectural review, Consistency lens at 35%
- Same change reviewed twice → adds "Fresh Eyes" pass

The SKILL.md is 5 lines. The Python brain is 400 lines.

### self-improve

A learning capture system with 5 modes:

| Mode | What Python does | What LLM does |
|---|---|---|
| `always-on` | Parse 87 entries, build pattern index, surface alerts | Make judgment calls |
| `status` | Full dashboard with counts, promotion queue, stale entries | Present to user |
| `check <key>` | Exact + fuzzy Pattern-Key lookup | Decide increment vs create |
| `drift` | Scan daily logs against principles | Deeper semantic check |
| `triage` | Age-calculate entries, bucket into stale/very-stale | Decide per entry |

### check-pattern

A hidden helper skill (`user-invocable: false`) that checks if a Pattern-Key exists before logging a new entry. Returns exact match, fuzzy match, or "safe to create."

## How It Works

A computed skill's SKILL.md is a thin shell:

```yaml
---
name: smart-review
description: Context-aware code review
---

!`python3 ${CLAUDE_SKILL_DIR}/scripts/generate.py $ARGUMENTS`
```

The `!`command`` syntax runs the Python script *before* the prompt reaches the LLM. The script's stdout becomes the skill's instructions.

The Python script:
1. Analyzes context (git diff, file types, codebase state)
2. Consults persistent state (past runs, outcomes)
3. Selects strategy (which lenses, what depth, what to prioritize)
4. Generates tailored markdown

## When To Use

**Keep static markdown** for simple, stable skills where instructions don't change.

**Use computed skills** when:
- Strategy should adapt based on live context
- The skill works with structured data (logs, entries, diffs)
- The LLM wastes tokens parsing instead of thinking
- You want memory across invocations

## The Paper

See [paper.md](paper.md) for the full writeup covering architecture, related work, implementation details, and the computation level spectrum (Level 0-5).

## Requirements

- Python 3.8+
- An agent framework that supports `!`command`` or equivalent shell injection in skill files
- Tested with [Claude Code](https://claude.ai/claude-code) and [OpenClaw](https://openclaw.com)

## License

MIT
