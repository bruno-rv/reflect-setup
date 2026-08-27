# reflect-setup — Reference

This is the runtime-neutral operator contract behind `SKILL.md`. The Python
core uses only the standard library and writes a manifest before it accepts any
miner output.

## Runtime table

| Concern | Claude Code | Codex | Shared rule |
| --- | --- | --- | --- |
| Sessions | `~/.claude/projects/` | `~/.codex/sessions/` | `--source-root` overrides the default |
| Canonical scope | Project JSONL; `subagents/` excluded | `session_meta.payload.thread_source == "user"` | `--include-subagents` opts in |
| Inventory | skills, commands, agents, hooks, settings | skills, agents, `config.toml` | declaration is separate from operation |
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

Codex project identity uses `session_meta.payload.project` (or
`project_name`), then the basename of `cwd`. When both are absent, the
manifest and discovery layers use `codex:<relative source path>` as a stable
fallback. This preserves canonical user signals without pretending an unknown
session belongs to a named project; project filters still match only the
resulting recorded identity.

## Inventory and coverage

Inventory records `InventoryItem` declarations from runtime-specific roots. Every
artifact ID is namespaced as
`<runtime>:<global|project>:<stable-root-identity>/<relative-path>` (a root file
omits the final slash). This keeps global/project and Claude/Codex artifacts
distinct. Claude inventory includes global `~/.claude/hooks` and project
`.claude/hooks` roots so hook fixes participate in coverage checks; Codex has
no hook root because its runtime contract defines none. `memory` paths are
excluded from session discovery and are never inventory candidates. Existing
ledgers must migrate their `artifact_ids` to these IDs; no compatibility alias
is accepted because ambiguous coverage must fail closed. Each `CoverageRecord`
keeps these states independent:

- `declared`: an artifact exists in an inventory location;
- `eligible`: runtime trigger and scope rules select it;
- `triggered`: transcript/tool evidence shows it loaded or ran;
- `prevented`: independent behavioral evidence proves the symptom did not recur;
  `false` means recurrence and `null` means insufficient outcome evidence.

An existing `SKILL.md`, command, hook, or config file is never treated as proof
that the artifact operated. The host must collect explicit typed
`CoverageObservation` values when it can observe eligibility, invocation, and
independent prevention evidence:

```python
run_reflection(
    ...,
    coverage_observations=(
        CoverageObservation(
            artifact_id="claude:project:.claude/skills/fixture/SKILL.md",
            artifact_kind="skills",
            eligible=True,
            trigger_evidence=invocation_refs,
            prevention_evidence=outcome_refs,
            symptom_recurred=False,
        ),
    ),
)
```

Missing observations produce no coverage record. Hosts may pass already
validated `CoverageRecord` values through `coverage_records` instead; the two
forms are mutually exclusive. The core validates each artifact ID/type
against the inventory and never invents `eligible=False`.

## Ledger artifact mapping

Operating ledger entries (`fix-applied`, `built-not-operating`, and `resolved`)
must declare a non-empty `artifact_ids` list of stable runtime artifact IDs.
Coverage verification matches only those IDs. `wired_check` remains a
human-readable description of the independent operating check and is never
interpreted as an artifact ID. Ledger updates that receive a `LedgerEntry`
preserve and serialize its declared `artifact_ids`. `ledger.update_ledger()`
is the only supported serializer after host-authorized status changes; the
diagnosis run never mutates the ledger.

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

`scripts/digest.py` is an internal typed implementation module. Running it
directly, or calling its retired writer helpers, fails closed; use the typed
`scripts/reflect_setup.py` entry point so event-time filtering and manifest
completeness checks cannot be bypassed.

A later invocation with the same run directory reuses its complete manifest
only after validating schema, runtime, scope, source paths and SHA-256 hashes,
and every digest path's existence, run-directory containment, and persisted
digest hash. Changed source or digest bytes, deleted files, path escapes, or
tampered manifest metadata fail closed.

Each retained signal in a digest carries its source identity directly on the
rendered line:

