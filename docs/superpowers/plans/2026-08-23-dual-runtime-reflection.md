# Dual-Runtime Reflect-Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `reflect-setup` into one source-controlled skill that runs in
both Claude Code and Codex and produces complete, evidence-backed,
machine-checkable reflection runs with safe opt-in Apply behavior.

**Architecture:** Keep the skill's host instructions in `SKILL.md`,
`REFERENCE.md`, and `references/miner-prompt.md`, and add a small stdlib-only
Python core under `scripts/`. Runtime adapters normalize Claude and Codex
session formats into one event model; deterministic digest/manifest code feeds
validated JSON miner reports, which are merged into coverage, verification,
trend, ledger, and Apply decisions. A synthetic fixture corpus is the
cross-runtime regression gate.

**Tech Stack:** Python 3.11+ standard library, JSONL, JSON, Markdown, the
existing YAML ledger format, assert-based test scripts, and the existing
Claude Code/Codex skill and task dispatch mechanisms. No new third-party
runtime dependency is introduced.

**Spec:**
`docs/superpowers/specs/2026-08-23-dual-runtime-reflection-design.md`

## Global Constraints

- Support both `~/.claude/projects/` and `~/.codex/sessions/` from one
  checkout; explicit `--runtime claude|codex` overrides auto-detection.
- Default to canonical user sessions; `--include-subagents` is the only
  opt-in for subagent/sidechain material.
- Use event timestamps for inclusion in the time window; mtime may order work
  but must never exclude a file.
- Do not treat an unreadable or malformed source set as complete; record it in
  the manifest and exit nonzero after writing the manifest.
- Require a new or empty output directory, collision-free digest names, and a
  manifest proving source and miner-batch coverage.
- Keep the existing user/error truncation limits: 500 characters for user
  text and 300 characters for error text.
- Miners return JSON only; every finding must reference an in-scope digest line
  in its assigned batch.
- A fix is `resolved` only when symptom, invocation, and independent outcome
  checks all pass; missing evidence is not proof of resolution.
- Diagnosis remains non-mutating except for the existing local notes, ledger,
  manifest, and scratch digests; Apply remains opt-in and per cluster.
- Privacy, redaction, and retention work is out of scope. Do not add a secret
  scanner, PII scrubber, retention job, or raw-mode policy in these tasks.
- Preserve unrelated dirty paths and reject Apply changes outside the approved
  path scope.
- Keep the direct `python3 scripts/test_*.py` test style and run aggregate
  tests with `PYTHONPATH=scripts`.
- Commit each task's exact files with `git commit --only`; do not commit
  generated manifests, notes, ledgers, or scratch digests.

---

## File structure

The implementation creates or modifies only the paths below. Each module has
one responsibility and all imports work when `PYTHONPATH=scripts` is set.

```text
SKILL.md                              # dual-host entry point and workflow
README.md                             # install and usage for both hosts
REFERENCE.md                          # runtime-neutral operational detail
clusters.example.yaml                 # ledger schema and status semantics
references/miner-prompt.md            # JSON-only miner contract
scripts/runtime.py                    # runtime selection, discovery, install
scripts/install.py                    # CLI wrapper for source installation
scripts/digest.py                     # timestamp-aware normalized digest
scripts/test_support.py               # deterministic fixtures shared by tests
scripts/miner_contract.py             # report schema and batch validation
scripts/coverage_model.py             # inventory and four coverage states
scripts/ledger.py                     # ledger validation and transitions
scripts/verification.py               # three independent fix checks
scripts/scoring.py                    # trends and deterministic ranking
scripts/apply.py                      # preview, scope, proof validation
scripts/evaluate.py                   # fixture evaluation harness
scripts/reflect_setup.py              # end-to-end orchestration and CLI
scripts/test_runtime.py               # runtime adapter tests
scripts/test_install.py               # installer tests
scripts/test_digest.py                # existing and new digest tests
scripts/test_miner_contract.py        # structured report tests
scripts/test_coverage_model.py        # inventory/coverage tests
scripts/test_ledger.py                # schema and state transition tests
scripts/test_verification.py          # three-check verification tests
scripts/test_scoring.py               # metrics and ranking tests
scripts/test_apply.py                 # Apply safety/proof tests
scripts/test_evaluate.py              # corpus/harness tests
scripts/test_reflect_setup.py         # end-to-end run tests
fixtures/evaluation/claude/canonical.jsonl
fixtures/evaluation/claude/subagent.jsonl
fixtures/evaluation/codex/canonical.jsonl
fixtures/evaluation/codex/subagent.jsonl
fixtures/evaluation/expected.json
```

## Shared interfaces

The tasks below use these exact names and types. Implementers must preserve
the signatures so tasks can be executed independently.

```python
# scripts/runtime.py
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


@dataclass(frozen=True, slots=True)
class InstallResult:
    runtime: Runtime
    mode: Literal["symlink", "copy"]
    target: Path
    source_hash: str


def resolve_runtime(
    name: str | None,
    *,
    home: Path,
    env: Mapping[str, str],
    source_root: Path | None = None,
) -> RuntimeSpec:
    raise NotImplementedError


def discover_sessions(
    spec: RuntimeSpec,
    scope: Scope,
) -> tuple[SessionRef, ...]:
    raise NotImplementedError


def install_skill(
    spec: RuntimeSpec,
    source_root: Path,
    *,
    mode: Literal["symlink", "copy"] = "symlink",
) -> InstallResult:
    raise NotImplementedError


# scripts/digest.py
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


def signals_from_event(
    raw_line: str,
    *,
    runtime: Runtime,
    session_id: str,
    source_line: int,
    scope: Scope,
) -> tuple[Signal, ...]:
    raise NotImplementedError


def run_digest(
    spec: RuntimeSpec,
    scope: Scope,
    out_dir: Path,
) -> DigestManifest:
    raise NotImplementedError


# scripts/miner_contract.py
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


@dataclass(frozen=True, slots=True)
class BatchSpec:
    batch_id: str
    digest_paths: tuple[str, ...]


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


def parse_report(
    raw_json: str,
    manifest: DigestManifest,
    batch: BatchSpec,
) -> MinerReport:
    raise NotImplementedError


# scripts/coverage_model.py
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


# scripts/verification.py
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


# scripts/apply.py
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


# scripts/ledger.py
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


# scripts/coverage_model.py
@dataclass(frozen=True, slots=True)
class InventoryItem:
    artifact_id: str
    artifact_kind: str
    path: Path
    declared: bool


@dataclass(frozen=True, slots=True)
class Inventory:
    items: tuple[InventoryItem, ...]


# scripts/verification.py
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


# scripts/scoring.py
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

The implementation may add private helpers, but it must not rename or weaken
the public interfaces.

The test files share deterministic fixture builders from
`scripts/test_support.py`. This keeps the test snippets concrete without
embedding real session data:

```python
# scripts/test_support.py
from datetime import datetime, timezone
from pathlib import Path


