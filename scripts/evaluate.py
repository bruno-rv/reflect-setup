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


def _stage_runtime(fixtures: Path, runtime: Runtime, root: Path) -> Path:
    """Build the runtime's source layout while preserving fixture bytes."""
    if runtime is Runtime.CLAUDE:
        source_root = root / "projects"
        canonical = source_root / "synthetic-project" / "canonical.jsonl"
        subagent = source_root / "synthetic-project" / "subagents" / "subagent.jsonl"
    else:
        source_root = root / "sessions"
        canonical = source_root / "canonical.jsonl"
        subagent = source_root / "subagents" / "subagent.jsonl"
    source_dir = fixtures / runtime.value
    _copy_with_mtime(source_dir / "canonical.jsonl", canonical, _OLD_MTIME)
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
        path = fixtures / runtime.value / "subagent.jsonl"
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


def _batch_coverage(manifests: Mapping[Runtime, object]) -> tuple[bool, int]:
    failures = 0
    complete = True
    for runtime, manifest in manifests.items():
        paths = tuple(
            sorted(source.digest_path for source in manifest.source_files if source.digest_path is not None)
        )
        report = MinerReport(
            schema_version=1,
            runtime=runtime,
            run_id=manifest.run_id,
            batch_id=f"evaluation-{runtime.value}",
            digest_paths=paths,
            findings=(),
            themes=(),
        )
        try:
            merge_reports((report,), manifest)
        except (TypeError, ValueError):
            failures += 1
            complete = False
    return complete, failures


def _score_determinism(signals: Iterable[Signal]) -> bool:
    signals = tuple(signals)
    type_map = {
        "user": FindingType.CORRECTION,
        "error": FindingType.FAILURE,
        "interrupt": FindingType.FRICTION,
    }
    findings: list[Finding] = []
    for index, signal in enumerate(signals):
        finding_type = type_map[signal.kind.value]
        findings.append(
            Finding(
                cluster_key=f"synthetic-{signal.kind.value}-{index}",
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
            analyzed_sessions=len({item.session_id for item in signals}),
            regression_count=0,
            confidence=finding.confidence,
            implementation_cost="S",
        )
        candidates.append(score_candidate(finding.cluster_key, metrics))
    first = rank_candidates(candidates)
    second = rank_candidates(candidates)
    return first == second


def evaluate_fixtures(fixtures: Path) -> EvaluationResult:
    """Run both runtime adapters and return stable, inspectable metrics."""
    fixtures = Path(fixtures).expanduser().resolve()
    expected = _load_expected(fixtures)
    since = _timestamp(expected.get("window_since"))
    scope = Scope(since=since, project_filter=None, include_subagents=False)
    all_signals: list[tuple[Runtime, Signal]] = []
    manifests = {}

    with tempfile.TemporaryDirectory(prefix="reflect-evaluation-") as raw:
        root = Path(raw)
        for runtime in (Runtime.CLAUDE, Runtime.CODEX):
            source_root = _stage_runtime(fixtures, runtime, root / runtime.value)
            spec = resolve_runtime(runtime.value, home=root / runtime.value, env={}, source_root=source_root)
            out_dir = root / f"digest-{runtime.value}"
            try:
                manifest = run_digest(spec, scope, out_dir)
            except IncompleteDigestError as exc:
                manifest = exc.manifest
            manifests[runtime] = manifest
            all_signals.extend((runtime, signal) for signal in _signals_for_manifest(spec, scope, manifest))

    normalized = tuple(
        sorted(
            (
                NormalizedSignal(runtime.value, signal.session_id, signal.source_line, signal.kind.value)
                for runtime, signal in all_signals
            ),
            key=lambda signal: (signal.runtime, signal.session_id, signal.source_line, signal.kind),
        )
    )
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
    manifests_complete = all(bool(manifest.complete) for manifest in manifests.values())
    subagents_filtered = all(
        all("subagents" not in Path(source.source_path).parts for source in manifest.source_files)
        for manifest in manifests.values()
    )
    malformed_cases_checked = _check_malformed_fixtures(fixtures, scope)
    batch_coverage_complete, batch_failures = _batch_coverage(manifests)
    score_is_deterministic = _score_determinism(signal for unused_runtime, signal in all_signals)
    expected_matches = (
        normalized == expected_values
        and precision == 1.0
        and recall == 1.0
        and false_positives == 0
        and excluded_violations == 0
        and manifests_complete
        and subagents_filtered
        and malformed_cases_checked
        and batch_coverage_complete
        and score_is_deterministic
    )
    return EvaluationResult(
        precision=precision,
        recall=recall,
        false_positives=false_positives,
        manifests_complete=manifests_complete,
        batch_coverage_complete=batch_coverage_complete,
        score_is_deterministic=score_is_deterministic,
        report_validation_failures=batch_failures,
        expected_matches=expected_matches,
        retained_signals=normalized,
        excluded_violations=excluded_violations,
        malformed_cases_checked=malformed_cases_checked,
        subagents_filtered=subagents_filtered,
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
