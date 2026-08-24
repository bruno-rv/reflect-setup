# reflect-setup — Reference

This is the runtime-neutral operator contract behind `SKILL.md`. The Python
core uses only the standard library and writes a manifest before it accepts any
miner output.

## Runtime table

| Concern | Claude Code | Codex | Shared rule |
| --- | --- | --- | --- |
| Sessions | `~/.claude/projects/` | `~/.codex/sessions/` | `--source-root` overrides the default |
| Canonical scope | Project JSONL; `subagents/` excluded | `session_meta.payload.thread_source == "user"` | `--include-subagents` opts in |
| Inventory | skills, commands, agents, settings | skills, agents, `config.toml` | declaration is separate from operation |
| Skill root | `~/.claude/skills/reflect-setup` | `~/.codex/skills/reflect-setup` | one checkout may be installed into both |

`--runtime auto` selects one readable root. If both or neither are available,
the command fails and asks for an explicit runtime.

## Scope

`--since DAYS` means a UTC cutoff of `now - DAYS`. The direct host command
accepts `/reflect-setup [days] [project-filter]`; the Python CLI accepts
`--since`, `--project-filter`, and `--include-subagents`. Event timestamps in
each JSONL record determine inclusion. Filesystem mtime may order work but can
never include or exclude a session. Events exactly at the cutoff are included.

Only canonical user-originated records are signal candidates by default.
Assistant prose, developer messages, successful tool output containing source
code errors, and subagent/sidechain records are not user signals.

## Inventory and coverage

Inventory records `InventoryItem` declarations from runtime-specific roots.
Each `CoverageRecord` keeps these states independent:

- `declared`: an artifact exists in an inventory location;
- `eligible`: runtime trigger and scope rules select it;
- `triggered`: transcript/tool evidence shows it loaded or ran;
- `prevented`: independent behavioral evidence proves the symptom did not recur;
  `false` means recurrence and `null` means insufficient outcome evidence.

An existing `SKILL.md`, command, hook, or config file is never treated as proof
that the artifact operated.

## Digest and manifest

```bash
PYTHONPATH=scripts python3 scripts/reflect_setup.py \
  --runtime claude --since 30 --out .reflect-setup-run
```

`run_digest()` requires a new or empty output directory, emits collision-free
digest names and `manifest.json`, and records every scanned source, readability,
malformed/untimestamped line counts, project identity, and digest path. A
malformed or unreadable source produces `complete: false`, persists the
manifest, and exits nonzero with `IncompleteDigestError`; miners are not
dispatched. A non-empty output directory without a persisted manifest is
rejected rather than reused.

When the first phase stops because reports are not yet available, host miners
may run over the persisted digest files. A later invocation with the same run
directory reuses its complete manifest only after validating schema, runtime,
scope, source paths and SHA-256 hashes, and every digest path's existence,
run-directory containment, and persisted digest hash. Changed source or digest
bytes, deleted files, path escapes, or tampered manifest metadata fail closed.

## Miner contract and batches

Every report is one JSON object with exactly these top-level fields:

```json
{
  "schema_version": 1,
  "runtime": "claude",
  "run_id": "manifest-run-id",
  "batch_id": "batch-a",
  "digest_paths": ["/path/to/digest.md"],
  "findings": [],
  "themes": []
}
```

Each finding has `cluster_key`, `finding_type` (`correction`, `friction`,
`failure`, or `complaint`), `session_id`, one-line `paraphrase`, positive
`occurrence_count`, confidence in `[0,1]`, and typed evidence. Each evidence
reference includes `digest_path`, positive `source_line`, RFC3339 `timestamp`,
matching `kind`, and explicit `project` identity.

The report's ordered `digest_paths` must equal its assigned `BatchSpec`. Across
all reports, each non-empty manifest digest path must occur in exactly one
batch. Unknown, overlapping, missing, malformed, or out-of-batch evidence
fails closed before clustering. An empty manifest has no miner work and accepts
an empty report set.

## Clustering and verification

Validated reports merge only true duplicates sharing normalized cluster,
finding type, and session. Evidence identity remains `(digest_path, source_line)`
and project identity is never inferred from filenames.

For each touched ledger entry with a non-empty `wired_check`, evaluate three
independent checks:

