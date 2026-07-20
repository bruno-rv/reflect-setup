---
name: reflect-setup
description: Diagnostic-only scan of .claude/projects/ session transcripts to find recurring friction and rank improvement candidates (skill / automation / fix / nothing) with cited evidence. Use when the user wants a periodic audit of their Claude Code setup based on how they actually work, not a config-hygiene pass.
argument-hint: [days-back] [project-filter]
allowed-tools: Read, Grep, Glob, Bash, Task, Write
---

# Reflect on Setup

This is a **diagnosis-only** run. Never edit, delete, or "helpfully fix" anything you find —
not a skill, not a CLAUDE.md, not a hook, not a subagent config. The only file you write is
`reflection-notes.md`. If a fix seems obvious, propose it in the notes; do not apply it.

## 0. Resolve scope

- Parse `$ARGUMENTS` for an optional day window (default: **last 30 days**; an explicit window in
  `$ARGUMENTS` overrides this) and an optional project-path filter. The last-run date in
  `reflection-notes.md`, if present, is still used for the "Since last run" comparison in step 5 —
  it just no longer sets the mining window itself.
- State the resolved scope at the top of your output.

## 1. Inventory what already exists

Before mining anything, catalog current coverage so you never propose something that's already built:

- Skills: `~/.claude/skills/*/SKILL.md`, `.claude/skills/*/SKILL.md`
- Commands (legacy): `~/.claude/commands/*.md`, `.claude/commands/*.md`
- Subagents: `.claude/agents/*.md`
- Hooks and permission rules: `.claude/settings.json`, `.claude/settings.local.json`

Keep this as a lookup table for step 4.

## 2. Dispatch subagents to pull raw signals

Partition session files under `.claude/projects/` within scope into batches (by week, or by a
fixed session count — pick whichever keeps each subagent's context manageable) and dispatch one
subagent per batch via the Task tool. Each subagent extracts only these signal types, each tagged
with session file path, timestamp, and a short paraphrase (never verbatim sensitive content):

- **Correction signals** — user says "no," "that's wrong," "actually," re-explains something already stated in CLAUDE.md
- **Friction signals** — repeated manual steps, the same multi-turn back-and-forth on the same topic across sessions
- **Failure signals** — tool-call errors, retries, permission denials, abandoned/reverted work
- **Explicit complaints** — user says some version of "this keeps happening" / "again?"

## 3. Cluster across sessions

In the main agent, merge subagent outputs into clusters by underlying cause (not surface wording).
For each cluster, record: session list, per-session count, first-seen date, last-seen date.

## 4. Decide per cluster

For each cluster, cross-check against the step-1 inventory, then decide using explicit thresholds:

| Recurrence | Nature of the fix | Verdict |
|---|---|---|
| ≥3 sessions across ≥2 distinct days | Same repeated task/workflow | **New skill** — only if no existing skill covers it |
| ≥3 sessions | Mechanical, deterministic (formatting, repeated bash sequence) | **Automation** (hook or script) |
| Any recurrence | One-line CLAUDE.md rule, config tweak, or permission entry | **Fix** |
| 1–2 sessions, or already covered by an existing skill/command | — | **Nothing** — note why, so it doesn't get re-proposed next run |

Never propose a skill for something that hasn't recurred, even if it looks high-value.

## 5. Write reflection-notes.md

Append (don't overwrite) a dated section, ranked most-leverage-first:

```markdown
## Run: 2026-07-10 (scope: last 21 days, all projects)

### 1. [Cluster name] — New skill — HIGH
- Recurrence: 5 sessions / 8 occurrences (2026-06-22 → 2026-07-09)
- Evidence: session_abc.jsonl, session_def.jsonl, ... (link or path)
- Build cost: S (single SKILL.md, no new tools)
- Rationale: ...
- Status: new

### 2. ...
```

At the end, add a **Since last run** subsection: what's resolved (no longer recurring), what's
still open, what's newly recurring.
