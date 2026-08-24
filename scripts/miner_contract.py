#!/usr/bin/env python3
"""Strict, typed contract for miner JSON reports."""
from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from digest import DigestManifest
from runtime import Runtime


UTC = timezone.utc
_TOP_FIELDS = frozenset(
    {"schema_version", "runtime", "run_id", "batch_id", "digest_paths", "findings", "themes"}
)
_FINDING_FIELDS = frozenset(
    {
        "cluster_key",
        "finding_type",
        "session_id",
        "paraphrase",
        "occurrence_count",
        "confidence",
        "evidence",
    }
)
_EVIDENCE_FIELDS = frozenset({"digest_path", "source_line", "timestamp", "kind", "project"})
_RFC3339_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _frozen_dataclass(cls):
    kwargs = {"frozen": True}
    if sys.version_info >= (3, 10):
        kwargs["slots"] = True
    return dataclass(**kwargs)(cls)


class FindingType(str, Enum):
    CORRECTION = "correction"
    FRICTION = "friction"
    FAILURE = "failure"
    COMPLAINT = "complaint"


@_frozen_dataclass
class EvidenceRef:
    digest_path: str
    source_line: int
    timestamp: datetime
    kind: FindingType
    project: str


@_frozen_dataclass
class Finding:
    cluster_key: str
    finding_type: FindingType
    session_id: str
    paraphrase: str
    occurrence_count: int
    confidence: float
    evidence: tuple[EvidenceRef, ...]


@_frozen_dataclass
class MinerReport:
    schema_version: int
    runtime: Runtime
    run_id: str
    batch_id: str
    digest_paths: tuple[str, ...]
    findings: tuple[Finding, ...]
    themes: tuple[str, ...]


@_frozen_dataclass
class BatchSpec:
    batch_id: str
    digest_paths: tuple[str, ...]


class ReportValidationError(ValueError):
    """Raised when miner output or batch coverage violates the contract."""


def _fail(message: str):
    raise ReportValidationError(message)


def _strict_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return value


def _exact_fields(value: Mapping[str, Any], expected: frozenset[str], label: str):
    actual = set(value)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        _fail(f"{label} missing fields: {', '.join(missing)}")
    if extra:
        _fail(f"{label} has unexpected fields: {', '.join(extra)}")


