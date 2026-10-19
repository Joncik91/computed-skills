#!/usr/bin/env python3
"""
smart-review: a computed skill that generates context-aware code review instructions.

Instead of static review instructions, this script inspects the current git
state and picks a review strategy tailored to what actually changed. A
security-heavy diff gets security-focused instructions. A test-only change
gets test-quality guidance. Same skill, different output every time.

What gets reviewed, in order of preference:
  1. Uncommitted work (staged, unstaged and untracked files) vs HEAD.
  2. If the working tree is clean: commits on this branch vs the default branch.
  3. If neither has changes: a short "nothing to review" message.

Usage:
    generate.py [--project-dir DIR] [STRATEGY]

    STRATEGY forces one of: security, config-audit, test-quality,
    architecture, correctness, general-scan.

Contract with the skill runner: always print markdown, always exit 0.
A non-zero exit aborts the entire skill invocation in Claude Code.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, NamedTuple, Sequence

# Keep the prompt lean: never dump hundreds of lines into the agent's context.
MAX_STAT_LINES = 40
MAX_LISTED_FILES = 25

# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------
# Claude Code substitutes $ARGUMENTS into the command text before the shell
# runs it, so arguments arrive as ordinary argv. There is no ARGUMENTS
# environment variable.


class Args(NamedTuple):
    project_dir: str | None
    positional: list[str]


def parse_args(argv: Sequence[str]) -> Args:
    """Tiny parser that never exits. argparse calls sys.exit(2) on bad
    input, which would abort the whole skill."""
    project_dir = None
    positional = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--project-dir" and i + 1 < len(argv):
            project_dir = argv[i + 1] or None
            i += 2
            continue
        if arg.startswith("--project-dir="):
            project_dir = arg.split("=", 1)[1] or None
        elif arg.strip():
            positional.append(arg.strip())
        i += 1
    return Args(project_dir, positional)


def resolve_start_dir(project_dir: str | None) -> Path:
    """The `!` command runs in the session's current directory, which moves
    whenever the agent runs `cd`. Prefer an explicit project dir."""
    for candidate in (project_dir, os.environ.get("CLAUDE_PROJECT_DIR")):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return Path.cwd()


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def git(cwd: Path, *args: str, timeout: float = 15) -> str | None:
    """Run git with an argument list (no shell). Returns stdout, or None on
    any failure so callers can tell "failed" apart from "empty"."""
    env = dict(os.environ)
    # Read-only commands like `git diff` may refresh the index and take
    # index.lock. That races with a concurrent `git commit` from the agent.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def split_z(raw: str | None) -> list[str]:
    """Split NUL-delimited git output. -z keeps unusual filenames (spaces,
    quotes, non-ASCII) intact instead of C-quoting them."""
    return [p for p in (raw or "").split("\0") if p]


def unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def detect_base_ref(repo: Path) -> str | None:
    """Find the default branch: origin/HEAD, then the usual names."""
    head = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    candidates = [head.strip()] if head and head.strip() else []
    candidates += ["origin/main", "origin/master", "main", "master"]
    for ref in candidates:
        if git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}") is not None:
            return ref
    return None


class Scope(NamedTuple):
    files: list[str]
    description: str       # human-readable: what is being reviewed
    review_command: str    # how the agent sees the full diff
    review_note: str       # anything the diff command doesn't show
    diff_stat: str


def working_tree_scope(repo: Path) -> Scope | None:
    has_head = git(repo, "rev-parse", "--verify", "--quiet", "HEAD") is not None
    # On an unborn branch there is no HEAD; --cached compares to the empty tree.
    against = ["HEAD"] if has_head else ["--cached"]
    tracked = split_z(git(repo, "diff", "--name-only", "-z", *against))
    untracked = split_z(git(repo, "ls-files", "--others", "--exclude-standard", "-z"))
    files = unique(tracked + untracked)
    if not files:
        return None
    stat = (git(repo, "diff", "--stat=100", *against) or "").rstrip()
    if untracked:
        stat += "\n" + "\n".join(f" {f} | untracked (new file)" for f in untracked)
    cmd = "git diff HEAD" if has_head else "git diff --cached"
    note = (f"{len(untracked)} untracked file(s) are not in that diff: read them directly."
            if untracked else "")
    return Scope(files, "uncommitted changes (staged, unstaged and untracked)",
                 cmd, note, stat.strip("\n"))


def branch_scope(repo: Path, branch: str) -> Scope | None:
    base = detect_base_ref(repo)
    if not base:
        return None
    # Skip when HEAD is the base itself: nothing branch-specific to review.
    head_sha = git(repo, "rev-parse", "HEAD")
    base_sha = git(repo, "rev-parse", base + "^{commit}")
    if head_sha is None or head_sha == base_sha:
        return None
    rng = f"{base}...HEAD"
    files = split_z(git(repo, "diff", "--name-only", "-z", rng))
    if not files:
        return None
    stat = (git(repo, "diff", "--stat=100", rng) or "").strip("\n")
    return Scope(files, f"commits on `{branch}` not yet in `{base}`",
                 f"git diff {rng}", "", stat)


def truncate_stat(stat: str) -> str:
    lines = stat.splitlines()
    if len(lines) <= MAX_STAT_LINES:
        return stat or "(no diff stat available)"
    hidden = len(lines) - MAX_STAT_LINES
    # Keep the last line: it's git's "N files changed, ..." summary.
    return "\n".join(lines[: MAX_STAT_LINES - 1] + [f" ... {hidden} more lines ...", lines[-1]])


def describe_branch(repo: Path) -> str:
    name = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if name and name.strip():
        return name.strip()
    sha = git(repo, "rev-parse", "--short", "HEAD")
    return f"detached at {sha.strip()}" if sha else "no commits yet"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
# Paths are split into word tokens (snake_case, kebab-case, camelCase, dots)
# and matched as whole words. Plain substring matching misfires: "acl" is
# inside "oracle", "test" inside "latest", "token" inside "tokenizer".

_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")

SECURITY_WORDS = {
    "auth", "authn", "authz", "authenticate", "authentication", "authenticator",
    "authorize", "authorization", "authorizer", "login", "logout", "signin",
    "password", "passwords", "passwd", "secret", "secrets", "token", "tokens",
    "credential", "credentials", "creds", "oauth", "jwt", "jwks", "crypto",
    "cryptography", "encrypt", "decrypt", "encryption", "cipher", "hmac",
    "ssl", "tls", "cert", "certs", "certificate", "permission", "permissions",
    "rbac", "acl", "acls", "sanitize", "sanitizer", "escape", "csrf", "xss",
    "cors", "saml", "sso", "session", "sessions", "keystore",
}
SECRET_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".jks"}

CONFIG_EXTENSIONS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties"}

TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "testing", "e2e"}
TEST_WORDS = {"test", "tests", "spec", "specs"}


def words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)}


def is_env_file(basename: str) -> bool:
    return basename == ".env" or basename.startswith(".env.")


def is_test_path(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    if any(p.lower() in TEST_DIRS for p in parts[:-1]):
        return True
    base = parts[-1]
    return base.lower() == "conftest.py" or bool(words(base) & TEST_WORDS)


def classify_file(path: str) -> set[str]:
    """Return the review tags for one path."""
    tags = set()
    base = os.path.basename(path).lower()
    ext = os.path.splitext(base)[1]
    if words(path) & SECURITY_WORDS or is_env_file(base) or ext in SECRET_EXTENSIONS:
        tags.add("security")
    # os.path.splitext(".env") returns ext "", so .env files need their own check.
    if ext in CONFIG_EXTENSIONS or is_env_file(base):
        tags.add("config")
    if is_test_path(path):
        tags.add("test")
    return tags


def top_level_dirs(files: Iterable[str]) -> set[str]:
    return {f.split("/", 1)[0] for f in files if "/" in f}


def detect_strategy(files: Sequence[str]) -> tuple[str, str]:
    """Pick a strategy from the changed files. Returns (strategy, reason).

    This is the core of the computed pattern: deterministic logic that would
    waste tokens, and be done unreliably, inside the prompt."""
    total = len(files)
    tags: Counter[str] = Counter()
    for f in files:
        tags.update(classify_file(f))

    # Decision tree: most specific signal wins.
    if tags["security"] / total >= 0.3:
        return "security", f"{tags['security']}/{total} files touch auth, secrets, or crypto paths."
    if tags["config"] / total >= 0.5:
        return "config-audit", f"{tags['config']}/{total} files are configuration files."
    if tags["test"] / total >= 0.6:
        return "test-quality", f"{tags['test']}/{total} files are test files."
    if total >= 20:
        return "architecture", f"Large changeset ({total} files): reviewing structural impact."
    dirs = top_level_dirs(files)
    if len(dirs) >= 4:
        return "architecture", f"Changes span {len(dirs)} top-level directories: reviewing cross-cutting impact."
    return "correctness", "Focused changeset: reviewing logic and edge cases."


# ---------------------------------------------------------------------------
# Strategy-specific review instructions
# ---------------------------------------------------------------------------

STRATEGIES = {
    "security": """
