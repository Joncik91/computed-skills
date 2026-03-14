#!/usr/bin/env python3
"""
Dependency Audit — a multi-mode computed skill example.

Computed skills generate markdown instructions dynamically via a Python script,
instead of serving static markdown. This lets the skill do deterministic work
(file parsing, counting, diffing) in Python and only ask the LLM to handle
judgment calls.

Multi-mode convention:
  (no args)    — always-on context: brief output, only if something needs attention
  "status"     — manual dashboard: verbose, all findings, invoked by /dependency-audit
  "heartbeat"  — periodic check: outputs NOTHING if healthy. Silence means OK.

Argument handling uses the ARGUMENTS env var (set by the skill runner),
with sys.argv as fallback for direct invocation / testing.
"""

import os
import sys
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------
# The skill runner sets $ARGUMENTS. When running the script directly (e.g.,
# during development), fall back to sys.argv.
raw_args = os.environ.get("ARGUMENTS", "").strip()
if not raw_args:
    raw_args = " ".join(sys.argv[1:])
mode = raw_args.split()[0] if raw_args else ""

# The skill runs from the project root. Resolve it once.
PROJECT_ROOT = Path.cwd()

# ---------------------------------------------------------------------------
# Manifest detection
# ---------------------------------------------------------------------------
# Each tuple: (manifest filename, lock filename, ecosystem label)
ECOSYSTEMS = [
    ("package.json",      "package-lock.json", "npm"),
    ("package.json",      "yarn.lock",         "yarn"),
    ("requirements.txt",  None,                "pip"),
    ("Cargo.toml",        "Cargo.lock",        "cargo"),
    ("go.mod",            "go.sum",            "go"),
]


def detect_manifest():
    """Return (manifest_path, lock_path_or_None, ecosystem) for the first match."""
    for manifest, lock, eco in ECOSYSTEMS:
        mp = PROJECT_ROOT / manifest
        if mp.exists():
            lp = (PROJECT_ROOT / lock) if lock and (PROJECT_ROOT / lock).exists() else None
            return mp, lp, eco
    return None, None, None


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def parse_npm(manifest_path):
    """Parse package.json, return (prod_deps, dev_deps) as lists of (name, version)."""
    data = json.loads(manifest_path.read_text())
    prod = list(data.get("dependencies", {}).items())
    dev = list(data.get("devDependencies", {}).items())
    return prod, dev


def parse_requirements(manifest_path):
    """Parse requirements.txt, return (deps, []) — pip has no dev split in the file."""
    deps = []
    for line in manifest_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Split on any version operator to get (name, specifier)
        for op in ["==", ">=", "<=", "~=", "!="]:
            if op in line:
                name, ver = line.split(op, 1)
                deps.append((name.strip(), f"{op}{ver.strip()}"))
                break
        else:
            deps.append((line, "unpinned"))
    return deps, []


def parse_generic(manifest_path):
    """Fallback: count non-empty, non-comment lines as rough dep count."""
    lines = [l for l in manifest_path.read_text().splitlines()
             if l.strip() and not l.strip().startswith("#") and not l.strip().startswith("//")]
    return [(f"line-{i}", "unknown") for i, _ in enumerate(lines)], []


