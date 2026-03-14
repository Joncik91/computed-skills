#!/usr/bin/env python3
"""
smart-review: A computed skill that generates context-aware code review instructions.

Instead of static review instructions, this script inspects the current git state
and picks a review strategy tailored to what actually changed. A security-heavy diff
gets security-focused instructions. A test-only change gets test-quality guidance.
The same skill, different output every time.

Usage:
    python3 generate.py              # auto-detect strategy from git state
    python3 generate.py security     # force a specific strategy
"""

import subprocess
import sys
import os
from collections import Counter

# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def run(cmd):
    """Run a shell command and return stdout, or empty string on failure."""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        return result.stdout.strip()
    except Exception:
        return ""


def get_branch():
    return run("git rev-parse --abbrev-ref HEAD") or "unknown"


def get_changed_files():
    """Return list of changed files (staged + unstaged vs HEAD)."""
    # Try unstaged + staged against HEAD first
    raw = run("git diff --name-only HEAD")
    if not raw:
        # Fallback: only staged changes (e.g., initial commit)
        raw = run("git diff --name-only --cached")
    return [f for f in raw.splitlines() if f] if raw else []


def get_diff_stat():
    """Return the compact diff stat summary."""
    return run("git diff --stat HEAD") or run("git diff --stat --cached") or "No diff available."


def get_extensions(files):
    """Return a Counter of file extensions."""
    exts = Counter()
    for f in files:
        _, ext = os.path.splitext(f)
        exts[ext.lower() if ext else "(no ext)"] += 1
    return exts


def get_directories(files):
    """Return a set of top-level directories touched."""
    dirs = set()
    for f in files:
        parts = f.split("/")
        if len(parts) > 1:
            dirs.add(parts[0])
    return dirs


# ---------------------------------------------------------------------------
# Strategy detection
# ---------------------------------------------------------------------------

# Patterns that signal specific review strategies
SECURITY_PATTERNS = {
    ".env", "auth", "login", "password", "secret", "token",
    "credential", "oauth", "jwt", "crypto", "ssl", "tls",
    "permission", "rbac", "acl", "sanitize", "escape",
}

CONFIG_EXTENSIONS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env"}

TEST_PATTERNS = {"test", "spec", "__tests__", "tests", "testing", "_test", ".test"}


def classify_file(filepath):
    """Return tags for a single file based on its path and extension."""
    tags = set()
    lower = filepath.lower()
    _, ext = os.path.splitext(lower)
    basename = os.path.basename(lower)
    parts = set(lower.replace("\\", "/").split("/"))

    # Security signals -- check if any keyword appears anywhere in the path
    for keyword in SECURITY_PATTERNS:
        if keyword in lower:
            tags.add("security")
            break

    # Config signals -- match by extension
    if ext in CONFIG_EXTENSIONS:
        tags.add("config")

    # Test signals -- match by filename or directory
    for pattern in TEST_PATTERNS:
        if pattern in basename or parts & {pattern}:
            tags.add("test")
            break

    return tags


def detect_strategy(files, forced=None):
    """
    Pick the best review strategy based on changed files.
    Returns (strategy_name, reason) tuple.

    This is the core of the computed pattern: deterministic logic that would
    waste LLM tokens if done inside the prompt. The script decides, the LLM
    just follows the tailored instructions.
    """
    if forced:
        return forced, f"Strategy `{forced}` was explicitly requested."

    if not files:
        return "general-scan", "No changed files detected -- falling back to general scan."

    # Classify every changed file
    all_tags = Counter()
    for f in files:
        for tag in classify_file(f):
            all_tags[tag] += 1

    total = len(files)
    security_ratio = all_tags.get("security", 0) / total
    config_ratio = all_tags.get("config", 0) / total
    test_ratio = all_tags.get("test", 0) / total

    # Decision tree -- most specific signal wins
    if security_ratio >= 0.3:
        return "security", f"{all_tags['security']}/{total} files touch auth, secrets, or crypto paths."

    if config_ratio >= 0.5:
        return "config-audit", f"{all_tags['config']}/{total} files are configuration files."

    if test_ratio >= 0.6:
        return "test-quality", f"{all_tags['test']}/{total} files are test files."

    if total >= 20:
        return "architecture", f"Large changeset ({total} files) -- reviewing structural impact."

    dirs = get_directories(files)
    if len(dirs) >= 4:
        return "architecture", f"Changes span {len(dirs)} directories -- reviewing cross-cutting impact."

    return "correctness", "Focused changeset -- reviewing logic and edge cases."


