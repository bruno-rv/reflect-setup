"""Runnable checks for deterministic reflection candidate scoring."""
from dataclasses import replace
from datetime import datetime, timezone

from scoring import compute_metrics, rank_candidates, score_candidate
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
    assert metrics.first_seen == datetime(2026, 8, 20, tzinfo=timezone.utc)
    assert metrics.last_seen == datetime(2026, 8, 23, tzinfo=timezone.utc)


def test_ranking_is_deterministic_and_uses_cluster_key_as_final_tie_break():
    candidates = make_tied_candidates(keys=("b-cluster", "a-cluster"))
    first = rank_candidates(candidates)
    second = rank_candidates(tuple(reversed(candidates)))
    assert [item.cluster_key for item in first] == ["a-cluster", "b-cluster"]
    assert first == second


def test_score_uses_transparent_formula_and_rationale():
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
    candidate = score_candidate("fixture", metrics)
    assert candidate.score == 0.59
    assert any("occurrences" in item for item in candidate.rationale)
    assert any("regression" in item for item in candidate.rationale)
    assert any("raw metrics" in item for item in candidate.rationale)


def test_zero_analyzed_sessions_produce_zero_rate_without_division_error():
    metrics = compute_metrics(
        findings=make_findings(
            occurrences=1,
            sessions=("s1",),
            projects=("p1",),
            dates=("2026-08-23",),
        ),
        analyzed_sessions=0,
        regression_count=0,
        confidence=0.5,
        implementation_cost="S",
    )
    assert metrics.occurrences_per_100_sessions == 0.0


def test_confidence_at_input_boundary_is_preserved():
    metrics = compute_metrics(
        findings=make_findings(
            occurrences=1,
            sessions=("s1",),
            projects=("p1",),
            dates=("2026-08-23",),
        ),
        analyzed_sessions=1,
        regression_count=0,
        confidence=1.0,
        implementation_cost="S",
    )
    assert 0.0 <= metrics.confidence <= 1.0
    assert metrics.confidence == 1.0


def test_invalid_confidence_is_rejected():
    try:
        compute_metrics(
            findings=make_findings(1, ("s1",), ("p1",), ("2026-08-23",)),
            analyzed_sessions=1,
            regression_count=0,
            confidence=1.1,
            implementation_cost="S",
        )
    except ValueError as exc:
        assert "confidence" in str(exc)
    else:
        raise AssertionError("confidence outside [0, 1] must be rejected")


def test_impact_averages_mixed_finding_types():
    from miner_contract import EvidenceRef, Finding, FindingType

    findings = tuple(
        Finding(
            cluster_key="mixed",
            finding_type=finding_type,
            session_id=f"s{index}",
            paraphrase="fixture",
            occurrence_count=1,
            confidence=0.5,
            evidence=(
                EvidenceRef(
                    digest_path=f"p{index}.md",
                    source_line=1,
                    timestamp=datetime(2026, 8, 23, tzinfo=timezone.utc),
                    kind=finding_type,
                    project=f"project-{index}",
                ),
            ),
        )
        for index, finding_type in enumerate(
            (FindingType.FAILURE, FindingType.COMPLAINT, FindingType.CORRECTION, FindingType.FRICTION)
        )
    )
    metrics = compute_metrics(findings, 4, 0, 0.5, "L")
    assert metrics.impact == 0.85


def test_explicit_project_identity_handles_double_underscore_names():
    from miner_contract import EvidenceRef, Finding, FindingType

    findings = (
        Finding(
            cluster_key="project-test",
            finding_type=FindingType.FAILURE,
            session_id="session-a",
            paraphrase="fixture",
            occurrence_count=1,
            confidence=0.9,
            evidence=(
                EvidenceRef(
                    digest_path="opaque-output-name--hash.md",
                    source_line=1,
                    timestamp=datetime(2026, 8, 23, tzinfo=timezone.utc),
                    kind=FindingType.FAILURE,
                    project="team__alpha",
                ),
            ),
        ),
        Finding(
            cluster_key="project-test",
            finding_type=FindingType.FAILURE,
            session_id="session-b",
            paraphrase="fixture",
            occurrence_count=1,
            confidence=0.9,
            evidence=(
                EvidenceRef(
                    digest_path="another-opaque-name--hash.md",
                    source_line=1,
                    timestamp=datetime(2026, 8, 23, tzinfo=timezone.utc),
                    kind=FindingType.FAILURE,
                    project="team__alpha",
                ),
            ),
        ),
    )
    metrics = compute_metrics(findings, 2, 0, 0.9, "S")
    assert metrics.projects == 1


def test_utc_boundary_normalizes_dates_before_counting():
    from miner_contract import EvidenceRef, Finding, FindingType

    findings = tuple(
        Finding(
            cluster_key="utc-boundary",
            finding_type=FindingType.FAILURE,
            session_id=f"session-{index}",
            paraphrase="fixture",
            occurrence_count=1,
            confidence=0.5,
            evidence=(
                EvidenceRef(
                    digest_path=f"project-{index}.md",
                    source_line=1,
                    timestamp=timestamp,
                    kind=FindingType.FAILURE,
                    project=f"project-{index}",
                ),
            ),
        )
        for index, timestamp in enumerate(
            (
                datetime.fromisoformat("2026-08-22T23:30:00-02:00"),
                datetime.fromisoformat("2026-08-23T00:15:00+00:00"),
            )
        )
    )
    metrics = compute_metrics(findings, 2, 0, 0.5, "S")
    assert metrics.distinct_days == 1
    assert metrics.first_seen == datetime(2026, 8, 23, 0, 15, tzinfo=timezone.utc)


def test_zero_occurrences_with_regression_do_not_divide_by_zero():
    metrics = compute_metrics((), 0, 1, 0.5, "S")
    candidate = score_candidate("zero-occurrence", metrics)
    assert metrics.occurrences == 0
    assert candidate.score == -0.05


def test_implementation_cost_breaks_equal_score_ties_before_cluster_key():
    metrics = compute_metrics(make_findings(1, ("s1",), ("p1",), ("2026-08-23",)), 1, 0, 0.5, "S")
    from scoring import rank_candidates

    cheaper = score_candidate("z-cluster", replace(metrics, implementation_cost="S"))
    more_expensive = score_candidate("a-cluster", replace(metrics, implementation_cost="L"))
    ranked = rank_candidates((more_expensive, cheaper))
    assert [item.cluster_key for item in ranked] == ["z-cluster", "a-cluster"]


def run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    run_all()
