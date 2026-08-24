#!/usr/bin/env python3
"""Evaluate the synthetic Claude/Codex reflection corpus.

The harness stages committed fixtures in a temporary runtime-shaped source
tree, runs the public digest and miner-batch interfaces, and emits a stable
JSON summary.  It deliberately never reads a user's live session directory.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from digest import IncompleteDigestError, Signal, signals_from_event, run_digest
from miner_contract import EvidenceRef, Finding, FindingType, MinerReport, merge_reports
from runtime import Runtime, Scope, discover_sessions, resolve_runtime
from scoring import rank_candidates, score_candidate, compute_metrics


UTC = timezone.utc
_OLD_MTIME = datetime(2026, 8, 1, tzinfo=UTC).timestamp()
_RECENT_MTIME = datetime(2026, 8, 24, tzinfo=UTC).timestamp()


def _frozen_dataclass(cls):
    kwargs = {"frozen": True}
    if sys.version_info >= (3, 10):
        kwargs["slots"] = True
    return dataclass(**kwargs)(cls)


@_frozen_dataclass
class NormalizedSignal:
    runtime: str
    session_id: str
    source_line: int
    kind: str


@_frozen_dataclass
class EvaluationResult:
    precision: float
    recall: float
    false_positives: int
    manifests_complete: bool
    batch_coverage_complete: bool
    score_is_deterministic: bool
    report_validation_failures: int
    expected_matches: bool
    retained_signals: tuple[NormalizedSignal, ...]
    excluded_violations: int
    malformed_cases_checked: bool
    subagents_filtered: bool
    manifest_completeness: tuple[tuple[str, bool], ...]
    malformed_lines: tuple[tuple[str, int], ...]
    batch_report_counts: tuple[tuple[str, int], ...]
    ranking: tuple[str, ...]

    @property
    def batch_coverage(self) -> bool:
        """Compatibility alias for consumers using the shorter report name."""
        return self.batch_coverage_complete

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "precision": self.precision,
            "recall": self.recall,
            "false_positives": self.false_positives,
            "manifests_complete": self.manifests_complete,
            "batch_coverage_complete": self.batch_coverage_complete,
            "score_is_deterministic": self.score_is_deterministic,
            "report_validation_failures": self.report_validation_failures,
            "expected_matches": self.expected_matches,
            "excluded_violations": self.excluded_violations,
            "malformed_cases_checked": self.malformed_cases_checked,
            "subagents_filtered": self.subagents_filtered,
            "manifest_completeness": dict(self.manifest_completeness),
            "malformed_lines": dict(self.malformed_lines),
            "batch_report_counts": dict(self.batch_report_counts),
            "ranking": list(self.ranking),
            "retained_signals": [
                {
                    "runtime": signal.runtime,
                    "session_id": signal.session_id,
                    "source_line": signal.source_line,
                    "kind": signal.kind,
                }
                for signal in self.retained_signals
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, indent=2) + "\n"


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("window_since must be an RFC 3339 timestamp")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("window_since must include a timezone")
    return parsed.astimezone(UTC)


def _load_expected(fixtures: Path) -> dict[str, object]:
    path = fixtures / "expected.json"
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("expected.json must contain an object")
    return value


def _copy_with_mtime(source: Path, target: Path, mtime: float) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(source), str(target))
    os.utime(str(target), (mtime, mtime))


def _write_fixture_lines(target: Path, lines: Iterable[str], mtime: float) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(lines), encoding="utf-8")
    os.utime(str(target), (mtime, mtime))


def _valid_fixture_lines(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8") as stream:
        return [line for line in stream if line.strip()]


def _second_session_lines(runtime: Runtime, source: Path) -> list[str]:
    """Reorder one synthetic session and give Codex its own identity."""
    lines = []
    for line in _valid_fixture_lines(source):
        try:
            json.loads(line)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        lines.append(line)
    if runtime is Runtime.CLAUDE:
        order = (6, 3, 5, 4, 2, 0, 1)
    else:
        order = (0, 7, 4, 6, 5, 3, 1, 2)
    reordered = [lines[index] for index in order]
    if runtime is Runtime.CODEX:
        reordered[0] = reordered[0].replace('"codex-a"', '"codex-b"').replace(
            '"synthetic-project-a"', '"synthetic-project-b"'
        )
    return reordered


def _stage_runtime(fixtures: Path, runtime: Runtime, root: Path) -> Path:
    """Build the runtime's source layout while preserving fixture bytes."""
    if runtime is Runtime.CLAUDE:
        source_root = root / "projects"
        canonical_a = source_root / "synthetic-project-a" / "canonical-a.jsonl"
        canonical_b = source_root / "synthetic-project-b" / "canonical-b.jsonl"
        subagent = source_root / "synthetic-project-a" / "subagents" / "subagent.jsonl"
    else:
        source_root = root / "sessions"
        canonical_a = source_root / "canonical-a.jsonl"
        canonical_b = source_root / "canonical-b.jsonl"
        subagent = source_root / "subagents" / "subagent.jsonl"
    source_dir = fixtures / runtime.value
    canonical_fixture = source_dir / "canonical.jsonl"
    _copy_with_mtime(canonical_fixture, canonical_a, _OLD_MTIME)
    _write_fixture_lines(canonical_b, _second_session_lines(runtime, canonical_fixture), _RECENT_MTIME)
    _copy_with_mtime(source_dir / "subagent.jsonl", subagent, _RECENT_MTIME)
    return source_root


