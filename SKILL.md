---
name: reflect-setup
description: Diagnostic-only, dual-runtime scan of Claude Code or Codex session transcripts to find recurring friction and rank improvement candidates with cited evidence.
metadata:
  argument-hint: "[days-back] [project-filter]"
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
   The host must separately collect typed `CoverageObservation` values for any
   artifact whose eligibility, invocation, or prevention behavior it can prove;
   the core never invents eligibility from inventory presence.
3. **Digest** — run `scripts/reflect_setup.py` into a new
   scratch directory. The manifest records every candidate source and fails
   closed on malformed or unreadable input. [Detail](REFERENCE.md#digest)
4. **Mine** — the run writes a persisted `dispatch-plan.json` that partitions
   non-empty digest paths into byte-balanced batches. Read each `DispatchBatch`
   from the plan, send its exact metadata plus `references/miner-prompt.md` to
   one host miner, and write the returned JSON to that batch's prescribed
   `miner-reports/<batch-id>.json` path. Rerun the same command; it advances
   only when every prescribed report exists. [Detail](REFERENCE.md#miner-contract-and-batches)
5. **Cluster** — validate every report against the manifest evidence index,
   require exact batch coverage, and merge findings by cause while preserving
   session, finding type, project, and evidence identity. A finding cannot cite
   invented metadata or mix sessions. [Detail](REFERENCE.md#clustering-and-verification)
6. **Reconcile** — when more than one finding exists, the run writes
   `reconciliation-request.json` and stops. Run one host worker with
   `references/reconciler-prompt.md`; write its JSON to
   `reconciliation-report.json` and rerun the same command. The reconciler may
   group or rename findings but cannot change counts, classifications, projects,
   or evidence. [Detail](REFERENCE.md#reconciliation)
7. **Verify wiring** — evaluate symptom, invocation, and independent behavioral
   outcome checks for touched ledger entries. Missing evidence never resolves a
   fix; recurrence produces `built-not-operating`. [Detail](REFERENCE.md#clustering-and-verification)
8. **Rank** — calculate normalized trend metrics and the deterministic candidate
   score, including recurrence, sessions, projects, days, confidence, impact,
   and regressions. Every ranked item carries its own evidence, finding types,
   and paraphrases. [Detail](REFERENCE.md#ranking)
9. **Write report** — write the atomic `reflection-report.json`. The host then
   appends its local notes and, when explicitly authorized, updates its ledger;
   this Python core does not perform those host mutations. [Detail](REFERENCE.md#report-output)
10. **Apply (optional, opt-in)** — the host asks for approval per selected
    cluster, supplies a strict `--host-input` file with an `ApplyRequest` and
    `WorkspaceState`, inspects bounded previews, dispatches the host fixer, and
    supplies exact proof before any ledger transition. [Detail](REFERENCE.md#apply)

## CLI

```bash
PYTHONPATH=scripts python3 scripts/reflect_setup.py \
  --runtime auto --since 30 --out .reflect-setup-run
```

The first invocation writes `manifest.json` and `dispatch-plan.json`, prints
`stage=awaiting-miners` with the missing report paths, and exits `0`. After
each host miner writes its prescribed report, rerun the exact same command.
When more than one finding exists, the run prints
`stage=awaiting-reconciliation` with the request path; run one reconciler
worker and rerun. `--miner-batches COUNT` (default `8`) controls the batch
count and is fixed for the life of a run. Use `--include-subagents` only when
sidechain material is intentionally in scope. `--ledger PATH` is required when
the host wants Python to verify ledger entries; no implicit current-directory
ledger is read. `--apply` requires `--host-input PATH` whose `apply` object
carries the approved clusters, requests, workspaces, and optional proofs; a
bare CLI approval fails closed. The Python core never guesses or invokes a
Claude/Codex task tool.

## Hard constraints

- Runtime selection is explicit when auto-detection is ambiguous.
- Event timestamps, not filesystem mtimes, determine scope.
- A partial manifest or incomplete batch cannot be reported as complete.
- The persisted dispatch plan is authoritative: a changed `--miner-batches`,
  changed manifest, changed digest bytes, or unplanned report file fails
  closed on continuation.
- Operating ledger `artifact_ids` use runtime/root-namespaced inventory IDs;
  legacy unqualified IDs are not aliases.
- Python writes only the local manifest, scratch digests, dispatch plan,
  reconciliation request, and report. The host owns notes/ledger updates;
  Apply is never implicit.
- No privacy, redaction, or retention behavior is added by this skill.

## Host dispatch concepts

- **Claude Code:** use the active Claude host's configured task/subagent
  delegation to launch one bounded miner per dispatch batch and one reconciler
  per run. Collect each JSON report, write it to the prescribed path, resume
  the Python run, then obtain consent before constructing one
  request/workspace pair per approved cluster. Dispatch fixers through that
  same host mechanism and return command output plus exact `:: PASS` records.
- **Codex:** use the active Codex host's configured worker/subagent delegation
  for the same one-batch/one-report and one-reconciler flow. After consent,
  construct the strict host-input file, inspect Python previews, execute the
  approved fix through the active Codex workflow, and return a validated
  `FixProof`.

Neither flow assumes a particular task-tool name. The host owns execution;
Python validates manifests, plans, reports, previews, and proofs.
