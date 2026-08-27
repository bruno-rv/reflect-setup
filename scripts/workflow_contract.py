#!/usr/bin/env python3
"""Strict, frozen persisted workflow schemas and host-input codecs.

The dispatch plan, reconciliation request/report, and host input are the
replayable handoff artifacts between the Python core and the host workers.
Every schema is frozen, rejects unknown fields and duplicate JSON keys, and
round-trips through canonical JSON so a persisted artifact can be revalidated
on every continuation.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from apply import ApplyRequest, FixProof, WorkspaceState
from coverage_model import CoverageObservation
from miner_contract import (
    EvidenceRef,
    FindingType,
    ReportValidationError,
    _parse_constant,
    _reject_duplicate_keys,
)
from runtime import Runtime


UTC = timezone.utc


def _frozen_dataclass(cls):
    kwargs = {"frozen": True}
    if sys.version_info >= (3, 10):
        kwargs["slots"] = True
    return dataclass(**kwargs)(cls)


class RunStage(str, Enum):
    AWAITING_MINERS = "awaiting-miners"
    AWAITING_RECONCILIATION = "awaiting-reconciliation"
    DIAGNOSIS_COMPLETE = "diagnosis-complete"
    APPLY_PREVIEW = "apply-preview"
    APPLY_VALIDATED = "apply-validated"


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


def _path_list(value: Any, label: str) -> tuple[Path, ...]:
    return tuple(Path(item) for item in _string_list(value, label))


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


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{label} must be a boolean")
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


@_frozen_dataclass
class DispatchBatch:
    batch_id: str
    digest_paths: tuple[str, ...]
    report_path: str
    total_bytes: int

    def to_json(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "digest_paths": list(self.digest_paths),
            "report_path": self.report_path,
            "total_bytes": self.total_bytes,
        }

    @classmethod
    def from_json(cls, raw: Any, label: str) -> "DispatchBatch":
        value = _strict_object(raw, label)
        _exact_fields(
            value,
            frozenset({"batch_id", "digest_paths", "report_path", "total_bytes"}),
            label,
        )
        batch_id = _non_empty_string(value["batch_id"], f"{label}.batch_id")
        digest_paths = _string_list(value["digest_paths"], f"{label}.digest_paths")
        report_path = _non_empty_string(value["report_path"], f"{label}.report_path")
        total_bytes = _positive_integer(value["total_bytes"], f"{label}.total_bytes")
        return cls(batch_id, digest_paths, report_path, total_bytes)


@_frozen_dataclass
class DispatchPlan:
    schema_version: int
    runtime: Runtime
    run_id: str
    manifest_sha256: str
    batches: tuple[DispatchBatch, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runtime": self.runtime.value,
            "run_id": self.run_id,
            "manifest_sha256": self.manifest_sha256,
            "batches": [batch.to_json() for batch in self.batches],
        }

    @classmethod
    def from_json(cls, raw: Any, label: str) -> "DispatchPlan":
        value = _strict_object(raw, label)
        _exact_fields(
            value,
            frozenset(
                {"schema_version", "runtime", "run_id", "manifest_sha256", "batches"}
            ),
            label,
        )
        if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
            _fail(f"{label}.schema_version must be 1")
        runtime = _runtime(value["runtime"], f"{label}.runtime")
        run_id = _non_empty_string(value["run_id"], f"{label}.run_id")
        manifest_sha256 = _non_empty_string(
            value["manifest_sha256"], f"{label}.manifest_sha256"
        )
        if len(manifest_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in manifest_sha256
        ):
            _fail(f"{label}.manifest_sha256 must be a lowercase 64-character hex digest")
        raw_batches = value["batches"]
        if not isinstance(raw_batches, list) or not raw_batches:
            _fail(f"{label}.batches must be a non-empty array")
        batches = tuple(
            DispatchBatch.from_json(item, f"{label}.batches[{index}]")
            for index, item in enumerate(raw_batches)
        )
        if len({batch.batch_id for batch in batches}) != len(batches):
            _fail(f"{label}.batches must not contain duplicate batch ids")
        return cls(1, runtime, run_id, manifest_sha256, batches)


@_frozen_dataclass
class WorkspaceBinding:
    cluster_id: str
    workspace: WorkspaceState

    def to_json(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "workspace": {
                "project_root": str(self.workspace.project_root),
                "dirty_paths": [str(path) for path in self.workspace.dirty_paths],
                "conflicted": self.workspace.conflicted,
            },
        }

    @classmethod
    def from_json(cls, raw: Any, label: str) -> "WorkspaceBinding":
        value = _strict_object(raw, label)
        _exact_fields(value, frozenset({"cluster_id", "workspace"}), label)
        cluster_id = _non_empty_string(value["cluster_id"], f"{label}.cluster_id")
        workspace_value = _strict_object(value["workspace"], f"{label}.workspace")
        _exact_fields(
            workspace_value,
            frozenset({"project_root", "dirty_paths", "conflicted"}),
            f"{label}.workspace",
        )
        project_root = _non_empty_string(
            workspace_value["project_root"], f"{label}.workspace.project_root"
        )
        root = Path(project_root)
        if not root.is_absolute():
            _fail(f"{label}.workspace.project_root must be an absolute path")
        dirty_paths = _path_list(
            workspace_value["dirty_paths"], f"{label}.workspace.dirty_paths"
        )
        conflicted = _boolean(
            workspace_value["conflicted"], f"{label}.workspace.conflicted"
        )
        return cls(
            cluster_id,
            WorkspaceState(root, dirty_paths, conflicted),
        )


@_frozen_dataclass
class ApplyInput:
    approved_clusters: tuple[str, ...]
    requests: tuple[ApplyRequest, ...]
    workspaces: tuple[WorkspaceBinding, ...]
    proofs: tuple[FixProof, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "approved_clusters": list(self.approved_clusters),
            "requests": [
                {
                    "cluster_id": request.cluster_id,
                    "target_paths": [str(path) for path in request.target_paths],
                    "commands": list(request.commands),
                    "verification_commands": list(request.verification_commands),
                }
                for request in self.requests
            ],
            "workspaces": [binding.to_json() for binding in self.workspaces],
            "proofs": [
                {
                    "cluster_id": proof.cluster_id,
                    "changed_paths": [str(path) for path in proof.changed_paths],
                    "command_output": list(proof.command_output),
                    "verification_output": list(proof.verification_output),
                    "passed": proof.passed,
                }
                for proof in self.proofs
            ],
        }

    @classmethod
    def from_json(cls, raw: Any, label: str) -> "ApplyInput":
        value = _strict_object(raw, label)
        _exact_fields(
            value,
            frozenset({"approved_clusters", "requests", "workspaces", "proofs"}),
            label,
        )
        approved_clusters = _string_list(
            value["approved_clusters"], f"{label}.approved_clusters"
        )
        raw_requests = value["requests"]
        if not isinstance(raw_requests, list):
            _fail(f"{label}.requests must be an array")
        requests = []
        for index, item in enumerate(raw_requests):
            request_value = _strict_object(item, f"{label}.requests[{index}]")
            _exact_fields(
                request_value,
                frozenset(
                    {"cluster_id", "target_paths", "commands", "verification_commands"}
                ),
                f"{label}.requests[{index}]",
            )
            cluster_id = _non_empty_string(
                request_value["cluster_id"], f"{label}.requests[{index}].cluster_id"
            )
            target_paths = _path_list(
                request_value["target_paths"], f"{label}.requests[{index}].target_paths"
            )
            commands = _string_list(
                request_value["commands"], f"{label}.requests[{index}].commands"
            )
            verification_commands = _string_list(
                request_value["verification_commands"],
                f"{label}.requests[{index}].verification_commands",
            )
            requests.append(
                ApplyRequest(cluster_id, target_paths, commands, verification_commands)
            )
        raw_workspaces = value["workspaces"]
        if not isinstance(raw_workspaces, list):
            _fail(f"{label}.workspaces must be an array")
        workspaces = tuple(
            WorkspaceBinding.from_json(item, f"{label}.workspaces[{index}]")
            for index, item in enumerate(raw_workspaces)
        )
        raw_proofs = value["proofs"]
        if not isinstance(raw_proofs, list):
            _fail(f"{label}.proofs must be an array")
        proofs = []
        for index, item in enumerate(raw_proofs):
            proof_value = _strict_object(item, f"{label}.proofs[{index}]")
            _exact_fields(
                proof_value,
                frozenset(
                    {
                        "cluster_id",
                        "changed_paths",
                        "command_output",
                        "verification_output",
                        "passed",
                    }
                ),
                f"{label}.proofs[{index}]",
            )
            cluster_id = _non_empty_string(
                proof_value["cluster_id"], f"{label}.proofs[{index}].cluster_id"
            )
            changed_paths = _path_list(
                proof_value["changed_paths"], f"{label}.proofs[{index}].changed_paths"
            )
            command_output = _string_list(
                proof_value["command_output"], f"{label}.proofs[{index}].command_output"
            )
            verification_output = _string_list(
                proof_value["verification_output"],
                f"{label}.proofs[{index}].verification_output",
            )
            passed = _boolean(proof_value["passed"], f"{label}.proofs[{index}].passed")
            proofs.append(
                FixProof(
                    cluster_id,
                    changed_paths,
                    command_output,
                    verification_output,
                    passed,
                )
            )
        return cls(approved_clusters, tuple(requests), workspaces, tuple(proofs))


@_frozen_dataclass
class HostInput:
    schema_version: int
    coverage_observations: tuple[CoverageObservation, ...]
    apply: ApplyInput | None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "coverage_observations": [
                {
                    "artifact_id": observation.artifact_id,
                    "artifact_kind": observation.artifact_kind,
                    "eligible": observation.eligible,
                    "trigger_evidence": [
                        _evidence_json(ref) for ref in observation.trigger_evidence
                    ],
                    "prevention_evidence": [
                        _evidence_json(ref) for ref in observation.prevention_evidence
                    ],
                    "symptom_recurred": observation.symptom_recurred,
                }
                for observation in self.coverage_observations
            ],
            "apply": self.apply.to_json() if self.apply is not None else None,
        }

    @classmethod
    def from_json(cls, raw: Any, label: str) -> "HostInput":
        value = _strict_object(raw, label)
        _exact_fields(
            value,
            frozenset({"schema_version", "coverage_observations", "apply"}),
            label,
        )
        if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
            _fail(f"{label}.schema_version must be 1")
        raw_observations = value["coverage_observations"]
        if not isinstance(raw_observations, list):
            _fail(f"{label}.coverage_observations must be an array")
        observations = []
        for index, item in enumerate(raw_observations):
            observation_value = _strict_object(
                item, f"{label}.coverage_observations[{index}]"
            )
            _exact_fields(
                observation_value,
                frozenset(
                    {
                        "artifact_id",
                        "artifact_kind",
                        "eligible",
                        "trigger_evidence",
                        "prevention_evidence",
                        "symptom_recurred",
                    }
                ),
                f"{label}.coverage_observations[{index}]",
            )
            artifact_id = _non_empty_string(
                observation_value["artifact_id"],
                f"{label}.coverage_observations[{index}].artifact_id",
            )
            artifact_kind = _non_empty_string(
                observation_value["artifact_kind"],
                f"{label}.coverage_observations[{index}].artifact_kind",
            )
            eligible = _boolean(
                observation_value["eligible"],
                f"{label}.coverage_observations[{index}].eligible",
            )
            trigger_evidence = tuple(
                _evidence_from_json(
                    item,
                    f"{label}.coverage_observations[{index}].trigger_evidence[{j}]",
                )
                for j, item in enumerate(observation_value["trigger_evidence"])
            )
            prevention_evidence = tuple(
                _evidence_from_json(
                    item,
                    f"{label}.coverage_observations[{index}].prevention_evidence[{j}]",
                )
                for j, item in enumerate(observation_value["prevention_evidence"])
            )
            symptom_recurred = _boolean(
                observation_value["symptom_recurred"],
                f"{label}.coverage_observations[{index}].symptom_recurred",
            )
            observations.append(
                CoverageObservation(
                    artifact_id,
                    artifact_kind,
                    eligible,
                    trigger_evidence,
                    prevention_evidence,
                    symptom_recurred,
                )
            )
        apply_value = value["apply"]
        if apply_value is not None:
            apply_input = ApplyInput.from_json(apply_value, f"{label}.apply")
        else:
            apply_input = None
        return cls(1, tuple(observations), apply_input)

    @classmethod
    def parse(cls, raw: str, label: str = "host input") -> "HostInput":
        return cls.from_json(_loads(raw, label), label)


_EVIDENCE_FIELDS = frozenset(
    {"digest_path", "source_line", "timestamp", "kind", "project", "occurrence_count"}
)


def _evidence_json(ref: EvidenceRef) -> dict[str, Any]:
    return {
        "digest_path": ref.digest_path,
        "source_line": ref.source_line,
        "timestamp": _timestamp_text(ref.timestamp),
        "kind": ref.kind.value,
        "project": ref.project,
        "occurrence_count": ref.occurrence_count,
    }


def _evidence_from_json(raw: Any, label: str) -> EvidenceRef:
    value = _strict_object(raw, label)
    _exact_fields(value, _EVIDENCE_FIELDS, label)
    digest_path = _non_empty_string(value["digest_path"], f"{label}.digest_path")
    source_line = _positive_integer(value["source_line"], f"{label}.source_line")
    timestamp = _timestamp(value["timestamp"], f"{label}.timestamp")
    kind_value = value["kind"]
    if not isinstance(kind_value, str):
        _fail(f"{label}.kind must be a finding type")
    try:
        kind = FindingType(kind_value)
    except ValueError:
        _fail(f"{label}.kind must be a finding type")
    project = _non_empty_string(value["project"], f"{label}.project")
    occurrence_count = value.get("occurrence_count", 1)
    if (
        isinstance(occurrence_count, bool)
        or not isinstance(occurrence_count, int)
        or occurrence_count <= 0
    ):
        _fail(f"{label}.occurrence_count must be a positive integer")
    return EvidenceRef(
        digest_path=digest_path,
        source_line=source_line,
        timestamp=timestamp,
        kind=kind,
        project=project,
        occurrence_count=occurrence_count,
    )


__all__ = [
    "ApplyInput",
    "DispatchBatch",
    "DispatchPlan",
    "HostInput",
    "RunStage",
    "WorkspaceBinding",
]
