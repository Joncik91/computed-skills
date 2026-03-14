#!/usr/bin/env python3
"""
deploy-checklist: A hybrid computed skill example.

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
"""

import subprocess
import sys
import os

# ---------------------------------------------------------------------------
# 1. Gather context: what's about to be deployed?
# ---------------------------------------------------------------------------

def run(cmd):
    """Run a shell command and return stdout, or empty string on failure."""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        return result.stdout.strip()
    except Exception:
        return ""

def detect_base_branch():
    """Find the default branch (main or master)."""
    branches = run("git branch -r")
    if "origin/main" in branches:
        return "origin/main"
    if "origin/master" in branches:
        return "origin/master"
    # Fallback: compare against HEAD~1 if no remote
    return "HEAD~1"

def get_changed_files(base):
    """Return list of files changed relative to base branch."""
    diff_output = run(f"git diff --name-only {base}...HEAD")
    if not diff_output:
        # Maybe we're on the base branch itself; check staged + unstaged
        diff_output = run("git diff --name-only HEAD")
    if not diff_output:
        diff_output = run("git diff --name-only --cached")
    return [f for f in diff_output.splitlines() if f.strip()] if diff_output else []

# ---------------------------------------------------------------------------
# 2. Detect risk factors from the changed files
# ---------------------------------------------------------------------------

def analyze_risks(files):
    """Score risk factors. Returns (risk_level, risk_details) tuple."""
    risks = []
    score = 0

    # Database migrations
    migration_files = [f for f in files if "migration" in f.lower() or "migrate" in f.lower()
                       or f.endswith(".sql") or "/db/" in f or "/schema" in f]
    if migration_files:
        risks.append(("DB_MIGRATION", migration_files))
        score += 3

    # Auth / security changes
    auth_files = [f for f in files if any(k in f.lower() for k in
                  ["auth", "security", "permission", "rbac", "oauth", "token",
                   "password", "credential", "secret", ".env"])]
    if auth_files:
        risks.append(("AUTH_SECURITY", auth_files))
        score += 3

    # Environment / config changes
    config_files = [f for f in files if any(k in f.lower() for k in
                    [".env", "config", ".toml", ".yaml", ".yml", ".ini", "settings"])
                    and "test" not in f.lower()]
    if config_files:
        risks.append(("CONFIG_CHANGE", config_files))
        score += 2

    # CI/CD pipeline changes
    cicd_files = [f for f in files if any(k in f.lower() for k in
                  [".github/workflows", "jenkinsfile", ".gitlab-ci", "dockerfile",
                   "docker-compose", ".circleci", "buildspec", "cloudbuild"])]
    if cicd_files:
        risks.append(("CICD_CHANGE", cicd_files))
        score += 2

    # Dependency changes
    dep_files = [f for f in files if os.path.basename(f).lower() in
                 ["package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                  "requirements.txt", "poetry.lock", "pyproject.toml", "go.mod",
                  "go.sum", "cargo.toml", "cargo.lock", "gemfile", "gemfile.lock",
                  "composer.json", "composer.lock"]]
    if dep_files:
        risks.append(("DEPENDENCY_CHANGE", dep_files))
        score += 1

    # Sheer volume of changes
    if len(files) > 30:
        risks.append(("LARGE_CHANGESET", [f"{len(files)} files changed"]))
        score += 2
    elif len(files) > 15:
        risks.append(("MEDIUM_CHANGESET", [f"{len(files)} files changed"]))
        score += 1

    # Classify overall risk level
    if score >= 4:
        level = "HIGH"
    elif score >= 2:
        level = "MEDIUM"
    else:
        level = "LOW"

    return level, risks

# ---------------------------------------------------------------------------
# 3. Emit behavioral instructions tailored to what we found
#    This is the hybrid part: static instructions wrapped in conditionals.
#    The agent only sees the branch that applies right now.
# ---------------------------------------------------------------------------