def _session_ids(spec, scope: Scope) -> Mapping[str, str]:
    return {
        item.relative_path: item.session_id
        for item in discover_sessions(spec, scope)
    }


def _signals_for_manifest(
    spec,
    scope: Scope,
    manifest,
) -> tuple[Signal, ...]:
    ids = _session_ids(spec, scope)
    signals: list[Signal] = []
    for source in manifest.source_files:
        if source.digest_path is None or not source.readable:
            continue
        session_id = ids.get(source.source_path)
        if not session_id:
            continue
        path = spec.source_root / Path(source.source_path)
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for source_line, raw_line in enumerate(stream, 1):
                    signals.extend(
                        signals_from_event(
                            raw_line,
                            runtime=spec.runtime,
                            session_id=session_id,
                            source_line=source_line,
                            scope=scope,
                        )
                    )
        except OSError:
            continue
    return tuple(signals)


def _normalize(runtime: Runtime, signals: Iterable[Signal]) -> tuple[NormalizedSignal, ...]:
    return tuple(
        sorted(
            (
                NormalizedSignal(runtime.value, signal.session_id, signal.source_line, signal.kind.value)
                for signal in signals
            ),
            key=lambda signal: (signal.runtime, signal.session_id, signal.source_line, signal.kind),
        )
    )


def _expected_signals(expected: Mapping[str, object]) -> tuple[NormalizedSignal, ...]:
    raw_by_runtime = expected.get("retained_signals")
    if not isinstance(raw_by_runtime, dict):
        raise ValueError("expected retained_signals must be an object")
    values: list[NormalizedSignal] = []
    for runtime in (Runtime.CLAUDE.value, Runtime.CODEX.value):
        raw_values = raw_by_runtime.get(runtime)
        if not isinstance(raw_values, list):
            raise ValueError(f"expected retained_signals.{runtime} must be an array")
        for raw in raw_values:
            if not isinstance(raw, dict):
                raise ValueError("expected retained signal must be an object")
            values.append(
                NormalizedSignal(
                    runtime,
                    str(raw["session_id"]),
                    int(raw["source_line"]),
                    str(raw["kind"]),
                )
            )
    return tuple(sorted(values, key=lambda signal: (signal.runtime, signal.session_id, signal.source_line, signal.kind)))


def _excluded_keys(expected: Mapping[str, object]) -> frozenset[tuple[str, str, int]]:
    raw_by_runtime = expected.get("excluded_records")
    if not isinstance(raw_by_runtime, dict):
        raise ValueError("expected excluded_records must be an object")
    values: set[tuple[str, str, int]] = set()
    for runtime in (Runtime.CLAUDE.value, Runtime.CODEX.value):
        raw_values = raw_by_runtime.get(runtime)
        if not isinstance(raw_values, list):
            raise ValueError(f"expected excluded_records.{runtime} must be an array")
        for raw in raw_values:
            if not isinstance(raw, dict):
                raise ValueError("expected excluded record must be an object")
            values.add((runtime, str(raw["session_id"]), int(raw["source_line"])))
    return frozenset(values)


