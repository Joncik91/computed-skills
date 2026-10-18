#!/usr/bin/env python3
"""
Dependency Audit: a multi-mode computed skill example.

Computed skills generate markdown instructions dynamically via a script,
instead of serving static markdown. This lets the skill do deterministic
work (file parsing, counting, diffing) in Python and only ask the LLM to
handle judgment calls.

Multi-mode convention:
  (no args)    always-on context: one line, plus urgent issues if any
  "status"     manual dashboard: verbose, all findings (/dependency-audit status)
  "heartbeat"  periodic check: outputs NOTHING if healthy. Silence means OK.

Supported ecosystems (all that are present are audited):
  npm / yarn / pnpm / bun   package.json + lock file
  pip                       requirements.txt
  cargo                     Cargo.toml + Cargo.lock
  go                        go.mod + go.sum

Lock files are checked by CONTENT, not modification time: file mtimes are
reset by every clone and checkout, so mtime comparisons raise false alarms.

Usage:
    generate.py [--project-dir DIR] [status|heartbeat]

Contract with the skill runner: always exit 0. A non-zero exit aborts the
entire skill invocation in Claude Code.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple, Sequence

try:  # Python 3.11+
    import tomllib as _toml
except ImportError:  # pragma: no cover - exercised on 3.8-3.10
    _toml = None

MODES = ("status", "heartbeat")
MAX_NAMES = 5
MAX_FILE_BYTES = 50 * 1024 * 1024  # don't slurp pathological files
LARGE_DEP_COUNT = 200
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

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


def resolve_project_root(project_dir: str | None) -> Path:
    """The `!` command runs in the session's current directory, which moves
    whenever the agent runs `cd`. Prefer an explicit project dir."""
    for candidate in (project_dir, os.environ.get("CLAUDE_PROJECT_DIR")):
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return Path.cwd()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class Dep(NamedTuple):
    name: str          # name as declared in the manifest
    spec: str          # version specifier as written ("" if none)
    group: str         # "prod" | "dev" | "indirect"
    lock_name: str     # name to look for in the lock file


class Issue(NamedTuple):
    severity: str      # "high" | "medium" | "low"
    msg: str


class Report(NamedTuple):
    ecosystem: str
    manifest: str
    lockfile: str | None
    deps: list[Dep]
    issues: list[Issue]

    def count(self, group: str) -> int:
        return sum(1 for d in self.deps if d.group == group)


class ParseError(Exception):
    """A manifest or lock file exists but can't be understood."""


def read_text(path: Path) -> str:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ParseError(f"{path.name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB; skipped")
    return path.read_text(encoding="utf-8", errors="replace")


def load_json(path: Path) -> Any:
    try:
        return json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise ParseError(f"{path.name} is not valid JSON (line {exc.lineno}: {exc.msg})") from None


def names_preview(names: Sequence[str]) -> str:
    shown = ", ".join(names[:MAX_NAMES])
    return shown + (f" (+{len(names) - MAX_NAMES} more)" if len(names) > MAX_NAMES else "")


def lock_gap_issue(deps: Sequence[Dep], present: set[str], manifest: str, lock: str) -> Issue | None:
    missing = sorted({d.name for d in deps if d.lock_name not in present})
    if not missing:
        return None
    return Issue("high", f"{lock} is out of sync with {manifest}: {len(missing)} declared "
                         f"dependency(ies) missing from the lock ({names_preview(missing)}). "
                         "Run install to regenerate it.")


# ---------------------------------------------------------------------------
# npm family (npm, yarn, pnpm, bun)
# ---------------------------------------------------------------------------

NPM_LOCKS = (
    ("package-lock.json", "npm"),
    ("npm-shrinkwrap.json", "npm"),
    ("yarn.lock", "yarn"),
    ("pnpm-lock.yaml", "pnpm"),
    ("bun.lock", "bun"),
    ("bun.lockb", "bun"),
)
NPM_WILDCARDS = {"", "*", "x", "latest", "next"}


def parse_package_json(path: Path) -> list[Dep]:
    data = load_json(path)
    if not isinstance(data, dict):
        raise ParseError("package.json must contain a JSON object")
    deps = []
    for key, group in (("dependencies", "prod"), ("devDependencies", "dev")):
        section = data.get(key) or {}
        if not isinstance(section, dict):
            raise ParseError(f'"{key}" in package.json must be an object')
        for name, spec in section.items():
            deps.append(Dep(name, str(spec).strip(), group, name))
    return deps