## Review focus: Security

You are reviewing changes that touch authentication, secrets, permissions, or crypto.

Pay close attention to:
- **Hardcoded secrets**: API keys, tokens, passwords in source code
- **Input validation**: SQL injection, XSS, command injection, path traversal
- **Auth flows**: broken authentication, missing authorization checks
- **Crypto usage**: weak algorithms, improper key management, missing salt/IV
- **Dependency risk**: new dependencies with known CVEs
- **Error handling**: stack traces or internal details leaked to users
""",
    "config-audit": """
## Review focus: Configuration audit

You are reviewing configuration file changes.

Pay close attention to:
- **Environment leakage**: production secrets committed, debug flags left on
- **Default values**: insecure defaults, missing required fields
- **Format correctness**: valid JSON/YAML/TOML syntax, schema compliance
- **Breaking changes**: renamed keys, removed fields, changed types
- **Consistency**: config values that must match across files
""",
    "test-quality": """
## Review focus: Test quality

You are reviewing test code changes.

Pay close attention to:
- **Coverage gaps**: happy path only, missing edge cases and error paths
- **Assertion quality**: meaningful assertions vs. just "does not throw"
- **Test isolation**: shared mutable state, order-dependent tests
- **Flakiness risks**: timing dependencies, network calls, random data without seeds
- **Readability**: test names that describe behavior, clear arrange/act/assert
""",
    "architecture": """
