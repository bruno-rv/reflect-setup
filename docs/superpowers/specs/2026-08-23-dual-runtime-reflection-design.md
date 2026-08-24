# Dual-Runtime Reflect-Setup Design

**Approved direction:** August 23, 2026

## Goal

Make `reflect-setup` one canonical, source-controlled skill that operates in
both Claude Code and Codex, while making its findings reproducible,
machine-checkable, and safe to apply. The skill continues to diagnose how the
user actually works; it does not become a generic configuration linter.

## Scope

This design covers the ten approved improvements:

1. A shared core with explicit Claude and Codex runtime adapters.
2. Event-timestamp filtering instead of whole-file modification-time
   filtering.
3. Fresh, collision-free, complete digest inputs with a run manifest.
4. A structured JSON miner contract with evidence references and validation.
5. Separate symptom, invocation, and behavioral-outcome verification.
6. Trend metrics and deterministic candidate scoring.
7. Coverage states that distinguish declared, eligible, triggered, and
   prevented behavior.
8. Per-cluster Apply previews, approvals, bounded write scopes, and proof.
9. A synthetic evaluation corpus covering both transcript formats and the
   false-positive rules.
10. Documentation and installation checks that prevent repository/installed
    skill drift.

Privacy, redaction, and retention work is explicitly excluded. Existing
truncation and local scratch-data behavior remain unchanged unless a later
feature defines a separate privacy requirement.

## Current baseline and compatibility decision

The repository currently describes a Claude-only skill and has a
stdlib-only `scripts/digest.py` that scans Claude Code project transcripts.
The installed Codex copy has diverged from the repository. This feature makes
the repository authoritative and supports both runtimes from the same source:

| Concern | Claude Code | Codex | Shared rule |
| --- | --- | --- | --- |
| Session root | `~/.claude/projects/` | `~/.codex/sessions/` | Runtime adapter resolves the default; `--source-root` overrides it for tests. |
| Session format | JSONL records with `type: "user"` and `message.role: "user"` | JSONL records such as `session_meta`, `response_item`, and `event_msg` | Parser accepts only the runtime's user-originated signal records. |
| Canonical user scope | Project transcripts; `subagents/` excluded by default | `session_meta.payload.thread_source == "user"`; subagent/agent threads excluded by default | `--include-subagents` is the explicit opt-in for all thread sources. |
| Inventory roots | `~/.claude/skills`, project `.claude/skills`, commands, agents, settings | `~/.codex/skills`, project `.codex/skills`, agents, config | Inventory records where coverage is declared and whether it operated. |
| Skill install root | `~/.claude/skills/reflect-setup` | `~/.codex/skills/reflect-setup` | Both point to the same repository checkout or a generated mirror with a manifest. |
| User entry point | `/reflect-setup [days] [project-filter]` | `/reflect-setup [days] [project-filter]` | The workflow and flags are runtime-neutral. |

`SKILL.md`, `REFERENCE.md`, and `references/miner-prompt.md` become
runtime-neutral control-plane documents. The Python core never assumes that a
Claude or Codex tool name is available; each host-specific document says how
to dispatch miners and fixers using that host's task mechanism.

## Architecture

The implementation is a small stdlib-only Python core plus host instructions:

```text
runtime adapter
      |
      v
session discovery -> event-time digest -> run manifest
                                      |
                                      v
                           disjoint miner batches
                                      |
                                      v
                          validated JSON miner reports
                                      |
                                      v
               merge/cluster -> coverage -> fix verification
                                      |
                                      v
                         trends and candidate ranking
                                      |
                                      v
                  notes/ledger update -> opt-in Apply preview
```

The runtime adapter is the only layer that knows where a host stores
transcripts or coverage declarations. Digest extraction is deterministic and
does not call an LLM. Miners see only digest files and return JSON. The main
skill validates and merges those reports before any decision or Apply step.

All public Python functions use `pathlib.Path`, UTC-aware `datetime`, frozen
dataclasses, and explicit return values. Failures are represented by typed
exceptions or status values; a partial scan is never presented as complete.

## Runtime and installation contracts

`scripts/runtime.py` owns runtime resolution, session discovery, inventory
locations, and source installation. The interfaces are:

```python
class Runtime(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"


@dataclass(frozen=True, slots=True)
class Scope:
    since: datetime
    project_filter: str | None
    include_subagents: bool


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    runtime: Runtime
    home: Path
    source_root: Path
    skill_root: Path
    inventory_roots: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class SessionRef:
    runtime: Runtime
    source_path: Path
    relative_path: str
    session_id: str
    project: str


def resolve_runtime(
    name: str | None,
    *,
    home: Path,
    env: Mapping[str, str],
    source_root: Path | None = None,
) -> RuntimeSpec:
    """Resolve explicit or auto runtime without touching the filesystem.

    ``source_root`` overrides the selected runtime's default session root for
    tests and controlled operator runs; it does not change the runtime
    parser or inventory roots.
    """


def discover_sessions(
    spec: RuntimeSpec,
    scope: Scope,
) -> tuple[SessionRef, ...]:
    """Return sorted, canonical-scope sessions; never use mtime as inclusion."""


def install_skill(
    spec: RuntimeSpec,
    source_root: Path,
    *,
    mode: Literal["symlink", "copy"] = "symlink",
) -> InstallResult:
    """Install only under spec.skill_root and refuse an unrelated collision."""
```

Auto resolution uses explicit command-line selection first, then the runtime
that has a readable default session root. If both roots exist, the command
fails with an actionable ambiguity message unless the user supplies
`--runtime claude` or `--runtime codex`; it never mines both by accident.
`--source-root` is a test/operator override and is still parsed according to
the selected runtime.

The installer creates a symlink by default. A copy mode writes a
`.reflect-setup-source.json` manifest containing source commit, source path,
runtime, and file hashes. Reinstalling from the same source is idempotent;
replacing a non-symlink/non-manifest directory fails without deleting it.

## Scope and completeness model

The effective scope is a `Scope` resolved once at the beginning of a run.
Every JSONL file under the selected runtime root is a candidate. Filesystem
mtime may be used to order work, but it is never used to exclude a file from
the requested time window. Inclusion is decided per event timestamp.

Timestamp policy:

- RFC 3339 timestamps with `Z` or an explicit offset are converted to UTC.
- Unix seconds are accepted only when the runtime record declares a numeric
  timestamp field.
- An event without a parseable timestamp is counted as `untimestamped` and is
  excluded from the time window.
- A file that cannot be read or contains malformed JSON is counted in the
  manifest and makes the run `incomplete`; the command exits nonzero after
  writing the manifest. It does not silently mine the remaining files as a
  complete run.
- A project filter is matched against the normalized runtime project name,
  not arbitrary line content.

`DigestManifest` is written before miner dispatch and contains:

```python
@dataclass(frozen=True, slots=True)
class SourceFile:
    source_path: str
    sha256: str
    scanned: bool
    readable: bool
    json_lines: int
    malformed_lines: int
    untimestamped_lines: int
    in_scope_events: int
    digest_path: str | None


@dataclass(frozen=True, slots=True)
class DigestManifest:
    schema_version: int
    run_id: str
    runtime: Runtime
    scope: Scope
    source_files: tuple[SourceFile, ...]
    sessions_scanned: int
    sessions_with_signals: int
    signal_counts: Mapping[str, int]
    complete: bool
```

The output directory must not exist or must be empty. Each run gets a unique
run id and digest names include a stable hash of the runtime-relative source
path, so nested sessions with the same basename cannot overwrite each other.
Writes use a temporary file followed by `os.replace`; an existing digest is
never overwritten. The manifest lists every scanned source file and every
digest file, allowing the main agent to prove that miner batches are disjoint
and collectively cover the complete input set.

## Signal extraction

`scripts/digest.py` retains only the existing import-compatibility wrapper
`signals_from_line(raw_line)`; its retired writer workflow and direct CLI fail
closed. The public entry point is the typed `scripts/reflect_setup.py`
manifest workflow. New runtime-aware extraction is exposed as:

```python
class SignalKind(str, Enum):
    USER = "user"
    ERROR = "error"
    INTERRUPT = "interrupt"


@dataclass(frozen=True, slots=True)
class Signal:
    kind: SignalKind
    timestamp: datetime
    session_id: str
    source_line: int
    text: str


def signals_from_event(
    raw_line: str,
    *,
    runtime: Runtime,
    session_id: str,
    source_line: int,
    scope: Scope,
) -> tuple[Signal, ...]:
    """Extract only in-scope user/error/interrupt signals for one event."""


def run_digest(
    spec: RuntimeSpec,
    scope: Scope,
    out_dir: Path,
) -> DigestManifest:
    """Scan all candidates, write collision-free digests, and return coverage."""
```

Claude extraction keeps the current false-positive guard: inspect only
`type == "user"` with `message.role == "user"`, retain user text, true error
tool results, and interrupt markers, and ignore assistant text and successful
tool output. Codex extraction inspects user-originated `response_item` and
`event_msg` records, excludes developer/system/assistant/tool content, and
uses the session metadata to enforce the canonical `thread_source == "user"`
scope unless `include_subagents` is true. Both adapters emit the same `Signal`
type and apply the existing 500-character user / 300-character error limits.

## Structured miner contract

`references/miner-prompt.md` instructs both hosts to return one JSON document,
never prose. `scripts/miner_contract.py` validates the document against the
manifest and batch assignment before merging it.

```python
class FindingType(str, Enum):
    CORRECTION = "correction"
    FRICTION = "friction"
    FAILURE = "failure"
    COMPLAINT = "complaint"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    digest_path: str
    source_line: int
    timestamp: datetime
    kind: FindingType
    project: str
    occurrence_count: int = 1


@dataclass(frozen=True, slots=True)
class Finding:
    cluster_key: str
    finding_type: FindingType
    session_id: str
    paraphrase: str
    occurrence_count: int
    confidence: float
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True, slots=True)
class MinerReport:
    schema_version: int
    runtime: Runtime
    run_id: str
    batch_id: str
    digest_paths: tuple[str, ...]
    findings: tuple[Finding, ...]
    themes: tuple[str, ...]
```

`BatchSpec` identifies the digest paths assigned to a miner:

```python
@dataclass(frozen=True, slots=True)
class BatchSpec:
    batch_id: str
    digest_paths: tuple[str, ...]
```

The JSON report includes `digest_paths`; `parse_report(raw_json, manifest,
batch)` requires that list to exactly match `batch.digest_paths`. It rejects
unknown keys, duplicate
evidence references, unknown digest paths, evidence outside the batch,
non-positive counts, confidences outside `0.0 <= confidence <= 1.0`, malformed
timestamps, and any report that is not a JSON object. It also rejects a
report that contains prose before or after the JSON object. The validator
does not redact or retain additional content.

Merging is deterministic: evidence references are deduplicated by
`(digest_path, source_line)`, findings are grouped by normalized
`cluster_key`, counts are summed only for distinct evidence, and output is
sorted by cluster key, first evidence timestamp, and session id. Batch
partitioning is validated before the merge; overlapping or missing digest
files stop the run. Only manifest entries with a non-null `digest_path` are
required to belong to a miner batch; source files with no in-scope signals
remain accounted for in the manifest but do not create empty batch work.

## Inventory and coverage states

The inventory is a runtime-specific view of skills, commands/agents, hooks,
permissions, and configuration. It records four separate states for every
candidate artifact:

```python
@dataclass(frozen=True, slots=True)
class CoverageRecord:
    artifact_id: str
    artifact_kind: str
    declared: bool
    eligible: bool
    triggered: bool
    prevented: bool | None
    evidence: tuple[EvidenceRef, ...]
    detail: str
    trigger_evidence: tuple[EvidenceRef, ...]
    prevention_evidence: tuple[EvidenceRef, ...]
```

Hosts supply typed `CoverageObservation` values containing the artifact ID and
kind, explicit eligibility, trigger evidence, prevention evidence, and symptom
recurrence. The core validates those IDs/types against the inventory and never
invents eligibility from declaration state.

`declared` means the artifact exists in a configured inventory location.
`eligible` means the runtime's trigger and scope rules would have selected it
for the observed request. `triggered` requires transcript evidence that the
artifact was invoked or loaded. `prevented` is true only when an independent
outcome check shows that the associated symptom did not occur; it is false
when the symptom recurred and `None` when no outcome evidence exists.

These states prevent “a skill file exists” from being treated as “the skill
operated.” Existing coverage is considered during the decision step, but a
declared artifact with `triggered == false` can still produce a candidate for
repair or routing.