def emit(level, risks, files):
    """Print context-aware deployment instructions to stdout."""
    risk_names = {r[0] for r in risks}

    # --- HIGH RISK: full safety protocol ---
    if level == "HIGH":
        print("## DEPLOYMENT CHECKLIST — HIGH RISK")
        print()
        print("STOP. This deployment touches sensitive areas. Do NOT proceed on autopilot.")
        print("Read every item below before taking action.")
        print()

        if "DB_MIGRATION" in risk_names:
            migration_files = next(r[1] for r in risks if r[0] == "DB_MIGRATION")
            print("### Database Migration Detected")
            for f in migration_files:
                print(f"  - `{f}`")
            print()
            print("VERIFY before deploying:")
            print("- Is the migration reversible? If not, flag this to the team.")
            print("- Has the migration been tested against a copy of production data?")
            print("- Will this lock large tables? Check row counts on affected tables.")
            print("- Is there a rollback migration file? If not, write one now.")
            print()

        if "AUTH_SECURITY" in risk_names:
            auth_files = next(r[1] for r in risks if r[0] == "AUTH_SECURITY")
            print("### Authentication / Security Changes Detected")
            for f in auth_files:
                print(f"  - `{f}`")
            print()
            print("VERIFY before deploying:")
            print("- Review every changed line in these files. No skimming.")
            print("- Confirm no secrets, keys, or tokens are hardcoded.")
            print("- If permissions changed, verify least-privilege is maintained.")
            print("- Check that existing sessions won't break after deploy.")
            print()

        if "LARGE_CHANGESET" in risk_names or "MEDIUM_CHANGESET" in risk_names:
            count = len(files)
            print(f"### Large Changeset ({count} files)")
            print()
            print(f"Deploying {count} files at once increases blast radius.")
            print("- Can this be split into smaller deployments?")
            print("- If not, ensure you have a one-command rollback ready.")
            print()

        print("### Final Gate")
        print("- [ ] All tests pass on this exact commit")
        print("- [ ] Another person has reviewed the changes (not just the author)")
        print("- [ ] Rollback plan documented and tested")
        print("- [ ] Monitoring dashboards open during deploy")

    # --- MEDIUM RISK: expanded checklist with specific callouts ---
    elif level == "MEDIUM":
        print("## DEPLOYMENT CHECKLIST — MEDIUM RISK")
        print()
        print("This deployment has some areas worth a closer look.")
        print()

        if "CONFIG_CHANGE" in risk_names:
            config_files = next(r[1] for r in risks if r[0] == "CONFIG_CHANGE")
            print("### Configuration Changes")
            for f in config_files:
                print(f"  - `{f}`")
            print()
            print("- Confirm all environment-specific values are set (staging, production).")
            print("- Check that no dev/local defaults leaked into committed config.")
            print()

        if "DEPENDENCY_CHANGE" in risk_names:
            dep_files = next(r[1] for r in risks if r[0] == "DEPENDENCY_CHANGE")
            print("### Dependency Changes")
            for f in dep_files:
                print(f"  - `{f}`")
            print()
            print("- Review what was added or upgraded. Check changelogs for breaking changes.")
            print("- Run the full test suite — transitive dependency issues hide here.")
            print()

        if "CICD_CHANGE" in risk_names:
            cicd_files = next(r[1] for r in risks if r[0] == "CICD_CHANGE")
            print("### CI/CD Pipeline Changes")
            for f in cicd_files:
                print(f"  - `{f}`")
            print()
            print("- Test the pipeline in a non-production environment first.")
            print("- Verify deployment targets haven't changed unintentionally.")
            print()

        print("### Standard Checks")
        print("- [ ] Tests pass")
        print("- [ ] Changes reviewed")
        print("- [ ] Deploy to staging first if possible")

    # --- LOW RISK: brief, stay out of the way ---
    else:
        print("## DEPLOYMENT CHECKLIST — LOW RISK")
        print()
        count = len(files) if files else 0
        if count == 0:
            print("No file changes detected against the base branch.")
            print("If you expected changes, make sure you've committed your work.")
        else:
            print(f"{count} file(s) changed. No high-risk patterns detected.")
            print()
            print("Quick checks:")
            print("- [ ] Tests pass")
            print("- [ ] Changes have been reviewed")
            print("- [ ] Deploy and monitor for errors")

    print()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    base = detect_base_branch()
    files = get_changed_files(base)
    level, risks = analyze_risks(files)
    emit(level, risks, files)