## Review focus: Architecture

You are reviewing a large or cross-cutting changeset.

Pay close attention to:
- **Dependency direction**: do new imports create circular dependencies?
- **Abstraction boundaries**: is business logic leaking into transport/UI layers?
- **API surface changes**: breaking changes to public interfaces
- **Duplication**: similar logic introduced in multiple places
- **Migration path**: if this is a refactor, is there a safe incremental path?
""",
    "correctness": """
## Review focus: Correctness

You are reviewing a focused changeset for logic and edge cases.

Pay close attention to:
- **Off-by-one errors**: loop bounds, string slicing, array indexing
- **Null/undefined handling**: missing null checks, optional chaining needed
- **Error propagation**: swallowed exceptions, missing error returns
- **Race conditions**: shared state accessed without synchronization
- **Resource leaks**: unclosed files, connections, event listeners
- **Type mismatches**: implicit coercions, wrong argument types
""",
    "general-scan": """
## Review focus: General scan

Performing a broad review.

Check for:
- Obvious bugs or logic errors
- Code style and readability issues
- Missing error handling
- Potential performance problems
- Anything that looks surprising or risky
""",
}

OUTPUT_FORMAT = """## Output format

For each issue found, report:

```
[severity] file:line: description
  -> suggested fix
```

Severity levels:
- **critical**: security vulnerability, data loss, or crash
- **high**: bug that will manifest in production
- **medium**: code smell, maintainability concern, or edge case
- **low**: style, naming, or minor improvement