def npm_lock_issues(deps: Sequence[Dep], lock: Path) -> list[Issue]:
    """package-lock.json v2/v3 records the manifest's exact specifiers under
    packages[""]. Any difference means `npm ci` will refuse to install."""
    data = load_json(lock)
    if not isinstance(data, dict):
        raise ParseError(f"{lock.name} must contain a JSON object")
    packages = data.get("packages")
    root = packages.get("") if isinstance(packages, dict) else None
    if isinstance(root, dict):
        recorded: dict[str, str] = {}
        for key in ("dependencies", "devDependencies", "optionalDependencies"):
            section = root.get(key)
            if isinstance(section, dict):
                recorded.update({k: str(v) for k, v in section.items()})
        missing = sorted(d.name for d in deps if d.name not in recorded)
        changed = sorted(d.name for d in deps if d.name in recorded and recorded[d.name] != d.spec)
        issues = []
        if missing:
            issues.append(Issue("high", f"{lock.name} is out of sync with package.json: "
                                        f"{len(missing)} dependency(ies) not in the lock "
                                        f"({names_preview(missing)}). Run `npm install`."))
        if changed:
            issues.append(Issue("high", f"{lock.name} is out of sync with package.json: "
                                        f"{len(changed)} version range(s) changed "
                                        f"({names_preview(changed)}). Run `npm install`."))
        return issues
    # lockfileVersion 1: only names are recorded.
    legacy = data.get("dependencies")
    present = set(legacy) if isinstance(legacy, dict) else set()
    issue = lock_gap_issue(deps, present, "package.json", lock.name)
    return [issue] if issue else []


def yarn_lock_names(text: str) -> set[str]:
    """Package names from yarn.lock (classic and berry). Entry headers are
    unindented lines like `"@scope/pkg@^1.0.0, @scope/pkg@^1.2.0":`."""
    names = set()
    for line in text.splitlines():
        if not line or line[0] in " #" or not line.rstrip().endswith(":"):
            continue
        for selector in line.rstrip()[:-1].split(","):
            selector = selector.strip().strip('"')
            at = selector.find("@", 1)  # skip the leading @ of a scope
            if at > 0:
                names.add(selector[:at])
    return names


def pnpm_lock_has(text: str, name: str) -> bool:
    """Heuristic without a YAML parser: pnpm lists importer deps as `name:`
    keys and packages as `/name@version` or `name@version` keys."""
    pattern = r"(^|[\s/'\"])" + re.escape(name) + r"(@|['\"]?:)"
    return re.search(pattern, text, re.MULTILINE) is not None


def audit_npm(root: Path) -> Report | None:
    manifest = root / "package.json"
    if not manifest.is_file():
        return None
    deps = parse_package_json(manifest)
    locks = [(name, tool) for name, tool in NPM_LOCKS if (root / name).is_file()]
    tool = locks[0][1] if locks else "npm"
    issues: list[Issue] = []

    wild = sorted(d.name for d in deps if d.spec.lower() in NPM_WILDCARDS)
    if wild:
        issues.append(Issue("high", f"{len(wild)} unpinned/wildcard dep(s): {names_preview(wild)}"))

    if len({t for _, t in locks}) > 1:
        issues.append(Issue("medium", "Multiple package managers' lock files present ("
                                      + ", ".join(n for n, _ in locks)
                                      + "). Keep one so installs are reproducible."))

    lockfile = locks[0][0] if locks else None
    if not lockfile:
        if deps:
            issues.append(Issue("medium", "No lock file found. Builds may not be reproducible."))
    else:
        lock = root / lockfile
        if lockfile in ("package-lock.json", "npm-shrinkwrap.json"):
            issues += npm_lock_issues(deps, lock)
        elif lockfile == "yarn.lock":
            gap = lock_gap_issue(deps, yarn_lock_names(read_text(lock)), "package.json", lockfile)
            issues += [gap] if gap else []
        elif lockfile == "pnpm-lock.yaml":
            text = read_text(lock)
            present = {d.name for d in deps if pnpm_lock_has(text, d.name)}
            gap = lock_gap_issue(deps, present, "package.json", lockfile)
            issues += [gap] if gap else []
        # bun lock formats aren't parsed here: presence only.

    return Report(tool, "package.json", lockfile, deps, issues)


# ---------------------------------------------------------------------------
# pip (requirements.txt)
# ---------------------------------------------------------------------------