def make_manifest(run_id="run-1", files=("a.md",), lines=()):
    from digest import DigestManifest, SourceFile
    from runtime import Runtime, Scope

    line_map = {path: count for path, count, unused_timestamp, unused_kind in lines}
    records = tuple(
        SourceFile(
            source_path=path,
            sha256="0" * 64,
            scanned=True,
            readable=True,
            json_lines=line_map.get(path, 1),
            malformed_lines=0,
            untimestamped_lines=0,
            in_scope_events=line_map.get(path, 1),
            digest_path=path,
        )
        for path in files
    )
    return DigestManifest(
        schema_version=1,
        run_id=run_id,
        runtime=Runtime.CLAUDE,
        scope=Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False),
        source_files=records,
        sessions_scanned=len(files),
        sessions_with_signals=len(files),
        signal_counts={"user": len(files)},
        complete=True,
    )


def make_batch(batch_id, paths):
    from miner_contract import BatchSpec

    return BatchSpec(batch_id=batch_id, digest_paths=tuple(paths))


def make_report(batch_id, path, run_id="run-1"):
    from miner_contract import EvidenceRef, Finding, FindingType, MinerReport

    finding = Finding(
        cluster_key="fixture",
        finding_type=FindingType.FAILURE,
        session_id="session-a",
        paraphrase="fixture failure",
        occurrence_count=1,
        confidence=0.5,
        evidence=(EvidenceRef(path, 1, datetime(2026, 8, 23, tzinfo=timezone.utc), FindingType.FAILURE),),
    )
    return MinerReport(1, "claude", run_id, batch_id, (path,), (finding,), ())


def make_evidence(path="evidence.md", line=1, label="outcome-pass"):
    from miner_contract import EvidenceRef, FindingType

    return EvidenceRef(
        digest_path=path,
        source_line=line,
        timestamp=datetime(2026, 8, 23, tzinfo=timezone.utc),
        kind=FindingType.FAILURE,
    )


def make_evidence_set(label):
    from verification import VerificationEvidence

    return (VerificationEvidence(ref=make_evidence(label + ".md"), label=label),)


def make_verification(symptom, invocation, outcome):
    from verification import CheckStatus, FixVerification, VerificationCheck

    states = {"pass": CheckStatus.PASS, "fail": CheckStatus.FAIL, "insufficient": CheckStatus.INSUFFICIENT}
    checks = tuple(
        VerificationCheck(name=name, status=states[value], evidence=(), detail=value)
        for name, value in (("symptom", symptom), ("invocation", invocation), ("outcome", outcome))
    )
    overall = CheckStatus.FAIL if any(item.status is CheckStatus.FAIL for item in checks) else (
        CheckStatus.INSUFFICIENT if any(item.status is CheckStatus.INSUFFICIENT for item in checks) else CheckStatus.PASS
    )
    recommended = "resolved" if overall is CheckStatus.PASS else (
        "built-not-operating" if overall is CheckStatus.FAIL else "fix-applied"
    )
    return FixVerification("fixture", checks[0], checks[1], checks[2], overall, recommended)


def make_entry(cluster_id="fixture", status="fix-applied", wired_check="fixture evidence"):
    from ledger import LedgerEntry, LedgerStatus

    return LedgerEntry(cluster_id, LedgerStatus(status), wired_check)


def make_findings(occurrences, sessions, projects, dates, cluster_key="fixture"):
    from miner_contract import EvidenceRef, Finding, FindingType

    finding_values = []
    for index in range(occurrences):
        session_id = sessions[index % len(sessions)]
        project = projects[index % len(projects)]
        day = dates[index % len(dates)]
        finding_values.append(
            Finding(
                cluster_key=cluster_key,
                finding_type=FindingType.FAILURE,
                session_id=session_id,
                paraphrase="fixture failure in " + project,
                occurrence_count=1,
                confidence=0.8,
                evidence=(EvidenceRef(project + ".md", index + 1, datetime.fromisoformat(day).replace(tzinfo=timezone.utc), FindingType.FAILURE),),
            )
        )
    return tuple(finding_values)


def make_tied_candidates(keys):
    from scoring import compute_metrics, score_candidate

    return tuple(
        score_candidate(
            key,
            compute_metrics(make_findings(1, ("s1",), ("p1",), ("2026-08-23",), key), 1, 0, 0.5, "S"),
        )
        for key in keys
    )


def make_inventory():
    from coverage_model import Inventory, InventoryItem

    return Inventory((InventoryItem("skill:fixture", "skill", Path("SKILL.md"), True),))


def make_workspace(dirty=()):
    from apply import WorkspaceState

    return WorkspaceState(Path("."), tuple(dirty), False)


def make_request(cluster_id="fixture", target_paths=(Path("SKILL.md"),), commands=("true",), verification_commands=("true",)):
    from apply import ApplyRequest

    return ApplyRequest(cluster_id, tuple(target_paths), tuple(commands), tuple(verification_commands))


def make_preview(cluster_id="fixture", target_paths=(Path("SKILL.md"),)):
    from apply import ApplyPreview

    request = make_request(cluster_id=cluster_id, target_paths=target_paths)
    return ApplyPreview(request, make_workspace(), True, tuple(target_paths))


def make_proof(changed_paths, command_output, verification_output, passed):
    from apply import FixProof

    return FixProof("fixture", tuple(changed_paths), tuple(command_output), tuple(verification_output), passed)
```

Every test snippet that calls one of these builders begins with
`from test_support import <the builders used in that snippet>`; the builders
are created in Task 1 and reused by Tasks 3–9.

---

### Task 1: Add runtime adapters and source synchronization

**Files:**

- Create: `scripts/runtime.py`
- Create: `scripts/install.py`
- Create: `scripts/test_support.py`
- Create: `scripts/test_runtime.py`
- Create: `scripts/test_install.py`
- Test fixtures: temporary Claude and Codex directory trees created inside the
  tests; no committed runtime data

**Interfaces:**

- Consumes: a runtime name, `home`, environment mapping, and a `Scope`.
- Produces: `Runtime`, `RuntimeSpec`, `SessionRef`, `resolve_runtime()`,
  `discover_sessions()`, `install_skill()`, and `InstallResult` for Tasks 2–9.

- [ ] **Step 1: Write the failing runtime-resolution tests**

```python
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from runtime import Runtime, Scope, discover_sessions, resolve_runtime


def test_explicit_runtime_resolves_default_roots_without_writing():
    with TemporaryDirectory() as raw:
        home = Path(raw)
        spec = resolve_runtime("codex", home=home, env={})
        assert spec.runtime is Runtime.CODEX
        assert spec.source_root == home / ".codex" / "sessions"
        assert spec.skill_root == home / ".codex" / "skills" / "reflect-setup"


def test_source_root_override_changes_only_session_root():
    with TemporaryDirectory() as raw:
        home = Path(raw)
        override = Path(raw) / "fixture-sessions"
        spec = resolve_runtime("claude", home=home, env={}, source_root=override)
        assert spec.source_root == override
        assert spec.skill_root == home / ".claude" / "skills" / "reflect-setup"


