#!/usr/bin/env python3
"""
Smart Review — a Python-powered skill that generates contextual prompts.

Instead of static markdown instructions, this script:
1. Analyzes the current codebase state
2. Detects what kind of changes were made
3. Picks a review strategy based on context
4. Outputs a tailored markdown prompt
"""

import subprocess
import json
import os
import sys
from pathlib import Path
from collections import Counter
from datetime import datetime

STATE_FILE = Path(__file__).parent / ".smart-review-state.json"


def run(cmd, default=""):
    try:
        return subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL, timeout=10).decode().strip()
    except Exception:
        return default


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"runs": 0, "last_strategies": [], "hits": [], "misses": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def analyze_git():
    """Gather git context."""
    is_git = run("git rev-parse --is-inside-work-tree") == "true"
    if not is_git:
        return None

    diff_stat = run("git diff --stat HEAD~1 2>/dev/null || git diff --stat")
    diff_names = run("git diff --name-only HEAD~1 2>/dev/null || git diff --name-only").splitlines()
    recent_msgs = run("git log --oneline -5 2>/dev/null")
    branch = run("git branch --show-current")

    # Categorize changed files
    extensions = Counter(Path(f).suffix for f in diff_names if Path(f).suffix)
    directories = Counter(str(Path(f).parent) for f in diff_names)

    return {
        "branch": branch,
        "diff_stat": diff_stat,
        "changed_files": diff_names,
        "file_count": len(diff_names),
        "extensions": dict(extensions.most_common(5)),
        "hotspot_dirs": dict(directories.most_common(3)),
        "recent_commits": recent_msgs,
    }


def analyze_codebase():
    """Quick codebase fingerprint."""
    cwd = Path(".")
    markers = {
        "package.json": "javascript/node",
        "requirements.txt": "python",
        "Cargo.toml": "rust",
        "go.mod": "go",
        "pom.xml": "java",
        "Gemfile": "ruby",
        "composer.json": "php",
    }
    stack = []
    for marker, lang in markers.items():
        if (cwd / marker).exists():
            stack.append(lang)

    has_tests = bool(run("find . -maxdepth 3 -name '*test*' -o -name '*spec*' | head -5"))
    has_ci = any((cwd / f).exists() for f in [".github/workflows", ".gitlab-ci.yml", "Jenkinsfile"])

    return {"stack": stack, "has_tests": has_tests, "has_ci": has_ci}


def pick_strategy(git_ctx, codebase, state):
    """The brain — decide HOW to review based on context."""
    strategies = []
    reasoning = []

    if not git_ctx or git_ctx["file_count"] == 0:
        return ["general-scan"], ["No git changes detected — doing a general scan"]

    # Large changeset → architecture focus
    if git_ctx["file_count"] > 15:
        strategies.append("architecture")
        reasoning.append(f"{git_ctx['file_count']} files changed — checking architectural coherence")

    # Security-sensitive paths
    security_words = ["auth", "login", "token", "secret", "cred", "password", "session", "crypto", "key"]
    sec_files = [f for f in git_ctx["changed_files"] if any(w in f.lower() for w in security_words)]
    if sec_files:
        strategies.append("security")
        reasoning.append(f"Security-sensitive files touched: {', '.join(sec_files[:3])}")

    # Config/infra changes
    infra_patterns = [".yml", ".yaml", ".toml", ".json", ".env", "Dockerfile", "docker-compose"]
    infra_files = [f for f in git_ctx["changed_files"] if any(p in f for p in infra_patterns)]
    if len(infra_files) > len(git_ctx["changed_files"]) * 0.5:
        strategies.append("config-audit")
        reasoning.append(f"Majority config/infra changes ({len(infra_files)} files)")

    # Test changes
    test_files = [f for f in git_ctx["changed_files"] if "test" in f.lower() or "spec" in f.lower()]
    if test_files and not codebase["has_tests"]:
        strategies.append("test-quality")
        reasoning.append("New tests in a project without established test patterns")

    # Avoid repeating the last strategy
    if strategies and state["last_strategies"]:
        last = state["last_strategies"][-1]
        if strategies == [last] and len(strategies) == 1:
            strategies.append("fresh-eyes")
            reasoning.append(f"Same strategy as last run ({last}) — adding fresh-eyes pass")

    if not strategies:
        strategies.append("correctness")
        reasoning.append("Standard code changes — focusing on correctness")

    return strategies, reasoning