# PEP 508 subset: name, optional [extras], optional specifiers, optional ;marker
_REQ = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*"
    r"(?:\[[^\]]*\])?\s*"
    r"(?P<spec>(?:===|==|~=|!=|>=|<=|>|<)[^;]*)?"
    r"(?:;.*)?$"
)


def parse_requirements(path: Path) -> tuple[list[Dep], list[str]]:
    """Returns (deps, unparsed_lines)."""
    deps, unparsed = [], []
    logical = read_text(path).replace("\\\n", " ")  # join line continuations
    for raw in logical.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()  # strip comments
        if not line or line.startswith("-"):
            continue  # options: -r, -e, -c, --index-url, --hash ...
        if "://" in line or line.startswith((".", "/")) or " @ " in line:
            name = line.split("@")[0].strip() if " @ " in line else line
            deps.append(Dep(name, "url/path", "prod", name))
            continue
        match = _REQ.match(line)
        if not match:
            unparsed.append(line)
            continue
        spec = (match.group("spec") or "").strip().replace(" ", "")
        deps.append(Dep(match.group("name"), spec, "prod", match.group("name")))
    return deps, unparsed


def audit_pip(root: Path) -> Report | None:
    manifest = root / "requirements.txt"
    if not manifest.is_file():
        return None
    deps, unparsed = parse_requirements(manifest)
    issues = []
    unpinned = sorted(d.name for d in deps if not d.spec)
    if unpinned:
        issues.append(Issue("high", f"{len(unpinned)} unpinned dep(s): {names_preview(unpinned)}"))
    ranged = sorted(d.name for d in deps if d.spec and d.spec != "url/path"
                    and not d.spec.startswith(("==", "===")))
    if ranged:
        issues.append(Issue("low", f"{len(ranged)} dep(s) use version ranges, not exact pins "
                                   f"({names_preview(ranged)}). requirements.txt has no lock file."))
    if unparsed:
        issues.append(Issue("low", f"{len(unparsed)} line(s) in requirements.txt not understood: "
                                   f"{names_preview(unparsed)}"))
    return Report("pip", "requirements.txt", None, deps, issues)


# ---------------------------------------------------------------------------
# cargo (Cargo.toml + Cargo.lock)
# ---------------------------------------------------------------------------

_TOML_KV = re.compile(r'^\s*([A-Za-z0-9_.-]+|"[^"]+")\s*=\s*(.+?)\s*$')
_TOML_STR = re.compile(r'^"([^"]*)"')
_TOML_FIELD = re.compile(r'\b(version|package)\s*=\s*"([^"]*)"')


def _cargo_group(table: str) -> str | None:
    """Map a TOML table name to a dependency group, or None."""
    last = table.split(".")[-1]
    if last in ("dependencies", "build-dependencies"):
        return "prod"
    if last == "dev-dependencies":
        return "dev"
    return None


def _cargo_dep(name: str, value: Any, group: str) -> Dep:
    if isinstance(value, str):
        return Dep(name, value, group, name)
    value = value if isinstance(value, dict) else {}
    if value.get("workspace") is True:
        spec = "workspace"
    elif "git" in value or "path" in value:
        spec = "git/path"
    else:
        spec = str(value.get("version", ""))
    return Dep(name, spec, group, str(value.get("package", name)))


def _walk_cargo(table: dict[str, Any], prefix: str, deps: list[Dep]) -> None:
    for key, value in table.items():
        path = f"{prefix}.{key}" if prefix else key
        group = _cargo_group(path)
        if group and isinstance(value, dict):
            deps.extend(_cargo_dep(name, v, group) for name, v in value.items())
        elif isinstance(value, dict):
            _walk_cargo(value, path, deps)  # e.g. target.'cfg(unix)'.dependencies


def _parse_cargo_fallback(text: str) -> list[Dep]:
    """Line-based reader for Python < 3.11 (no tomllib). Handles the common
    forms: `name = "1.0"`, `name = { version = "1.0", ... }` and
    `[dependencies.name]` tables."""
    deps: list[Dep] = []
    section: str | None = None
    sub: str | None = None
    sub_fields: dict[str, Any] = {}

    def flush() -> None:
        if sub is not None and section:
            deps.append(_cargo_dep(sub, sub_fields, section))

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("["):
            flush()
            sub, sub_fields = None, {}
            name = line.strip("[] ").replace("'", "").replace('"', "")
            section = _cargo_group(name)
            if section is None and "." in name:
                parent, child = name.rsplit(".", 1)
                if _cargo_group(parent):
                    section, sub = _cargo_group(parent), child
            continue
        match = _TOML_KV.match(line)
        if not match or not section:
            continue
        key, value = match.group(1).strip('"'), match.group(2)
        if sub is not None:
            s = _TOML_STR.match(value)
            sub_fields[key] = s.group(1) if s else value == "true"
            continue
        s = _TOML_STR.match(value)
        if s:
            deps.append(Dep(key, s.group(1), section, key))
        else:
            fields: dict[str, Any] = dict(_TOML_FIELD.findall(value))
            if re.search(r"\b(git|path)\s*=", value):
                fields["git"] = True
            if re.search(r"\bworkspace\s*=\s*true", value):
                fields["workspace"] = True
            deps.append(_cargo_dep(key, fields, section))
    flush()
    return deps