Start with critical/high issues. If you find none, say so explicitly before
listing medium/low items."""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def code_span(text: str) -> str:
    return "`" + text.replace("`", "'") + "`"


def table_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def extension_summary(files: Sequence[str]) -> str:
    exts = Counter(os.path.splitext(f)[1].lower() or "(no ext)" for f in files)
    return ", ".join(f"{ext} ({n})" for ext, n in exts.most_common(5)) or "none"


def render(start_dir: Path, positional: Sequence[str]) -> str:
    notes = []
    forced = None
    if positional:
        requested = positional[0].lower()
        if requested in STRATEGIES:
            forced = requested
        else:
            valid = ", ".join(sorted(STRATEGIES))
            notes.append(f"> Note: unknown strategy {code_span(positional[0])} ignored. Valid: {valid}.")

    top = git(start_dir, "rev-parse", "--show-toplevel")
    if top is None:
        return "\n".join([
            "# Code Review: no git repository",
            "",
            f"{code_span(str(start_dir))} is not inside a git repository, so there is no diff to review.",
            "Ask the user which files to review.",
        ] + ([""] + notes if notes else []))
    repo = Path(top.strip())
    branch = describe_branch(repo)

    scope = working_tree_scope(repo) or branch_scope(repo, branch)
    if scope is None:
        lines = [
            "# Code Review: nothing to review",
            "",
            f"No uncommitted changes, and `{table_cell(branch)}` has no commits beyond the default branch.",
            "No pending changes. Ready for new work.",
        ]
        if forced:
            lines += ["", f"Strategy {code_span(forced)} was requested, but there is no diff to apply it to."]
        return "\n".join(lines + ([""] + notes if notes else []))

    if forced:
        strategy, reason = forced, f"Strategy {code_span(forced)} was explicitly requested."
    else:
        strategy, reason = detect_strategy(scope.files)

    listed = scope.files[:MAX_LISTED_FILES]
    file_lines = [f"- {code_span(f)}" for f in listed]
    if len(scope.files) > len(listed):
        file_lines.append(f"- ... and {len(scope.files) - len(listed)} more")

    parts = [
        f"# Code Review: {strategy.replace('-', ' ').title()} Strategy",
        "",
        f"> **Why this strategy:** {reason}",
    ]
    if notes:
        parts += [""] + notes
    parts += [
        "",
        "## What to review",
        "",
        f"Scope: {scope.description}.",
        f"Full diff: {code_span(scope.review_command)}",
    ] + ([scope.review_note] if scope.review_note else []) + [
        "",
        "| Detail | Value |",
        "|--------|-------|",
        f"| Branch | {code_span(table_cell(branch))} |",
        f"| Files changed | {len(scope.files)} |",
        f"| Extensions | {table_cell(extension_summary(scope.files))} |",
        "",
        "Files:",
    ] + file_lines + [
        "",
        "<details>",
        "<summary>Diff stat</summary>",
        "",
        "````",
        truncate_stat(scope.diff_stat),
        "````",
        "",
        "</details>",
        "",
        STRATEGIES[strategy].strip(),
        "",
        OUTPUT_FORMAT,
        "",
        "Run the diff command above and review it now.",
    ]
    return "\n".join(parts)


FALLBACK = """# Code Review: fallback instructions

The review generator failed, so no strategy was computed.
Run `git status` and `git diff HEAD` yourself, then review for correctness,
security, and missing tests. Report issues as `[severity] file:line: description`."""


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.project_dir and not Path(args.project_dir).is_dir():
            # An explicit but wrong path must not silently review some other repo.
            output = (f"# Code Review: project directory not found\n\n"
                      f"{code_span(args.project_dir)} is not a directory, so there is no diff to review.\n"
                      "Ask the user which directory to review.")
        else:
            output = render(resolve_start_dir(args.project_dir), args.positional)
    except Exception as exc:  # never let a traceback reach the agent
        output = f"{FALLBACK}\n\n<!-- generator error: {type(exc).__name__}: {exc} -->"
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