```json
{"project":"project-a","session_id":"session-a","source_kind":"user","source_line":7,"text":"signal text","timestamp":"2026-08-23T10:00:00Z"}
```

`source_line` always means the original JSONL line. It is not the rendered
digest's local line number. Each signal record is one physical line and JSON
escaping preserves arbitrary project, session, and text values. `source_kind`
is the runtime extraction kind (`user`, `error`, or `interrupt`). The miner's
`finding_type` and report evidence `kind` are separate classifications
(`correction`, `friction`, `failure`, or `complaint`); evidence `kind` must match
the selected `finding_type`, not copy `source_kind`. Miners must copy
`source_line`, `timestamp`, `project`, and `session_id` from the JSON record,
then use the manifest evidence index as the validation check before returning a
report. The manifest index's persisted `kind` field is this source kind.

## Dispatch plan and batches

For every signal-bearing run with no complete miner report set, the run writes
`dispatch-plan.json` atomically and stops at `awaiting-miners`. The plan is
the replayable handoff artifact: it records the runtime, run ID, the lowercase
SHA-256 of the persisted manifest bytes, and one `DispatchBatch` per batch.
Each batch has a `batch_id` (`batch-001`, `batch-002`, …), the exact ordered
digest paths assigned to it, the absolute `report_path` the host must write
(`<out>/miner-reports/<batch-id>.json`), and the `total_bytes` sum of the
current digest-file sizes in that batch.

Batching is byte-balanced: inputs sort by descending byte size then path, each
is assigned to the currently smallest batch (batch ID breaks load ties), and
paths inside each batch are sorted. `--miner-batches COUNT` (default `8`,
minimum `1`) caps the batch count; the actual count is
`min(COUNT, digest_count)`.

The host reads each `DispatchBatch`, sends its exact metadata plus
`references/miner-prompt.md` to one worker, and writes the returned JSON to
that batch's prescribed `report_path` before rerunning the same command. The
CLI prints `stage=awaiting-miners` with the exact missing report paths and
exits `0`; this is an expected pause, not a validation error.

On continuation the persisted plan is authoritative. The run revalidates its
runtime, run ID, manifest SHA-256, exact digest-path partition, byte totals,
and run-directory-contained report paths before reading any report. Each
report is parsed against its persisted `DispatchBatch`; a report can never
declare its own assignment. A changed `--miner-batches` value, a changed
manifest, any changed digest size/hash, a report path outside the run
directory, and any unplanned `.json` file in `miner-reports/` fail closed.
Missing reports keep the run at `awaiting-miners`; malformed, extra,
overlapping, or tampered reports still fail closed. The miner reports and the
later reconciliation report are revalidated on every continuation, including
Apply preview and proof validation.

A complete manifest with no signal-bearing digests skips dispatch and
reconciliation, writes `reflection-report.json` with an empty candidate list,
and returns `diagnosis-complete`.

## Miner contract

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
matching `kind`, explicit `project` identity, and a positive
`occurrence_count`. The finding count must equal the sum of distinct evidence
counts; overlapping reports count each evidence key once.

The manifest also stores a source evidence index for every retained signal. A
miner reference's `source_line`, `timestamp`, and `project`, together with its
finding's `session_id`, must match the indexed entry exactly. Evidence `kind`
is the selected finding classification: it must equal `finding_type` and be
compatible with the indexed `source_kind` (the persisted index `kind` field):
`user` accepts any finding type, `error` accepts only `failure`, and
`interrupt` accepts only `friction` or `failure`. Nonexistent lines, invented
metadata, incompatible classifications, and mixed-session findings fail
closed.

The report's ordered `digest_paths` must equal its assigned `DispatchBatch`.
Across all reports, each non-empty manifest digest path must occur in exactly
one batch. Unknown, overlapping, missing, malformed, or out-of-batch evidence
fails closed before clustering. An empty manifest has no miner work and accepts
an empty report set.

## Clustering and verification

Validated reports merge only true duplicates sharing normalized cluster,
finding type, and session. Evidence identity remains `(digest_path, source_line)`
for aggregation, while its timestamp/project/session metadata is bound to the
manifest source evidence index. Classified evidence `kind` must equal the
finding type and satisfy the indexed source-kind mapping; project identity is
never inferred from filenames.

