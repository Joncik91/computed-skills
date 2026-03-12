#!/usr/bin/env python3
"""
Self-Improve — Python brain for Reef's learning capture system.

Offloads all structured data parsing, recurrence detection, promotion logic,
drift analysis, and triage from the LLM to Python. The LLM focuses on judgment
calls, not bookkeeping.

Modes:
  (no args)     → always-on mode: pre-analyzed context for silent logging
  status        → /self-improve manual trigger: full status report
  check <key>   → recurrence check: does this Pattern-Key already exist?
  drift         → drift detection: compare SOUL.md vs recent daily logs
  triage        → identify stale/closeable entries
"""

import os
import re
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter, defaultdict

WORKSPACE = Path(os.environ.get("WORKSPACE", Path(__file__).parent.parent.parent.parent))
LEARNINGS_DIR = WORKSPACE / "memory/learnings"
MEMORY_DIR = WORKSPACE / "memory"
SCRIPT_DIR = Path(__file__).parent
STATE_FILE = SCRIPT_DIR / ".self-improve-state.json"


# ── Parsing ───────────────────────────────────────────────────────

def parse_entries(filepath):
    """Parse structured entries from a learnings/errors/features markdown file."""
    if not filepath.exists():
        return []

    content = filepath.read_text()
    entries = []
    # Split on --- separators, then parse each block
    blocks = re.split(r'\n---\n', content)

    for block in blocks:
        entry = {}
        block = block.strip()
        if not block:
            continue

        # Entry ID: [LRN-20260302-001] or [ERR-...] or [FEAT-...]
        id_match = re.search(r'\[(LRN|ERR|FEAT)-(\d{8})-(\d{3})\]\s*(.*)', block)
        if not id_match:
            # Try ## Entry: format
            entry_match = re.search(r'## Entry:\s*(.*)', block)
            if entry_match:
                entry['id'] = entry_match.group(1).strip()
                entry['type'] = 'LRN'  # Default for ## Entry format
            else:
                continue
        else:
            entry['type'] = id_match.group(1)
            entry['date_str'] = id_match.group(2)
            entry['seq'] = id_match.group(3)
            entry['id'] = f"{id_match.group(1)}-{id_match.group(2)}-{id_match.group(3)}"
            entry['title'] = id_match.group(4).strip()

        # Parse key-value fields
        for line in block.splitlines():
            line = line.strip()
            if line.startswith('- ') or line.startswith('**'):
                # Handle "- Key: Value", "**Key:** Value", "- **Key:** Value"
                kv = re.match(r'^[-*\s]*([A-Za-z][A-Za-z0-9_-]*)\**:\s*(.*)', line)
                if kv:
                    key = kv.group(1).lower().replace('-', '_').strip('*')
                    val = kv.group(2).strip().strip('*').strip()
                    entry[key] = val

        # Normalize fields
        if 'pattern_key' not in entry and 'pattern' in entry:
            entry['pattern_key'] = entry['pattern']
        if 'recurrence_count' in entry:
            try:
                entry['recurrence_count'] = int(entry['recurrence_count'])
            except (ValueError, TypeError):
                entry['recurrence_count'] = 1
        else:
            entry['recurrence_count'] = 1

        if 'status' not in entry:
            entry['status'] = 'pending'
        if 'priority' not in entry:
            entry['priority'] = 'medium'

        # Parse date
        date_str = entry.get('date_str') or entry.get('date', '')
        if date_str:
            try:
                if len(date_str) == 8:  # YYYYMMDD
                    entry['date'] = datetime.strptime(date_str, '%Y%m%d')
                else:
                    entry['date'] = datetime.strptime(date_str[:10], '%Y-%m-%d')
            except ValueError:
                entry['date'] = None
        else:
            entry['date'] = None

        if entry.get('id') or entry.get('pattern_key'):
            entries.append(entry)

    return entries


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"runs": 0, "last_mode": None, "last_run": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Analysis Functions ────────────────────────────────────────────

def build_pattern_index(all_entries):
    """Build a reverse index: pattern_key → [entries]."""
    index = defaultdict(list)
    for e in all_entries:
        pk = e.get('pattern_key', '').strip()
        if pk and pk not in ('', 'unknown', '(omit if clearly a one-off)', '(omit if one-off)'):
            index[pk].append(e)
    return dict(index)


