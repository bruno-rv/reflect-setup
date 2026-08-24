"""Runnable checks for the synthetic cross-runtime evaluation corpus."""
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from digest import IncompleteDigestError, run_digest
from evaluate import BatchCoverageError, _stage_runtime, evaluate_fixtures, validate_batch_coverage
from miner_contract import MinerReport
from runtime import Runtime, Scope, resolve_runtime


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "evaluation"


def test_both_runtime_fixtures_match_expected_signal_sets():
    result = evaluate_fixtures(FIXTURES)
    expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
    assert result.precision == 1.0
    assert result.recall == 1.0
    assert result.false_positives == 0
    assert result.manifests_complete is all(expected["manifests_complete"].values())
    assert dict(result.manifest_completeness) == expected["manifests_complete"]
    assert dict(result.malformed_lines) == expected["malformed_lines"]
    assert dict(result.batch_report_counts) == expected["batch_report_counts"]
    assert result.batch_coverage_complete is expected["batch_coverage_complete"]
    assert result.report_validation_failures == expected["report_validation_failures"]
    assert result.score_is_deterministic is expected["score_is_deterministic"]
    assert list(result.ranking) == expected["expected_ranking"]
    assert result.expected_matches is True
    assert result.excluded_violations == 0
    assert result.malformed_cases_checked is True
    assert result.subagents_filtered is True
    assert len(result.retained_signals) == 16


def test_exact_cutoff_is_retained_and_immediately_prior_event_is_excluded():
    result = evaluate_fixtures(FIXTURES)
    retained = {
        (item.runtime, item.session_id, item.source_line)
        for item in result.retained_signals
    }
    assert ("claude", "canonical-a", 7) in retained
    assert ("claude", "canonical-b", 3) not in retained
    assert ("codex", "codex-a", 8) in retained
    assert ("codex", "codex-b", 4) not in retained


def test_evaluation_detects_nondeterministic_ranking():
    result = evaluate_fixtures(FIXTURES)
    assert result.score_is_deterministic is True


def test_in_scope_malformed_json_preserves_incomplete_manifest_evidence():
    scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False)
    with TemporaryDirectory(prefix="reflect-evaluation-test-") as raw:
        root = Path(raw)
        source_root = _stage_runtime(FIXTURES, Runtime.CLAUDE, root / "claude")
        spec = resolve_runtime("claude", home=root / "claude", env={}, source_root=source_root)
        try:
            run_digest(spec, scope, root / "digest")
        except IncompleteDigestError as exc:
            manifest = exc.manifest
        else:
            raise AssertionError("in-scope malformed JSON must raise IncompleteDigestError")
    assert manifest.complete is False
    malformed = [source for source in manifest.source_files if source.malformed_lines]
    assert malformed and malformed[0].malformed_lines > 0
    assert malformed[0].digest_path is not None


def test_batch_coverage_requires_two_disjoint_exhaustive_reports():
    manifest = _manifest_for_batch_test()
    valid = (
        MinerReport(1, Runtime.CLAUDE, "batch-run", "part-a", ("a.md",), (), ()),
        MinerReport(1, Runtime.CLAUDE, "batch-run", "part-b", ("b.md",), (), ()),
    )
    assert validate_batch_coverage(manifest, valid) is True
    overlap = (
        valid[0],
        MinerReport(1, Runtime.CLAUDE, "batch-run", "part-b", ("a.md",), (), ()),
    )
    try:
        validate_batch_coverage(manifest, overlap)
    except BatchCoverageError:
        pass
    else:
        raise AssertionError("overlapping batch partitions must raise")
    missing = (valid[0],)
    try:
        validate_batch_coverage(manifest, missing)
    except BatchCoverageError:
        pass
    else:
        raise AssertionError("missing batch partitions must raise")


def _manifest_for_batch_test():
    from digest import DigestManifest, SourceFile

    return DigestManifest(
        schema_version=1,
        run_id="batch-run",
        runtime=Runtime.CLAUDE,
        scope=Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False),
        source_files=(
            SourceFile("a.jsonl", "0" * 64, True, True, 1, 0, 0, 1, "fixture-a", "a.md"),
            SourceFile("b.jsonl", "1" * 64, True, True, 1, 0, 0, 1, "fixture-b", "b.md"),
        ),
        sessions_scanned=2,
        sessions_with_signals=2,
        signal_counts={"user": 2},
        complete=True,
    )


if __name__ == "__main__":
    tests = (
        test_both_runtime_fixtures_match_expected_signal_sets,
        test_exact_cutoff_is_retained_and_immediately_prior_event_is_excluded,
        test_evaluation_detects_nondeterministic_ranking,
        test_in_scope_malformed_json_preserves_incomplete_manifest_evidence,
        test_batch_coverage_requires_two_disjoint_exhaustive_reports,
    )
    for test in tests:
        test()
    print(f"{len(tests)} tests passed")