def analyze(manifest_path, lock_path, ecosystem):
    """Run all checks. Returns a dict of findings."""
    # Parse dependencies
    if ecosystem in ("npm", "yarn"):
        prod, dev = parse_npm(manifest_path)
    elif ecosystem == "pip":
        prod, dev = parse_requirements(manifest_path)
    else:
        prod, dev = parse_generic(manifest_path)

    all_deps = prod + dev
    total = len(all_deps)
    findings = {
        "ecosystem": ecosystem,
        "manifest": manifest_path.name,
        "total": total,
        "prod": len(prod),
        "dev": len(dev),
        "issues": [],
    }

    # Check: wildcard or missing versions
    wildcards = [(n, v) for n, v in all_deps if v in ("*", "latest", "unpinned", "")]
    if wildcards:
        findings["issues"].append({
            "severity": "high",
            "msg": f"{len(wildcards)} unpinned/wildcard deps: {', '.join(n for n,_ in wildcards[:5])}",
        })

    # Check: dev/prod ratio
    if len(prod) > 0 and len(dev) > len(prod) * 3:
        findings["issues"].append({
            "severity": "medium",
            "msg": f"Dev dependencies ({len(dev)}) outnumber production ({len(prod)}) by >3x.",
        })

    # Check: lock file freshness
    if lock_path:
        manifest_mtime = manifest_path.stat().st_mtime
        lock_mtime = lock_path.stat().st_mtime
        if manifest_mtime > lock_mtime:
            findings["issues"].append({
                "severity": "high",
                "msg": f"Lock file ({lock_path.name}) is older than {manifest_path.name} — run install to sync.",
            })
    elif ecosystem in ("npm", "yarn", "cargo"):
        findings["issues"].append({
            "severity": "medium",
            "msg": f"No lock file found. Builds may not be reproducible.",
        })

    # Check: very large dependency count
    if total > 200:
        findings["issues"].append({
            "severity": "low",
            "msg": f"{total} total dependencies — consider auditing for unused packages.",
        })

    return findings


# ---------------------------------------------------------------------------
# Output generators — each mode produces markdown for the agent
# ---------------------------------------------------------------------------

def output_status(findings):
    """Verbose dashboard for /dependency-audit status."""
    f = findings
    lines = [
        f"## Dependency Audit — {f['ecosystem']} ({f['manifest']})",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total dependencies | {f['total']} |",
        f"| Production | {f['prod']} |",
        f"| Dev | {f['dev']} |",
        f"| Issues found | {len(f['issues'])} |",
        "",
    ]
    if f["issues"]:
        lines.append("### Issues")
        lines.append("")
        for issue in f["issues"]:
            icon = {"high": "[HIGH]", "medium": "[MEDIUM]", "low": "[LOW]"}[issue["severity"]]
            lines.append(f"- **{icon}** {issue['msg']}")
        lines.append("")
        lines.append("Review the issues above. For HIGH severity items, suggest a fix to the user.")
    else:
        lines.append("No issues detected. Dependencies look healthy.")
    print("\n".join(lines))


def output_default(findings):
    """Always-on mode: brief, only if something needs attention."""
    high = [i for i in findings["issues"] if i["severity"] == "high"]
    if not high:
        # Nothing urgent — stay quiet to keep context lean
        return
    print(f"**Dependency alert** ({findings['ecosystem']}): {len(high)} issue(s) need attention.")
    for issue in high:
        print(f"- {issue['msg']}")


def output_heartbeat(findings):
    """Heartbeat mode: output NOTHING if healthy. This is the key convention.

    In periodic/heartbeat mode, silence means everything is fine.
    The agent should only be interrupted when there is a real problem.
    Printing nothing here means the skill contributes zero tokens to context
    when the project is healthy — keeping heartbeat cycles fast and cheap.
    """
    critical = [i for i in findings["issues"] if i["severity"] == "high"]
    if not critical:
        # Silence. The agent won't even know this skill ran — by design.
        return
    # Only break silence for genuinely important problems.
    print(f"[dependency-audit] {len(critical)} issue(s) found:")
    for issue in critical:
        print(f"  - {issue['msg']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
manifest_path, lock_path, ecosystem = detect_manifest()

if not manifest_path:
    if mode == "status":
        print("No supported dependency manifest found (package.json, requirements.txt, Cargo.toml, go.mod).")
    # In default and heartbeat modes, say nothing — no manifest is not an error.
    sys.exit(0)

findings = analyze(manifest_path, lock_path, ecosystem)

if mode == "status":
    output_status(findings)
elif mode == "heartbeat":
    output_heartbeat(findings)
else:
    output_default(findings)