def test_auto_runtime_rejects_ambiguous_existing_roots():
    with TemporaryDirectory() as raw:
        home = Path(raw)
        (home / ".claude" / "projects").mkdir(parents=True)
        (home / ".codex" / "sessions").mkdir(parents=True)
        try:
            resolve_runtime(None, home=home, env={})
        except RuntimeError as exc:
            assert "--runtime claude" in str(exc)
            assert "--runtime codex" in str(exc)
        else:
            raise AssertionError("ambiguous auto selection must fail")


def test_codex_discovery_keeps_canonical_user_threads_only():
    with TemporaryDirectory() as raw:
        home = Path(raw)
        root = home / ".codex" / "sessions" / "2026" / "08" / "23"
        root.mkdir(parents=True)
        (root / "user.jsonl").write_text(
            '{"timestamp":"2026-08-23T10:00:00Z","type":"session_meta",'
            '"payload":{"id":"user-1","thread_source":"user"}}\n'
        )
        (root / "agent.jsonl").write_text(
            '{"timestamp":"2026-08-23T10:00:00Z","type":"session_meta",'
            '"payload":{"id":"agent-1","thread_source":"subagent"}}\n'
        )
        spec = resolve_runtime("codex", home=home, env={})
        scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False)
        sessions = discover_sessions(spec, scope)
        assert [item.session_id for item in sessions] == ["user-1"]
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_runtime.py
```

Expected: `ModuleNotFoundError: No module named 'runtime'` because
`scripts/runtime.py` does not exist yet.

- [ ] **Step 3: Write the failing installer tests**

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from runtime import Runtime, install_skill, resolve_runtime


def test_install_symlink_points_to_source_and_is_idempotent():
    with TemporaryDirectory() as raw:
        home = Path(raw) / "home"
        source = Path(raw) / "repo"
        source.mkdir()
        (source / "SKILL.md").write_text("# reflect-setup\n")
        spec = resolve_runtime("claude", home=home, env={})
        first = install_skill(spec, source)
        second = install_skill(spec, source)
        assert first.mode == "symlink"
        assert second.source_hash == first.source_hash
        assert spec.skill_root.is_symlink()
        assert spec.skill_root.resolve() == source.resolve()


def test_install_refuses_unmanaged_existing_directory():
    with TemporaryDirectory() as raw:
        home = Path(raw) / "home"
        source = Path(raw) / "repo"
        source.mkdir()
        (source / "SKILL.md").write_text("# reflect-setup\n")
        spec = resolve_runtime("codex", home=home, env={})
        spec.skill_root.mkdir(parents=True)
        (spec.skill_root / "user-file.md").write_text("keep me\n")
        try:
            install_skill(spec, source)
        except FileExistsError as exc:
            assert "user-file.md" in str(exc)
        else:
            raise AssertionError("unmanaged directory must not be replaced")
```

- [ ] **Step 4: Run the installer tests and verify the expected failure**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_install.py
```

Expected: `ModuleNotFoundError: No module named 'runtime'` until the shared
runtime module and installer are implemented.

- [ ] **Step 5: Implement the minimal runtime adapter and installer**

Implement `Runtime` paths exactly as specified in the design. Claude discovery
walks `source_root` for `.jsonl` files and skips directories named
`subagents` unless `include_subagents` is true. Codex discovery reads the first
`session_meta` record, uses `payload.id` as the session id, and includes only
`payload.thread_source == "user"` by default. Sort sessions by
`(runtime, project, relative_path)`.

`resolve_runtime()` uses explicit selection first. Its optional `source_root`
override replaces only the selected runtime's session root and leaves parser
and inventory selection unchanged. In auto mode, it selects a
single readable existing root and raises `RuntimeError` with both explicit
flags when both roots exist. It does not create directories.

`install_skill()` computes a SHA-256 over sorted relative file paths and file
bytes. Symlink mode creates parent directories and a symlink only when the
target is absent or already points at the same source; a different symlink is
reported as a source/installed mismatch. Copy mode writes files and
`.reflect-setup-source.json` atomically, and refuses a different manifest
hash. A non-symlink directory without a matching manifest raises
`FileExistsError`; no existing path is removed. The `scripts/install.py` CLI
accepts `--runtime auto|claude|codex`, `--source PATH`, `--home PATH`, and
`--mode symlink|copy`, then prints the JSON `InstallResult`.

Add `scripts/test_support.py` with the exact fixture builders in the shared
interfaces section. The builders import feature modules lazily so Task 1's
runtime tests do not depend on later tasks, while Tasks 3–9 can use one
deterministic set of typed records.

- [ ] **Step 6: Run both test files and verify green**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_runtime.py
PYTHONPATH=scripts python3 scripts/test_install.py
```

Expected: all runtime and installer tests pass.

- [ ] **Step 7: Commit Task 1**

```bash
git add -- scripts/runtime.py scripts/install.py scripts/test_support.py scripts/test_runtime.py scripts/test_install.py
git commit --only -m "feat: add dual-runtime adapters" -- scripts/runtime.py scripts/install.py scripts/test_support.py scripts/test_runtime.py scripts/test_install.py
```

---

### Task 2: Make digest extraction event-time accurate and complete

**Files:**

- Modify: `scripts/digest.py`
- Modify: `scripts/test_digest.py`

**Interfaces:**

- Consumes: `RuntimeSpec`, `Scope`, and JSONL source files from Task 1.
- Produces: `SignalKind`, `Signal`, `DigestManifest`,
  `signals_from_event()`, `run_digest()`, and the existing
  `signals_from_line()` compatibility wrapper.

- [ ] **Step 1: Add failing event-time and output-integrity tests**

```python
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import os

from runtime import Scope, resolve_runtime
from digest import run_digest


def test_event_timestamp_controls_scope_even_when_mtime_is_stale():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source_root = root / "projects" / "project-a"
        source_root.mkdir(parents=True)
        source = source_root / "session.jsonl"
        source.write_text(
            '{"type":"user","timestamp":"2026-08-23T10:00:00Z",'
            '"message":{"role":"user","content":"recent event"}}\n'
        )
        os.utime(source, (1, 1))
        spec = resolve_runtime("claude", home=root, env={}, source_root=source_root.parent)
        scope = Scope(datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc), None, False)
        manifest = run_digest(spec, scope, root / "out")
        assert manifest.sessions_with_signals == 1
        assert manifest.signal_counts["user"] == 1


def test_old_event_is_excluded_from_recent_file_and_output_is_collision_free():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source_root = root / "projects" / "project-a" / "nested"
        source_root.mkdir(parents=True)
        first = source_root / "same.jsonl"
        second = root / "projects" / "project-a" / "same.jsonl"
        second.parent.mkdir(parents=True, exist_ok=True)
        line = '{"type":"user","timestamp":"2026-08-01T10:00:00Z",' \
               '"message":{"role":"user","content":"old"}}\n'
        first.write_text(line)
        second.write_text(line)
        spec = resolve_runtime("claude", home=root, env={}, source_root=root / "projects")
        scope = Scope(datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc), None, False)
        manifest = run_digest(spec, scope, root / "out")
        assert manifest.sessions_with_signals == 0
        assert len(list((root / "out").glob("*.md"))) == 0
        assert len(manifest.source_files) == 2
```

