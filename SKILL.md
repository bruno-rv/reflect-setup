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
8. **Write report** — write the atomic `reflection-report.json`. The host then
   appends its local notes and, when explicitly authorized, updates its ledger;
   this Python core does not perform those host mutations. [Detail](REFERENCE.md#report-output)
9. **Apply (optional, opt-in)** — the host asks for approval per selected
   cluster, supplies an `ApplyRequest` and `WorkspaceState`, inspects bounded
   previews, dispatches the host fixer, and supplies exact proof before any
   ledger transition. [Detail](REFERENCE.md#apply)

## CLI

```bash
PYTHONPATH=scripts python3 scripts/reflect_setup.py \
  --runtime auto --since 30 --out .reflect-setup-run
```

Use `--miner-report PATH` once each host miner has returned its JSON report.
Use `--include-subagents` only when sidechain material is intentionally in
scope. `--ledger PATH` is required when the host wants Python to verify ledger
entries; no implicit current-directory ledger is read. `--apply` requires one
or more repeated `--approve-cluster ID` flags plus typed request/workspace
inputs through `run_reflection()`; a bare CLI approval fails closed. The Python
core never guesses or invokes a Claude/Codex task tool.

## Hard constraints

- Runtime selection is explicit when auto-detection is ambiguous.
- Event timestamps, not filesystem mtimes, determine scope.
- A partial manifest or incomplete batch cannot be reported as complete.
- Python writes only the local manifest, scratch digests, and report. The host
  owns notes/ledger updates; Apply is never implicit.
- No privacy, redaction, or retention behavior is added by this skill.

## Host dispatch concepts

- **Claude Code:** use the active Claude host's configured task/subagent
  delegation to launch one bounded miner per digest batch. Collect each JSON
  report, resume the Python run, then obtain consent before constructing one
  request/workspace pair per approved cluster. Dispatch fixers through that
  same host mechanism and return command output plus exact `:: PASS` records.
- **Codex:** use the active Codex host's configured worker/subagent delegation
  for the same one-batch/one-report flow. After consent, construct the typed
  request/workspace inputs, inspect Python previews, execute the approved fix
  through the active Codex workflow, and return a validated `FixProof`.

Neither flow assumes a particular task-tool name. The host owns execution;
Python validates manifests, reports, previews, and proofs.