def parse_cargo_toml(path: Path) -> list[Dep]:
    text = read_text(path)
    if _toml is None:
        return _parse_cargo_fallback(text)
    try:
        data = _toml.loads(text)
    except _toml.TOMLDecodeError as exc:
        raise ParseError(f"Cargo.toml is not valid TOML ({exc})") from None
    deps: list[Dep] = []
    _walk_cargo(data, "", deps)
    return deps


def audit_cargo(root: Path) -> Report | None:
    manifest = root / "Cargo.toml"
    if not manifest.is_file():
        return None
    deps = parse_cargo_toml(manifest)
    issues = []
    wild = sorted(d.name for d in deps if d.spec in ("*", ""))
    if wild:
        issues.append(Issue("high", f"{len(wild)} unpinned/wildcard dep(s): {names_preview(wild)}"))
    lock = root / "Cargo.lock"
    if lock.is_file():
        present = set(re.findall(r'^name\s*=\s*"([^"]+)"', read_text(lock), re.MULTILINE))
        external = [d for d in deps if d.spec not in ("git/path", "workspace")]
        gap = lock_gap_issue(external, present, "Cargo.toml", "Cargo.lock")
        issues += [gap] if gap else []
    elif deps:
        issues.append(Issue("medium", "No Cargo.lock found. Builds may not be reproducible "
                                      "(fine for a library crate, not for a binary)."))
    return Report("cargo", "Cargo.toml", "Cargo.lock" if lock.is_file() else None, deps, issues)


# ---------------------------------------------------------------------------
# go (go.mod + go.sum)
# ---------------------------------------------------------------------------

def parse_go_mod(path: Path) -> list[Dep]:
    deps = []
    in_block = False
    for raw in read_text(path).splitlines():
        line = raw.strip()
        indirect = "// indirect" in line
        line = line.split("//", 1)[0].strip()
        if in_block:
            if line == ")":
                in_block = False
                continue
            parts = line.split()
        elif line.startswith("require"):
            rest = line[len("require"):].strip()
            if rest == "(":
                in_block = True
                continue
            parts = rest.split()
        else:
            continue
        if len(parts) >= 2:
            group = "indirect" if indirect else "prod"
            deps.append(Dep(parts[0], parts[1], group, parts[0]))
    return deps


def audit_go(root: Path) -> Report | None:
    manifest = root / "go.mod"
    if not manifest.is_file():
        return None
    deps = parse_go_mod(manifest)
    issues = []
    lock = root / "go.sum"
    if lock.is_file():
        present = {line.split()[0] for line in read_text(lock).splitlines() if line.strip()}
        gap = lock_gap_issue(deps, present, "go.mod", "go.sum")
        issues += [gap] if gap else []
    elif deps:
        issues.append(Issue("high", "go.mod has requirements but go.sum is missing. Run `go mod tidy`."))
    return Report("go", "go.mod", "go.sum" if lock.is_file() else None, deps, issues)


# ---------------------------------------------------------------------------
# Cross-ecosystem checks
# ---------------------------------------------------------------------------

AUDITORS = (
    ("package.json", audit_npm),
    ("requirements.txt", audit_pip),
    ("Cargo.toml", audit_cargo),
    ("go.mod", audit_go),
)


def general_issues(report: Report) -> list[Issue]:
    prod, dev = report.count("prod"), report.count("dev")
    issues = []
    if prod > 0 and dev > prod * 3:
        issues.append(Issue("medium", f"Dev dependencies ({dev}) outnumber production ({prod}) by more than 3x."))
    if len(report.deps) > LARGE_DEP_COUNT:
        issues.append(Issue("low", f"{len(report.deps)} total dependencies: consider auditing for unused packages."))
    return issues