# ---------------------------------------------------------------------------
# Strategy-specific review instructions
# ---------------------------------------------------------------------------

STRATEGIES = {
    "security": """
## Review focus: Security

You are reviewing changes that touch authentication, secrets, permissions, or crypto.

Pay close attention to:
- **Hardcoded secrets** -- API keys, tokens, passwords in source code
- **Input validation** -- SQL injection, XSS, command injection, path traversal
- **Auth flows** -- broken authentication, missing authorization checks
- **Crypto usage** -- weak algorithms, improper key management, missing salt/IV
- **Dependency risk** -- new dependencies with known CVEs
- **Error handling** -- stack traces or internal details leaked to users
""",

    "config-audit": """
## Review focus: Configuration audit

You are reviewing configuration file changes.

Pay close attention to:
- **Environment leakage** -- production secrets committed, debug flags left on
- **Default values** -- insecure defaults, missing required fields
- **Format correctness** -- valid JSON/YAML/TOML syntax, schema compliance
- **Breaking changes** -- renamed keys, removed fields, changed types
- **Consistency** -- config values that must match across files
""",

    "test-quality": """
## Review focus: Test quality

You are reviewing test code changes.

Pay close attention to:
- **Coverage gaps** -- happy path only, missing edge cases and error paths
- **Assertion quality** -- meaningful assertions vs. just "does not throw"
- **Test isolation** -- shared mutable state, order-dependent tests
- **Flakiness risks** -- timing dependencies, network calls, random data without seeds
- **Readability** -- test names that describe behavior, clear arrange/act/assert
""",

    "architecture": """
## Review focus: Architecture

You are reviewing a large or cross-cutting changeset.

Pay close attention to:
- **Dependency direction** -- do new imports create circular dependencies?
- **Abstraction boundaries** -- is business logic leaking into transport/UI layers?
- **API surface changes** -- breaking changes to public interfaces
- **Duplication** -- similar logic introduced in multiple places
- **Migration path** -- if this is a refactor, is there a safe incremental path?
""",

    "correctness": """
## Review focus: Correctness

You are reviewing a focused changeset for logic and edge cases.

Pay close attention to:
- **Off-by-one errors** -- loop bounds, string slicing, array indexing
- **Null/undefined handling** -- missing null checks, optional chaining needed
- **Error propagation** -- swallowed exceptions, missing error returns
- **Race conditions** -- shared state accessed without synchronization
- **Resource leaks** -- unclosed files, connections, event listeners
- **Type mismatches** -- implicit coercions, wrong argument types
""",

    "general-scan": """
## Review focus: General scan

No strong signal detected -- performing a broad review.

Check for:
- Obvious bugs or logic errors
- Code style and readability issues
- Missing error handling
- Potential performance problems
- Anything that looks surprising or risky
""",
}


# ---------------------------------------------------------------------------
# Output -- assemble the final prompt the AI agent will follow
# ---------------------------------------------------------------------------

def main():
    # Allow forcing a strategy via command-line argument
    forced = None
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower().strip()
        if arg in STRATEGIES:
            forced = arg

    # Gather git context
    branch = get_branch()
    files = get_changed_files()
    diff_stat = get_diff_stat()
    extensions = get_extensions(files)
    strategy, reason = detect_strategy(files, forced)

    # Format extension summary (top 5)
    ext_summary = ", ".join(f"{ext} ({n})" for ext, n in extensions.most_common(5))
    if not ext_summary:
        ext_summary = "none"

    # Emit the tailored review prompt
    print(f"""# Code Review -- {strategy.replace("-", " ").title()} Strategy

> **Why this strategy:** {reason}

## Git context

| Detail | Value |
|--------|-------|
| Branch | `{branch}` |
| Files changed | {len(files)} |
| Extensions | {ext_summary} |

<details>
<summary>Diff stat</summary>

```
{diff_stat}
```

</details>

{STRATEGIES[strategy].strip()}

## Output format

For each issue found, report:

```
[severity] file:line -- description
  -> suggested fix
```

Severity levels:
- **critical** -- security vulnerability, data loss, or crash
- **high** -- bug that will manifest in production
- **medium** -- code smell, maintainability concern, or edge case
- **low** -- style, naming, or minor improvement

Start with critical/high issues. If you find none, say so explicitly before
listing medium/low items.

Review the diff now.""")


if __name__ == "__main__":
    main()