## Fix-wiring verification and ledger semantics

Every `fix-applied` ledger entry gets three explicit checks. Its stable
`artifact_ids` identify the runtime artifacts whose typed coverage may satisfy
invocation and outcome checks; `wired_check` remains human-readable prose.

```python
class LedgerStatus(str, Enum):
    NEW = "new"
    FIX_APPLIED = "fix-applied"
    BUILT_NOT_OPERATING = "built-not-operating"
    MONITOR = "monitor"
    WONT_FIX = "wont-fix"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    cluster_id: str
    status: LedgerStatus
    wired_check: str
    artifact_ids: tuple[str, ...]


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    ref: EvidenceRef
    label: Literal[
        "symptom-absent",
        "symptom-recurred",
        "invoked",
        "outcome-pass",
        "outcome-fail",
    ]


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    name: Literal["symptom", "invocation", "outcome"]
    status: CheckStatus
    evidence: tuple[EvidenceRef, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class FixVerification:
    cluster_id: str
    symptom: VerificationCheck
    invocation: VerificationCheck
    outcome: VerificationCheck
    overall: CheckStatus
    recommended_status: str


def verify_fix(
    entry: LedgerEntry,
    *,
    symptom_evidence: tuple[VerificationEvidence, ...],
    invocation_evidence: tuple[VerificationEvidence, ...],
    outcome_evidence: tuple[VerificationEvidence, ...],
) -> FixVerification:
```

The checks mean:

- `symptom`: the recurrence signal is absent or present in the current
  window, according to the entry's `wired_check`.
- `invocation`: the changed skill, rule, hook, script, or configuration was
  actually loaded or invoked, not merely found in the inventory.
- `outcome`: a separate success or behavior signal demonstrates the intended
  result; it is not inferred from the file's presence.

State transitions are strict:

| Current status | Evidence | Next status |
| --- | --- | --- |
| `fix-applied` | invocation missing or symptom recurs | `built-not-operating` |
| `fix-applied` | any check insufficient, with no recurrence | `fix-applied` |
| `fix-applied` | all three checks pass, including positive non-recurrence evidence | `resolved` |
| `built-not-operating` | all three checks pass in a later run | `resolved` |
| `resolved` | symptom recurs | `built-not-operating` |

Absence of a mined signal alone never resolves a cluster. The ledger example
and validator add `built-not-operating` to the accepted statuses and require a
`wired_check` plus non-empty `artifact_ids` for `fix-applied`,
`built-not-operating`, and `resolved` entries.

## Trends and ranking

Each merged cluster receives a stable `TrendMetrics` record:

```python
@dataclass(frozen=True, slots=True)
class TrendMetrics:
    occurrences: int
    sessions: int
    projects: int
    distinct_days: int
    analyzed_sessions: int
    first_seen: datetime
    last_seen: datetime
    occurrences_per_100_sessions: float
    regression_count: int
    confidence: float
    impact: float
    implementation_cost: Literal["S", "M", "L"]


@dataclass(frozen=True, slots=True)
class CandidateScore:
    cluster_key: str
    score: float
    metrics: TrendMetrics
    rationale: tuple[str, ...]
```

The score is transparent and stable:

```text
score = round(
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

Impact is fixed by signal type (`failure=1.0`, `complaint=0.9`,
`correction=0.8`, `friction=0.7`) and averaged for mixed clusters. Ties sort
by score descending, then regression count ascending, then first-seen date,
then cluster key. The notes display raw counts, affected projects, dates,
rate, regression count, confidence, impact, and build cost so a reviewer can
challenge the ranking.

## Apply safety contract

Diagnosis writes only the existing notes, ledger, manifest, and scratch
digests. Apply is opt-in and per cluster. `scripts/apply.py` provides:

```python
@dataclass(frozen=True, slots=True)
class WorkspaceState:
    project_root: Path
    dirty_paths: tuple[Path, ...]
    conflicted: bool


@dataclass(frozen=True, slots=True)
class ApplyRequest:
    cluster_id: str
    target_paths: tuple[Path, ...]
    commands: tuple[str, ...]
    verification_commands: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApplyPreview:
    request: ApplyRequest
    workspace_state: WorkspaceState
    disjoint_scope: bool
    expected_diff: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class FixProof:
    cluster_id: str
    changed_paths: tuple[Path, ...]
    command_output: tuple[str, ...]
    verification_output: tuple[str, ...]
    passed: bool


