"""Deterministic trend metrics and candidate ranking for reflection findings."""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

from miner_contract import EvidenceRef, Finding, FindingType


UTC = timezone.utc
ImplementationCost = Literal["S", "M", "L"]
_IMPLEMENTATION_COSTS = frozenset(("S", "M", "L"))
_IMPACT_WEIGHTS: dict[FindingType, float] = {
    FindingType.FAILURE: 1.0,
    FindingType.COMPLAINT: 0.9,
    FindingType.CORRECTION: 0.8,
    FindingType.FRICTION: 0.7,
}
_EMPTY_TIMESTAMP = datetime.min.replace(tzinfo=UTC)


def _frozen_dataclass(cls):
    kwargs = {"frozen": True}
    if sys.version_info >= (3, 10):
        kwargs["slots"] = True
    return dataclass(**kwargs)(cls)


@_frozen_dataclass
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
    implementation_cost: ImplementationCost


@_frozen_dataclass
class CandidateScore:
    cluster_key: str
    score: float
    metrics: TrendMetrics
    rationale: tuple[str, ...]


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence must be a number between 0 and 1")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("confidence must be a finite number")
    return max(0.0, min(value, 1.0))


def _implementation_cost(value: object) -> ImplementationCost:
    if value not in _IMPLEMENTATION_COSTS:
        raise ValueError("implementation_cost must be one of: S, M, L")
    return value  # type: ignore[return-value]


def _utc_timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("evidence timestamps must be datetime values")
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _project_key(digest_path: str) -> str:
    """Derive a stable project key from a collision-safe digest filename.

    The digest writer encodes the source path as ``project__session--hash``.
    Simple fixture paths such as ``project.md`` naturally use their stem.
    """
    if not isinstance(digest_path, str) or not digest_path:
        raise ValueError("evidence digest_path must be a non-empty string")
    stem = Path(digest_path).name.rsplit(".", 1)[0]
    readable = stem.split("--", 1)[0]
    return readable.split("__", 1)[0] or readable


