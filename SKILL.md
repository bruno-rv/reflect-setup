---
name: reflect-setup
description: Diagnostic-only, dual-runtime scan of Claude Code or Codex session transcripts to find recurring friction and rank improvement candidates with cited evidence.
argument-hint: [days-back] [project-filter]
allowed-tools: Read, Grep, Glob, Bash, Task, Write, AskUserQuestion
---

# Reflect on Setup

`reflect-setup` is diagnosis-only through step 8. It never edits a project or
dispatches a fixer unless the user explicitly approves an Apply preview in
step 9. The same checkout supports Claude Code and Codex; the host workflow
chooses the host task tool, while the Python core remains runtime-neutral.

## Quick start

Run `/reflect-setup` (optionally `[days-back] [project-filter]`, default **last
30 days**, all projects). Choose the runtime explicitly when both session roots
exist. The detailed operator contract is in `REFERENCE.md`.

## Workflow

1. **Scope** — resolve `--runtime auto|claude|codex`, the UTC event-time window,
   project filter, and subagent inclusion. [Detail](REFERENCE.md#scope)
2. **Inventory** — record declared skills, commands, agents, hooks, and
   configuration for the selected host; declaration is not operation.
   [Detail](REFERENCE.md#inventory)
3. **Digest** — run `scripts/reflect_setup.py`/`scripts/digest.py` into a new
   scratch directory. The manifest records every candidate source and fails
   closed on malformed or unreadable input. [Detail](REFERENCE.md#digest)
4. **Mine** — partition non-empty digest paths into disjoint batches and dispatch
   one host miner per batch using `references/miner-prompt.md`. Miners return
   JSON only; resume the run with `--miner-report` paths. [Detail](REFERENCE.md#miner-contract-and-batches)
5. **Cluster** — validate every report against the manifest, require exact
   batch coverage, and merge findings by cause while preserving session,
   finding type, project, and evidence identity. [Detail](REFERENCE.md#clustering-and-verification)
6. **Verify wiring** — evaluate symptom, invocation, and independent behavioral
   outcome checks for touched ledger entries. Missing evidence never resolves a
   fix; recurrence produces `built-not-operating`. [Detail](REFERENCE.md#clustering-and-verification)
7. **Rank** — calculate normalized trend metrics and the deterministic candidate
   score, including recurrence, sessions, projects, days, confidence, impact,
   regressions, and implementation cost. [Detail](REFERENCE.md#ranking)
8. **Write report/notes** — write the atomic `reflection-report.json` and append
   the host's local notes/ledger updates. Diagnosis does not mutate project
   files. [Detail](REFERENCE.md#report-output)
9. **Apply (optional, opt-in)** — ask for approval per selected cluster, build a
   bounded preview, dispatch the host fixer, and require exact command plus
   verification proof before any ledger transition. [Detail](REFERENCE.md#apply)

## CLI

```bash
PYTHONPATH=scripts python3 scripts/reflect_setup.py \
  --runtime auto --since 30 --out .reflect-setup-run
```

Use `--miner-report PATH` once each host miner has returned its JSON report.
Use `--include-subagents` only when sidechain material is intentionally in
scope. `--apply` requires one or more repeated `--approve-cluster ID` flags;
the Python core never guesses or invokes a Claude/Codex task tool.

## Hard constraints

- Runtime selection is explicit when auto-detection is ambiguous.
- Event timestamps, not filesystem mtimes, determine scope.
- A partial manifest or incomplete batch cannot be reported as complete.
- Diagnosis writes only the local manifest, scratch digests, report, notes, and
  ledger; Apply is never implicit.
- No privacy, redaction, or retention behavior is added by this skill.
