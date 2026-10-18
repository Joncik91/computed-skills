---
name: dependency-audit
description: Checks project dependencies (npm/yarn/pnpm/bun, pip, cargo, go) for unpinned versions and lock files out of sync with their manifest. Use /dependency-audit status for the full report; "heartbeat" mode stays silent unless something needs attention.
argument-hint: "[status|heartbeat]"
---

!`python3 "${CLAUDE_SKILL_DIR}/scripts/generate.py" --project-dir "${CLAUDE_PROJECT_DIR}" $ARGUMENTS`