For each touched operating ledger entry, evaluate three independent checks
against its declared `artifact_ids` and human-readable `wired_check`:

1. **Symptom** — explicit `symptom-absent` passes; current recurrence fails.
2. **Invocation** — `invoked` proves the artifact was loaded/called.
3. **Outcome** — a separate `outcome-pass` proves successful behavior;
   `outcome-fail` fails. Reusing invocation evidence cannot pass outcome.

`fix-applied` or `built-not-operating` becomes `resolved` only when all three
pass. Missing evidence leaves it unresolved; recurrence or failed invocation/
outcome recommends `built-not-operating`. A resolved entry regresses when its
symptom recurs. The ledger statuses are `new`, `fix-applied`,
`built-not-operating`, `monitor`, `wont-fix`, and `resolved`.

## Reconciliation

After all miner reports pass validation, every merged `Finding` receives a
stable `finding_id`: the lowercase SHA-256 of the canonical JSON payload
binding its sorted evidence pairs, finding type, normalized proposed key,
runtime, and session. Duplicate evidence pairs are rejected before hashing.
The run then writes `reconciliation-request.json` with `schema_version`,
runtime, run ID, and one item per finding: `finding_id`, proposed key, finding
type, paraphrase, occurrence count, projects, first seen, and last seen. No
transcript text is included.

When more than one finding exists and no reconciliation report is present, the
run stops at `awaiting-reconciliation` and tells the host to run one worker
with `references/reconciler-prompt.md`; its required output path is
`<out>/reconciliation-report.json`. A zero- or one-finding run skips this
stage. The reconciler may group or rename findings but cannot change counts,
classifications, projects, or evidence.

The report must contain exactly `schema_version`, runtime, run ID, and
`groups`. Each group contains exactly `cluster_key`, one-line `summary`,
one-line `rationale`, and `member_finding_ids`. Validation requires every
request ID to appear exactly once, no unknown ID, unique normalized kebab-slug
cluster keys, and report metadata matching the run. For a zero-finding run the
final candidate list is empty; for exactly one finding its normalized proposed
key and paraphrase become the candidate key and summary; for reconciled groups
the report's `cluster_key` and `summary` are used, with `rationale` retained as
audit metadata in the final candidate's rationale list. The validated grouping
is applied to the original findings before ranking, replacing exact free-form
key matching across batches while preserving the manifest-bound evidence under
every merged group.

## Ranking

For each cluster, expose occurrences, sessions, projects, distinct days,
analyzed sessions, first/last seen, occurrences per 100 sessions, regression
count, confidence, and impact. Confidence and impact are derived from the
grouped findings themselves as occurrence-weighted means; callers never
supply either value. The fixed score is:

```text
round(
  0.30 * min(occurrences / 10, 1.0)
+ 0.20 * min(sessions / 5, 1.0)
+ 0.15 * min(projects / 3, 1.0)
+ 0.15 * min(distinct_days / 5, 1.0)
+ 0.10 * confidence
+ 0.10 * impact
+ 0.10 * min(regression_count, 1),
  4,
)
```

Impact is fixed by finding type: failure `1.0`, complaint `0.9`, correction
`0.8`, friction `0.7`; mixed clusters use the occurrence-weighted average.
`regression_count` is the number of currently failed ledger verifications for
the normalized cluster; it is a priority bonus, not a penalty, so a fix that
failed in real use rises rather than falls. Ties sort by score descending,
regression bonus descending, first-seen date, then cluster key. There is no
implementation-cost field: the orchestrator cannot know it, and implementation
effort belongs in the later human-approved Apply request.

Every ranked item carries `cluster_key`, `summary`, sorted `finding_types`,
sorted unique `paraphrases`, and deduplicated `evidence` (by
`(session_id, digest_path, source_line)`, sorted by timestamp, project,
session ID, digest path, and source line) so each score explains itself.

## Report output