def find_promotion_candidates(entries):
    """Entries with recurrence >= 2 and still pending."""
    return [e for e in entries
            if e.get('recurrence_count', 1) >= 2
            and e.get('status', '').lower() == 'pending']


def find_auto_promote(entries):
    """Entries with recurrence >= 3 — should promote immediately."""
    return [e for e in entries
            if e.get('recurrence_count', 1) >= 3
            and e.get('status', '').lower() == 'pending']


def find_high_priority_pending(entries):
    """High/critical entries still pending."""
    return [e for e in entries
            if e.get('priority', '').lower() in ('high', 'critical')
            and e.get('status', '').lower() == 'pending']


def find_stale_entries(entries, days=30):
    """Entries older than N days still pending with recurrence 1."""
    cutoff = datetime.now() - timedelta(days=days)
    return [e for e in entries
            if e.get('date') and e['date'] < cutoff
            and e.get('status', '').lower() == 'pending'
            and e.get('recurrence_count', 1) <= 1]


def find_recently_promoted(entries, days=7):
    """Entries promoted in last N days that need outcome tracking."""
    return [e for e in entries
            if e.get('status', '').lower() == 'promoted'
            and not e.get('outcome', '').strip()]


def find_duplicates(entries):
    """Pattern-Keys that appear in both LEARNINGS and ERRORS (potential merge)."""
    by_file = defaultdict(set)
    for e in entries:
        pk = e.get('pattern_key', '')
        etype = e.get('type', '')
        if pk:
            by_file[pk].add(etype)
    return {pk: types for pk, types in by_file.items() if len(types) > 1}


def count_by_status(entries):
    counts = Counter(e.get('status', 'unknown').lower() for e in entries)
    return dict(counts)


def count_by_priority(entries):
    counts = Counter(e.get('priority', 'unknown').lower() for e in entries)
    return dict(counts)


def count_by_area(entries):
    counts = Counter(e.get('area', 'unknown').lower() for e in entries)
    return dict(counts)


def get_known_pattern_keys(entries):
    """All active pattern keys for quick lookup."""
    return {e.get('pattern_key', '').strip()
            for e in entries
            if e.get('pattern_key', '').strip()
            and e.get('pattern_key', '').strip() not in ('', 'unknown')}


# ── Drift Detection ──────────────────────────────────────────────

def load_soul_principles():
    """Extract principles from SOUL.md."""
    soul = WORKSPACE / "SOUL.md"
    if not soul.exists():
        return []
    content = soul.read_text()
    # Extract short principles/rules — lines that look like directives
    principles = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith('-') or line.startswith('*'):
            cleaned = line.lstrip('-* ').strip()
            if len(cleaned) > 10 and len(cleaned) < 200:
                principles.append(cleaned)
    return principles


def load_recent_daily_logs(n=3):
    """Load the most recent N daily memory files."""
    daily_files = sorted(MEMORY_DIR.glob("2026-*.md"), reverse=True)[:n]
    logs = {}
    for f in daily_files:
        try:
            logs[f.name] = f.read_text()
        except Exception:
            pass
    return logs


def detect_drift_signals(principles, daily_logs):
    """Look for known drift patterns in recent logs."""
    drift_keywords = {
        "over-explaining": ["explained at length", "detailed explanation", "walked through",
                           "let me explain", "here's what happened"],
        "sycophantic-openers": ["great question", "excellent point", "good thinking",
                               "that's a great", "wonderful idea"],
        "philosophy-creep": ["philosophically", "existential", "deeper meaning",
                           "nature of", "what it means to"],
        "not-resourceful": ["I don't know", "I'm not sure", "I can't find",
                          "could you tell me", "where is the"],
    }

    signals = []
    for drift_type, keywords in drift_keywords.items():
        hits = 0
        examples = []
        for fname, content in daily_logs.items():
            content_lower = content.lower()
            for kw in keywords:
                if kw in content_lower:
                    hits += 1
                    examples.append(f"{fname}: matched '{kw}'")
        if hits >= 2:
            signals.append({
                "type": drift_type,
                "hits": hits,
                "examples": examples[:3],
            })

    return signals


# ── Prompt Generation ─────────────────────────────────────────────

