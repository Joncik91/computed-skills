# Computed Skills

[![License: MIT](https://img.shields.io/badge/license-MIT-E8954A.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Claude Code](https://img.shields.io/badge/works%20with-Claude%20Code-E8954A)](https://claude.ai/claude-code)
[![GitHub stars](https://img.shields.io/github/stars/Joncik91/computed-skills?color=E8954A&logo=github)](https://github.com/Joncik91/computed-skills/stargazers)
[![Concept + examples](https://img.shields.io/badge/concept%20%2B%20examples-✓-E8954A)]()

**Skills that adapt to what's actually happening.**

A static skill gives the same instructions every time. A computed skill runs a script first — analyzes the situation, pre-digests data, picks a strategy — then hands the agent tailored markdown. The agent never knows a script was involved.

## Table of Contents

- [Why Computed Skills](#why-computed-skills)
- [Quick Start](#quick-start)
- [Three Patterns](#three-patterns)
- [Security Review](#security-review)
- [When to Use What](#when-to-use-what)
- [How to Build One](#how-to-build-one)
- [Examples](#examples)
- [Production Case Study](#production-case-study)
- [Prior Art](#prior-art)
- [Requirements](#requirements)
- [Security](#security)
- [License](#license)

```
Static:    SKILL.md ──────────────────────────→ Agent reads markdown
Computed:  SKILL.md → runs script → markdown ─→ Agent reads markdown
```

Works with [Claude Code](https://claude.ai/claude-code) and any skill system that supports `!`command`` preprocessing. Any language that prints to stdout.

## Why Computed Skills

Static skills have a fundamental problem: they can't see context. A static code review skill gives the same checklist whether you changed 2 files or 40, whether you touched auth code or CSS. A static deploy checklist shows 20 items every time — and agents learn to skim them all.

Computed skills fix this by letting a script decide what the agent sees:

```
2 auth files changed  →  "Security review: check for secrets, validate auth flows"
40 config files       →  "Config audit: check YAML syntax, verify no breaking changes"
3 test files only     →  "Test review: check coverage, look for flaky patterns"
Nothing changed       →  "No pending changes. Ready for new work."
```

Same skill, different instructions. The script does the mechanical work (reading files, counting things, comparing dates). The agent focuses on judgment.

### The key insight

**Don't make the LLM do work that code can do faster and more reliably.**

LLMs are bad at: counting entries in a file, comparing timestamps, parsing structured data, doing math, detecting duplicates. Python does all of this in milliseconds with 100% accuracy. When you catch yourself writing static instructions like "parse the file and count how many entries have status=pending," that's a signal to compute instead.

## Quick Start

**1. Create the skill:**

```
my-skill/
├── SKILL.md
└── scripts/
    └── generate.py
```

**2. SKILL.md** (the thin shell):

```yaml
---
name: my-skill
description: What it does and when to trigger it
---

!`python3 ${CLAUDE_SKILL_DIR}/scripts/generate.py $ARGUMENTS`
```

**3. generate.py** (the brain):

```python
#!/usr/bin/env python3
import os, sys

def main():
    args_str = os.environ.get("ARGUMENTS", "").strip()
    args = args_str.split() if args_str else sys.argv[1:]
    mode = args[0] if args else ""

    if mode == "status":
        print("# Status Dashboard\n")
        # ... verbose output for manual invocation
    else:
        print("# Default Mode\n")
        # ... context-aware instructions

if __name__ == "__main__":
    main()
```

The `!`command`` syntax is a preprocessing directive — it runs before the agent sees anything and replaces itself with stdout. The agent receives pure markdown, as if you wrote it by hand.

## Three Patterns

### 1. Computed — script generates context-aware instructions

The script reads the environment and outputs different instructions based on what it finds. The agent receives a prompt tailored to the current situation.

**Example: code review that picks its own strategy**

The script analyzes `git diff`, categorizes the changes, and outputs a focused review prompt:

```python
# What the script does:
changed_files = get_git_diff()
has_auth_changes = any("auth" in f for f in changed_files)
has_config_changes = any(f.endswith((".yml", ".json", ".env")) for f in changed_files)

if has_auth_changes:
    print("## Security Review")
    print("Auth files changed. Check for hardcoded secrets, validate input...")
elif has_config_changes:
    print("## Config Audit")
    print("Config files changed. Check for breaking changes, validate syntax...")
else:
    print("## Correctness Review")
    print("Standard code changes. Check logic, error handling, edge cases...")
```

```
# What the agent sees (when auth files changed):

## Security Review
Auth files changed. Check for hardcoded secrets, validate input...
```

The agent doesn't know a script ran. It just sees the right instructions for the right situation.

Best for: code review, data analysis, anything where context determines strategy.

**Full example:** [`examples/smart-review`](examples/smart-review)

### 2. Hybrid — conditional behavioral instructions

The script wraps plain English instructions in conditionals. The agent only sees the relevant branch. This solves a real problem: agents ignore "always-on" mandates buried in long static documents.

**Example: deploy checklist that escalates based on risk**

```python
risk = assess_deployment_risk()

if risk == "high":
    print("# STOP — High-Risk Deployment")
    print("Database migration detected. Before proceeding:")
    print("- [ ] Verify migration is reversible")
    print("- [ ] Confirm backup exists")
    print("- [ ] Get explicit approval")
elif risk == "medium":
    print("# Deploy Checklist")
    print("Config changes detected. Verify:")
    print("- [ ] No secrets in committed files")
    print("- [ ] Environment variables documented")
else:
    print("# Ready to Deploy")
    print("Low-risk changes. Standard deploy process.")
```

A static checklist with 20 items gets ignored. Three items that only appear when relevant get followed.

Best for: always-on behaviors, escalating warnings, conditional workflows.

**Full example:** [`examples/deploy-checklist`](examples/deploy-checklist)

### 3. Multi-mode — one skill, multiple interfaces

Production skills often support multiple modes via arguments. The convention:

| Mode | Purpose | Output |
|------|---------|--------|
| *(no args)* | Default / always-on | Context-aware instructions |
| `status` | Manual dashboard (`/command`) | Verbose report |
| `heartbeat` | Periodic health check | Silent unless problems found |

The `heartbeat` convention is the key: **output nothing when everything is OK.** Only speak up when there's something to act on. Silence means healthy.

**Example: dependency auditor with three modes**

```python
mode = get_mode()
issues = scan_dependencies()

if mode == "heartbeat":
    # Silent unless problems found
    if issues:
        print(f"# {len(issues)} dependency issues need attention")
        for i in issues:
            print(f"- {i}")
    # else: print nothing — silence means healthy

elif mode == "status":
    # Full dashboard for manual invocation
    print("# Dependency Audit Report\n")
    print(f"Total: {total}, Pinned: {pinned}, Unpinned: {unpinned}")
    print(f"Lock file: {'current' if lock_ok else 'STALE'}")
    for i in issues:
        print(f"- {i}")

else:
    # Default: brief context, only if relevant
    if issues:
        print(f"Note: {len(issues)} dependency issues. Run /dependency-audit for details.")
```

```yaml
# SKILL.md — user can invoke with /dependency-audit or /dependency-audit status
!`python3 ${CLAUDE_SKILL_DIR}/scripts/generate.py $ARGUMENTS`
```

Best for: skills that need both an always-on presence and a manual dashboard.

**Full example:** [`examples/dependency-audit`](examples/dependency-audit)

## When to Use What

| Situation | Pattern |
|-----------|---------|
| Instructions never change | Static (plain markdown) |
| Instructions depend on context (git state, file contents, time) | Computed |
| Behavioral instructions that get ignored when too long | Hybrid |
| Agent needs to parse structured data (logs, entries, state files) | Computed |
| Skill should remember across runs | Computed + state file |
| Skill needs both always-on monitoring and manual dashboard | Multi-mode |

**Start static, switch to computed when you notice the agent doing work that code could do faster.**

## How to Build One

1. **Write the skill as static markdown first.** Get the instructions right.
2. **Notice what changes between invocations.** What context matters? What does the agent keep getting wrong because it can't see the current state?
3. **Write a script that generates the markdown.** Read context, make decisions, print to stdout.
4. **Replace the SKILL.md body** with the `!`command`` invocation.
5. **Add a state file** (JSON) if you need memory across runs.

### Passing arguments

`$ARGUMENTS` in SKILL.md is text substitution — replaced with invocation arguments *before* the shell runs.

In Python, read arguments like this:

```python
args_str = os.environ.get("ARGUMENTS", "").strip()
args = args_str.split() if args_str else sys.argv[1:]
```

This handles both skill invocation (via environment variable) and direct CLI usage (via argv).

### Error handling

If the script crashes, the agent gets nothing (or a traceback). For production skills:

```python
try:
    generate()
except Exception as e:
    print("# Fallback Instructions\n\nRun the standard checklist manually.")
    print(f"\n<!-- generator error: {e} -->")
```

### State files

For skills that should remember across runs (tracking what was reviewed, avoiding duplicate strategies):

```python
import json
from pathlib import Path

STATE_FILE = Path(__file__).parent / ".state.json"

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"runs": 0}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))
```

Keep state minimal — just enough to avoid repeating yourself.

## Examples

| Example | Pattern | What it does |
|---------|---------|-------------|
| [`smart-review`](examples/smart-review) | Computed | Reads git diff, categorizes changes, picks a review strategy |
| [`deploy-checklist`](examples/deploy-checklist) | Hybrid | Assesses deployment risk, shows only relevant checklist items |
| [`dependency-audit`](examples/dependency-audit) | Multi-mode | Scans dependencies with default/status/heartbeat modes |

Each example is self-contained — clone the repo and use them directly.

## Production Case Study

These patterns come from running 11 computed skills on a 24/7 autonomous agent. Some lessons learned:

**What worked immediately:**
- Moving structured data parsing from LLM to Python eliminated an entire class of errors. The agent was miscounting entries, missing duplicates, and getting date math wrong. Python does it in milliseconds with zero mistakes.
- The hybrid pattern dramatically improved instruction compliance. A 200-line static behavior document was routinely ignored. Conditional sections that only show what's relevant right now get followed consistently.
- Multi-mode skills with the heartbeat convention ("silence means healthy") reduced noise by ~90%. The agent stopped reporting "all systems normal" every 10 minutes.

**What we learned the hard way:**
- Don't convert everything to computed. Skills that are mostly behavioral instructions or conversational workflows don't benefit — the maintenance cost of a Python script isn't worth it. Only compute when the LLM would otherwise do mechanical work.
- State files need atomic writes (write to temp file, then rename). A crash mid-write corrupts the state and the skill breaks silently.
- Error handling in the generator is critical. If the script crashes, the agent gets an empty prompt or a traceback and behaves unpredictably. Always have a fallback.
- LLMs cannot do time math. If your skill involves timestamps, timezone conversions, or "how long since X," compute it in Python and inject the result. Never ask the LLM to calculate time differences.

## Prior Art

The [`!`command`` syntax](https://code.claude.com/docs/en/skills#inject-dynamic-context) is documented by Anthropic under "inject dynamic context" but rarely used for full prompt generation.

As of March 2026:

- **[Anthropic's skills repo](https://github.com/anthropics/skills/)** (17 skills) — all static, zero computed
- **[SkillsMP](https://skillsmp.com)** (400K+ indexed) — no category for computed skills
- **[vibereq](https://github.com/dipasqualew/vibereq)** — another project using the pattern in production

Related approaches: [DSPy](https://dspy.ai/) (programmatic prompt compilation via SDK), [context engineering](https://martinfowler.com/articles/exploring-gen-ai/context-engineering-coding-agents.html) (the broader concept).

## Requirements

- Python 3.8+ (or any language that prints to stdout)
- A skill system that supports `!`command`` preprocessing
- Works with [Claude Code](https://claude.ai/claude-code) and compatible platforms

## Security

A computed skill runs a script in your shell every time the skill
fires. That makes the threat model identical to "running someone
else's script on your machine."

- **Read every script before installing a third-party computed
  skill.** The `!`command`` syntax executes whatever the SKILL.md
  says to execute, in your shell, with your filesystem permissions.
  Static skills can be eyeballed for prompt-injection surface;
  computed skills can do anything a Python program can.
- **Pin script paths.** Use `${CLAUDE_SKILL_DIR}/scripts/...` or
  similar absolute resolution. Don't accept skills that shell out
  to commands found via `$PATH` lookup — that's a hijack vector.
- **Don't pass `$ARGUMENTS` blindly to a subprocess.** If your
  generator script invokes other commands using user-supplied
  arguments, treat `$ARGUMENTS` as untrusted input. Validate or
  escape before passing to `subprocess.run`.
- **Atomic writes for state.** As the [Production Case Study](#production-case-study)
  notes, a crash mid-write corrupts state and the skill breaks
  silently. Always write to a temp file and rename.

The patterns here are written as teaching examples. Audit them
before adopting in load-bearing pipelines.

## License

MIT — see [LICENSE](LICENSE).