def generate_prompt(strategies, reasoning, git_ctx, codebase, state):
    """Compose the final markdown prompt from strategy decisions."""
    sections = []

    # Header with meta-awareness
    sections.append(f"# Smart Review (run #{state['runs'] + 1})")
    sections.append("")
    sections.append("## Why this review strategy")
    for r in reasoning:
        sections.append(f"- {r}")
    sections.append("")

    # Git context
    if git_ctx and git_ctx["diff_stat"]:
        sections.append("## Changes")
        sections.append(f"**Branch:** `{git_ctx['branch']}`")
        sections.append(f"**Files changed:** {git_ctx['file_count']}")
        sections.append("")
        sections.append("```")
        sections.append(git_ctx["diff_stat"])
        sections.append("```")
        sections.append("")

    # Strategy-specific instructions
    strategy_prompts = {
        "architecture": """## Architecture Review
- Are the changes cohesive? Do they belong in one PR or should they be split?
- Do new files follow existing directory conventions?
- Are there new dependencies? Are they justified?
- Check for layering violations (e.g., UI code importing DB modules directly)""",

        "security": """## Security Review
- Check for hardcoded secrets, tokens, or credentials
- Validate input sanitization on any new endpoints
- Check authentication/authorization on new routes
- Look for SQL injection, XSS, command injection vectors
- Verify secrets aren't logged or exposed in error messages""",

        "config-audit": """## Configuration Audit
- Are there breaking changes to config formats?
- Are defaults sensible and documented?
- Check for secrets in config files that should be in env vars
- Validate YAML/JSON syntax if possible
- Check Docker/CI changes for security implications""",

        "test-quality": """## Test Quality Review
- Do tests cover edge cases, not just happy paths?
- Are assertions specific (not just "doesn't throw")?
- Check for flaky patterns (timing, ordering, external deps)
- Is test naming clear about what's being tested?""",

        "correctness": """## Correctness Review
- Read each changed file and check for logic errors
- Look for off-by-one errors, null/undefined handling
- Check error paths — are exceptions caught and handled?
- Verify function contracts (do inputs match expected types?)
- Check for race conditions in async code""",

        "fresh-eyes": """## Fresh Eyes Pass
- Pretend you've never seen this code. What's confusing?
- Are there implicit assumptions that should be documented?
- Could any of this break under load or unusual input?
- What would a new team member struggle with?""",

        "general-scan": """## General Scan
- Look at the current working directory for code quality issues
- Check for any obvious bugs, security issues, or anti-patterns
- Suggest improvements if anything stands out""",
    }

    for s in strategies:
        if s in strategy_prompts:
            sections.append(strategy_prompts[s])
            sections.append("")

    # Adaptive: mention past patterns
    if state["misses"]:
        sections.append("## Learn from past misses")
        sections.append("Previous reviews missed these — pay extra attention:")
        for m in state["misses"][-3:]:
            sections.append(f"- {m}")
        sections.append("")

    # Output format
    sections.append("## Output format")
    sections.append("For each finding:")
    sections.append("1. **File:line** — exact location")
    sections.append("2. **Severity** — critical / high / medium / low")
    sections.append("3. **Issue** — one sentence")
    sections.append("4. **Fix** — concrete suggestion")
    sections.append("")
    sections.append("End with a summary: total findings by severity, and one sentence on overall code health.")

    return "\n".join(sections)


def main():
    state = load_state()
    git_ctx = analyze_git()
    codebase = analyze_codebase()
    strategies, reasoning = pick_strategy(git_ctx, codebase, state)
    prompt = generate_prompt(strategies, reasoning, git_ctx, codebase, state)

    # Update state
    state["runs"] += 1
    state["last_strategies"] = strategies
    save_state(state)

    # Output the generated prompt
    print(prompt)


if __name__ == "__main__":
    main()