def generate_always_on_prompt(learnings, errors, features, pattern_index, state):
    """Generate context for the always-on silent logging mode."""
    sections = []

    all_entries = learnings + errors + features
    known_keys = get_known_pattern_keys(all_entries)
    promo_candidates = find_promotion_candidates(all_entries)
    auto_promo = find_auto_promote(all_entries)
    high_pending = find_high_priority_pending(all_entries)

    sections.append("# Self-Improve")
    sections.append("")
    sections.append("Reef's learning capture system. Log what went wrong or was learned, so future-Reef doesn't repeat it.")
    sections.append("")

    # ── Pre-computed state
    sections.append("## Current State (pre-analyzed)")
    sections.append("")
    sections.append(f"- **LEARNINGS.md**: {len(learnings)} entries ({sum(1 for e in learnings if e.get('status','').lower() == 'pending')} pending)")
    sections.append(f"- **ERRORS.md**: {len(errors)} entries ({sum(1 for e in errors if e.get('status','').lower() == 'pending')} pending)")
    sections.append(f"- **FEATURE_REQUESTS.md**: {len(features)} entries")
    sections.append(f"- **Known Pattern-Keys**: {len(known_keys)}")
    sections.append("")

    # ── Auto-promote alerts
    if auto_promo:
        sections.append("## ⚠ AUTO-PROMOTE NOW (Recurrence >= 3)")
        sections.append("")
        for e in auto_promo:
            sections.append(f"- **{e.get('pattern_key', e.get('id', '?'))}** — Count: {e.get('recurrence_count')}, Status: {e.get('status')}")
            sections.append(f"  Action: {e.get('action', e.get('correction', '?'))}")
        sections.append("")
        sections.append("**Promote these immediately. Do not wait for user input.**")
        sections.append("")

    # ── Flag for Jounes
    if promo_candidates:
        sections.append("## Flag for Jounes (Recurrence >= 2)")
        sections.append("")
        for e in promo_candidates:
            sections.append(f"- **{e.get('pattern_key', e.get('id', '?'))}** — Count: {e.get('recurrence_count')}")
            sections.append(f"  Summary: {e.get('summary', e.get('correction', e.get('action', '?')))[:100]}")
        sections.append("")

    # ── Known Pattern-Keys (for recurrence matching)
    sections.append("## Known Pattern-Keys (check BEFORE creating new entries)")
    sections.append("")
    sections.append("If logging a new event, scan this list first. If it matches, INCREMENT the existing entry.")
    sections.append("")

    # Group by area
    area_keys = defaultdict(list)
    for e in all_entries:
        pk = e.get('pattern_key', '').strip()
        if pk and pk not in ('', 'unknown'):
            area = e.get('area', 'general')
            area_keys[area].append((pk, e.get('recurrence_count', 1), e.get('status', 'pending')))

    for area in sorted(area_keys.keys()):
        keys = area_keys[area]
        # Deduplicate
        seen = set()
        unique = []
        for pk, rc, st in keys:
            if pk not in seen:
                seen.add(pk)
                unique.append((pk, rc, st))
        sections.append(f"**{area}**: {', '.join(f'`{pk}`(×{rc})' if rc > 1 else f'`{pk}`' for pk, rc, st in unique)}")
    sections.append("")

    # ── When to log
    sections.append("## When to Log (Auto-Triggers)")
    sections.append("")
    sections.append("**LOG SILENTLY AND IMMEDIATELY — NO ANNOUNCEMENTS — when:**")
    sections.append("")
    sections.append("1. **Jounes corrects Reef** → LEARNINGS.md")
    sections.append("2. **A command/tool fails** → ERRORS.md")
    sections.append("3. **A capability gap** → FEATURE_REQUESTS.md")
    sections.append("4. **Knowledge was outdated/wrong** → LEARNINGS.md")
    sections.append("5. **A better approach discovered** → LEARNINGS.md")
    sections.append("")
    sections.append("**Acknowledging a correction verbally is NOT the same as logging it. WRITE IT TO THE FILE.**")
    sections.append("")

    # ── Entry formats (compact)
    sections.append("## Entry Format")
    sections.append("")
    sections.append("### LEARNINGS.md")
    sections.append("```")
    sections.append("[LRN-YYYYMMDD-NNN] Short title")
    sections.append("- Logged: YYYY-MM-DD HH:MM CET")
    sections.append("- Priority: low | medium | high | critical")
    sections.append("- Status: pending")
    sections.append("- Area: infra | memory | heartbeat | api | tools | skill | trading | identity")
    sections.append("- Pattern-Key: <stable kebab-case key>")
    sections.append("- Recurrence-Count: 1")
    sections.append("- Summary: What went wrong")
    sections.append("- Correction: What the right answer is")
    sections.append("- Promote-To: (blank)")
    sections.append("```")
    sections.append("")
    sections.append("### ERRORS.md")
    sections.append("```")
    sections.append("[ERR-YYYYMMDD-NNN] Short title")
    sections.append("- Logged: YYYY-MM-DD HH:MM CET")
    sections.append("- Priority: low | medium | high | critical")
    sections.append("- Status: pending")
    sections.append("- Area: infra | memory | heartbeat | api | tools | skill | trading | identity")
    sections.append("- Pattern-Key: <stable kebab-case key>")
    sections.append("- Recurrence-Count: 1")
    sections.append("- Command/Context: What was attempted")
    sections.append("- Error: Exact error message")
    sections.append("- Root-Cause: Why (or 'unknown')")
    sections.append("- Fix: What resolved it (or 'open')")
    sections.append("```")
    sections.append("")
    sections.append("### FEATURE_REQUESTS.md")
    sections.append("```")
    sections.append("[FEAT-YYYYMMDD-NNN] Short title")
    sections.append("- Logged: YYYY-MM-DD HH:MM CET")
    sections.append("- Priority: low | medium | high")
    sections.append("- Status: pending")
    sections.append("- Area: infra | memory | heartbeat | api | tools | skill | trading | identity")
    sections.append("- Request: What was asked for")
    sections.append("- Context: What triggered it")
    sections.append("- Gap: What Reef couldn't do")
    sections.append("- Suggested-Approach: How to build (or 'unclear')")
    sections.append("```")
    sections.append("")

    # ── Sequence
    sections.append(f"### Next sequence numbers")
    today = datetime.now().strftime('%Y%m%d')
    for prefix, entries_list in [('LRN', learnings), ('ERR', errors), ('FEAT', features)]:
        today_entries = [e for e in entries_list if e.get('date_str') == today]
        next_seq = len(today_entries) + 1
        sections.append(f"- {prefix}-{today}-{next_seq:03d}")
    sections.append("")

    # ── Recurrence rules (compact)
    sections.append("## Recurrence Rules")
    sections.append("")
    sections.append("**BEFORE writing any new entry, check if the Pattern-Key exists.**")
    sections.append("")
    sections.append("Two ways to check:")
    sections.append("1. **Scan the Known Pattern-Keys list above** — fast, covers all known keys")
    sections.append("2. **Use `check-pattern` skill** — invoke with the candidate key for exact + fuzzy matching,")
    sections.append("   recurrence count, and auto-promote alerts. Preferred when the key is ambiguous.")
    sections.append("")
    sections.append("If match found:")
    sections.append("- Increment `Recurrence-Count` on existing entry")
    sections.append("- Add: `- Recurred: YYYY-MM-DD (context: one line)`")
    sections.append("- Count >= 2 → flag for Jounes next heartbeat")
    sections.append("- Count >= 3 → auto-promote immediately")
    sections.append("")

    # ── Promotion targets (compact)
    sections.append("## Promotion Targets")
    sections.append("")
    sections.append("| What | Target |")
    sections.append("|------|--------|")
    sections.append("| Behavioral rule | AGENTS.md |")
    sections.append("| Infra fact | memory/infrastructure.md or MEMORY.md |")
    sections.append("| Skill workflow fix | skills/<name>/SKILL.md |")
    sections.append("| Heartbeat check | HEARTBEAT.md |")
    sections.append("| Daily context | MEMORY.md |")
    sections.append("| Style/preference | USER.md |")
    sections.append("")

    # ── Rules (compact)
    sections.append("## Rules")
    sections.append("")
    sections.append("- **LOG SILENTLY.** No announcements. EVER.")
    sections.append("- One entry per incident.")
    sections.append("- **BE SPECIFIC.** Exact file, exact error, exact scenario.")
    sections.append("- Don't log volatile preferences. Log stable facts.")
    sections.append("- **NEVER log private info** (creds, personal details).")
    sections.append("- On promotion: write as a rule/fact, not 'I once made this mistake'.")
    sections.append("- **CHECK Pattern-Keys BEFORE creating new entries.** Never duplicate.")

    return "\n".join(sections)