- [ ] **Step 2: Run the digest tests and verify the expected failure**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_digest.py
```

Expected: the new stale-mtime test fails because the current implementation
filters files by `os.path.getmtime()` before reading event timestamps; the
collision test also fails once its assertions execute.

- [ ] **Step 3: Implement timestamp filtering and manifest accounting**

Add UTC timestamp parsing for RFC 3339 and numeric runtime timestamps. Scan
all candidate JSONL files; mtime may determine iteration order but cannot be a
scope filter. Count malformed lines, unreadable files, missing timestamps,
and in-scope events in `SourceFile` records. Set `complete=False` when any
source read or parse failure occurs.

Retain the existing Claude `signals_from_line()` iterator/generator return for compatibility
and implement `signals_from_event()` as the typed path used by `run_digest()`.
Add Codex extraction for canonical user `response_item`/`event_msg` content,
true tool errors, and interrupt markers while excluding assistant, developer,
system, and successful tool content. Apply the 500/300 truncation constants.

Reject a pre-existing non-empty output directory with `FileExistsError`. Create
a run id with `uuid.uuid4().hex`, write each digest using a hash of its
runtime-relative source path, and write `manifest.json` through a temporary
file plus `os.replace`. Record all source files, including files with no
in-scope signals. A complete run returns normally; an incomplete run writes
the manifest and raises `IncompleteDigestError` after all accounting is done.

- [ ] **Step 4: Run the complete digest test file and verify green**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_digest.py
```

Expected: the original signal false-positive tests plus the new timestamp,
collision, malformed-input, empty-output, and manifest tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add -- scripts/digest.py scripts/test_digest.py
git commit --only -m "fix: make digests event-time scoped" -- scripts/digest.py scripts/test_digest.py
```

---

### Task 3: Replace prose miner output with a validated JSON contract

**Files:**

- Create: `scripts/miner_contract.py`
- Create: `scripts/test_miner_contract.py`
- Modify: `references/miner-prompt.md`

**Interfaces:**

- Consumes: `DigestManifest`, digest batch paths, and miner JSON strings.
- Produces: `FindingType`, `EvidenceRef`, `Finding`, `MinerReport`,
  `BatchSpec`, `parse_report()`, `merge_reports()`, and a JSON-only prompt
  contract for both hosts.

- [ ] **Step 1: Write failing report-validation tests**

```python
import json
from datetime import datetime, timezone

from miner_contract import parse_report, ReportValidationError
from test_support import make_batch, make_manifest, make_report


def manifest_and_batch():
    manifest = make_manifest(
        run_id="run-1",
        files=("project__session-a--abc.md",),
        lines=(("project__session-a--abc.md", 4, "2026-08-23T10:00:00Z", "user"),),
    )
    batch = make_batch("batch-1", ("project__session-a--abc.md",))
    return manifest, batch


def test_valid_report_preserves_typed_evidence():
    manifest, batch = manifest_and_batch()
    raw = json.dumps({
        "schema_version": 1,
        "runtime": "claude",
        "run_id": "run-1",
        "batch_id": "batch-1",
        "digest_paths": ["project__session-a--abc.md"],
        "findings": [{
            "cluster_key": "repeated-shell-retry",
            "finding_type": "failure",
            "session_id": "session-a",
            "paraphrase": "A command needed repeated retries.",
            "occurrence_count": 1,
            "confidence": 0.9,
            "evidence": [{
                "digest_path": "project__session-a--abc.md",
                "source_line": 4,
                "timestamp": "2026-08-23T10:00:00Z",
                "kind": "failure"
            }]
        }],
        "themes": ["repeated command retries"]
    })
    report = parse_report(raw, manifest, batch)
    assert report.findings[0].evidence[0].source_line == 4
    assert report.findings[0].confidence == 0.9


def test_report_rejects_evidence_from_another_batch():
    manifest, batch = manifest_and_batch()
    raw = json.dumps({
        "schema_version": 1,
        "runtime": "claude",
        "run_id": "run-1",
        "batch_id": "batch-1",
        "digest_paths": ["project__session-a--abc.md"],
        "findings": [{
            "cluster_key": "bad",
            "finding_type": "failure",
            "session_id": "session-a",
            "paraphrase": "bad evidence",
            "occurrence_count": 1,
            "confidence": 0.5,
            "evidence": [{
                "digest_path": "not-in-batch.md",
                "source_line": 4,
                "timestamp": "2026-08-23T10:00:00Z",
                "kind": "failure"
            }]
        }],
        "themes": []
    })
    try:
        parse_report(raw, manifest, batch)
    except ReportValidationError as exc:
        assert "batch" in str(exc)
    else:
        raise AssertionError("cross-batch evidence must be rejected")
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_miner_contract.py
```

Expected: `ModuleNotFoundError: No module named 'miner_contract'`.

- [ ] **Step 3: Implement strict JSON parsing and deterministic merging**

Parse one JSON object with no leading/trailing prose. Require exactly the
declared fields and validate schema version, runtime, run id, batch id,
`digest_paths` (exactly equal to the assigned batch),
finding type, positive counts, confidence bounds, non-empty cluster keys,
non-empty paraphrases, and evidence references present in the manifest and
assigned batch. Deduplicate evidence by `(digest_path, source_line)`.

Implement `merge_reports(reports, manifest)` by grouping normalized
`cluster_key`, summing only distinct evidence, taking the maximum confidence,
and sorting clusters by key, first timestamp, and session id. Reject duplicate
batch ids, missing manifest digests, overlapping batch assignments, and
uncovered non-empty manifest `digest_path` values; source files with
`digest_path=None` are accounted for but require no miner batch.

Update the prompt to require the exact JSON fields, including the assigned
`digest_paths`, one-line paraphrases,
typed evidence references, count/confidence bounds, two theme strings at most,
and no raw transcript reopening. State the same contract in the Claude and
Codex dispatch instructions.

- [ ] **Step 4: Add malformed, duplicate, and coverage tests**

```python
from miner_contract import ReportValidationError, merge_reports, parse_report
from test_support import make_manifest, make_report


def test_report_rejects_prose_and_out_of_range_confidence():
    manifest, batch = manifest_and_batch()
    for raw in ("Here is the report: {}", '{"schema_version":1}'):
        try:
            parse_report(raw, manifest, batch)
        except ReportValidationError:
            pass
        else:
            raise AssertionError("invalid report must fail")


def test_merge_rejects_missing_batch_coverage():
    reports = [make_report("batch-1", "a.md"), make_report("batch-2", "b.md")]
    manifest = make_manifest(run_id="run-1", files=("a.md", "b.md", "c.md"))
    try:
        merge_reports(reports, manifest)
    except ReportValidationError as exc:
        assert "c.md" in str(exc)
    else:
        raise AssertionError("uncovered digest must fail closed")


def test_merge_rejects_overlapping_batch_coverage():
    manifest = make_manifest(run_id="run-1", files=("a.md",))
    reports = [make_report("batch-1", "a.md"), make_report("batch-2", "a.md")]
    try:
        merge_reports(reports, manifest)
    except ReportValidationError as exc:
        assert "overlap" in str(exc)
    else:
        raise AssertionError("overlapping batches must fail closed")
