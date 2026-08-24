"""Runnable checks for the synthetic cross-runtime evaluation corpus."""
from pathlib import Path

from evaluate import evaluate_fixtures


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "evaluation"


def test_both_runtime_fixtures_match_expected_signal_sets():
    result = evaluate_fixtures(FIXTURES)
    assert result.precision == 1.0
    assert result.recall == 1.0
    assert result.false_positives == 0
    assert result.manifests_complete is True


def test_evaluation_detects_nondeterministic_ranking():
    result = evaluate_fixtures(FIXTURES)
    assert result.score_is_deterministic is True


if __name__ == "__main__":
    tests = (
        test_both_runtime_fixtures_match_expected_signal_sets,
        test_evaluation_detects_nondeterministic_ranking,
    )
    for test in tests:
        test()
    print(f"{len(tests)} tests passed")
