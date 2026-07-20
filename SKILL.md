---
name: reflect-setup
description: Diagnostic-only scan of .claude/projects/ session transcripts to find recurring friction and rank improvement candidates (skill / automation / fix / nothing) with cited evidence. Use when the user wants a periodic audit of their Claude Code setup based on how they actually work — a recurring friction and session-transcript scan, not a config-hygiene pass.
argument-hint: [days-back] [project-filter]
allowed-tools: Read, Grep, Glob, Bash, Task, Write, AskUserQuestion
---

# Reflect on Setup

This is **diagnosis-only** through step 8 — never edit, delete, or "helpfully fix" anything until
the optional Apply phase (opt-in only, never runs unasked). The only files written during
diagnosis are `reflection-notes.md` and the local `clusters.yaml` ledger, plus throwaway scratch
digests. If a fix seems obvious before Apply, propose it in the notes — do not apply it.

## Quick start

Run `/reflect-setup` (optionally `[days-back] [project-filter]`, default **last 30 days**, all
projects). It scopes the window, inventories what already exists, digests + mines session
transcripts, clusters findings, verifies fix-wiring, decides per cluster, and appends a dated
section to `reflection-notes.md`. Full detail on every step: `REFERENCE.md`.

## Workflow

1. **Scope** — parse `$ARGUMENTS` for a day window (default 30) and project filter; state the
   resolved scope. [Detail](REFERENCE.md#scope)
2. **Inventory** — catalog existing skills, commands, subagents, hooks/permissions so nothing gets
   re-proposed. [Detail](REFERENCE.md#inventory)
3. **Digest** — run `scripts/digest.py` to pre-extract user text, errors, and interrupts from
   session `.jsonl` files into a scratch dir. [Detail](REFERENCE.md#digest)
4. **Mine** — dispatch one subagent per digest batch using `references/miner-prompt.md`.
   [Detail](REFERENCE.md#mining)
5. **Cluster** — merge miner outputs by underlying cause, with session list, counts, and dates.
   [Detail](REFERENCE.md#clustering)
6. **Fix-wiring verification** — check `clusters.yaml` entries with `status: fix-applied` for
   proof the fix operates; flag **built-not-operating** if not. [Detail](REFERENCE.md#fix-wiring-verification)
7. **Decide** — apply the recurrence/nature thresholds per cluster (new skill / automation / fix /
   nothing). [Detail](REFERENCE.md#decision-thresholds)
8. **Write notes** — append a dated, ranked section to `reflection-notes.md` plus a "Since last
   run" summary. [Detail](REFERENCE.md#notes-format)
9. **Apply (optional, opt-in)** — ask via `AskUserQuestion`; if yes, dispatch parallel fixers, each
   required to return proof-of-fix. [Detail](REFERENCE.md#apply-phase)

## Hard constraints

- Diagnosis-only through step 8: no edits/fixes anywhere until Apply, and Apply never runs unasked.
- Only writes `reflection-notes.md` and `clusters.yaml` (plus scratch digests) — nothing else.
- Default window is **30 days**; an explicit window in `$ARGUMENTS` overrides it, but the ledger's
  last-run date still drives the "Since last run" comparison in step 8.