```

- [ ] **Step 5: Run the miner contract tests and verify green**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_miner_contract.py
```

Expected: valid reports parse, invalid reports fail with typed errors, and
merging is deterministic and complete.

- [ ] **Step 6: Commit Task 3**

```bash
git add -- scripts/miner_contract.py scripts/test_miner_contract.py references/miner-prompt.md
git commit --only -m "feat: validate structured miner reports" -- scripts/miner_contract.py scripts/test_miner_contract.py references/miner-prompt.md
```

---

### Task 4: Model inventory coverage and strict ledger transitions

**Files:**

- Create: `scripts/coverage_model.py`
- Create: `scripts/ledger.py`
- Create: `scripts/test_coverage_model.py`
- Create: `scripts/test_ledger.py`
- Modify: `clusters.example.yaml`

**Interfaces:**

- Consumes: runtime inventory paths, merged findings, typed evidence, and the
  existing `clusters.yaml` status vocabulary.
- Produces: `CoverageRecord`, `InventoryItem`, `Inventory`, `LedgerEntry`,
  `LedgerStatus`, `parse_ledger()`, `validate_transition()`, and
  `update_ledger()`.

- [ ] **Step 1: Write failing coverage-state tests**

```python
from coverage_model import CoverageRecord, assess_coverage
from test_support import make_evidence


def test_declared_artifact_is_not_triggered_or_preventing_without_evidence():
    record = assess_coverage(
        artifact_id="skill:reflect-setup",
        artifact_kind="skill",
        exists=True,
        eligible=True,
        trigger_evidence=(),
        prevention_evidence=(),
        symptom_recurred=False,
    )
    assert record.declared is True
    assert record.eligible is True
    assert record.triggered is False
    assert record.prevented is None


def test_trigger_and_positive_outcome_are_distinct():
    triggered = make_evidence("skill.md", 3, "trigger")
    outcome = make_evidence("session.md", 8, "outcome")
    record = assess_coverage(
        artifact_id="hook:recording-health",
        artifact_kind="hook",
        exists=True,
        eligible=True,
        trigger_evidence=(triggered,),
        prevention_evidence=(outcome,),
        symptom_recurred=False,
    )
    assert record.triggered is True
    assert record.prevented is True
```

- [ ] **Step 2: Run the coverage tests and verify the expected failure**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_coverage_model.py
```

Expected: `ModuleNotFoundError: No module named 'coverage_model'`.

- [ ] **Step 3: Write failing ledger-transition tests**

```python
from ledger import LedgerStatus, validate_transition, LedgerTransitionError
from test_support import make_verification


def test_fix_applied_without_invocation_becomes_built_not_operating():
    verification = make_verification(symptom="fail", invocation="insufficient", outcome="pass")
    assert validate_transition(LedgerStatus.FIX_APPLIED, verification) is LedgerStatus.BUILT_NOT_OPERATING


def test_missing_evidence_never_resolves():
    verification = make_verification(symptom="insufficient", invocation="pass", outcome="pass")
    try:
        validate_transition(LedgerStatus.FIX_APPLIED, verification)
    except LedgerTransitionError as exc:
        assert "insufficient" in str(exc)
    else:
        raise AssertionError("insufficient symptom evidence cannot resolve")


def test_all_three_passes_resolve():
    verification = make_verification(symptom="pass", invocation="pass", outcome="pass")
    assert validate_transition(LedgerStatus.FIX_APPLIED, verification) is LedgerStatus.RESOLVED
```

- [ ] **Step 4: Run the ledger tests and verify the expected failure**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_ledger.py
```

Expected: `ModuleNotFoundError: No module named 'ledger'`.

- [ ] **Step 5: Implement coverage assessment and a strict ledger subset parser**

`assess_coverage()` sets `declared` from `exists`, `eligible` from the
runtime trigger predicate, `triggered` only when trigger evidence is present,
and `prevented=True` only when trigger evidence, positive outcome evidence,
and no current recurrence are all present. Otherwise `prevented=None` unless
the symptom is explicitly recurring, in which case it is `False`.

Implement a strict parser for the existing list-of-mappings YAML subset used
by `clusters.yaml`; accept scalar strings, integers, dates, and bracketed
string lists, and reject unknown status values, duplicate ids, missing ids,
and missing `wired_check` on applied/non-operating/resolved entries. Do not
introduce a YAML dependency. Preserve comments and human-readable fields when
serializing updated entries. Add `built-not-operating` to
`clusters.example.yaml` with its exact transition description.

`update_ledger()` updates only touched entries, preserves unrelated entries,
bumps session/date fields from the merged run, and delegates status changes to
`validate_transition()`.

- [ ] **Step 6: Run both module test files and verify green**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_coverage_model.py
PYTHONPATH=scripts python3 scripts/test_ledger.py
```

Expected: coverage state distinctions, parser validation, and all transition
rules pass.

- [ ] **Step 7: Commit Task 4**

```bash
git add -- scripts/coverage_model.py scripts/ledger.py scripts/test_coverage_model.py scripts/test_ledger.py clusters.example.yaml
git commit --only -m "feat: track operating coverage states" -- scripts/coverage_model.py scripts/ledger.py scripts/test_coverage_model.py scripts/test_ledger.py clusters.example.yaml
```

---

### Task 5: Add three-part fix-wiring verification

**Files:**

- Create: `scripts/verification.py`
- Create: `scripts/test_verification.py`

**Interfaces:**

- Consumes: merged findings, a ledger entry's `wired_check`, inventory
  coverage records, and current-window evidence.
- Produces: `CheckStatus`, `VerificationCheck`, `FixVerification`, and
  `verify_fix()`.

- [ ] **Step 1: Write failing verification tests**

```python
from verification import CheckStatus, verify_fix
from test_support import make_entry, make_evidence_set


def test_all_checks_are_required_for_resolution():
    result = verify_fix(
        make_entry("background-task-opacity"),
        symptom_evidence=make_evidence_set("symptom-absent"),
        invocation_evidence=make_evidence_set("invoked"),
        outcome_evidence=make_evidence_set("outcome-pass"),
    )
    assert result.overall is CheckStatus.PASS
    assert result.symptom.status is CheckStatus.PASS
    assert result.invocation.status is CheckStatus.PASS
    assert result.outcome.status is CheckStatus.PASS


def test_recurrence_marks_built_not_operating():
    result = verify_fix(
        make_entry("background-task-opacity"),
        symptom_evidence=make_evidence_set("symptom-recurred"),
        invocation_evidence=make_evidence_set("invoked"),
        outcome_evidence=make_evidence_set("outcome-pass"),
    )
    assert result.symptom.status is CheckStatus.FAIL
    assert result.overall is CheckStatus.FAIL


def test_absence_of_evidence_is_insufficient():
    result = verify_fix(
        make_entry("background-task-opacity"),
        symptom_evidence=(),
        invocation_evidence=(),
        outcome_evidence=(),
    )
    assert result.overall is CheckStatus.INSUFFICIENT
