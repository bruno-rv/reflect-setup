"""Runnable checks for deterministic reflection candidate scoring."""
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


def test_confidence_is_clamped_at_input_boundary():
    metrics = compute_metrics(
        findings=make_findings(
            occurrences=1,
            sessions=("s1",),
            projects=("p1",),
            dates=("2026-08-23",),
        ),
        analyzed_sessions=1,
        regression_count=0,
        confidence=1.5,
        implementation_cost="S",
    )
    assert 0.0 <= metrics.confidence <= 1.0
    assert metrics.confidence == 1.0


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
                ),
            ),
        )
        for index, finding_type in enumerate(
            (FindingType.FAILURE, FindingType.COMPLAINT, FindingType.CORRECTION, FindingType.FRICTION)
        )
    )
    metrics = compute_metrics(findings, 4, 0, 0.5, "L")
    assert metrics.impact == 0.85


def run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    run_all()