def generate_status_prompt(learnings, errors, features, pattern_index, state):
    """Generate the /self-improve manual trigger report."""
    sections = []
    all_entries = learnings + errors + features

    promo_candidates = find_promotion_candidates(all_entries)
    auto_promo = find_auto_promote(all_entries)
    high_pending = find_high_priority_pending(all_entries)
    stale = find_stale_entries(all_entries, days=21)
    recently_promoted = find_recently_promoted(all_entries)
    dupes = find_duplicates(all_entries)

    lrn_status = count_by_status(learnings)
    err_status = count_by_status(errors)
    feat_status = count_by_status(features)

    lrn_prio = count_by_priority([e for e in learnings if e.get('status', '').lower() == 'pending'])
    err_prio = count_by_priority([e for e in errors if e.get('status', '').lower() == 'pending'])

    areas = count_by_area(all_entries)

    sections.append("# Self-Improve Status Report")
    sections.append("")
    sections.append("Pre-analyzed by Python. Present this data to the user, then offer actions.")
    sections.append("")

    # ── Summary
    sections.append("## Summary")
    sections.append("")
    lrn_open = lrn_status.get('pending', 0)
    err_open = err_status.get('pending', 0)
    feat_open = feat_status.get('pending', 0)
    lrn_hc = lrn_prio.get('high', 0) + lrn_prio.get('critical', 0)
    err_hc = err_prio.get('high', 0) + err_prio.get('critical', 0)

    sections.append(f"**LEARNINGS** — {len(learnings)} total, {lrn_open} open ({lrn_hc} high/critical)")
    sections.append(f"**ERRORS** — {len(errors)} total, {err_open} open ({err_hc} high/critical)")
    sections.append(f"**FEATURE_REQUESTS** — {len(features)} total, {feat_open} open")
    sections.append("")

    # ── Area breakdown
    sections.append("**By area:** " + ", ".join(f"{a}: {c}" for a, c in sorted(areas.items(), key=lambda x: -x[1])))
    sections.append("")

    # ── Auto-promote
    if auto_promo:
        sections.append("## ⚠ AUTO-PROMOTE NOW (Recurrence >= 3)")
        for e in auto_promo:
            pk = e.get('pattern_key', e.get('id', '?'))
            sections.append(f"- **{pk}** — Count: {e.get('recurrence_count')}")
            sections.append(f"  Action: {e.get('action', e.get('correction', '?'))[:120]}")
        sections.append("")

    # ── Due for promotion
    if promo_candidates:
        sections.append("## Due for Promotion (Recurrence >= 2)")
        for e in promo_candidates:
            pk = e.get('pattern_key', e.get('id', '?'))
            sections.append(f"- [{e.get('id', '?')}] **{pk}** — Count: {e.get('recurrence_count')}")
            sections.append(f"  {e.get('summary', e.get('action', e.get('correction', '?')))[:120]}")
        sections.append("")

    # ── High/critical pending
    if high_pending:
        sections.append("## High/Critical Pending")
        for e in high_pending:
            eid = e.get('id', '?')
            title = e.get('title', e.get('pattern_key', '?'))
            sections.append(f"- [{eid}] {title} — {e.get('summary', e.get('error', '?'))[:100]}")
        sections.append("")

    # ── Stale entries
    if stale:
        sections.append(f"## Stale Entries (>{21} days old, recurrence 1, still pending)")
        sections.append(f"**{len(stale)} entries** could be triaged (resolved/won't_fix):")
        for e in stale[:10]:
            eid = e.get('id', e.get('pattern_key', '?'))
            age = (datetime.now() - e['date']).days if e.get('date') else '?'
            sections.append(f"- [{eid}] {e.get('title', e.get('pattern_key', '?'))} — {age} days old")
        if len(stale) > 10:
            sections.append(f"  ... and {len(stale) - 10} more")
        sections.append("")

    # ── Outcome tracking
    if recently_promoted:
        sections.append("## Needs Outcome Check (promoted, no outcome recorded)")
        for e in recently_promoted:
            sections.append(f"- [{e.get('id', '?')}] {e.get('pattern_key', '?')} — promoted to {e.get('promote_to', '?')}")
        sections.append("")

    # ── Duplicates across files
    if dupes:
        sections.append("## Cross-File Duplicates (same Pattern-Key in LEARNINGS + ERRORS)")
        for pk, types in dupes.items():
            sections.append(f"- `{pk}` — appears in {', '.join(sorted(types))}")
        sections.append("")

    # ── Offer actions
    sections.append("## Present to user:")
    sections.append("```")
    sections.append("## Self-Improve Status")
    sections.append("")
    sections.append(f"**LEARNINGS** — {lrn_open} open ({lrn_hc} high/critical)")
    sections.append(f"**ERRORS** — {err_open} open ({err_hc} high/critical)")
    sections.append(f"**FEATURE_REQUESTS** — {feat_open} open")
    sections.append("")
    if promo_candidates:
        sections.append("### Due for promotion (Recurrence >= 2)")
        for e in promo_candidates:
            pk = e.get('pattern_key', e.get('id', '?'))
            sections.append(f"- {pk} — Count: {e.get('recurrence_count')}")
    if high_pending:
        sections.append("")
        sections.append("### High/Critical pending")
        for e in high_pending[:5]:
            sections.append(f"- [{e.get('id','?')}] {e.get('title', e.get('pattern_key', '?'))}")
    if stale:
        sections.append("")
        sections.append(f"### Stale ({len(stale)} entries, >21 days, single occurrence)")
    sections.append("")
    sections.append("[P] Promote all due  [T] Triage together  [S] Skip")
    sections.append("```")

    return "\n".join(sections)