```

- [ ] **Step 2: Run the verification tests and verify the expected failure**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_verification.py
```

Expected: `ModuleNotFoundError: No module named 'verification'`.

- [ ] **Step 3: Implement independent checks and typed evidence**

Implement `verify_fix()` so each check is computed separately from evidence
tagged by the entry's `wired_check`. A symptom pass requires explicit
non-recurrence evidence; a missing symptom line is `INSUFFICIENT`, not pass.
An invocation pass requires a transcript or tool event showing the artifact
was loaded or called. An outcome pass requires a separate success marker or
behavioral result. A current symptom recurrence makes the symptom check fail
even if the file exists and was invoked.

Set `overall=PASS` only when all checks pass, `FAIL` when any check fails, and
`INSUFFICIENT` when no check fails but at least one is insufficient. Include
the exact evidence references and a concise detail string in every check.

- [ ] **Step 4: Add regression and missing wired-check tests**

```python
def test_resolved_fix_regresses_when_symptom_returns():
    result = verify_fix(
        make_entry("recording-health-check-not-wired", status="resolved"),
        symptom_evidence=make_evidence_set("symptom-recurred"),
        invocation_evidence=make_evidence_set("invoked"),
        outcome_evidence=make_evidence_set("outcome-fail"),
    )
    assert result.overall is CheckStatus.FAIL
    assert result.recommended_status == "built-not-operating"


def test_applied_entry_without_wired_check_is_rejected():
    try:
        verify_fix(make_entry("bad-entry", wired_check=""), (), (), ())
    except ValueError as exc:
        assert "wired_check" in str(exc)
    else:
        raise AssertionError("applied entries need a wired check")
```

- [ ] **Step 5: Run the verification tests and verify green**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_verification.py
```

Expected: all check-state, regression, and evidence-sufficiency tests pass.

- [ ] **Step 6: Commit Task 5**

```bash
git add -- scripts/verification.py scripts/test_verification.py
git commit --only -m "feat: verify fix wiring in three dimensions" -- scripts/verification.py scripts/test_verification.py
```

---

### Task 6: Add trend metrics and deterministic candidate scoring

**Files:**

- Create: `scripts/scoring.py`
- Create: `scripts/test_scoring.py`

**Interfaces:**

- Consumes: merged `Finding` values, analyzed-session/project totals, prior
  ledger evidence, and implementation-cost labels.
- Produces: `TrendMetrics`, `CandidateScore`, `compute_metrics()`,
  `score_candidate()`, and `rank_candidates()`.

- [ ] **Step 1: Write failing metric and ranking tests**

```python
from datetime import datetime, timezone

from scoring import compute_metrics, rank_candidates
from test_support import make_findings, make_tied_candidates


def test_metrics_include_rate_breadth_and_regression():
    metrics = compute_metrics(
        findings=make_findings(
            occurrences=5,
            sessions=("s1", "s2", "s3"),
            projects=("p1", "p2"),
            dates=("2026-08-20", "2026-08-23"),
        ),
        analyzed_sessions=10,
        regression_count=1,
        confidence=0.8,
        implementation_cost="M",
    )
    assert metrics.occurrences_per_100_sessions == 50.0
    assert metrics.sessions == 3
    assert metrics.projects == 2
    assert metrics.distinct_days == 2
    assert metrics.regression_count == 1


def test_ranking_is_deterministic_and_uses_cluster_key_as_final_tie_break():
    candidates = make_tied_candidates(keys=("b-cluster", "a-cluster"))
    first = rank_candidates(candidates)
    second = rank_candidates(tuple(reversed(candidates)))
    assert [item.cluster_key for item in first] == ["a-cluster", "b-cluster"]
    assert first == second
```

- [ ] **Step 2: Run the scoring tests and verify the expected failure**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_scoring.py
```

Expected: `ModuleNotFoundError: No module named 'scoring'`.

- [ ] **Step 3: Implement the transparent metrics and score formula**

Compute first/last timestamps, occurrence count, unique sessions/projects,
distinct UTC days, occurrences per 100 analyzed sessions, regressions,
confidence, fixed signal-type impact, and S/M/L implementation cost. Use the
exact formula from the design:

```python
score = round(
    0.30 * min(metrics.occurrences / 10, 1.0)
    + 0.20 * min(metrics.sessions / 5, 1.0)
    + 0.15 * min(metrics.projects / 3, 1.0)
    + 0.15 * min(metrics.distinct_days / 5, 1.0)
    + 0.10 * metrics.confidence
    + 0.10 * metrics.impact
    - 0.10 * min(metrics.regression_count / max(metrics.occurrences, 1), 1.0),
    4,
)
```

Use impact weights `failure=1.0`, `complaint=0.9`, `correction=0.8`, and
`friction=0.7`, averaging mixed findings. Sort by score descending,
regressions ascending, first-seen timestamp ascending, then cluster key.
Include rationale strings for each nonzero score component and display raw
metrics to the notes writer.

- [ ] **Step 4: Add threshold and zero-input tests**

```python
def test_zero_analyzed_sessions_produce_zero_rate_without_division_error():
    metrics = compute_metrics(
        findings=make_findings(occurrences=1, sessions=("s1",), projects=("p1",), dates=("2026-08-23",)),
        analyzed_sessions=0,
        regression_count=0,
        confidence=0.5,
        implementation_cost="S",
    )
    assert metrics.occurrences_per_100_sessions == 0.0


def test_confidence_is_clamped_at_input_boundary():
    metrics = compute_metrics(
        findings=make_findings(occurrences=1, sessions=("s1",), projects=("p1",), dates=("2026-08-23",)),
        analyzed_sessions=1,
        regression_count=0,
        confidence=1.0,
        implementation_cost="S",
    )
    assert 0.0 <= metrics.confidence <= 1.0
```

- [ ] **Step 5: Run the scoring tests and verify green**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_scoring.py
```

Expected: metric calculation, formula output, zero-input handling, and
deterministic ties pass.

- [ ] **Step 6: Commit Task 6**

```bash
git add -- scripts/scoring.py scripts/test_scoring.py
git commit --only -m "feat: rank reflection candidates by trends" -- scripts/scoring.py scripts/test_scoring.py
```

---

### Task 7: Make Apply granular, scope-bounded, and proof-based

**Files:**

- Create: `scripts/apply.py`
- Create: `scripts/test_apply.py`

**Interfaces:**

- Consumes: a scored cluster, inventory, current workspace status, proposed
  paths/commands, and fixer command output.
- Produces: `ApplyRequest`, `ApplyPreview`, `FixProof`, `WorkspaceState`,
  `build_preview()`, `validate_proof()`, and `check_scope_overlap()`.

- [ ] **Step 1: Write failing Apply-preview tests**

```python
from pathlib import Path

from apply import ApplyScopeError, build_preview, check_scope_overlap, validate_proof
from test_support import make_inventory, make_preview, make_proof, make_request, make_workspace


