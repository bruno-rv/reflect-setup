"""Runnable checks for finding IDs and reconciliation grouping."""
import json
from datetime import datetime, timezone
from pathlib import Path

from miner_contract import EvidenceRef, Finding, FindingType, ReportValidationError
from reconciliation import (
    ReconciliationGroup,
    ReconciliationReport,
    build_request,
    finding_id,
    parse_report,
    parse_request,
    validate_groups,
)
from runtime import Runtime


def _ref(digest_path, line, timestamp="2026-08-23T10:00:00Z", project="project-a", kind="failure"):
    return EvidenceRef(
        digest_path=digest_path,
        source_line=line,
        timestamp=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
        kind=FindingType(kind),
        project=project,
        occurrence_count=1,
    )


def _finding(cluster_key, session_id, evidence, paraphrase="fixture failure", finding_type="failure"):
    return Finding(
        cluster_key=cluster_key,
        finding_type=FindingType(finding_type),
        session_id=session_id,
        paraphrase=paraphrase,
        occurrence_count=len(evidence),
        confidence=0.8,
        evidence=tuple(evidence),
    )


def _two_synonym_findings():
    first = _finding(
        "repeated shell retry",
        "session-a",
        (_ref("digest-a.md", 1),),
        paraphrase="a command needed repeated retries",
    )
    second = _finding(
        "shell retry loop",
        "session-b",
        (_ref("digest-b.md", 2),),
        paraphrase="the same command retried many times",
    )
    return first, second


def test_finding_ids_are_deterministic_and_evidence_bound():
    first, second = _two_synonym_findings()
    assert finding_id(first, Runtime.CLAUDE) == finding_id(first, Runtime.CLAUDE)
    assert finding_id(first, Runtime.CLAUDE) != finding_id(second, Runtime.CLAUDE)
    renamed = _finding(
        "completely different key",
        "session-a",
        (_ref("digest-a.md", 1),),
        paraphrase="a command needed repeated retries",
    )
    assert finding_id(renamed, Runtime.CLAUDE) != finding_id(first, Runtime.CLAUDE)
    same_evidence = _finding(
        "repeated shell retry",
        "session-a",
        (_ref("digest-a.md", 1),),
        paraphrase="a command needed repeated retries",
    )
    assert finding_id(same_evidence, Runtime.CLAUDE) == finding_id(first, Runtime.CLAUDE)


def test_finding_id_rejects_duplicate_evidence_pairs():
    finding = _finding(
        "duplicate-evidence",
        "session-a",
        (_ref("digest-a.md", 1), _ref("digest-a.md", 1)),
    )
    try:
        finding_id(finding, Runtime.CLAUDE)
    except ReportValidationError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate evidence pairs must fail before hashing")


def test_build_request_round_trips_and_sorts_by_id():
    first, second = _two_synonym_findings()
    request = build_request((first, second), runtime=Runtime.CLAUDE, run_id="run-1")
    assert request.schema_version == 1
    assert request.runtime is Runtime.CLAUDE
    assert request.run_id == "run-1"
    assert len(request.items) == 2
    assert [item.finding_id for item in request.items] == sorted(
        item.finding_id for item in request.items
    )
    parsed = parse_request(json.dumps(request.to_json()))
    assert parsed == request
    assert parsed.items[0].proposed_key == "repeated shell retry"
    assert parsed.items[0].projects == ("project-a",)
    assert parsed.items[0].first_seen == datetime(2026, 8, 23, 10, tzinfo=timezone.utc)


def test_parse_request_rejects_unknown_fields_and_duplicate_ids():
    first, second = _two_synonym_findings()
    request = build_request((first, second), runtime=Runtime.CLAUDE, run_id="run-1")
    value = request.to_json()
    value["extra"] = True
    try:
        parse_request(json.dumps(value))
    except ReportValidationError as exc:
        assert "unexpected" in str(exc)
    else:
        raise AssertionError("unknown request fields must fail")
    value = request.to_json()
    value["items"][1]["finding_id"] = value["items"][0]["finding_id"]
    try:
        parse_request(json.dumps(value))
    except ReportValidationError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate request finding ids must fail")


def test_report_validation_accepts_true_synonym_merge():
    first, second = _two_synonym_findings()
    request = build_request((first, second), runtime=Runtime.CLAUDE, run_id="run-1")
    report = ReconciliationReport(
        1,
        Runtime.CLAUDE,
        "run-1",
        (
            ReconciliationGroup(
                "repeated-shell-retry",
                "A command needed repeated retries across projects.",
                "Both findings cite the same retry loop.",
                tuple(item.finding_id for item in request.items),
            ),
        ),
    )
    parsed = parse_report(json.dumps(report.to_json()), request)
    groups = validate_groups(parsed, request)
    assert len(groups) == 1
    assert groups[0].cluster_key == "repeated-shell-retry"
    assert set(groups[0].member_finding_ids) == {
        item.finding_id for item in request.items
    }