def build_preview(
    request: ApplyRequest,
    *,
    inventory: Inventory,
    workspace: WorkspaceState,
) -> ApplyPreview:


def validate_proof(
    preview: ApplyPreview,
    proof: FixProof,
) -> None:
```

The host asks for approval separately for each preview, displaying exact
paths, commands, current dirty paths, and verification commands. A preview is
rejected if a target path is outside the selected project, paths overlap a
parallel preview, or the workspace has an unresolved conflict. Existing dirty
paths are captured before execution and may remain untouched; the proof
validator accepts only new changes inside the approved scope. A fixer that
changes an unapproved path, cannot show command output, or fails a verification
command is not marked applied. The main agent shows the resulting diff and
verification output before updating `clusters.yaml`.

## Evaluation corpus

The committed evaluation corpus is synthetic and contains no real session
transcript. It covers:

- Claude user text, true error results, assistant text, successful tool output,
  interrupt markers, stale mtime with recent event timestamps, and nested
  duplicate basenames.
- Codex `session_meta`, canonical `response_item`/`event_msg` user events,
  subagent metadata, developer messages, assistant messages, and tool errors.
- Malformed JSON, unreadable-source accounting, overlapping/missing miner
  batches, invalid structured reports, all three fix checks, regression
  scoring, coverage states, and Apply path violations.

`scripts/evaluate.py` runs both adapters against the fixtures and reports
precision, recall, false-positive count, batch coverage, report-validation
failures, and deterministic-score checks. A fixture changes only with an
updated expected-output file and a passing evaluation run.

## File manifest

The implementation is limited to these paths:

```text
SKILL.md
README.md
REFERENCE.md
clusters.example.yaml
references/miner-prompt.md
scripts/runtime.py
scripts/install.py
scripts/digest.py
scripts/miner_contract.py
scripts/coverage_model.py
scripts/ledger.py
scripts/verification.py
scripts/scoring.py
scripts/apply.py
scripts/evaluate.py
scripts/reflect_setup.py
scripts/test_runtime.py
scripts/test_install.py
scripts/test_digest.py
scripts/test_miner_contract.py
scripts/test_coverage_model.py
scripts/test_ledger.py
scripts/test_verification.py
scripts/test_scoring.py
scripts/test_apply.py
scripts/test_evaluate.py
scripts/test_reflect_setup.py
fixtures/evaluation/claude/canonical.jsonl
fixtures/evaluation/claude/subagent.jsonl
fixtures/evaluation/codex/canonical.jsonl
fixtures/evaluation/codex/subagent.jsonl
fixtures/evaluation/expected.json
```

No other files are required for the feature. Generated manifests, digests,
notes, ledgers, and reports remain local runtime artifacts and stay ignored by
the repository's existing policy.

## Acceptance criteria

The feature is ready when all of the following are true:

- The same checkout can be installed into both runtime skill roots, and the
  installer detects a source/installed hash mismatch.
- Claude and Codex fixtures produce the same normalized signal kinds and
  canonical-user filtering behavior for equivalent events.
- A recent event in an old-mtime file is included, and an old event in a
  recent-mtime file is excluded.
- Every digest run uses a new or empty output directory, never overwrites an
  existing digest, and writes a complete manifest; malformed or unreadable
  input produces an incomplete, nonzero run.
- Miner reports are JSON, schema-valid, batch-scoped, and evidence-backed;
  invalid reports cannot enter clustering.
- Coverage output distinguishes declared, eligible, triggered, and prevented.
- A fix cannot become `resolved` without passing symptom, invocation, and
  independent outcome checks; recurrence regresses it to
  `built-not-operating`.
- Ranking exposes trend metrics and produces identical scores for identical
  input.
- Apply previews are per cluster, show exact scope and commands, reject dirty
  out-of-scope changes, and require command plus verification output.
- The synthetic evaluation corpus passes for both runtimes.
- Existing direct digest tests continue to pass, and the root-level test
  command uses an explicit `PYTHONPATH=scripts` so import behavior is
  reproducible.