def test_preview_lists_paths_commands_and_preexisting_dirty_paths():
    request = make_request(
        cluster_id="shell-retry",
        target_paths=(Path(".claude/hooks/retry.sh"),),
        commands=("python3 scripts/check_retry.py",),
        verification_commands=("python3 scripts/test_retry.py",),
    )
    preview = build_preview(
        request,
        inventory=make_inventory(),
        workspace=make_workspace(dirty=(Path("README.md"),)),
    )
    assert preview.disjoint_scope is True
    assert preview.workspace_state.dirty_paths == (Path("README.md"),)
    assert preview.request.commands == ("python3 scripts/check_retry.py",)


def test_preview_rejects_path_outside_project_scope():
    request = make_request(
        cluster_id="shell-retry",
        target_paths=(Path("../other-project/file.py"),),
        commands=("true",),
        verification_commands=("true",),
    )
    try:
        build_preview(request, inventory=make_inventory(), workspace=make_workspace())
    except ApplyScopeError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("out-of-scope Apply must fail")
```

- [ ] **Step 2: Run the Apply tests and verify the expected failure**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_apply.py
```

Expected: `ModuleNotFoundError: No module named 'apply'`.

- [ ] **Step 3: Write failing proof-validation tests**

```python
def test_proof_requires_only_new_in_scope_paths_and_command_output():
    preview = make_preview(target_paths=(Path(".claude/hooks/retry.sh"),))
    proof = make_proof(
        changed_paths=(Path(".claude/hooks/retry.sh"),),
        command_output=("updated hook",),
        verification_output=("1 test passed",),
        passed=True,
    )
    validate_proof(preview, proof)


def test_proof_rejects_new_path_outside_scope():
    preview = make_preview(target_paths=(Path(".claude/hooks/retry.sh"),))
    proof = make_proof(
        changed_paths=(Path(".claude/hooks/retry.sh"), Path("AGENTS.md")),
        command_output=("updated",),
        verification_output=("pass",),
        passed=True,
    )
    try:
        validate_proof(preview, proof)
    except ApplyScopeError as exc:
        assert "AGENTS.md" in str(exc)
    else:
        raise AssertionError("unapproved changed path must fail")
```

- [ ] **Step 4: Implement preview, overlap, and proof validation**

`build_preview()` normalizes paths relative to the selected project, rejects
absolute paths and `..` escapes, records pre-existing dirty paths, and checks
that the request's target paths do not overlap another approved preview.
`check_scope_overlap()` compares normalized path sets and returns the two
cluster ids when an overlap exists. The preview contains exact commands and
verification commands for the host's user confirmation prompt.

`validate_proof()` accepts pre-existing dirty paths only when they were present
in `WorkspaceState` before Apply. Every new changed path must be in
`target_paths`; `command_output` and `verification_output` must both be
non-empty; `passed` must be true; and every declared verification command must
have a matching successful output line. Raise `ApplyScopeError` on any scope
violation and `ApplyProofError` on missing or failed proof.

- [ ] **Step 5: Add per-cluster approval and overlap tests**

```python
def test_two_approved_clusters_with_disjoint_paths_are_independent():
    first = make_preview(cluster_id="first", target_paths=(Path("a.py"),))
    second = make_preview(cluster_id="second", target_paths=(Path("b.py"),))
    assert check_scope_overlap(first, second) is None


def test_overlapping_clusters_cannot_run_in_parallel():
    first = make_preview(cluster_id="first", target_paths=(Path("a.py"),))
    second = make_preview(cluster_id="second", target_paths=(Path("a.py"),))
    assert check_scope_overlap(first, second) == ("first", "second")
```

- [ ] **Step 6: Run the Apply tests and verify green**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_apply.py
```

Expected: previews, dirty-path preservation, scope rejection, proof
validation, and overlap checks pass.

- [ ] **Step 7: Commit Task 7**

```bash
git add -- scripts/apply.py scripts/test_apply.py
git commit --only -m "feat: gate Apply by scope and proof" -- scripts/apply.py scripts/test_apply.py
```

---

### Task 8: Add the synthetic cross-runtime evaluation corpus

**Files:**

- Create: `scripts/evaluate.py`
- Create: `scripts/test_evaluate.py`
- Create: `fixtures/evaluation/claude/canonical.jsonl`
- Create: `fixtures/evaluation/claude/subagent.jsonl`
- Create: `fixtures/evaluation/codex/canonical.jsonl`
- Create: `fixtures/evaluation/codex/subagent.jsonl`
- Create: `fixtures/evaluation/expected.json`

**Interfaces:**

- Consumes: fixture JSONL, expected normalized signals, and Tasks 1–7
  interfaces.
- Produces: `EvaluationResult`, `evaluate_fixtures()`, and a stable JSON
  report with precision, recall, false positives, manifest completeness,
  batch coverage, and score determinism.

- [ ] **Step 1: Write the failing evaluation tests**

```python
from pathlib import Path

from evaluate import evaluate_fixtures


def test_both_runtime_fixtures_match_expected_signal_sets():
    result = evaluate_fixtures(Path("fixtures/evaluation"))
    assert result.precision == 1.0
    assert result.recall == 1.0
    assert result.false_positives == 0
    assert result.manifests_complete is True


def test_evaluation_detects_nondeterministic_ranking():
    result = evaluate_fixtures(Path("fixtures/evaluation"))
    assert result.score_is_deterministic is True
```

- [ ] **Step 2: Run the evaluation tests and verify the expected failure**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_evaluate.py
```

Expected: `ModuleNotFoundError: No module named 'evaluate'`.

- [ ] **Step 3: Create the four minimal synthetic fixtures and expected output**

The Claude canonical fixture contains one in-window user correction, one true
error result, one interrupt marker, one assistant line with error-looking
text, and one successful tool result containing source-code error text. The
Claude subagent fixture contains the same signal shapes below a `subagents`
directory. The Codex canonical fixture contains a `session_meta` record with
`thread_source: "user"`, a user `response_item`, a true tool error, and an
assistant message. The Codex subagent fixture changes only the thread source to
`subagent`.

Set fixture mtimes in the test to a timestamp before the evaluation window and
include one recent event timestamp, plus one recent-mtime file containing an
old event. `expected.json` lists the three normalized retained signals and the
three excluded records for each runtime. Keep fixture text synthetic and
short; do not copy any user session data.

- [ ] **Step 4: Implement the evaluation harness**

Run each fixture through the corresponding adapter, compare normalized signal
identity `(runtime, session_id, source_line, kind)`, assert canonical user
filtering and false-positive exclusions, assert complete manifests, and run
the same `rank_candidates()` input twice. Return exact decimal metrics and a
boolean `score_is_deterministic`; exit nonzero when expected output differs.