def generate_check_prompt(key, learnings, errors, features, pattern_index):
    """Check if a Pattern-Key exists — used before logging."""
    sections = []
    key_lower = key.lower().strip()

    sections.append(f"# Pattern-Key Check: `{key}`")
    sections.append("")

    if key_lower in pattern_index:
        entries = pattern_index[key_lower]
        sections.append(f"## MATCH FOUND — {len(entries)} existing entries")
        sections.append("")
        sections.append("**DO NOT create a new entry. INCREMENT the existing one.**")
        sections.append("")
        for e in entries:
            sections.append(f"### [{e.get('id', '?')}]")
            sections.append(f"- File: {'LEARNINGS' if e.get('type') == 'LRN' else 'ERRORS' if e.get('type') == 'ERR' else 'FEATURE_REQUESTS'}.md")
            sections.append(f"- Current Recurrence-Count: {e.get('recurrence_count', 1)}")
            sections.append(f"- Status: {e.get('status', '?')}")
            sections.append(f"- Action needed: Increment to {e.get('recurrence_count', 1) + 1}")
            new_count = e.get('recurrence_count', 1) + 1
            if new_count >= 3:
                sections.append(f"- **⚠ This will hit count {new_count} — AUTO-PROMOTE IMMEDIATELY**")
            elif new_count >= 2:
                sections.append(f"- **This will hit count {new_count} — flag for Jounes next heartbeat**")
            sections.append("")
    else:
        # Fuzzy match — look for partial matches
        partials = []
        for pk in pattern_index:
            # Check word overlap
            key_words = set(key_lower.split('-'))
            pk_words = set(pk.split('-'))
            overlap = key_words & pk_words
            if len(overlap) >= 1 and len(overlap) / max(len(key_words), len(pk_words)) > 0.3:
                partials.append((pk, len(overlap)))

        if partials:
            sections.append("## NO EXACT MATCH — but similar keys exist:")
            sections.append("")
            for pk, score in sorted(partials, key=lambda x: -x[1]):
                entries = pattern_index[pk]
                sections.append(f"- `{pk}` (×{entries[0].get('recurrence_count', 1)}) — {entries[0].get('summary', entries[0].get('action', '?'))[:80]}")
            sections.append("")
            sections.append("If one of these is the same pattern, use that key instead of creating a new one.")
        else:
            sections.append("## NO MATCH — safe to create new entry.")
        sections.append("")

    return "\n".join(sections)


