"""Deterministic trend metrics and candidate ranking for reflection findings."""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from miner_contract import EvidenceRef, Finding, FindingType, normalize_cluster_key


UTC = timezone.utc
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
class CandidateEvidence:
    """Report-view evidence that retains its owning session.

    This is a derived view of validated miner evidence: the session identity
    comes from the finding, while the remaining fields are copied from the
    manifest-bound ``EvidenceRef``.
    """

    session_id: str
    digest_path: str
    source_line: int
    timestamp: datetime
    kind: FindingType
    project: str
    occurrence_count: int


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


@_frozen_dataclass
class CandidateScore:
    cluster_key: str
    summary: str
    finding_types: tuple[str, ...]
    paraphrases: tuple[str, ...]
    evidence: tuple[CandidateEvidence, ...]
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
    if not 0.0 <= value <= 1.0:
        raise ValueError("confidence must be a number between 0 and 1")
    return value


def _utc_timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("evidence timestamps must be datetime values")
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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
            if not isinstance(evidence.project, str) or not evidence.project.strip():
                raise ValueError("finding evidence must contain explicit project identity")
    return values


def _occurrence_weighted_mean(
    values: tuple[Finding, ...],
    value_of: Callable[[Finding], float],
) -> float:
    total_occurrences = sum(finding.occurrence_count for finding in values)
    if total_occurrences <= 0:
        return 0.0
    weighted = sum(
        finding.occurrence_count * value_of(finding)
        for finding in values
    )
    return round(weighted / total_occurrences, 4)


def compute_metrics(
    findings: Iterable[Finding],
    analyzed_sessions: int,
    regression_count: int,
) -> TrendMetrics:
    """Compute normalized, runtime-neutral metrics for one merged cluster.

    Confidence and impact are derived from the grouped findings themselves
    (occurrence-weighted means) rather than supplied by the caller, so a
    ranking cannot be steered by invented confidence or cost values.
    """
    values = _finding_values(findings)
    analyzed_sessions = _nonnegative_integer(analyzed_sessions, "analyzed_sessions")
    regression_count = _nonnegative_integer(regression_count, "regression_count")

    timestamps: list[datetime] = []
    sessions: set[str] = set()
    projects: set[str] = set()
    occurrences = 0
    for finding in values:
        occurrences += finding.occurrence_count
        sessions.add(finding.session_id)
        for evidence in finding.evidence:
            timestamps.append(_utc_timestamp(evidence.timestamp))
            projects.add(evidence.project)

    first_seen = min(timestamps) if timestamps else _EMPTY_TIMESTAMP
    last_seen = max(timestamps) if timestamps else _EMPTY_TIMESTAMP
    distinct_days = len({timestamp.date() for timestamp in timestamps})
    occurrences_per_100_sessions = (
        round(occurrences * 100.0 / analyzed_sessions, 4) if analyzed_sessions else 0.0
    )
    confidence = _occurrence_weighted_mean(
        values,
        lambda finding: _confidence(finding.confidence),
    )
    impact = _occurrence_weighted_mean(
        values,
        lambda finding: _IMPACT_WEIGHTS[finding.finding_type],
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
        f"impact={metrics.impact}"
    )


def _candidate_evidence(values: tuple[Finding, ...]) -> tuple[CandidateEvidence, ...]:
    """Derive deduplicated, session-bound report evidence for one cluster."""
    seen: set[tuple[str, str, int]] = set()
    items: list[CandidateEvidence] = []
    for finding in values:
        for ref in finding.evidence:
            key = (finding.session_id, ref.digest_path, ref.source_line)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                CandidateEvidence(
                    session_id=finding.session_id,
                    digest_path=ref.digest_path,
                    source_line=ref.source_line,
                    timestamp=_utc_timestamp(ref.timestamp),
                    kind=ref.kind,
                    project=ref.project,
                    occurrence_count=ref.occurrence_count,
                )
            )
    return tuple(
        sorted(
            items,
            key=lambda item: (
                item.timestamp,
                item.project,
                item.session_id,
                item.digest_path,
                item.source_line,
            ),
        )
    )


def score_candidate(
    cluster_key: str,
    summary: str,
    findings: Iterable[Finding],
    metrics: TrendMetrics,
) -> CandidateScore:
    """Apply the fixed score formula and retain an auditable rationale."""
    if not isinstance(cluster_key, str) or not cluster_key.strip():
        raise ValueError("cluster_key must be a non-empty string")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary must be a non-empty string")
    if not isinstance(metrics, TrendMetrics):
        raise TypeError("metrics must be a TrendMetrics value")
    values = _finding_values(findings)
    if not values:
        raise ValueError("findings must contain at least one finding")

    occurrence_component = 0.30 * min(metrics.occurrences / 10, 1.0)
    session_component = 0.20 * min(metrics.sessions / 5, 1.0)
    project_component = 0.15 * min(metrics.projects / 3, 1.0)
    days_component = 0.15 * min(metrics.distinct_days / 5, 1.0)
    confidence_component = 0.10 * metrics.confidence
    impact_component = 0.10 * metrics.impact
    regression_component = 0.10 * min(metrics.regression_count, 1)
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
    return CandidateScore(
        cluster_key.strip(),
        summary.strip(),
        tuple(sorted({finding.finding_type.value for finding in values})),
        tuple(sorted({finding.paraphrase for finding in values})),
        _candidate_evidence(values),
        score,
        metrics,
        tuple(rationale),
    )


def rank_candidates(candidates: Iterable[CandidateScore]) -> tuple[CandidateScore, ...]:
    """Return candidates in a stable, score-first order.

    A fix that failed in real use (a regression) rises rather than falls: the
    regression bonus is already inside the score, and the tie-break prefers
    the candidate with a regression before first-seen date.
    """
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                -candidate.score,
                -min(candidate.metrics.regression_count, 1),
                candidate.metrics.first_seen,
                candidate.cluster_key,
            ),
        )
    )


__all__ = [
    "CandidateEvidence",
    "CandidateScore",
    "TrendMetrics",
    "compute_metrics",
    "rank_candidates",
    "score_candidate",
]