1. **Symptom** — explicit `symptom-absent` passes; current recurrence fails.
2. **Invocation** — `invoked` proves the artifact was loaded/called.
3. **Outcome** — a separate `outcome-pass` proves successful behavior;
   `outcome-fail` fails. Reusing invocation evidence cannot pass outcome.

`fix-applied` or `built-not-operating` becomes `resolved` only when all three
pass. Missing evidence leaves it unresolved; recurrence or failed invocation/
outcome recommends `built-not-operating`. A resolved entry regresses when its
symptom recurs. The ledger statuses are `new`, `fix-applied`,
`built-not-operating`, `monitor`, `wont-fix`, and `resolved`.

## Ranking

For each cluster, expose occurrences, sessions, projects, distinct days,
analyzed sessions, first/last seen, occurrences per 100 sessions, regression
count, confidence, impact, and implementation cost (`S`, `M`, `L`). The fixed
score is:

```text
round(
  0.30 * min(occurrences / 10, 1.0)
+ 0.20 * min(sessions / 5, 1.0)
+ 0.15 * min(projects / 3, 1.0)
+ 0.15 * min(distinct_days / 5, 1.0)
+ 0.10 * confidence
+ 0.10 * impact
- 0.10 * min(regression_count / max(occurrences, 1), 1.0),
  4,
)
```

Impact is fixed by finding type: failure `1.0`, complaint `0.9`, correction
`0.8`, friction `0.7`; mixed clusters use the average. Ties sort by score
descending, regression count ascending, first-seen date, implementation cost,
then cluster key.

## Report output

The orchestrator writes `reflection-report.json` atomically beside the digest
manifest. It includes the manifest, runtime inventory, coverage records,
verification results, ranked candidates, and Apply approval metadata. The report
is not a substitute for host notes or ledger updates. Python writes no notes or
ledger state, and never writes a report when the digest is incomplete. Pass
`--ledger PATH` (or `ledger_path=...` in the API) when explicit ledger
verification is wanted; no implicit current-directory ledger is read.

## Apply

Apply is opt-in and per cluster. `--apply` without repeated
`--approve-cluster ID` flags fails before fixer work. The host displays a
preview containing the project-relative target paths, dirty baseline, commands,
and verification commands. `scripts/apply.py` rejects path escapes, unresolved
workspace conflicts, overlapping parallel scopes, duplicate commands, and
unapproved new paths.

The API requires typed host inputs for every approved ID:

```python
run_reflection(
    ...,
    apply=True,
    approved_clusters=("stable-cluster-id",),
    apply_requests=(apply_request,),
    apply_workspaces={"stable-cluster-id": workspace_state},
)
```

Approval IDs, candidate IDs, request IDs, workspace keys, and proof IDs are
normalized to stable kebab slugs. Duplicate or ambiguous normalized IDs fail
closed. The result exposes `apply_previews` (and the legacy first-preview
`apply_preview` field). Previews are created exactly for approved clusters;
scope overlaps fail before any host execution. A `FixProof` is optional for a
preview-only pass, but every supplied proof is checked with
`validate_proof()` before Python reports it as validated. Python never executes
fixer commands or changes project files.

Fixers must return non-empty output and `passed=True`. Every declared
verification command must appear exactly once as the byte-exact record:

```text
<declared command> :: PASS
```

Any explicit failure status, missing/unknown/non-PASS record, or failed command
invalidates the proof. The host must obtain consent, execute through its active
workflow, and only then supply proof and update notes/ledger. Claude uses its
configured task/subagent delegation; Codex uses its configured
worker/subagent delegation. These instructions describe concepts, not assumed
tool names.

## Direct checks

Run the focused suites with the repository's explicit import path:

```bash
PYTHONPATH=scripts python3 scripts/test_runtime.py
PYTHONPATH=scripts python3 scripts/test_install.py
PYTHONPATH=scripts python3 scripts/test_digest.py
PYTHONPATH=scripts python3 scripts/test_miner_contract.py
PYTHONPATH=scripts python3 scripts/test_coverage_model.py
PYTHONPATH=scripts python3 scripts/test_ledger.py
PYTHONPATH=scripts python3 scripts/test_verification.py
PYTHONPATH=scripts python3 scripts/test_scoring.py
PYTHONPATH=scripts python3 scripts/test_apply.py
PYTHONPATH=scripts python3 scripts/test_evaluate.py
PYTHONPATH=scripts python3 scripts/test_reflect_setup.py
```
