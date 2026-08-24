"""Runnable checks for the structured miner report contract."""
import json
from datetime import datetime, timezone

from miner_contract import (
    EvidenceRef,
    Finding,
    FindingType,
    MinerReport,
    ReportValidationError,
    merge_reports,
    parse_report,
)
from test_support import make_batch, make_manifest, make_report


def manifest_and_batch():
    manifest = make_manifest(
        run_id="run-1",
        files=("project__session-a--abc.md",),
        lines=(("project__session-a--abc.md", 4, "2026-08-23T10:00:00Z", "user"),),
        project="project-a",
    )
    batch = make_batch("batch-1", ("project__session-a--abc.md",))
    return manifest, batch


def valid_payload(**overrides):
    payload = {
        "schema_version": 1,
        "runtime": "claude",
        "run_id": "run-1",
        "batch_id": "batch-1",
        "digest_paths": ["project__session-a--abc.md"],
        "findings": [
            {
                "cluster_key": "repeated-shell-retry",
                "finding_type": "failure",
                "session_id": "session-a",
                "paraphrase": "A command needed repeated retries.",
                "occurrence_count": 1,
                "confidence": 0.9,
                "evidence": [
                    {
                        "digest_path": "project__session-a--abc.md",
                        "project": "project-a",
                        "source_line": 4,
                        "timestamp": "2026-08-23T10:00:00Z",
                        "kind": "failure",
                    }
                ],
            }
        ],
        "themes": ["repeated command retries"],
    }
    payload.update(overrides)
    return payload


def test_valid_report_preserves_typed_evidence():
    manifest, batch = manifest_and_batch()
    raw = json.dumps(valid_payload())
    report = parse_report(raw, manifest, batch)
    assert report.findings[0].evidence[0].source_line == 4
    assert report.findings[0].confidence == 0.9


def test_report_accepts_strict_rfc3339_z_and_numeric_offset():
    manifest, batch = manifest_and_batch()
    for timestamp, expected in (
        ("2026-08-23T10:00:00Z", datetime(2026, 8, 23, 10, tzinfo=timezone.utc)),
        ("2026-08-23T12:30:45+02:30", datetime(2026, 8, 23, 10, 0, 45, tzinfo=timezone.utc)),
    ):
        payload = valid_payload()
        payload["findings"][0]["evidence"][0]["timestamp"] = timestamp
        report = parse_report(json.dumps(payload), manifest, batch)
        assert report.findings[0].evidence[0].timestamp == expected


def test_report_rejects_rfc3339_near_misses():
    manifest, batch = manifest_and_batch()
    for timestamp in (
        "2026-08-23 10:00:00Z",
        "2026-08-23T10:00Z",
        "2026-08-23T10:00:00",
    ):
        payload = valid_payload()
        payload["findings"][0]["evidence"][0]["timestamp"] = timestamp
        try:
            parse_report(json.dumps(payload), manifest, batch)
        except ReportValidationError:
            pass
        else:
            raise AssertionError(f"timestamp near-miss must fail: {timestamp}")


def test_report_rejects_evidence_from_another_batch():
    manifest, batch = manifest_and_batch()
    payload = valid_payload()
    payload["findings"][0]["evidence"][0]["digest_path"] = "not-in-batch.md"
    try:
        parse_report(json.dumps(payload), manifest, batch)
    except ReportValidationError as exc:
        assert "batch" in str(exc)
    else:
        raise AssertionError("cross-batch evidence must be rejected")


def test_report_rejects_evidence_with_wrong_project_identity():
    manifest, batch = manifest_and_batch()
    payload = valid_payload()
    payload["findings"][0]["evidence"][0]["project"] = "another-project"
    try:
        parse_report(json.dumps(payload), manifest, batch)
    except ReportValidationError as exc:
        assert "project" in str(exc)
    else:
        raise AssertionError("evidence project must match manifest metadata")


def test_report_rejects_prose_and_out_of_range_confidence():
    manifest, batch = manifest_and_batch()
    for raw in ("Here is the report: {}", json.dumps(valid_payload(confidence=1.1))):
        try:
            parse_report(raw, manifest, batch)
        except ReportValidationError:
            pass
        else:
            raise AssertionError("invalid report must fail")


def test_report_rejects_missing_or_extra_fields():
    manifest, batch = manifest_and_batch()
    payload = valid_payload()
    payload.pop("themes")
    try:
        parse_report(json.dumps(payload), manifest, batch)
    except ReportValidationError:
        pass
    else:
        raise AssertionError("missing field must fail")

    payload = valid_payload()
    payload["unexpected"] = True
    try:
        parse_report(json.dumps(payload), manifest, batch)
    except ReportValidationError:
        pass
    else:
        raise AssertionError("extra field must fail")