def _check_malformed_fixtures(fixtures: Path, scope: Scope) -> bool:
    """Ensure each committed malformed record is rejected by both adapters."""
    checked = 0
    for runtime in (Runtime.CLAUDE, Runtime.CODEX):
        for path in (fixtures / runtime.value / "canonical.jsonl", fixtures / runtime.value / "subagent.jsonl"):
            try:
                with path.open("r", encoding="utf-8") as stream:
                    for source_line, raw_line in enumerate(stream, 1):
                        try:
                            json.loads(raw_line)
                        except (json.JSONDecodeError, ValueError, TypeError):
                            checked += 1
                            if signals_from_event(
                                raw_line,
                                runtime=runtime,
                                session_id="synthetic-malformed",
                                source_line=source_line,
                                scope=scope,
                            ):
                                return False
            except OSError:
                return False
    return checked > 0


class BatchCoverageError(ValueError):
    """Raised when evaluation reports do not partition every digest path."""


def validate_batch_coverage(manifest, reports: Iterable[MinerReport]) -> bool:
    """Require at least two disjoint reports whose union covers the manifest."""
    report_values = tuple(reports)
    if len(report_values) < 2:
        raise BatchCoverageError("evaluation requires at least two batch reports")
    assigned: dict[str, str] = {}
    for report in report_values:
        if not isinstance(report, MinerReport):
            raise BatchCoverageError("batch reports must contain MinerReport values")
        for path in report.digest_paths:
            previous = assigned.get(path)
            if previous is not None:
                raise BatchCoverageError(f"batch path assigned more than once: {path}")
            assigned[path] = report.batch_id
    expected = {
        source.digest_path
        for source in manifest.source_files
        if source.digest_path is not None
    }
    if set(assigned) != expected:
        missing = sorted(expected - set(assigned))
        extra = sorted(set(assigned) - expected)
        detail = []
        if missing:
            detail.append(f"missing={','.join(missing)}")
        if extra:
            detail.append(f"unknown={','.join(extra)}")
        raise BatchCoverageError("batch reports are not exhaustive: " + "; ".join(detail))
    try:
        merge_reports(report_values, manifest)
    except (TypeError, ValueError) as exc:
        raise BatchCoverageError(str(exc)) from exc
    return True


def _build_batch_reports(runtime: Runtime, manifest) -> tuple[MinerReport, ...]:
    paths = tuple(sorted(source.digest_path for source in manifest.source_files if source.digest_path is not None))
    if len(paths) < 2:
        raise BatchCoverageError("evaluation manifest must contain at least two digest paths")
    midpoint = len(paths) // 2
    partitions = (paths[:midpoint], paths[midpoint:])
    return tuple(
        MinerReport(
            schema_version=1,
            runtime=runtime,
            run_id=manifest.run_id,
            batch_id=f"evaluation-{runtime.value}-{index}",
            digest_paths=partition,
            findings=(),
            themes=(),
        )
        for index, partition in enumerate(partitions, 1)
    )


def _batch_coverage(manifests: Mapping[Runtime, object]) -> tuple[bool, int, tuple[tuple[str, int], ...]]:
    failures = 0
    complete = True
    report_counts = []
    for runtime, manifest in manifests.items():
        try:
            reports = _build_batch_reports(runtime, manifest)
            validate_batch_coverage(manifest, reports)
            report_counts.append((runtime.value, len(reports)))
        except BatchCoverageError:
            failures += 1
            complete = False
            report_counts.append((runtime.value, 0))
    return complete, failures, tuple(sorted(report_counts))