def audit(root: Path) -> list[Report]:
    """Audit every supported manifest in root. One broken manifest becomes a
    HIGH issue in its own report; it never hides the other ecosystems."""
    reports = []
    for manifest, auditor in AUDITORS:
        try:
            report = auditor(root)
        except (ParseError, OSError, UnicodeError, ValueError) as exc:
            reports.append(Report(manifest, manifest, None, [],
                                  [Issue("high", f"Could not audit {manifest}: {exc}")]))
            continue
        if report is not None:
            issues = sorted(report.issues + general_issues(report),
                            key=lambda i: SEVERITY_ORDER[i.severity])
            reports.append(report._replace(issues=issues))
    return reports


# ---------------------------------------------------------------------------
# Output generators: each mode produces markdown for the agent
# ---------------------------------------------------------------------------

def high_issues(reports: Sequence[Report]) -> list[tuple[Report, Issue]]:
    return [(r, i) for r in reports for i in r.issues if i.severity == "high"]


def output_status(reports: Sequence[Report], root: Path) -> str:
    """Verbose dashboard for /dependency-audit status."""
    if not reports:
        return (f"No supported dependency manifest found in `{root}` "
                "(package.json, requirements.txt, Cargo.toml, go.mod).")
    lines = ["# Dependency Audit", ""]
    for r in reports:
        lines += [
            f"## {r.ecosystem} ({r.manifest})",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total dependencies | {len(r.deps)} |",
            f"| Production | {r.count('prod')} |",
            f"| Dev | {r.count('dev')} |",
        ]
        if r.count("indirect"):
            lines.append(f"| Indirect | {r.count('indirect')} |")
        lines += [f"| Lock file | {r.lockfile or 'none'} |", f"| Issues | {len(r.issues)} |", ""]
        for issue in r.issues:
            lines.append(f"- **[{issue.severity.upper()}]** {issue.msg}")
        if r.issues:
            lines.append("")
    if any(r.issues for r in reports):
        lines.append("Review the issues above. For HIGH severity items, suggest a fix to the user.")
    else:
        lines.append("No issues detected. Dependencies look healthy.")
    return "\n".join(lines)


def output_default(reports: Sequence[Report], root: Path) -> str:
    """Always-on mode: one line of context, plus urgent issues if any.
    Never empty: a model-invoked skill that prints nothing leaves the
    agent guessing."""
    if not reports:
        return f"Dependencies: no supported manifest found in `{root}`."
    high = high_issues(reports)
    ecos = ", ".join(r.ecosystem for r in reports)
    total = sum(len(r.deps) for r in reports)
    if not high:
        return f"Dependencies: {total} across {ecos}. No urgent issues. Run `/dependency-audit status` for details."
    lines = [f"**Dependency alert** ({ecos}): {len(high)} issue(s) need attention."]
    lines += [f"- [{r.ecosystem}] {i.msg}" for r, i in high]
    lines.append("Run `/dependency-audit status` for the full report.")
    return "\n".join(lines)


def output_heartbeat(reports: Sequence[Report], root: Path) -> str:
    """Heartbeat mode: output NOTHING if healthy. This is the key convention.

    In periodic/heartbeat mode, silence means everything is fine. Printing
    nothing here means the skill contributes zero tokens to context when the
    project is healthy, keeping heartbeat cycles fast and cheap."""
    high = high_issues(reports)
    if not high:
        return ""
    lines = [f"[dependency-audit] {len(high)} issue(s) found:"]
    lines += [f"  - [{r.ecosystem}] {i.msg}" for r, i in high]
    return "\n".join(lines)


def render(root: Path, positional: Sequence[str]) -> str:
    mode = positional[0].lower() if positional else ""
    reports = audit(root)
    if mode == "status":
        return output_status(reports, root)
    if mode == "heartbeat":
        return output_heartbeat(reports, root)
    out = output_default(reports, root)
    if mode:
        out += f"\n\n> Note: unknown mode `{positional[0]}`. Valid modes: {', '.join(MODES)}."
    return out


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    heartbeat = bool(args.positional) and args.positional[0].lower() == "heartbeat"
    try:
        output = render(resolve_project_root(args.project_dir), args.positional)
    except Exception as exc:
        # Even in heartbeat mode, a crash must NOT be silent: silence means healthy.
        error = f"{type(exc).__name__}: {exc}"
        output = (f"[dependency-audit] generator error: {error}" if heartbeat else
                  "# Dependency Audit: generator failed\n\n"
                  "Inspect the project's dependency manifest and lock file manually.\n\n"
                  f"<!-- generator error: {error} -->")
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
