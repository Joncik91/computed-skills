#!/usr/bin/env python3
"""
deploy-checklist: a hybrid computed skill example.

HYBRID PATTERN EXPLAINED:
A static SKILL.md gives the agent the same 20-item checklist every deploy.
After a few runs, the agent treats it as background noise and skips items.
The hybrid pattern fixes this: a Python script inspects the actual state
(git diff, changed files, risk signals) and emits ONLY the instructions
that matter right now. Every line the agent reads is relevant, so nothing
gets ignored.

The script does the mechanical work (parsing diffs, counting files,
detecting patterns). The output is plain English behavioral instructions
that change based on what it found.

Two rules keep it honest:
  - Every detected risk gets its own section. The overall level only sets
    the tone and the final gate; it never hides a finding.
  - Database migrations and auth/secret changes are HIGH on their own.

Usage:
    generate.py [--project-dir DIR] [BASE_REF]

    BASE_REF overrides the branch to compare against (default: origin/HEAD,
    then origin/main, origin/master, main, master).

Contract with the skill runner: always print markdown, always exit 0.
A non-zero exit aborts the entire skill invocation in Claude Code.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, NamedTuple, Sequence

MAX_LISTED_FILES = 15

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
# 1. Gather context: what's about to be deployed?
# ---------------------------------------------------------------------------

def git(cwd: Path, *args: str, timeout: float = 15) -> str | None:
    """Run git with an argument list (no shell). Returns stdout, or None on
    any failure so callers can tell "failed" apart from "empty"."""
    env = dict(os.environ)
    # Read-only commands may refresh the index and take index.lock, which
    # races with a concurrent `git commit` from the agent.
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
    """Split NUL-delimited git output (-z keeps odd filenames intact)."""
    return [p for p in (raw or "").split("\0") if p]


def verify_ref(repo: Path, ref: str) -> bool:
    # A leading "-" would be parsed by git as an option.
    if not ref or ref.startswith("-"):
        return False
    return git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}") is not None


def detect_base_ref(repo: Path) -> str | None:
    """Find the default branch: origin/HEAD, then the usual names."""
    head = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    candidates = [head.strip()] if head and head.strip() else []
    candidates += ["origin/main", "origin/master", "main", "master"]
    for ref in candidates:
        if verify_ref(repo, ref):
            return ref
    return None


def committed_changes(repo: Path, base: str) -> list[str] | None:
    """Files changed by commits on HEAD that are not in base."""
    raw = git(repo, "diff", "--name-only", "-z", f"{base}...HEAD")
    if raw is None:
        # No merge base (e.g. shallow clone): fall back to a direct comparison.
        raw = git(repo, "diff", "--name-only", "-z", base, "HEAD")
    return None if raw is None else split_z(raw)


def uncommitted_changes(repo: Path) -> list[str]:
    """Staged, unstaged and untracked paths. These are NOT part of a deploy."""
    entries = split_z(git(repo, "status", "--porcelain", "-z", "--untracked-files=all"))
    files = []
    skip_next = False
    for entry in entries:
        if skip_next:  # the original path of a rename/copy entry
            skip_next = False
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            skip_next = True
        files.append(path)
    return files


# ---------------------------------------------------------------------------
# 2. Detect risk factors from the changed files
# ---------------------------------------------------------------------------
# Paths are split into word tokens and matched as whole words: substring
# matching flags "tokenizer.py" as auth and "latest.py" as a test.

_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)}


def segments(path: str) -> list[str]:
    return [s.lower() for s in path.replace("\\", "/").split("/") if s]


def raw_basename(path: str) -> str:
    """Basename with original case: word splitting needs camelCase intact."""
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def is_test_path(path: str) -> bool:
    segs = segments(path)
    if not segs:
        return False
    if any(s in {"test", "tests", "__tests__", "spec", "specs", "testing", "e2e"} for s in segs[:-1]):
        return True
    return segs[-1] == "conftest.py" or bool(words(raw_basename(path)) & {"test", "tests", "spec", "specs"})


# Only unambiguous migration signals: this rule forces HIGH, and a STOP that
# fires on every `src/db/pool.py` edit teaches the agent to ignore it.
MIGRATION_DIRS = {"migrations", "migration", "migrate", "alembic", "flyway", "liquibase"}
MIGRATION_WORDS = {"migration", "migrations", "migrate"}
SCHEMA_BASENAMES = {"schema.prisma", "schema.rb", "structure.sql"}

AUTH_WORDS = {
    "auth", "authn", "authz", "authenticate", "authentication", "authenticator",
    "authorize", "authorization", "authorizer", "login", "logout", "signin",
    "password", "passwords", "passwd", "secret", "secrets", "token", "tokens",
    "credential", "credentials", "creds", "oauth", "jwt", "jwks", "permission",
    "permissions", "rbac", "acl", "acls", "security", "saml", "sso", "csrf",
    "session", "sessions", "crypto", "encrypt", "decrypt", "encryption",
}
SECRET_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".jks"}

CICD_DIR_MARKERS = (".github/workflows/", ".circleci/", ".buildkite/", ".gitlab/")
CICD_BASENAMES = {
    "jenkinsfile", ".gitlab-ci.yml", "buildspec.yml", "buildspec.yaml",
    "cloudbuild.yaml", "cloudbuild.yml", "azure-pipelines.yml", "bitbucket-pipelines.yml",
    "compose.yml", "compose.yaml",
}

DEPENDENCY_BASENAMES = {
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "bun.lock", "bun.lockb", "requirements.txt", "pipfile",
    "pipfile.lock", "poetry.lock", "uv.lock", "pyproject.toml", "go.mod",
    "go.sum", "cargo.toml", "cargo.lock", "gemfile", "gemfile.lock",
    "composer.json", "composer.lock",
}

CONFIG_EXTENSIONS = {".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".properties"}
CONFIG_WORDS = {"config", "configs", "configuration", "settings"}


def _base(path: str) -> str:
    segs = segments(path)
    return segs[-1] if segs else ""


def _is_env_file(base: str) -> bool:
    return base == ".env" or base.startswith(".env.")


def is_migration(path: str) -> bool:
    base = _base(path)
    return (
        base.endswith(".sql")
        or base in SCHEMA_BASENAMES
        or any(s in MIGRATION_DIRS for s in segments(path)[:-1])
        or bool(words(raw_basename(path)) & MIGRATION_WORDS)
    )


def is_auth(path: str) -> bool:
    base = _base(path)
    return (
        _is_env_file(base)
        or os.path.splitext(base)[1] in SECRET_EXTENSIONS
        or bool(words(path) & AUTH_WORDS)
    )


def is_cicd(path: str) -> bool:
    lower = "/" + "/".join(segments(path)) + ("/" if path.endswith("/") else "")
    base = _base(path)
    return (
        any(marker in lower for marker in CICD_DIR_MARKERS)
        or base in CICD_BASENAMES
        or base.startswith(("dockerfile", "docker-compose"))
        or base.endswith(".dockerfile")
    )


def is_dependency(path: str) -> bool:
    return _base(path) in DEPENDENCY_BASENAMES


def is_config(path: str) -> bool:
    base = _base(path)
    return os.path.splitext(base)[1] in CONFIG_EXTENSIONS or bool(words(path) & CONFIG_WORDS)


class Rule(NamedTuple):
    key: str
    title: str
    weight: int
    forces_high: bool
    matches: Callable[[str], bool]
    checks: tuple[str, ...]


# Order is precedence: each file counts toward the FIRST rule it matches,
# so a CI workflow (.yml) isn't double-counted as config, and a .env file
# is treated as a secrets change rather than plain config.
RULES: tuple[Rule, ...] = (
    Rule("DB_MIGRATION", "Database Migration / Schema Change", 3, True, is_migration, (
        "Is the migration reversible? If not, flag this to the team.",
        "Has it been tested against a copy of production data?",
        "Will it lock large tables? Check row counts on affected tables.",
        "Is there a rollback migration? If not, write one now.",
    )),
    Rule("AUTH_SECURITY", "Authentication / Security / Secrets", 3, True, is_auth, (
        "Review every changed line in these files. No skimming.",
        "Confirm no secrets, keys, or tokens are hardcoded or committed.",
        "If permissions changed, verify least privilege is maintained.",
        "Check that existing sessions and tokens keep working after deploy.",
    )),
    Rule("CICD_CHANGE", "CI/CD Pipeline Changes", 2, False, is_cicd, (
        "Test the pipeline in a non-production environment first.",
        "Verify deployment targets and credentials haven't changed unintentionally.",
    )),
    Rule("DEPENDENCY_CHANGE", "Dependency Changes", 1, False, is_dependency, (
        "Review what was added or upgraded. Check changelogs for breaking changes.",
        "Run the full test suite: transitive dependency issues hide here.",
    )),
    Rule("CONFIG_CHANGE", "Configuration Changes", 2, False, is_config, (
        "Confirm environment-specific values are set for staging and production.",
        "Check that no dev/local defaults leaked into committed config.",
    )),
)


class Risk(NamedTuple):
    rule: Rule
    files: list[str]


class Assessment(NamedTuple):
    level: str            # HIGH | MEDIUM | LOW
    score: int
    risks: list[Risk]
    file_count: int


def assess(files: Sequence[str]) -> Assessment:
    """Assign each non-test file to its first matching rule, then score."""
    buckets: dict[str, list[str]] = {rule.key: [] for rule in RULES}
    for path in files:
        if is_test_path(path):
            continue  # test-only changes don't raise deploy risk
        for rule in RULES:
            if rule.matches(path):
                buckets[rule.key].append(path)
                break

    risks = [Risk(rule, buckets[rule.key]) for rule in RULES if buckets[rule.key]]
    score = sum(r.rule.weight for r in risks)
    if len(files) > 30:
        score += 2
    elif len(files) > 15:
        score += 1

    if any(r.rule.forces_high for r in risks) or score >= 4:
        level = "HIGH"
    elif score >= 2:
        level = "MEDIUM"
    else:
        level = "LOW"
    return Assessment(level, score, risks, len(files))


# ---------------------------------------------------------------------------
# 3. Emit behavioral instructions tailored to what we found
#    This is the hybrid part: plain-English instructions wrapped in
#    conditionals. The agent only sees what applies right now.
# ---------------------------------------------------------------------------

def code_span(text: str) -> str:
    return "`" + text.replace("`", "'") + "`"


def file_list(files: Sequence[str]) -> list[str]:
    lines = [f"  - {code_span(f)}" for f in files[:MAX_LISTED_FILES]]
    if len(files) > MAX_LISTED_FILES:
        lines.append(f"  - ... and {len(files) - MAX_LISTED_FILES} more")
    return lines


INTRO = {
    "HIGH": ["STOP. This deployment touches sensitive areas. Do NOT proceed on autopilot.",
             "Work through every item below before taking action."],
    "MEDIUM": ["This deployment has some areas worth a closer look."],
    "LOW": [],
}

GATE = {
    "HIGH": ("### Final Gate", (
        "All tests pass on this exact commit",
        "Another person has reviewed the changes (not just the author)",
        "Rollback plan documented and tested",
        "Monitoring dashboards open during deploy",
    )),
    "MEDIUM": ("### Standard Checks", (
        "Tests pass",
        "Changes reviewed",
        "Deploy to staging first if possible",
    )),
    "LOW": ("Quick checks:", (
        "Tests pass",
        "Changes have been reviewed",
        "Deploy and monitor for errors",
    )),
}


def render_checklist(a: Assessment, scope_line: str, notes: Sequence[str]) -> str:
    out = [f"## DEPLOYMENT CHECKLIST: {a.level} RISK", "", scope_line, ""]
    for note in notes:
        out += [note, ""]
    if INTRO[a.level]:
        out += INTRO[a.level] + [""]

    if a.file_count == 0:
        out += ["No file changes detected against the base branch.",
                "If you expected changes, make sure you've committed your work."]
        return "\n".join(out)

    if not a.risks:
        out += [f"{a.file_count} file(s) changed. No high-risk patterns detected.", ""]

    for risk in a.risks:
        out += [f"### {risk.rule.title}"] + file_list(risk.files) + ["", "VERIFY before deploying:"]
        out += [f"- {check}" for check in risk.rule.checks] + [""]

    if a.file_count > 15:
        out += [f"### Large Changeset ({a.file_count} files)", "",
                f"Deploying {a.file_count} files at once increases blast radius.",
                "- Can this be split into smaller deployments?",
                "- If not, have a one-command rollback ready.", ""]

    heading, items = GATE[a.level]
    out += [heading] + [f"- [ ] {item}" for item in items]
    return "\n".join(out)


def render(start_dir: Path, positional: Sequence[str]) -> str:
    top = git(start_dir, "rev-parse", "--show-toplevel")
    if top is None:
        return ("## DEPLOYMENT CHECKLIST: UNKNOWN RISK\n\n"
                f"{code_span(str(start_dir))} is not inside a git repository, so the deploy contents "
                "can't be assessed. Ask the user what is being deployed and review it manually.")
    repo = Path(top.strip())
    notes = []

    base = None
    if positional:
        if verify_ref(repo, positional[0]):
            base = positional[0]
        else:
            notes.append(f"> Note: {code_span(positional[0])} is not a valid git ref; using the default branch.")
    base = base or detect_base_ref(repo)

    pending = uncommitted_changes(repo)
    committed = committed_changes(repo, base) if base else None

    if committed and base:
        files = committed
        scope = f"Comparing `HEAD` against {code_span(base)}: {len(files)} file(s) in committed changes."
        if pending:
            notes.append(f"> Warning: {len(pending)} uncommitted file(s) are NOT part of this deploy. "
                         "Commit or stash them first if they belong in it.")
    elif pending:
        files = pending
        where = f"against {code_span(base)}" if base else "(no base branch found)"
        scope = (f"No committed changes {where}. Assessing {len(files)} uncommitted file(s) instead; "
                 "commit them before deploying.")
    else:
        files = []
        scope = (f"Comparing `HEAD` against {code_span(base)}." if base
                 else "No base branch found (tried origin/HEAD, origin/main, origin/master, main, master). "
                      "Pass one explicitly, e.g. `/deploy-checklist origin/release`.")

    return render_checklist(assess(files), scope, notes)


FALLBACK = """## DEPLOYMENT CHECKLIST: UNKNOWN RISK

The checklist generator failed, so risk could not be assessed. Treat this as HIGH risk:
- [ ] Run `git diff <base>...HEAD --stat` and review every changed file
- [ ] Look for database migrations, auth/secret changes, and CI/CD edits
- [ ] All tests pass on this exact commit
- [ ] Rollback plan ready"""


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        output = render(resolve_start_dir(args.project_dir), args.positional)
    except Exception as exc:  # never let a traceback reach the agent
        output = f"{FALLBACK}\n\n<!-- generator error: {type(exc).__name__}: {exc} -->"
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