def generate_drift_prompt(principles, daily_logs, drift_signals):
    """Generate drift detection report."""
    sections = []

    sections.append("# Drift Detection Report")
    sections.append("")
    sections.append("Comparison of SOUL.md principles against recent daily logs.")
    sections.append("")

    if not principles:
        sections.append("Could not load SOUL.md principles.")
        return "\n".join(sections)

    if not daily_logs:
        sections.append("No recent daily logs found to compare against.")
        return "\n".join(sections)

    sections.append(f"**Principles loaded:** {len(principles)}")
    sections.append(f"**Daily logs scanned:** {', '.join(sorted(daily_logs.keys(), reverse=True))}")
    sections.append("")

    if drift_signals:
        sections.append("## Drift Signals Detected")
        sections.append("")
        for sig in drift_signals:
            sections.append(f"### `drift-{sig['type']}` — {sig['hits']} hits")
            for ex in sig['examples']:
                sections.append(f"- {ex}")
            sections.append("")

        sections.append("## Action Required")
        sections.append("")
        sections.append("For each signal with 2+ hits:")
        sections.append("1. Check if a `drift-<type>` Pattern-Key already exists in LEARNINGS.md")
        sections.append("2. If yes: increment Recurrence-Count")
        sections.append("3. If no: create new LRN entry with Pattern-Key `drift-<type>`")
        sections.append("")
    else:
        sections.append("## No Drift Detected")
        sections.append("")
        sections.append("Recent behavior aligns with SOUL.md principles. No action needed.")

    # Also present principles for LLM to do deeper semantic check
    sections.append("## SOUL.md Principles (for deeper manual check)")
    sections.append("")
    for p in principles[:15]:
        sections.append(f"- {p}")
    sections.append("")
    sections.append("The keyword scan above catches obvious drift. You should also do a quick semantic check:")
    sections.append("read the daily logs and see if any behavior contradicts these principles in ways keywords wouldn't catch.")

    return "\n".join(sections)