def test_report_validation_keeps_similar_symptoms_with_different_causes_separate():
    first = _finding(
        "missing-python-dependency",
        "session-a",
        (_ref("digest-a.md", 1),),
        paraphrase="module not found because the venv is missing",
    )
    second = _finding(
        "missing-python-dependency",
        "session-b",
        (_ref("digest-b.md", 2),),
        paraphrase="module not found because the package was never installed",
    )
    request = build_request((first, second), runtime=Runtime.CLAUDE, run_id="run-1")
    report = ReconciliationReport(
        1,
        Runtime.CLAUDE,
        "run-1",
        (
            ReconciliationGroup(
                "venv-not-activated",
                "The project venv is missing from the session.",
                "First finding names the venv as the cause.",
                (request.items[0].finding_id,),
            ),
            ReconciliationGroup(
                "package-not-installed",
                "The package was never installed.",
                "Second finding names installation as the cause.",
                (request.items[1].finding_id,),
            ),
        ),
    )
    parsed = parse_report(json.dumps(report.to_json()), request)
    groups = validate_groups(parsed, request)
    assert len(groups) == 2
    assert {group.cluster_key for group in groups} == {
        "venv-not-activated",
        "package-not-installed",
    }


def test_report_rejects_missing_duplicate_and_unknown_ids():
    first, second = _two_synonym_findings()
    request = build_request((first, second), runtime=Runtime.CLAUDE, run_id="run-1")
    ids = [item.finding_id for item in request.items]

    missing = ReconciliationReport(
        1,
        Runtime.CLAUDE,
        "run-1",
        (ReconciliationGroup("only-one", "summary", "rationale", (ids[0],)),),
    )
    try:
        validate_groups(missing, request)
    except ReportValidationError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("missing finding ids must fail")

    duplicated = ReconciliationReport(
        1,
        Runtime.CLAUDE,
        "run-1",
        (
            ReconciliationGroup("first", "summary", "rationale", (ids[0], ids[1])),
            ReconciliationGroup("second", "summary", "rationale", (ids[1],)),
        ),
    )
    try:
        validate_groups(duplicated, request)
    except ReportValidationError as exc:
        assert "more than once" in str(exc)
    else:
        raise AssertionError("duplicated finding ids must fail")

    unknown = ReconciliationReport(
        1,
        Runtime.CLAUDE,
        "run-1",
        (ReconciliationGroup("unknown", "summary", "rationale", ("f" * 64,)),),
    )
    try:
        validate_groups(unknown, request)
    except ReportValidationError as exc:
        assert "unknown" in str(exc)
    else:
        raise AssertionError("unknown finding ids must fail")


def test_report_rejects_duplicate_normalized_keys_and_bad_slugs():
    first, second = _two_synonym_findings()
    request = build_request((first, second), runtime=Runtime.CLAUDE, run_id="run-1")
    ids = [item.finding_id for item in request.items]
    duplicate_keys = ReconciliationReport(
        1,
        Runtime.CLAUDE,
        "run-1",
        (
            ReconciliationGroup("same-key", "summary", "rationale", (ids[0],)),
            ReconciliationGroup("same-key", "summary", "rationale", (ids[1],)),
        ),
    )
    try:
        validate_groups(duplicate_keys, request)
    except ReportValidationError as exc:
        assert "same key" in str(exc)
    else:
        raise AssertionError("duplicate normalized keys must fail")
    bad_slug = ReconciliationReport(
        1,
        Runtime.CLAUDE,
        "run-1",
        (ReconciliationGroup("Not A Slug!", "summary", "rationale", (ids[0], ids[1])),),
    )
    try:
        parse_report(json.dumps(bad_slug.to_json()), request)
    except ReportValidationError as exc:
        assert "kebab" in str(exc)
    else:
        raise AssertionError("invalid kebab slugs must fail")


def test_report_rejects_metadata_mismatch():
    first, second = _two_synonym_findings()
    request = build_request((first, second), runtime=Runtime.CLAUDE, run_id="run-1")
    wrong_runtime = ReconciliationReport(
        1,
        Runtime.CODEX,
        "run-1",
        (ReconciliationGroup("key", "summary", "rationale", (item.finding_id for item in request.items)),),
    )
    try:
        parse_report(json.dumps(wrong_runtime.to_json()), request)
    except ReportValidationError as exc:
        assert "runtime" in str(exc)
    else:
        raise AssertionError("runtime mismatch must fail")
    wrong_run = ReconciliationReport(
        1,
        Runtime.CLAUDE,
        "other-run",
        (ReconciliationGroup("key", "summary", "rationale", (item.finding_id for item in request.items)),),
    )
    try:
        parse_report(json.dumps(wrong_run.to_json()), request)
    except ReportValidationError as exc:
        assert "run_id" in str(exc)
    else:
        raise AssertionError("run_id mismatch must fail")


def test_singleton_finding_skips_reconciliation():
    finding = _finding("single", "session-a", (_ref("digest-a.md", 1),))
    request = build_request((finding,), runtime=Runtime.CLAUDE, run_id="run-1")
    assert len(request.items) == 1
    report = ReconciliationReport(
        1,
        Runtime.CLAUDE,
        "run-1",
        (ReconciliationGroup("single", "summary", "rationale", (request.items[0].finding_id,)),),
    )
    groups = validate_groups(parse_report(json.dumps(report.to_json()), request), request)
    assert groups[0].cluster_key == "single"


def run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    run_all()
