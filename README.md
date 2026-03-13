# Computed Skills

**Skills that write themselves based on what's happening right now.**

```
Static skill:    SKILL.md ──────────────────────────→ Agent reads markdown
Computed skill:  SKILL.md → runs script → markdown ─→ Agent reads markdown
```

The agent always receives markdown. But with computed skills, that markdown comes from a script that analyzed the situation first — so the instructions adapt to context instead of being the same every time.

## Why

Static skills give the same instructions regardless of context:

- Changed 2 auth files? *"Check for bugs, security, consistency."*
- Changed 40 config files? *"Check for bugs, security, consistency."*

The agent can't prioritize because the instructions don't. A script can look at the git diff, see that auth files changed, and tell the agent to focus 50% of its attention on security.

It gets worse when skills manage data. An agent tracking its own mistakes across 87 entries shouldn't re-read 1,200 lines every session to check for duplicates — Python can parse that in milliseconds and hand the agent a summary.

## How it works

A computed skill has the same structure as any skill, but the SKILL.md delegates to a script:

```
my-skill/
├── SKILL.md              # Thin shell — calls the script
└── scripts/
    └── generate.py       # The brain — outputs markdown to stdout
```

The SKILL.md is a one-liner:

```yaml
---
name: smart-review
description: Context-aware code review that adapts based on what changed
---

!`python3 ${CLAUDE_SKILL_DIR}/scripts/generate.py $ARGUMENTS`
```

The [`!`command`` syntax](https://code.claude.com/docs/en/skills#inject-dynamic-context) is a preprocessing directive. It runs the shell command before the agent sees anything, and replaces itself with the command's stdout. The agent never knows a script was involved — it just sees tailored markdown.

### What the script can do

| Capability | Example |
|---|---|
| **Read context** | Check `git diff`, count files, detect file types |
| **Pick a strategy** | Auth files → security focus. Config files → consistency focus |
| **Pre-digest data** | Parse 1,200 lines of entries, hand the agent a 20-line summary |
| **Remember past runs** | Track state in a JSON file between invocations |

## Examples

This repo includes three working examples from a production agent system.

### [`smart-review`](examples/smart-review) — Adaptive code review

The script reads git state and picks a review strategy:

```
2 auth files changed         →  "Security Review: check for hardcoded secrets,
                                  validate input sanitization, verify auth on new routes"

22 config files changed      →  "Configuration Audit: check for breaking changes,
                                  validate YAML/JSON syntax, look for secrets in config"

3 test files in a project    →  "Test Quality Review: check edge cases,
without established tests         look for flaky patterns, verify assertion specificity"
```

It also tracks past runs — if the same strategy fires twice in a row, it adds a "fresh eyes" pass. If past reviews had misses, those get highlighted.

**Key file:** [`examples/smart-review/scripts/generate.py`](examples/smart-review/scripts/generate.py)

### [`self-improve`](examples/self-improve) — Learning capture with data pre-processing

An agent that logs its own mistakes, detects recurrence, and promotes recurring patterns to permanent docs. The static version was 214 lines of instructions telling the agent how to parse entries and count duplicates. The computed version offloads all bookkeeping to Python:

| What Python does (<100ms) | What the agent gets |
|---|---|
| Parses all entries into a hash index | "Here are 4 entries due for promotion" |
| Finds promotion candidates (count >= 2) | "Here are 12 stale entries to triage" |
| Does exact + fuzzy duplicate matching | "Next sequence number: LRN-20260313-004" |
| Detects behavioral drift against principles | "Drift signal: over-explaining (3 hits)" |

The agent focuses on judgment calls — *should* this be promoted? *is* this a real drift? — not data parsing.

**Key file:** [`examples/self-improve/scripts/generate.py`](examples/self-improve/scripts/generate.py)

### [`check-pattern`](examples/check-pattern) — Duplicate prevention helper

A sub-skill called before logging a new entry. The script checks if the pattern already exists (exact match), looks for similar keys (fuzzy match), and tells the agent whether to create a new entry or increment an existing one.

**Key file:** [`examples/check-pattern/SKILL.md`](examples/check-pattern/SKILL.md) — note how it reuses the self-improve generator with a different argument (`check $ARGUMENTS`).

## When to use computed skills

**Start with static.** Static skills are simpler, readable, and easy to edit. Use them for instructions that don't change: style guides, deploy checklists, commit formats.

**Switch to computed when:**

- Instructions should change based on what's happening right now
- The agent is parsing structured data that code could pre-digest
- You want the skill to remember and adapt across runs
- Different contexts genuinely need different strategies

## How to build one

1. **Write the skill as static markdown first.** Get the instructions right.
2. **Notice what changes between invocations.** What context matters? What's the agent doing that code could do faster?
3. **Write a script that generates the markdown.** Read context, make decisions, print markdown to stdout.
4. **Replace the SKILL.md body** with `!`python3 ${CLAUDE_SKILL_DIR}/scripts/generate.py $ARGUMENTS``
5. **Add a state file** (JSON) if you want memory across runs.

The interface is just stdout. No framework, no SDK, any language works.

### Error handling

If the script crashes, the agent gets the traceback as its instructions (or nothing). For production skills, wrap your main function:

```python
def main():
    try:
        # ... your logic ...
        print(prompt)
    except Exception as e:
        # Fallback: print static instructions so the agent isn't left empty-handed
        print("# Code Review\n\nCheck for bugs, security issues, and consistency.")
        print(f"\n<!-- generator error: {e} -->")
```

## Requirements

- Python 3.8+ (or any language that prints to stdout)
- A skill system that supports `!`command`` preprocessing
- Works with [Claude Code](https://claude.ai/claude-code) and [OpenClaw](https://openclaw.com)

## License

MIT
