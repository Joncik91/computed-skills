---
name: check-pattern
description: Check if a Pattern-Key already exists in learnings/errors before logging a new self-improve entry. Use this BEFORE creating any new LRN or ERR entry to avoid duplicates and correctly increment recurrence counts. Silent helper — never announce usage to user.
user-invocable: false
---

!`python3 ${CLAUDE_SKILL_DIR}/../self-improve/scripts/generate.py check $ARGUMENTS`