- [ ] **Step 5: Run the evaluation tests and verify green**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_evaluate.py
PYTHONPATH=scripts python3 scripts/evaluate.py --fixtures fixtures/evaluation
```

Expected: both commands pass with precision and recall `1.0`, zero false
positives, complete manifests, and deterministic scores.

- [ ] **Step 6: Commit Task 8**

```bash
git add -- scripts/evaluate.py scripts/test_evaluate.py fixtures/evaluation/claude/canonical.jsonl fixtures/evaluation/claude/subagent.jsonl fixtures/evaluation/codex/canonical.jsonl fixtures/evaluation/codex/subagent.jsonl fixtures/evaluation/expected.json
git commit --only -m "test: add cross-runtime reflection fixtures" -- scripts/evaluate.py scripts/test_evaluate.py fixtures/evaluation/claude/canonical.jsonl fixtures/evaluation/claude/subagent.jsonl fixtures/evaluation/codex/canonical.jsonl fixtures/evaluation/codex/subagent.jsonl fixtures/evaluation/expected.json
```

---

### Task 9: Wire the end-to-end workflow and update host documentation

**Files:**

- Create: `scripts/reflect_setup.py`
- Create: `scripts/test_reflect_setup.py`
- Modify: `SKILL.md`
- Modify: `README.md`
- Modify: `REFERENCE.md`

**Interfaces:**

- Consumes: runtime selection, scope arguments, `run_digest()`, validated
  miner report paths, inventory, ledger, verification, scoring, and Apply
  preview modules from Tasks 1–8.
- Produces: `ReflectionRun`, `run_reflection()`, and the operator CLI.

- [ ] **Step 1: Write the failing end-to-end tests**

```python
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from reflect_setup import run_reflection


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "scripts/reflect_setup.py", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_run_reflection_produces_manifest_report_and_ranked_candidates():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "projects" / "project-a"
        source.mkdir(parents=True)
        (source / "session.jsonl").write_text(
            '{"type":"assistant","timestamp":"2026-08-23T10:00:00Z",'
            '"message":{"role":"assistant","content":[{"type":"text",'
            '"text":"no signal"}]}}\n'
        )
        result = run_reflection(
            runtime_name="claude",
            home=root,
            source_root=root / "projects",
            since=datetime(2026, 8, 23, tzinfo=timezone.utc),
            project_filter=None,
            include_subagents=False,
            out_dir=root / "run",
            miner_report_paths=(),
            apply=False,
        )
        assert result.manifest.complete is True
        assert result.report_path == root / "run" / "reflection-report.json"
        assert result.ranked_candidates == ()
        assert result.apply_preview is None


def test_cli_rejects_apply_without_explicit_cluster_approval():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "projects"
        source.mkdir()
        completed = run_cli(
            "--runtime", "claude",
            "--source-root", str(source),
            "--apply",
            "--out", str(root / "run"),
        )
        assert completed.returncode != 0
        assert "cluster approval" in completed.stderr
```

- [ ] **Step 2: Run the end-to-end tests and verify the expected failure**

Run:

```bash
PYTHONPATH=scripts python3 scripts/test_reflect_setup.py
```

Expected: `ModuleNotFoundError: No module named 'reflect_setup'`.

- [ ] **Step 3: Implement the minimal orchestrator and CLI**

Implement:

```python
@dataclass(frozen=True, slots=True)
class ReflectionRun:
    manifest: DigestManifest
    report_path: Path
    ranked_candidates: tuple[CandidateScore, ...]
    coverage: tuple[CoverageRecord, ...]
    apply_preview: ApplyPreview | None


def run_reflection(
    *,
    runtime_name: str | None,
    home: Path,
    source_root: Path | None,
    since: datetime,
    project_filter: str | None,
    include_subagents: bool,
    out_dir: Path,
    miner_report_paths: tuple[Path, ...],
    apply: bool,
) -> ReflectionRun:
    raise NotImplementedError
```

Resolve one runtime, build one `Scope`, run the digest, validate that every
miner report belongs to the manifest, merge reports, inventory coverage,
verify touched ledger entries, compute trend metrics/ranking, and write
`reflection-report.json` atomically. If `apply=True`, require an explicit
`--approve-cluster <id>` for each selected cluster before creating previews;
without approval, exit nonzero and do not write project files. Keep Apply
execution in the host workflow so the Python core never guesses which Claude
or Codex task tool is available.

When the manifest has no digest paths, an empty `miner_report_paths` tuple is
valid and produces an empty candidate set. Once any digest path exists, every
non-empty manifest digest path must be covered by exactly one validated report
batch. The CLI maps `--runtime auto` to the API's `runtime_name=None`.

CLI flags are:

```text
--runtime auto|claude|codex
--source-root PATH
--since DAYS
--project-filter TEXT
--include-subagents
--out PATH
--miner-report PATH   (repeatable)
--apply
--approve-cluster ID  (repeatable)
--json
```

Update `SKILL.md` to describe the same nine-step workflow for both hosts,
including explicit runtime selection, manifest/batch checks, JSON miner
reports, coverage states, three verification checks, trend metrics, and
per-cluster Apply approval. Update `README.md` with both symlink install
commands and `scripts/install.py` usage. Update `REFERENCE.md` with the
runtime table, event-time scope rules, manifest failure behavior, report
schema, ledger transitions, scoring formula, and Apply proof requirements.

- [ ] **Step 4: Add end-to-end failure-path tests**

```python
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from reflect_setup import run_reflection
from digest import IncompleteDigestError


def test_incomplete_digest_cannot_dispatch_miners():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "projects" / "project-a"
        source.mkdir(parents=True)
        (source / "broken.jsonl").write_text("{not valid json\n")
        try:
            run_reflection(
                runtime_name="claude",
                home=root,
                source_root=root / "projects",
                since=datetime(2026, 8, 23, tzinfo=timezone.utc),
                project_filter=None,
                include_subagents=False,
                out_dir=root / "run",
                miner_report_paths=(),
                apply=False,
            )
        except IncompleteDigestError as exc:
            assert "manifest" in str(exc)
        else:
            raise AssertionError("incomplete input must stop the run")
```

- [ ] **Step 5: Run the full focused verification**

Run:

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

Expected: every test script exits zero. Then run the source/install drift
checks:

```bash
PYTHONPATH=scripts python3 scripts/install.py --help
PYTHONPATH=scripts python3 scripts/reflect_setup.py --help
git diff --check
```

Expected: both CLIs print help, `git diff --check` prints no findings, and
only the planned implementation paths are changed.

- [ ] **Step 6: Commit Task 9**

```bash
git add -- scripts/reflect_setup.py scripts/test_reflect_setup.py SKILL.md README.md REFERENCE.md
git commit --only -m "feat: wire dual-runtime reflection workflow" -- scripts/reflect_setup.py scripts/test_reflect_setup.py SKILL.md README.md REFERENCE.md
```

---

## Final self-review checklist

- [ ] The design's runtime, timestamp, completeness, structured-report,
  verification, scoring, coverage, Apply, evaluation, and synchronization
  requirements each map to at least one task above.
- [ ] Every task names exact files, interfaces, a failing test, an expected
  failure, minimal implementation guidance, a passing verification command,
  and a task-scoped commit.
- [ ] No task introduces privacy, redaction, or retention functionality.
- [ ] Public names and return types match the shared-interface block and the
  design document.
- [ ] The plan does not rely on a third-party YAML, test, or runtime package.
- [ ] Existing `signals_from_line()` behavior remains covered by the current
  direct tests while new runtime-aware behavior uses typed interfaces.
- [ ] A partial or ambiguous run cannot be reported as complete, and Apply
  cannot silently mutate an unrelated dirty path.