def _non_empty_string(value: Any, label: str, *, one_line: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be a non-empty string")
    if one_line and ("\n" in value or "\r" in value):
        _fail(f"{label} must be one line")
    return value.strip()


def _string_list(value: Any, label: str, *, max_items: int | None = None) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail(f"{label} must be an array")
    if max_items is not None and len(value) > max_items:
        _fail(f"{label} must contain at most {max_items} items")
    values = tuple(_non_empty_string(item, f"{label}[{index}") for index, item in enumerate(value))
    if len(set(values)) != len(values):
        _fail(f"{label} must not contain duplicates")
    return values


def _runtime(value: Any, label: str) -> Runtime:
    if not isinstance(value, str):
        _fail(f"{label} must be claude or codex")
    try:
        return Runtime(value)
    except ValueError:
        _fail(f"{label} must be claude or codex")


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not _RFC3339_DATETIME.fullmatch(value):
        _fail(f"{label} must be an RFC 3339 timestamp")
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        _fail(f"{label} must be an RFC 3339 timestamp")
    if parsed.tzinfo is None:
        _fail(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _manifest_paths(manifest: DigestManifest) -> frozenset[str]:
    paths = [source.digest_path for source in manifest.source_files if source.digest_path is not None]
    if len(set(paths)) != len(paths):
        _fail("manifest contains duplicate digest paths")
    return frozenset(paths)


def _manifest_projects(manifest: DigestManifest) -> dict[str, str]:
    projects = {}
    for source in manifest.source_files:
        if source.digest_path is None:
            continue
        project = _non_empty_string(source.project, f"manifest project for {source.digest_path}")
        if source.digest_path in projects and projects[source.digest_path] != project:
            _fail(f"manifest contains conflicting project metadata for {source.digest_path}")
        projects[source.digest_path] = project
    return projects


def _evidence_key(ref: EvidenceRef) -> tuple[str, int]:
    return ref.digest_path, ref.source_line


def _normalize_cluster_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _parse_constant(value: str):
    _fail(f"invalid JSON constant: {value}")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _parse_evidence(
    raw: Any,
    *,
    finding_type: FindingType,
    manifest_paths: frozenset[str],
    manifest_projects: Mapping[str, str],
    batch_paths: frozenset[str],
    index: int,
) -> EvidenceRef:
    value = _strict_object(raw, f"findings[].evidence[{index}]")
    _exact_fields(value, _EVIDENCE_FIELDS, f"findings[].evidence[{index}]")
    digest_path = _non_empty_string(value["digest_path"], "evidence.digest_path")
    if digest_path not in manifest_paths:
        _fail(f"evidence digest path is missing from manifest: {digest_path}")
    if digest_path not in batch_paths:
        _fail(f"evidence digest path is outside assigned batch: {digest_path}")
    project = _non_empty_string(value["project"], "evidence.project")
    expected_project = manifest_projects.get(digest_path)
    if expected_project is None:
        _fail(f"evidence project metadata is missing from manifest: {digest_path}")
    if project != expected_project:
        _fail(f"evidence.project does not match manifest metadata for {digest_path}")
    source_line = value["source_line"]
    if isinstance(source_line, bool) or not isinstance(source_line, int) or source_line <= 0:
        _fail("evidence.source_line must be a positive integer")
    evidence_kind = value["kind"]
    if not isinstance(evidence_kind, str):
        _fail("evidence.kind must be a finding type")
    try:
        kind = FindingType(evidence_kind)
    except ValueError:
        _fail(f"unsupported evidence kind: {evidence_kind}")
    if kind is not finding_type:
        _fail("evidence.kind must match finding_type")
    return EvidenceRef(
        digest_path=digest_path,
        source_line=source_line,
        timestamp=_timestamp(value["timestamp"], "evidence.timestamp"),
        kind=kind,
        project=project,
    )


def _parse_finding(
    raw: Any,
    *,
    manifest_paths: frozenset[str],
    manifest_projects: Mapping[str, str],
    batch_paths: frozenset[str],
    index: int,
) -> Finding:
    value = _strict_object(raw, f"findings[{index}]")
    _exact_fields(value, _FINDING_FIELDS, f"findings[{index}]")
    cluster_key = _non_empty_string(value["cluster_key"], "finding.cluster_key")
    if not _normalize_cluster_key(cluster_key):
        _fail("finding.cluster_key must be non-empty")
    finding_type_value = value["finding_type"]
    if not isinstance(finding_type_value, str):
        _fail("finding.finding_type must be a finding type")
    try:
        finding_type = FindingType(finding_type_value)
    except ValueError:
        _fail(f"unsupported finding type: {finding_type_value}")
    session_id = _non_empty_string(value["session_id"], "finding.session_id")
    paraphrase = _non_empty_string(value["paraphrase"], "finding.paraphrase", one_line=True)
    occurrence_count = value["occurrence_count"]
    if isinstance(occurrence_count, bool) or not isinstance(occurrence_count, int) or occurrence_count <= 0:
        _fail("finding.occurrence_count must be a positive integer")
    confidence = value["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        _fail("finding.confidence must be a number between 0 and 1")
    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        _fail("finding.confidence must be a number between 0 and 1")
    raw_evidence = value["evidence"]
    if not isinstance(raw_evidence, list) or not raw_evidence:
        _fail("finding.evidence must contain at least one reference")
    evidence = []
    seen = set()
    for evidence_index, raw_ref in enumerate(raw_evidence):
        ref = _parse_evidence(
            raw_ref,
            finding_type=finding_type,
            manifest_paths=manifest_paths,
            manifest_projects=manifest_projects,
            batch_paths=batch_paths,
            index=evidence_index,
        )
        key = _evidence_key(ref)
        if key not in seen:
            seen.add(key)
            evidence.append(ref)
    return Finding(
        cluster_key=cluster_key,
        finding_type=finding_type,
        session_id=session_id,
        paraphrase=paraphrase,
        occurrence_count=occurrence_count,
        confidence=confidence,
        evidence=tuple(evidence),
    )


def parse_report(raw: str, manifest: DigestManifest, batch: BatchSpec) -> MinerReport:
    """Parse and validate one miner's JSON-only report for one assigned batch."""
    if not isinstance(raw, str):
        _fail("miner report must be a JSON string")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_parse_constant,
        )
    except ReportValidationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        _fail(f"miner report must contain exactly one JSON object: {exc}")
    value = _strict_object(value, "miner report")
    _exact_fields(value, _TOP_FIELDS, "miner report")
    if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
        _fail("schema_version must be 1")
    runtime = _runtime(value["runtime"], "runtime")
    if runtime is not manifest.runtime:
        _fail("runtime does not match manifest")
    run_id = _non_empty_string(value["run_id"], "run_id")
    if run_id != manifest.run_id:
        _fail("run_id does not match manifest")
    batch_id = _non_empty_string(value["batch_id"], "batch_id")
    if batch_id != batch.batch_id:
        _fail("batch_id does not match assigned batch")
    digest_paths = _string_list(value["digest_paths"], "digest_paths")
    assigned_paths = tuple(batch.digest_paths)
    if digest_paths != assigned_paths:
        _fail("digest_paths must exactly equal assigned batch paths")
    if len(set(assigned_paths)) != len(assigned_paths):
        _fail("assigned batch paths must not contain duplicates")
    manifest_paths = _manifest_paths(manifest)
    manifest_projects = _manifest_projects(manifest)
    batch_path_set = frozenset(assigned_paths)
    if not batch_path_set.issubset(manifest_paths):
        missing = sorted(batch_path_set - manifest_paths)
        _fail(f"batch digest path is missing from manifest: {', '.join(missing)}")
    raw_findings = value["findings"]
    if not isinstance(raw_findings, list):
        _fail("findings must be an array")
    findings = tuple(
        _parse_finding(
            finding,
            manifest_paths=manifest_paths,
            manifest_projects=manifest_projects,
            batch_paths=batch_path_set,
            index=index,
        )
        for index, finding in enumerate(raw_findings)
    )
    themes = _string_list(value["themes"], "themes", max_items=2)
    return MinerReport(1, runtime, run_id, batch_id, digest_paths, findings, themes)


def _validate_typed_finding(
    finding: Finding,
    *,
    report_paths: tuple[str, ...],
    manifest_paths: frozenset[str],
    manifest_projects: Mapping[str, str],
    label: str,
) -> None:
    if not isinstance(finding.finding_type, FindingType):
        _fail(f"{label} has an invalid finding type")
    _non_empty_string(finding.cluster_key, f"{label}.cluster_key")
    _non_empty_string(finding.session_id, f"{label}.session_id")
    _non_empty_string(finding.paraphrase, f"{label}.paraphrase", one_line=True)
    if (
        isinstance(finding.occurrence_count, bool)
        or not isinstance(finding.occurrence_count, int)
        or finding.occurrence_count <= 0
    ):
        _fail(f"{label}.occurrence_count must be a positive integer")
    if isinstance(finding.confidence, bool) or not isinstance(finding.confidence, (int, float)):
        _fail(f"{label}.confidence must be a number between 0 and 1")
    if not math.isfinite(float(finding.confidence)) or not 0 <= finding.confidence <= 1:
        _fail(f"{label}.confidence must be a number between 0 and 1")
    if not isinstance(finding.evidence, (tuple, list)) or not finding.evidence:
        _fail(f"{label}.evidence must contain at least one reference")
    seen = set()
    for evidence_index, ref in enumerate(finding.evidence):
        evidence_label = f"{label}.evidence[{evidence_index}]"
        if not isinstance(ref, EvidenceRef):
            _fail(f"{evidence_label} is invalid")
        digest_path = _non_empty_string(ref.digest_path, f"{evidence_label}.digest_path")
        if digest_path not in manifest_paths:
            _fail(f"{evidence_label}.digest_path is missing from manifest: {digest_path}")
        if digest_path not in report_paths:
            _fail(f"{evidence_label}.digest_path is outside assigned batch: {digest_path}")
        project = _non_empty_string(ref.project, f"{evidence_label}.project")
        expected_project = manifest_projects.get(digest_path)
        if expected_project is None or project != expected_project:
            _fail(f"{evidence_label}.project does not match manifest metadata")
        if isinstance(ref.source_line, bool) or not isinstance(ref.source_line, int) or ref.source_line <= 0:
            _fail(f"{evidence_label}.source_line must be a positive integer")
        if not isinstance(ref.timestamp, datetime) or ref.timestamp.tzinfo is None:
            _fail(f"{evidence_label}.timestamp must include a timezone")
        if not isinstance(ref.kind, FindingType) or ref.kind is not finding.finding_type:
            _fail(f"{evidence_label}.kind must match finding_type")
        key = _evidence_key(ref)
        if key in seen:
            continue
        seen.add(key)


def _typed_report_paths(report: MinerReport, manifest: DigestManifest) -> tuple[str, ...]:
    if not isinstance(report, MinerReport):
        _fail("merge_reports expects MinerReport values")
    if report.schema_version != 1 or isinstance(report.schema_version, bool):
        _fail(f"report {report.batch_id!r} has unsupported schema version")
    runtime = report.runtime if isinstance(report.runtime, Runtime) else _runtime(report.runtime, "report.runtime")
    if runtime is not manifest.runtime:
        _fail(f"report {report.batch_id!r} runtime does not match manifest")
    if not isinstance(report.batch_id, str) or not report.batch_id.strip():
        _fail("report.batch_id must be a non-empty string")
    if not isinstance(report.run_id, str) or not report.run_id.strip():
        _fail("report.run_id must be a non-empty string")
    if report.run_id != manifest.run_id:
        _fail(f"report {report.batch_id!r} run_id does not match manifest")
    if not isinstance(report.digest_paths, (tuple, list)):
        _fail(f"report {report.batch_id!r} digest_paths must be a sequence")
    paths = tuple(report.digest_paths)
    if any(not isinstance(path, str) or not path.strip() for path in paths):
        _fail(f"report {report.batch_id!r} digest_paths must contain non-empty strings")
    if len(set(paths)) != len(paths):
        _fail(f"report {report.batch_id!r} has duplicate digest paths")
    manifest_paths = _manifest_paths(manifest)
    manifest_projects = _manifest_projects(manifest)
    unknown = sorted(set(paths) - manifest_paths)
    if unknown:
        _fail(f"report digest path is missing from manifest: {', '.join(unknown)}")
    if not isinstance(report.findings, (tuple, list)):
        _fail(f"report {report.batch_id!r} findings must be a sequence")
    for finding_index, finding in enumerate(report.findings):
        if not isinstance(finding, Finding):
            _fail(f"report {report.batch_id!r} has an invalid finding")
        _validate_typed_finding(
            finding,
            report_paths=paths,
            manifest_paths=manifest_paths,
            manifest_projects=manifest_projects,
            label=f"report finding {finding_index}",
        )
    return paths


def merge_reports(reports: tuple[MinerReport, ...] | list[MinerReport], manifest: DigestManifest) -> tuple[Finding, ...]:
    """Validate complete batch coverage and deterministically merge findings."""
    report_values = tuple(reports)
    seen_batch_ids = set()
    assigned: dict[str, str] = {}
    for report in report_values:
        if not isinstance(report, MinerReport):
            _fail("merge_reports expects MinerReport values")
        if report.batch_id in seen_batch_ids:
            _fail(f"duplicate batch id: {report.batch_id}")
        seen_batch_ids.add(report.batch_id)
        for path in _typed_report_paths(report, manifest):
            previous = assigned.get(path)
            if previous is not None:
                _fail(f"batch coverage overlap for {path}: {previous} and {report.batch_id}")
            assigned[path] = report.batch_id

    expected = _manifest_paths(manifest)
    uncovered = sorted(expected - set(assigned))
    if uncovered:
        _fail(f"uncovered manifest digest paths: {', '.join(uncovered)}")

    groups: dict[tuple[str, FindingType, str], list[Finding]] = {}
    for report in report_values:
        for finding in report.findings:
            key = (
                _normalize_cluster_key(finding.cluster_key),
                finding.finding_type,
                finding.session_id,
            )
            groups.setdefault(key, []).append(finding)

    merged = []
    for (normalized_key, finding_type, session_id), findings in groups.items():
        all_evidence: dict[tuple[str, int], EvidenceRef] = {}
        merged_count = 0
        seen_observation_evidence = set()
        ordered_findings = sorted(
            findings,
            key=lambda item: (
                min((ref.timestamp, ref.digest_path, ref.source_line) for ref in item.evidence),
                item.session_id,
                item.finding_type.value,
                _normalize_cluster_key(item.paraphrase),
                item.occurrence_count,
                tuple(sorted(_evidence_key(ref) for ref in item.evidence)),
            ),
        )
        for finding in ordered_findings:
            finding_keys = {_evidence_key(ref) for ref in finding.evidence}
            if finding_keys - seen_observation_evidence:
                merged_count += finding.occurrence_count
                seen_observation_evidence.update(finding_keys)
            for ref in finding.evidence:
                key = _evidence_key(ref)
                existing = all_evidence.get(key)
                if existing is None or (
                    ref.timestamp,
                    ref.project,
                    ref.digest_path,
                    ref.source_line,
                ) < (
                    existing.timestamp,
                    existing.project,
                    existing.digest_path,
                    existing.source_line,
                ):
                    all_evidence[key] = ref
        evidence_values = sorted(
            all_evidence.values(),
            key=lambda ref: (ref.timestamp, ref.project, ref.digest_path, ref.source_line),
        )
        if not evidence_values:
            _fail(f"finding cluster has no evidence: {normalized_key}")
        representative = min(
            findings,
            key=lambda item: (_normalize_cluster_key(item.paraphrase), len(item.paraphrase), item.paraphrase),
        )
        merged.append(
            (
                normalized_key,
                session_id,
                finding_type.value,
                evidence_values[0].timestamp,
                Finding(
                    cluster_key=normalized_key,
                    finding_type=finding_type,
                    session_id=session_id,
                    paraphrase=representative.paraphrase,
                    occurrence_count=merged_count,
                    confidence=max(item.confidence for item in findings),
                    evidence=tuple(evidence_values),
                ),
            )
        )
    merged.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return tuple(item[4] for item in merged)


__all__ = [
    "BatchSpec",
    "EvidenceRef",
    "Finding",
    "FindingType",
    "MinerReport",
    "ReportValidationError",
    "merge_reports",
    "parse_report",
]