def _score_ranking(signals: Iterable[tuple[Runtime, Signal]]) -> tuple[str, ...]:
    values = tuple(signals)
    type_map = {
        "user": FindingType.CORRECTION,
        "error": FindingType.FAILURE,
        "interrupt": FindingType.FRICTION,
    }
    findings: list[Finding] = []
    for index, (runtime, signal) in enumerate(values):
        finding_type = type_map[signal.kind.value]
        findings.append(
            Finding(
                cluster_key=f"{runtime.value}-{signal.session_id}-{signal.source_line}-{signal.kind.value}",
                finding_type=finding_type,
                session_id=signal.session_id,
                paraphrase=signal.text,
                occurrence_count=1,
                confidence=1.0,
                evidence=(
                    EvidenceRef(
                        digest_path=f"synthetic-{index}.md",
                        source_line=signal.source_line,
                        timestamp=signal.timestamp,
                        kind=finding_type,
                        project="synthetic-project",
                    ),
                ),
            )
        )
    candidates = []
    for finding in findings:
        metrics = compute_metrics(
            (finding,),
            analyzed_sessions=len({item.session_id for unused_runtime, item in values}),
            regression_count=0,
            confidence=finding.confidence,
            implementation_cost="S",
        )
        candidates.append(score_candidate(finding.cluster_key, metrics))
    return tuple(item.cluster_key for item in rank_candidates(candidates))


def _interleave_signals(signals: Iterable[tuple[Runtime, Signal]]) -> tuple[tuple[Runtime, Signal], ...]:
    values = tuple(signals)
    grouped = {
        runtime: [item for item in values if item[0] is runtime]
        for runtime in (Runtime.CLAUDE, Runtime.CODEX)
    }
    interleaved = []
    for index in range(max((len(items) for items in grouped.values()), default=0)):
        for runtime in (Runtime.CODEX, Runtime.CLAUDE):
            if index < len(grouped[runtime]):
                interleaved.append(grouped[runtime][index])
    return tuple(interleaved)


def _collect_fixture_run(
    fixtures: Path,
    scope: Scope,
    runtime_order: tuple[Runtime, ...],
) -> tuple[tuple[tuple[Runtime, Signal], ...], Mapping[Runtime, object]]:
    all_signals: list[tuple[Runtime, Signal]] = []
    manifests = {}
    with tempfile.TemporaryDirectory(prefix="reflect-evaluation-") as raw:
        root = Path(raw)
        for runtime in runtime_order:
            source_root = _stage_runtime(fixtures, runtime, root / runtime.value)
            spec = resolve_runtime(runtime.value, home=root / runtime.value, env={}, source_root=source_root)
            out_dir = root / f"digest-{runtime.value}"
            try:
                manifest = run_digest(spec, scope, out_dir)
            except IncompleteDigestError as exc:
                manifest = exc.manifest
            manifests[runtime] = manifest
            all_signals.extend((runtime, signal) for signal in _signals_for_manifest(spec, scope, manifest))
    return tuple(all_signals), manifests


def _normalized_signals(signals: Iterable[tuple[Runtime, Signal]]) -> tuple[NormalizedSignal, ...]:
    return tuple(
        sorted(
            (
                NormalizedSignal(runtime.value, signal.session_id, signal.source_line, signal.kind.value)
                for runtime, signal in signals
            ),
            key=lambda signal: (signal.runtime, signal.session_id, signal.source_line, signal.kind),
        )
    )