The orchestrator writes `reflection-report.json` atomically beside the digest
manifest. It includes the manifest, runtime inventory, coverage records,
verification results, ranked candidates, and Apply approval metadata. The
report is not a substitute for host notes or ledger updates. Python writes no
notes or ledger state, and never writes a report when the digest is
incomplete. Pass `--ledger PATH` (or `ledger_path=...` in the API) when
explicit ledger verification is wanted; no implicit current-directory ledger
is read.

Plain CLI output starts with `stage=<stage> runtime=<runtime> run_id=<run-id>`
and then prints only the paths relevant to the current pause: `missing=...`
lines at `awaiting-miners`, the `reconciliation_request=...` path at
`awaiting-reconciliation`, and `report=...` at the terminal stages. `--json`
always prints one result envelope with exactly `schema_version`, `stage`,
`runtime`, `run_id`, `manifest_path`, `dispatch_plan_path`,
`reconciliation_request_path`, `report_path`, and `missing_paths`; unavailable
paths are `null` and `missing_paths` is an array. The full diagnosis remains in
`reflection-report.json` rather than changing the `--json` shape between
stages.

`reflection-notes.md` is host-owned. Its existing per-run scope, method,
candidate, and applied-proof sections are the only notes format; the Python
core never writes or rewrites it.

## Apply

Apply is opt-in and per cluster. `--apply` without `--host-input PATH` fails
before any fixer work. The host-input file is one strict JSON object with
exactly `schema_version`, `coverage_observations`, and `apply`; every object
rejects unknown keys, every named array is required even when empty, and
duplicate JSON keys at any nesting depth are errors. Diagnosis uses
`"apply": null`; preview uses a non-null Apply object with `"proofs": []`;
proof-bearing input supplies validated proofs. The Apply object contains
arrays for `approved_clusters`, `requests`, `workspaces`, and `proofs`, each
with an explicit `cluster_id` so duplicate normalized IDs are rejected instead
of hidden by object-key replacement. Request paths remain project-relative;
workspace roots are absolute; dirty paths, conflict state, commands,
verification commands, changed paths, output, and pass/fail values map
directly to the Apply dataclasses. `--apply` is the explicit command-line
safety signal: a non-null Apply input without `--apply` is rejected, and
`--apply` without at least one approved cluster plus a matching request and
workspace is rejected. An empty proofs array creates only `apply-preview`;
supplied valid proofs create `apply-validated`. Python continues to preview
and validate only — it never executes the listed commands or edits the target
project.

The API accepts the same strict input as a typed value:

```python
run_reflection(
    ...,
    apply=True,
    host_input=host_input,
    coverage_observations=coverage_observations,
)
```

Approval IDs, candidate IDs, request IDs, workspace keys, and proof IDs are
normalized to stable kebab slugs. Duplicate or ambiguous normalized IDs fail
closed. The result exposes `stage`, `apply_previews` (and the legacy
first-preview `apply_preview` field). Previews are created exactly for
approved clusters; scope overlaps fail before any host execution. A `FixProof`
is optional for a preview-only pass, but every supplied proof is checked with
`validate_proof()` before Python reports it as validated. Python never
executes fixer commands or changes project files.

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
PYTHONPATH=scripts python3 scripts/test_workflow_contract.py
PYTHONPATH=scripts python3 scripts/test_reconciliation.py
PYTHONPATH=scripts python3 scripts/test_prompt_conformance.py
PYTHONPATH=scripts python3 scripts/test_evaluate.py
PYTHONPATH=scripts python3 scripts/test_reflect_setup.py
```

The evaluation gate drives both runtime adapters through the complete staged
API — digest, persisted dispatch plan, golden miner reports, reconciliation,
and ranking — and requires extraction and cluster precision/recall `1.0`, zero
evidence/reason violations, complete batch and reconciliation coverage,
deterministic ordering, the stage sequence
`awaiting-miners → awaiting-reconciliation → diagnosis-complete`, and
`expected_matches: true`:

```bash
PYTHONPATH=scripts python3 scripts/evaluate.py --fixtures fixtures/evaluation
```
