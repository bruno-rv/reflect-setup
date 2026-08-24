"""Deterministic typed fixtures shared by the reflection test modules."""
from datetime import datetime, timezone
from pathlib import Path
import re


def make_manifest(
    run_id="run-1",
    files=("a.md",),
    lines=(),
    project="fixture-project",
    projects=(),
    sessions=(),
):
    from digest import DigestManifest, EvidenceIndexEntry, SignalKind, SourceFile
    from miner_contract import FindingType
    from runtime import Runtime, Scope

    line_map = {path: count for path, count, unused_timestamp, unused_kind in lines}
    line_metadata = {path: (count, timestamp, kind) for path, count, timestamp, kind in lines}
    project_map = dict(projects)
    session_map = dict(sessions)

    def session_for(path):
        match = re.search(r"(session-[A-Za-z0-9]+)", Path(path).stem)
        return session_map.get(path, match.group(1) if match else "session-a")

    def index_kind(raw_kind):
        try:
            return SignalKind(raw_kind)
        except ValueError:
            return FindingType(raw_kind)

    def index_for(path):
        line, timestamp, kind = line_metadata.get(
            path,
            (line_map.get(path, 1), "2026-08-23T10:00:00Z", "failure"),
        )
        return (
            EvidenceIndexEntry(
                source_line=line,
                timestamp=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
                kind=index_kind(kind),
                project=project_map.get(path, project),
                session_id=session_for(path),
            ),
        )

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
            project=project_map.get(path, project),
            evidence_index=index_for(path),
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


def make_report(batch_id, path, run_id="run-1", project="fixture-project"):
    from miner_contract import EvidenceRef, Finding, FindingType, MinerReport

    session_id = "session-a"
    finding = Finding(
        cluster_key="fixture",
        finding_type=FindingType.FAILURE,
        session_id=session_id,
        paraphrase="fixture failure",
        occurrence_count=1,
        confidence=0.5,
        evidence=(EvidenceRef(path, 1, datetime(2026, 8, 23, 10, tzinfo=timezone.utc), FindingType.FAILURE, project),),
    )
    return MinerReport(1, "claude", run_id, batch_id, (path,), (finding,), ())


def make_evidence(path="evidence.md", line=1, label="outcome-pass", project="fixture-project"):
    from miner_contract import EvidenceRef, FindingType

    return EvidenceRef(
        digest_path=path,
        source_line=line,
        timestamp=datetime(2026, 8, 23, tzinfo=timezone.utc),
        kind=FindingType.FAILURE,
        project=project,
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


def make_entry(
    cluster_id="fixture",
    status="fix-applied",
    wired_check="fixture evidence",
    artifact_ids=None,
):
    from ledger import LedgerEntry, LedgerStatus

    declared_artifacts = (wired_check,) if artifact_ids is None else tuple(artifact_ids)
    return LedgerEntry(cluster_id, LedgerStatus(status), wired_check, declared_artifacts)


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
                evidence=(EvidenceRef(project + ".md", index + 1, datetime.fromisoformat(day).replace(tzinfo=timezone.utc), FindingType.FAILURE, project),),
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
