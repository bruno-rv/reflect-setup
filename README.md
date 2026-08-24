# reflect-setup

`reflect-setup` is one source-controlled, stdlib-only diagnostic skill for both
Claude Code and Codex. It scans canonical user session transcripts for repeated
corrections, friction, failures, complaints, and interrupts; validates
evidence-backed miner reports; then ranks improvement candidates. Diagnosis
does not edit projects. Apply is opt-in, per cluster, scope-bounded, and proof
gated.

## Runtime support

| Host | Session root | Skill root |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/` | `~/.claude/skills/reflect-setup` |
| Codex | `~/.codex/sessions/` | `~/.codex/skills/reflect-setup` |

The default `--runtime auto` selects exactly one readable session root and
fails when both or neither are available. Use `--runtime claude` or
`--runtime codex` to remove ambiguity. `--source-root` is available for
controlled runs and tests.

## Install

From the parent directory of this checkout, install the same source into either
or both hosts:

```bash
ln -s "$(pwd)/reflect-setup" ~/.claude/skills/reflect-setup
ln -s "$(pwd)/reflect-setup" ~/.codex/skills/reflect-setup
```

For a managed copy with a source hash and provenance manifest:

```bash
PYTHONPATH=scripts python3 scripts/install.py --runtime claude --source . --mode symlink
PYTHONPATH=scripts python3 scripts/install.py --runtime codex --source . --mode symlink
```

Use `--mode copy` when a symlink is not suitable. Reinstalling an identical
managed copy is idempotent; an unmanaged or hash-mismatched target is rejected
without deleting it. Never replace live installed skill copies as part of a
diagnostic run.

## Usage

The host entry point is:

```text
/reflect-setup [days] [project-filter]
```

The runtime-neutral core can be run directly:

```bash
PYTHONPATH=scripts python3 scripts/reflect_setup.py \
  --runtime auto --since 30 --out .reflect-setup-run
```

When signal digests exist, the first phase writes `manifest.json` and stops
until host miners return JSON reports. Resume against the same run directory:

```bash
PYTHONPATH=scripts python3 scripts/reflect_setup.py \
  --runtime claude --out .reflect-setup-run \
  --miner-report .reflect-setup-run/miner-batch-a.json \
  --miner-report .reflect-setup-run/miner-batch-b.json --json
```

Reports must cover every non-empty digest path exactly once. Use
`--include-subagents` to opt into sidechains. Apply additionally requires
repeated `--approve-cluster ID` flags, an explicit `--ledger PATH` when ledger
verification is wanted, and host-supplied typed request/workspace inputs. A
bare CLI approval fails closed; the host performs fixer execution and notes or
ledger updates after Python validates previews and optional `FixProof` values.

## Host workflow

Claude Code and Codex use the same sequence but their hosts choose their own
available delegation mechanism:

1. Delegate one bounded miner worker per digest batch and collect JSON reports.
2. Collect typed `CoverageObservation` values (or validated coverage records)
   from host trigger/invocation and independent outcome evidence; do not infer
   eligibility from declared files.
3. Resume `reflect_setup.py` with those reports and observations, then inspect
   the ranked report.
4. Ask for explicit consent per cluster. The host constructs an
   `ApplyRequest`, `WorkspaceState`, and any host `CoverageObservation` values,
   then calls `run_reflection(..., apply=True, approved_clusters=...,
   apply_requests=..., apply_workspaces=...,
   coverage_observations=...)` to obtain bounded previews.
5. Execute only after preview consent, collect a `FixProof`, and call the API
   with `apply_proofs=...`. The host then updates notes/ledger using its own
   workflow.

Claude uses its configured task/subagent delegation for steps 1 and 4; Codex
uses its configured worker/subagent delegation. No unavailable tool name is
assumed by the shared Python core.

## Structure

```text
reflect-setup/
├── SKILL.md
├── REFERENCE.md
├── references/miner-prompt.md
├── scripts/runtime.py, digest.py, miner_contract.py
├── scripts/coverage_model.py, ledger.py, verification.py
├── scripts/scoring.py, apply.py, evaluate.py
├── scripts/install.py, reflect_setup.py
├── scripts/test_*.py
└── fixtures/evaluation/
```

Local manifests, scratch digests, reports, notes, and ledgers contain
session-derived data and remain uncommitted.