def generate_triage_prompt(learnings, errors, features):
    """Generate triage report for stale entries."""
    sections = []
    all_entries = learnings + errors + features

    stale = find_stale_entries(all_entries, days=21)
    very_stale = find_stale_entries(all_entries, days=45)

    sections.append("# Triage Report")
    sections.append("")
    sections.append(f"**Total entries:** {len(all_entries)}")
    sections.append(f"**Stale (>21 days, pending, recurrence 1):** {len(stale)}")
    sections.append(f"**Very stale (>45 days):** {len(very_stale)}")
    sections.append("")

    if very_stale:
        sections.append("## Candidates for won't_fix (>45 days, never recurred)")
        sections.append("")
        sections.append("These are likely one-off events. Consider closing:")
        sections.append("")
        for e in very_stale:
            age = (datetime.now() - e['date']).days if e.get('date') else '?'
            sections.append(f"- [{e.get('id', e.get('pattern_key', '?'))}] {e.get('title', e.get('pattern_key', '?'))} — {age} days")
            sections.append(f"  {e.get('action', e.get('summary', e.get('correction', '?')))[:100]}")
        sections.append("")

    if stale and stale != very_stale:
        remaining = [e for e in stale if e not in very_stale]
        if remaining:
            sections.append(f"## Review (21-45 days old)")
            sections.append("")
            for e in remaining:
                age = (datetime.now() - e['date']).days if e.get('date') else '?'
                sections.append(f"- [{e.get('id', e.get('pattern_key', '?'))}] {e.get('title', e.get('pattern_key', '?'))} — {age} days")
            sections.append("")

    if not stale:
        sections.append("No stale entries. Everything is recent or already resolved.")

    sections.append("")
    sections.append("## Actions")
    sections.append("For each stale entry, decide: `resolved` (lesson learned, no longer relevant), `won't_fix` (one-off, not worth tracking), or keep `pending` (still relevant).")

    return "\n".join(sections)


# ── Main ──────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    mode = args[0] if args else "always-on"

    state = load_state()

    # Parse all files
    learnings = parse_entries(LEARNINGS_DIR / "LEARNINGS.md")
    errors = parse_entries(LEARNINGS_DIR / "ERRORS.md")
    features = parse_entries(LEARNINGS_DIR / "FEATURE_REQUESTS.md")

    all_entries = learnings + errors + features
    pattern_index = build_pattern_index(all_entries)
    # Normalize keys to lowercase
    pattern_index = {k.lower(): v for k, v in pattern_index.items()}

    if mode == "status":
        prompt = generate_status_prompt(learnings, errors, features, pattern_index, state)
    elif mode == "check" and len(args) > 1:
        key = args[1]
        prompt = generate_check_prompt(key, learnings, errors, features, pattern_index)
    elif mode == "drift":
        principles = load_soul_principles()
        daily_logs = load_recent_daily_logs(3)
        drift_signals = detect_drift_signals(principles, daily_logs)
        prompt = generate_drift_prompt(principles, daily_logs, drift_signals)
    elif mode == "triage":
        prompt = generate_triage_prompt(learnings, errors, features)
    else:
        prompt = generate_always_on_prompt(learnings, errors, features, pattern_index, state)

    # Update state
    state["runs"] += 1
    state["last_mode"] = mode
    state["last_run"] = datetime.now().isoformat()
    save_state(state)

    print(prompt)


if __name__ == "__main__":
    main()
