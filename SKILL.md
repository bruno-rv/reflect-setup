---
name: reflect-setup
description: Diagnostic-only scan of .claude/projects/ session transcripts to find recurring friction and rank improvement candidates (skill / automation / fix / nothing) with cited evidence. Use when the user wants a periodic audit of their Claude Code setup based on how they actually work, not a config-hygiene pass.
argument-hint: [days-back] [project-filter]
allowed-tools: Read, Grep, Glob, Bash, Task, Write, AskUserQuestion
---

# Reflect on Setup

This is a **diagnosis-only** run through step 6. Never edit, delete, or "helpfully fix"
anything you find — not a skill, not a CLAUDE.md, not a hook, not a subagent config — until
the optional Apply phase, and that phase only ever runs if the user opts in. Through
diagnosis, the files you write are `reflection-notes.md` and the local `clusters.yaml`
ledger, plus scratch digest files that are throwaway working data, not deliverables. If a
fix seems obvious before the Apply phase, propose it in the notes; do not apply it.

## 0. Resolve scope

- Parse `$ARGUMENTS` for an optional day window (default: **last 30 days**; an explicit window in
  `$ARGUMENTS` overrides this) and an optional project-path filter. The last-run date in
  `reflection-notes.md`, if present, is still used for the "Since last run" comparison in step 6 —
  it just no longer sets the mining window itself.
- State the resolved scope at the top of your output.

## 1. Inventory what already exists

Before mining anything, catalog current coverage so you never propose something that's already built:

- Skills: `~/.claude/skills/*/SKILL.md`, `.claude/skills/*/SKILL.md`
- Commands (legacy): `~/.claude/commands/*.md`, `.claude/commands/*.md`
- Subagents: `.claude/agents/*.md`
- Hooks and permission rules: `.claude/settings.json`, `.claude/settings.local.json`

Keep this as a lookup table for step 4.

## 2. Pre-extract, then dispatch miners over digests

**First**, run `digest.py` (repo root of this skill) into a scratch directory — this
deterministically streams every in-scope session `.jsonl` line-by-line and keeps only
user-role text, `is_error:true` tool results, and interrupt markers, so no subagent ever has
to stream raw multi-MB transcripts itself or risk matching noise inside file-read content:

```bash
python3 digest.py --projects-dir ~/.claude/projects --since <days> --out <scratch-dir> [--project-filter <substr>]
```

Read the stdout summary line for a sanity count (sessions scanned, sessions with signal,
per-type totals) before proceeding.

**Then**, partition the resulting digest files in `<scratch-dir>` into batches (by week, or by
a fixed file count — pick whichever keeps each subagent's context manageable) and dispatch one
subagent per batch via the Task tool. Each miner subagent uses the canonical prompt at
`references/miner-prompt.md` in this skill directory, filled in with its digest batch — it
extracts only the signal types and false-positive rules defined there, each tagged with
session basename, timestamp, and a short paraphrase (never verbatim sensitive content).

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

## 5. Fix-wiring verification

For every entry in the local `clusters.yaml` ledger with `status: fix-applied`, check this
window's digests for evidence the fix actually **operates**, per that entry's `wired_check`
(e.g. "digests contain zero 'is the shell stuck' user messages," or "recording_health.py
appears invoked in digests, not just present in the repo"). A fix that never ran, or whose
symptom recurred anyway, gets flagged **built-not-operating** in the notes and re-ranked —
built code is not the same claim as an operating fix.

Update `clusters.yaml` for every touched entry: bump `last_seen` and `sessions` for anything
still recurring, and move `status` to `resolved` only when a window shows explicit
non-recurrence evidence (not merely that no new evidence was mined). New clusters from step 4
get a fresh ledger `id` (kebab-slug) and `status: new`.

## 6. Write reflection-notes.md

Append (don't overwrite) a dated section, ranked most-leverage-first. Each cluster heading
carries its `clusters.yaml` ledger id so notes and ledger stay cross-referenced:

```markdown
## Run: 2026-07-10 (scope: last 21 days, all projects)

### 1. [Cluster name] (`ledger-id`) — New skill — HIGH
- Recurrence: 5 sessions / 8 occurrences (2026-06-22 → 2026-07-09)
- Evidence: session_abc.jsonl, session_def.jsonl, ... (link or path)
- Build cost: S (single SKILL.md, no new tools)
- Rationale: ...
- Status: new

### 2. ...
```

At the end, add a **Since last run** subsection: what's resolved (no longer recurring), what's
still open, what's newly recurring, and any **built-not-operating** flags from step 5.

## Apply phase (optional, opt-in)

Diagnosis-only governs everything above; this phase only runs if the user opts in, and never
runs unasked.

After writing `reflection-notes.md`, use `AskUserQuestion` to ask whether to apply the
actionable fixes from this run. If yes: dispatch one fixer subagent per actionable cluster, in
parallel, and require each one to return **proof** (actual command output, not a claim) that
its fix works. Then:

- Append an "Applied `<date>`" addendum to `reflection-notes.md` summarizing what each fixer did.
- Set the corresponding `clusters.yaml` entries to `status: fix-applied` with a `wired_check`
  defined for each — the next run's step 5 is what turns "applied" into "verified operating."

Pairs well with a monthly `/schedule` routine that runs this skill unattended and surfaces the
notes.