def evaluate_fixtures(fixtures: Path) -> EvaluationResult:
    """Run both runtime adapters twice and return stable, inspectable metrics."""
    fixtures = Path(fixtures).expanduser().resolve()
    expected = _load_expected(fixtures)
    since = _timestamp(expected.get("window_since"))
    scope = Scope(since=since, project_filter=None, include_subagents=False)
    all_signals, manifests = _collect_fixture_run(
        fixtures, scope, (Runtime.CLAUDE, Runtime.CODEX)
    )
    fresh_signals, fresh_manifests = _collect_fixture_run(
        fixtures, scope, (Runtime.CODEX, Runtime.CLAUDE)
    )

    normalized = _normalized_signals(all_signals)
    fresh_normalized = _normalized_signals(fresh_signals)
    expected_values = _expected_signals(expected)
    predicted_counts = Counter(normalized)
    expected_counts = Counter(expected_values)
    true_positives = sum((predicted_counts & expected_counts).values())
    predicted_total = sum(predicted_counts.values())
    expected_total = sum(expected_counts.values())
    precision = round(true_positives / predicted_total, 10) if predicted_total else 1.0 if not expected_total else 0.0
    recall = round(true_positives / expected_total, 10) if expected_total else 1.0
    false_positives = sum((predicted_counts - expected_counts).values())
    excluded = _excluded_keys(expected)
    excluded_violations = sum(
        count
        for signal, count in predicted_counts.items()
        if (signal.runtime, signal.session_id, signal.source_line) in excluded
    )
    manifest_completeness = tuple(
        (runtime.value, bool(manifests[runtime].complete))
        for runtime in (Runtime.CLAUDE, Runtime.CODEX)
    )
    fresh_manifest_completeness = tuple(
        (runtime.value, bool(fresh_manifests[runtime].complete))
        for runtime in (Runtime.CLAUDE, Runtime.CODEX)
    )
    malformed_lines = tuple(
        (
            runtime.value,
            sum(source.malformed_lines for source in manifests[runtime].source_files),
        )
        for runtime in (Runtime.CLAUDE, Runtime.CODEX)
    )
    manifests_complete = all(value for unused_runtime, value in manifest_completeness)
    subagents_filtered = all(
        all("subagents" not in Path(source.source_path).parts for source in manifest.source_files)
        for manifest in manifests.values()
    )
    malformed_cases_checked = _check_malformed_fixtures(fixtures, scope)
    batch_coverage_complete, batch_failures, batch_report_counts = _batch_coverage(manifests)
    ranking = _score_ranking(all_signals)
    fresh_ranking = _score_ranking(fresh_signals)
    reordered_ranking = _score_ranking(_interleave_signals(all_signals))
    raw_expected_ranking = expected.get("expected_ranking")
    if not isinstance(raw_expected_ranking, list) or not all(isinstance(item, str) for item in raw_expected_ranking):
        raise ValueError("expected_ranking must be an array of strings")
    expected_ranking = tuple(raw_expected_ranking)
    expected_manifests = expected.get("manifests_complete")
    expected_malformed = expected.get("malformed_lines")
    expected_batch_counts = expected.get("batch_report_counts")
    expected_report_failures = expected.get("report_validation_failures")
    score_expected = expected.get("score_is_deterministic")
    batch_expected = expected.get("batch_coverage_complete")
    expected_matches = (
        normalized == expected_values
        and fresh_normalized == expected_values
        and precision == 1.0
        and recall == 1.0
        and false_positives == 0
        and excluded_violations == 0
        and dict(manifest_completeness) == expected_manifests
        and fresh_manifest_completeness == manifest_completeness
        and dict(malformed_lines) == expected_malformed
        and dict(batch_report_counts) == expected_batch_counts
        and batch_failures == expected_report_failures
        and batch_coverage_complete is batch_expected
        and (ranking == fresh_ranking == reordered_ranking == expected_ranking)
        and isinstance(score_expected, bool)
        and (ranking == fresh_ranking == reordered_ranking == expected_ranking) is score_expected
        and subagents_filtered
        and malformed_cases_checked
    )
    return EvaluationResult(
        precision=precision,
        recall=recall,
        false_positives=false_positives,
        manifests_complete=manifests_complete,
        batch_coverage_complete=batch_coverage_complete,
        score_is_deterministic=(ranking == fresh_ranking == reordered_ranking == expected_ranking),
        report_validation_failures=batch_failures,
        expected_matches=expected_matches,
        retained_signals=normalized,
        excluded_violations=excluded_violations,
        malformed_cases_checked=malformed_cases_checked,
        subagents_filtered=subagents_filtered,
        manifest_completeness=manifest_completeness,
        malformed_lines=malformed_lines,
        batch_report_counts=batch_report_counts,
        ranking=ranking,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=Path("fixtures/evaluation"))
    args = parser.parse_args(argv)
    try:
        result = evaluate_fixtures(args.fixtures)
    except (OSError, TypeError, ValueError, KeyError) as exc:
        print(f"evaluation: {exc}", file=sys.stderr)
        return 2
    print(result.to_json(), end="")
    return 0 if result.expected_matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
