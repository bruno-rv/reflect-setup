#!/usr/bin/env python3
"""Stable finding IDs, reconciliation requests, and grouping validation.

Different miner batches may name the same recurring problem differently.
This module assigns every merged finding a deterministic ID from its
manifest-bound evidence, asks the host reconciler to group those IDs, and
validates the returned grouping before ranking consumes it. The reconciler
may group or rename findings but cannot change counts, classifications,
projects, or evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from miner_contract import (
    EvidenceRef,
    Finding,
    FindingType,
    ReportValidationError,
    _parse_constant,
    _reject_duplicate_keys,
    normalize_cluster_key,
)
from runtime import Runtime


UTC = timezone.utc
_KEBAB_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "runtime",
        "run_id",
        "items",
    }
)
_ITEM_FIELDS = frozenset(
    {
        "finding_id",
        "proposed_key",
        "finding_type",
        "paraphrase",
        "occurrence_count",
        "projects",
        "first_seen",
        "last_seen",
    }
)
_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "runtime",
        "run_id",
        "groups",
    }
)
_GROUP_FIELDS = frozenset(
    {
        "cluster_key",
        "summary",
        "rationale",
        "member_finding_ids",
    }
)


def _frozen_dataclass(cls):
    kwargs = {"frozen": True}
    if sys.version_info >= (3, 10):
        kwargs["slots"] = True
    return dataclass(**kwargs)(cls)


@_frozen_dataclass
class ReconciliationItem:
    finding_id: str
    proposed_key: str
    finding_type: FindingType
    paraphrase: str
    occurrence_count: int
    projects: tuple[str, ...]
    first_seen: datetime
    last_seen: datetime


@_frozen_dataclass
class ReconciliationRequest:
    schema_version: int
    runtime: Runtime
    run_id: str
    items: tuple[ReconciliationItem, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runtime": self.runtime.value,
            "run_id": self.run_id,
            "items": [
                {
                    "finding_id": item.finding_id,
                    "proposed_key": item.proposed_key,
                    "finding_type": item.finding_type.value,
                    "paraphrase": item.paraphrase,
                    "occurrence_count": item.occurrence_count,
                    "projects": list(item.projects),
                    "first_seen": _timestamp_text(item.first_seen),
                    "last_seen": _timestamp_text(item.last_seen),
                }
                for item in self.items
            ],
        }


@_frozen_dataclass
class ReconciliationGroup:
    cluster_key: str
    summary: str
    rationale: str
    member_finding_ids: tuple[str, ...]


@_frozen_dataclass
class ReconciliationReport:
    schema_version: int
    runtime: Runtime
    run_id: str
    groups: tuple[ReconciliationGroup, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runtime": self.runtime.value,
            "run_id": self.run_id,
            "groups": [
                {
                    "cluster_key": group.cluster_key,
                    "summary": group.summary,
                    "rationale": group.rationale,
                    "member_finding_ids": list(group.member_finding_ids),
                }
                for group in self.groups
            ],
        }


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


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail(f"{label} must be an array")
    values = tuple(_non_empty_string(item, f"{label}[{index}]") for index, item in enumerate(value))
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
    if not isinstance(value, str) or not value:
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


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail(f"{label} must be a positive integer")
    return value


def _loads(raw: str, label: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        _fail(f"{label} must be a JSON string")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_parse_constant,
        )
    except ReportValidationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        _fail(f"{label} must contain exactly one JSON object: {exc}")
    return _strict_object(value, label)


def _dumps(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _evidence_key(ref: EvidenceRef) -> tuple[str, int]:
    return ref.digest_path, ref.source_line


def finding_id(finding: Finding, runtime: Runtime) -> str:
    """Return the deterministic SHA-256 ID for one merged finding.

    The payload binds the finding's manifest-validated evidence, type,
    normalized proposed key, runtime, and session. Two findings with the same
    evidence and classification always produce the same ID; a renamed key
    changes the ID, which is why the reconciler must return the exact IDs it
    was given.
    """
    if not isinstance(finding, Finding):
        raise TypeError("finding_id expects a Finding value")
    if not isinstance(runtime, Runtime):
        raise TypeError("runtime must be a Runtime value")
    evidence = sorted(finding.evidence, key=lambda ref: (ref.digest_path, ref.source_line))
    keys = [_evidence_key(ref) for ref in evidence]
    if len(set(keys)) != len(keys):
        raise ReportValidationError("finding contains duplicate evidence pairs")
    payload = {
        "evidence": [[ref.digest_path, ref.source_line] for ref in evidence],
        "finding_type": finding.finding_type.value,
        "proposed_key": normalize_cluster_key(finding.cluster_key),
        "runtime": runtime.value,
        "session_id": finding.session_id,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_request(
    findings: Iterable[Finding],
    *,
    runtime: Runtime,
    run_id: str,
) -> ReconciliationRequest:
    """Build the host reconciliation request from merged findings."""
    values = tuple(findings)
    if not isinstance(runtime, Runtime):
        raise TypeError("runtime must be a Runtime value")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")
    items = []
    for finding in values:
        if not isinstance(finding, Finding):
            raise TypeError("findings must contain Finding values")
        timestamps = [ref.timestamp for ref in finding.evidence]
        if not timestamps:
            raise ReportValidationError("finding has no evidence timestamps")
        projects = sorted({ref.project for ref in finding.evidence})
        items.append(
            ReconciliationItem(
                finding_id=finding_id(finding, runtime),
                proposed_key=normalize_cluster_key(finding.cluster_key),
                finding_type=finding.finding_type,
                paraphrase=finding.paraphrase,
                occurrence_count=finding.occurrence_count,
                projects=tuple(projects),
                first_seen=min(timestamps),
                last_seen=max(timestamps),
            )
        )
    items.sort(key=lambda item: item.finding_id)
    return ReconciliationRequest(1, runtime, run_id, tuple(items))


def parse_request(raw: str, label: str = "reconciliation request") -> ReconciliationRequest:
    value = _loads(raw, label)
    _exact_fields(value, _REQUEST_FIELDS, label)
    if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
        _fail(f"{label}.schema_version must be 1")
    runtime = _runtime(value["runtime"], f"{label}.runtime")
    run_id = _non_empty_string(value["run_id"], f"{label}.run_id")
    raw_items = value["items"]
    if not isinstance(raw_items, list):
        _fail(f"{label}.items must be an array")
    items = []
    for index, item in enumerate(raw_items):
        item_value = _strict_object(item, f"{label}.items[{index}]")
        _exact_fields(item_value, _ITEM_FIELDS, f"{label}.items[{index}]")
        finding_id_value = _non_empty_string(
            item_value["finding_id"], f"{label}.items[{index}].finding_id"
        )
        if len(finding_id_value) != 64 or any(
            character not in "0123456789abcdef" for character in finding_id_value
        ):
            _fail(f"{label}.items[{index}].finding_id must be a 64-character hex digest")
        proposed_key = _non_empty_string(
            item_value["proposed_key"], f"{label}.items[{index}].proposed_key"
        )
        finding_type_value = item_value["finding_type"]
        if not isinstance(finding_type_value, str):
            _fail(f"{label}.items[{index}].finding_type must be a finding type")
        try:
            finding_type = FindingType(finding_type_value)
        except ValueError:
            _fail(f"{label}.items[{index}].finding_type must be a finding type")
        paraphrase = _non_empty_string(
            item_value["paraphrase"],
            f"{label}.items[{index}].paraphrase",
            one_line=True,
        )
        occurrence_count = _positive_integer(
            item_value["occurrence_count"], f"{label}.items[{index}].occurrence_count"
        )
        projects = _string_list(
            item_value["projects"], f"{label}.items[{index}].projects"
        )
        first_seen = _timestamp(
            item_value["first_seen"], f"{label}.items[{index}].first_seen"
        )
        last_seen = _timestamp(
            item_value["last_seen"], f"{label}.items[{index}].last_seen"
        )
        items.append(
            ReconciliationItem(
                finding_id_value,
                proposed_key,
                finding_type,
                paraphrase,
                occurrence_count,
                projects,
                first_seen,
                last_seen,
            )
        )
    if len({item.finding_id for item in items}) != len(items):
        _fail(f"{label}.items must not contain duplicate finding ids")
    return ReconciliationRequest(1, runtime, run_id, tuple(items))


def parse_report(
    raw: str,
    request: ReconciliationRequest,
    label: str = "reconciliation report",
) -> ReconciliationReport:
    """Parse and validate one reconciler report against its request."""
    if not isinstance(request, ReconciliationRequest):
        raise TypeError("request must be a ReconciliationRequest value")
    value = _loads(raw, label)
    _exact_fields(value, _REPORT_FIELDS, label)
    if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
        _fail(f"{label}.schema_version must be 1")
    runtime = _runtime(value["runtime"], f"{label}.runtime")
    if runtime is not request.runtime:
        _fail(f"{label}.runtime does not match the request")
    run_id = _non_empty_string(value["run_id"], f"{label}.run_id")
    if run_id != request.run_id:
        _fail(f"{label}.run_id does not match the request")
    raw_groups = value["groups"]
    if not isinstance(raw_groups, list):
        _fail(f"{label}.groups must be an array")
    groups = []
    for index, group in enumerate(raw_groups):
        group_value = _strict_object(group, f"{label}.groups[{index}]")
        _exact_fields(group_value, _GROUP_FIELDS, f"{label}.groups[{index}]")
        cluster_key = _non_empty_string(
            group_value["cluster_key"], f"{label}.groups[{index}].cluster_key"
        )
        if not _KEBAB_SLUG.fullmatch(cluster_key):
            _fail(
                f"{label}.groups[{index}].cluster_key must be a kebab slug: {cluster_key}"
            )
        summary = _non_empty_string(
            group_value["summary"], f"{label}.groups[{index}].summary", one_line=True
        )
        rationale = _non_empty_string(
            group_value["rationale"],
            f"{label}.groups[{index}].rationale",
            one_line=True,
        )
        member_finding_ids = _string_list(
            group_value["member_finding_ids"],
            f"{label}.groups[{index}].member_finding_ids",
        )
        groups.append(
            ReconciliationGroup(cluster_key, summary, rationale, member_finding_ids)
        )
    return ReconciliationReport(1, runtime, run_id, tuple(groups))


def validate_groups(
    report: ReconciliationReport,
    request: ReconciliationRequest,
) -> tuple[ReconciliationGroup, ...]:
    """Validate the grouping against the request and return the groups.

    Every request ID must appear exactly once, no unknown ID may appear, and
    normalized cluster keys must be unique kebab slugs. The caller maps the
    validated member IDs back to the original findings.
    """
    if not isinstance(report, ReconciliationReport):
        raise TypeError("report must be a ReconciliationReport value")
    if not isinstance(request, ReconciliationRequest):
        raise TypeError("request must be a ReconciliationRequest value")
    if report.runtime is not request.runtime:
        _fail("reconciliation report runtime does not match the request")
    if report.run_id != request.run_id:
        _fail("reconciliation report run_id does not match the request")
    by_id = {item.finding_id: item for item in request.items}
    if len(by_id) != len(request.items):
        _fail("reconciliation request contains duplicate finding ids")
    seen: set[str] = set()
    normalized_keys: set[str] = set()
    for group in report.groups:
        if not isinstance(group, ReconciliationGroup):
            raise TypeError("report groups must contain ReconciliationGroup values")
        normalized = normalize_cluster_key(group.cluster_key)
        if normalized in normalized_keys:
            _fail(f"reconciliation groups normalize to the same key: {group.cluster_key}")
        normalized_keys.add(normalized)
        for finding_id_value in group.member_finding_ids:
            if finding_id_value not in by_id:
                _fail(f"reconciliation report references unknown finding id: {finding_id_value}")
            if finding_id_value in seen:
                _fail(f"reconciliation report assigns finding id more than once: {finding_id_value}")
            seen.add(finding_id_value)
    missing = sorted(set(by_id) - seen)
    if missing:
        _fail("reconciliation report is missing finding ids: " + ", ".join(missing))
    return report.groups


__all__ = [
    "ReconciliationGroup",
    "ReconciliationItem",
    "ReconciliationReport",
    "ReconciliationRequest",
    "build_request",
    "finding_id",
    "parse_report",
    "parse_request",
    "validate_groups",
]