def test_report_rejects_batch_path_mismatch():
    manifest, batch = manifest_and_batch()
    payload = valid_payload(digest_paths=[])
    try:
        parse_report(json.dumps(payload), manifest, batch)
    except ReportValidationError as exc:
        assert "digest_paths" in str(exc)
    else:
        raise AssertionError("assigned batch paths must be declared exactly")


def test_report_deduplicates_evidence_references():
    manifest, batch = manifest_and_batch()
    payload = valid_payload()
    evidence = payload["findings"][0]["evidence"][0]
    payload["findings"][0]["evidence"].append(dict(evidence))
    report = parse_report(json.dumps(payload), manifest, batch)
    assert len(report.findings[0].evidence) == 1


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


def test_merge_rejects_duplicate_batch_ids():
    manifest = make_manifest(run_id="run-1", files=("a.md",))
    reports = [make_report("batch-1", "a.md"), make_report("batch-1", "a.md")]
    try:
        merge_reports(reports, manifest)
    except ReportValidationError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate batch IDs must fail closed")


def test_merge_rejects_typed_finding_without_evidence():
    manifest = make_manifest(run_id="run-1", files=("a.md",))
    finding = Finding(
        cluster_key="fixture",
        finding_type=FindingType.FAILURE,
        session_id="session-a",
        paraphrase="fixture failure",
        occurrence_count=1,
        confidence=0.5,
        evidence=(),
    )
    report = MinerReport(1, "claude", "run-1", "batch-1", ("a.md",), (finding,), ())
    try:
        merge_reports((report,), manifest)
    except ReportValidationError as exc:
        assert "evidence" in str(exc)
    else:
        raise AssertionError("typed findings without evidence must fail closed")


def test_merge_sums_distinct_evidence_and_sorts_clusters():
    manifest = make_manifest(run_id="run-1", files=("a.md", "b.md"))
    first = make_report("batch-1", "a.md")
    second = make_report("batch-2", "b.md")
    merged = merge_reports((first, second), manifest)
    assert len(merged) == 1
    assert merged[0].cluster_key == "fixture"
    assert merged[0].occurrence_count == 2
    assert len(merged[0].evidence) == 2
    assert merged[0].confidence == 0.5


def test_merge_preserves_distinct_sessions_and_finding_types():
    manifest = make_manifest(
        run_id="run-1",
        files=("a.md", "b.md", "c.md"),
        projects=(("a.md", "project-a"), ("b.md", "project-b"), ("c.md", "project-c")),
    )
    reports = []
    for batch_id, path, session_id, finding_type, project in (
        ("batch-1", "a.md", "session-a", FindingType.FAILURE, "project-a"),
        ("batch-2", "b.md", "session-b", FindingType.FAILURE, "project-b"),
        ("batch-3", "c.md", "session-c", FindingType.COMPLAINT, "project-c"),
    ):
        evidence = EvidenceRef(
            path,
            1,
            datetime(2026, 8, 23, tzinfo=timezone.utc),
            finding_type,
            project,
        )
        finding = Finding("same-cluster", finding_type, session_id, "fixture", 1, 0.8, (evidence,))
        reports.append(MinerReport(1, "claude", "run-1", batch_id, (path,), (finding,), ()))

    merged = merge_reports(tuple(reports), manifest)
    assert [(item.session_id, item.finding_type) for item in merged] == [
        ("session-a", FindingType.FAILURE),
        ("session-b", FindingType.FAILURE),
        ("session-c", FindingType.COMPLAINT),
    ]
    assert sum(item.occurrence_count for item in merged) == 3
    assert [len(item.evidence) for item in merged] == [1, 1, 1]


def test_merge_accounts_for_manifest_files_without_digest_paths():
    manifest = make_manifest(run_id="run-1", files=("a.md", "empty.md"))
    manifest = manifest.__class__(
        schema_version=manifest.schema_version,
        run_id=manifest.run_id,
        runtime=manifest.runtime,
        scope=manifest.scope,
        source_files=(manifest.source_files[0], manifest.source_files[1].__class__(
            source_path="empty.md",
            sha256=manifest.source_files[1].sha256,
            scanned=True,
            readable=True,
            json_lines=0,
            malformed_lines=0,
            untimestamped_lines=0,
            in_scope_events=0,
            project="fixture-project",
            digest_path=None,
        )),
        sessions_scanned=manifest.sessions_scanned,
        sessions_with_signals=1,
        signal_counts=manifest.signal_counts,
        complete=True,
    )
    merged = merge_reports((make_report("batch-1", "a.md"),), manifest)
    assert merged[0].cluster_key == "fixture"


def run_all():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    run_all()