def _finding_values(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    values = tuple(findings)
    for finding in values:
        if not isinstance(finding, Finding):
            raise TypeError("findings must contain Finding values")
        if isinstance(finding.occurrence_count, bool) or not isinstance(finding.occurrence_count, int):
            raise TypeError("finding occurrence_count must be an integer")
        if finding.occurrence_count < 0:
            raise ValueError("finding occurrence_count must be non-negative")
        if not isinstance(finding.finding_type, FindingType):
            raise TypeError("finding finding_type must be a FindingType")
        for evidence in finding.evidence:
            if not isinstance(evidence, EvidenceRef):
                raise TypeError("finding evidence must contain EvidenceRef values")
    return values


def compute_metrics(
    findings: Iterable[Finding],
    analyzed_sessions: int,
    regression_count: int,
    confidence: float,
    implementation_cost: ImplementationCost,
) -> TrendMetrics:
    """Compute normalized, runtime-neutral metrics for one merged cluster."""
    values = _finding_values(findings)
    analyzed_sessions = _nonnegative_integer(analyzed_sessions, "analyzed_sessions")
    regression_count = _nonnegative_integer(regression_count, "regression_count")
    confidence = _confidence(confidence)
    implementation_cost = _implementation_cost(implementation_cost)

    timestamps: list[datetime] = []
    sessions: set[str] = set()
    projects: set[str] = set()
    finding_types: list[FindingType] = []
    occurrences = 0
    for finding in values:
        occurrences += finding.occurrence_count
        sessions.add(finding.session_id)
        finding_types.append(finding.finding_type)
        for evidence in finding.evidence:
            timestamps.append(_utc_timestamp(evidence.timestamp))
            projects.add(_project_key(evidence.digest_path))

    first_seen = min(timestamps) if timestamps else _EMPTY_TIMESTAMP
    last_seen = max(timestamps) if timestamps else _EMPTY_TIMESTAMP
    distinct_days = len({timestamp.date() for timestamp in timestamps})
    occurrences_per_100_sessions = (
        round(occurrences * 100.0 / analyzed_sessions, 4) if analyzed_sessions else 0.0
    )
    impact = (
        round(
            sum(_IMPACT_WEIGHTS[finding_type] for finding_type in finding_types)
            / len(finding_types),
            4,
        )
        if finding_types
        else 0.0
    )

    return TrendMetrics(
        occurrences=occurrences,
        sessions=len(sessions),
        projects=len(projects),
        distinct_days=distinct_days,
        analyzed_sessions=analyzed_sessions,
        first_seen=first_seen,
        last_seen=last_seen,
        occurrences_per_100_sessions=occurrences_per_100_sessions,
        regression_count=regression_count,
        confidence=confidence,
        impact=impact,
        implementation_cost=implementation_cost,
    )


def _component_rationale(name: str, raw_value: object, contribution: float) -> str:
    return f"{name}={raw_value} contributes {contribution:+.4f}"


def _raw_metrics_rationale(metrics: TrendMetrics) -> str:
    return (
        "raw metrics: "
        f"occurrences={metrics.occurrences}, sessions={metrics.sessions}, "
        f"projects={metrics.projects}, distinct_days={metrics.distinct_days}, "
        f"analyzed_sessions={metrics.analyzed_sessions}, "
        f"first_seen={metrics.first_seen.isoformat()}, last_seen={metrics.last_seen.isoformat()}, "
        f"occurrences_per_100_sessions={metrics.occurrences_per_100_sessions}, "
        f"regression_count={metrics.regression_count}, confidence={metrics.confidence}, "
        f"impact={metrics.impact}, implementation_cost={metrics.implementation_cost}"
    )


def score_candidate(cluster_key: str, metrics: TrendMetrics) -> CandidateScore:
    """Apply the fixed score formula and retain an auditable rationale."""
    if not isinstance(cluster_key, str) or not cluster_key.strip():
        raise ValueError("cluster_key must be a non-empty string")
    if not isinstance(metrics, TrendMetrics):
        raise TypeError("metrics must be a TrendMetrics value")

    occurrence_component = 0.30 * min(metrics.occurrences / 10, 1.0)
    session_component = 0.20 * min(metrics.sessions / 5, 1.0)
    project_component = 0.15 * min(metrics.projects / 3, 1.0)
    days_component = 0.15 * min(metrics.distinct_days / 5, 1.0)
    confidence_component = 0.10 * metrics.confidence
    impact_component = 0.10 * metrics.impact
    regression_component = -0.10 * min(
        metrics.regression_count / max(metrics.occurrences, 1), 1.0
    )
    score = round(
        occurrence_component
        + session_component
        + project_component
        + days_component
        + confidence_component
        + impact_component
        + regression_component,
        4,
    )

    rationale = []
    components = (
        ("occurrences", metrics.occurrences, occurrence_component),
        ("sessions", metrics.sessions, session_component),
        ("projects", metrics.projects, project_component),
        ("distinct_days", metrics.distinct_days, days_component),
        ("confidence", metrics.confidence, confidence_component),
        ("impact", metrics.impact, impact_component),
        ("regression_count", metrics.regression_count, regression_component),
    )
    for name, raw_value, contribution in components:
        if contribution:
            rationale.append(_component_rationale(name, raw_value, contribution))
    rationale.append(_raw_metrics_rationale(metrics))
    return CandidateScore(cluster_key.strip(), score, metrics, tuple(rationale))


def rank_candidates(candidates: Iterable[CandidateScore]) -> tuple[CandidateScore, ...]:
    """Return candidates in a stable, score-first order."""
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                -candidate.score,
                candidate.metrics.regression_count,
                candidate.metrics.first_seen,
                candidate.cluster_key,
            ),
        )
    )
