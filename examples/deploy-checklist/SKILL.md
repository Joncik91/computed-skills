---
name: deploy-checklist
description: Pre-deployment safety checklist that escalates based on what's about to be deployed (migrations, auth/secrets, CI/CD, dependencies, config). Optionally pass the base ref to compare against.
argument-hint: "[base-ref]"
---

!`python3 "${CLAUDE_SKILL_DIR}/scripts/generate.py" --project-dir "${CLAUDE_PROJECT_DIR}" $ARGUMENTS`
