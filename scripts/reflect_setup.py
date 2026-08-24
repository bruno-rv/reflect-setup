#!/usr/bin/env python3
"""End-to-end, host-neutral orchestration for reflect-setup."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from apply import (
    ApplyPreview,
    ApplyRequest,
    FixProof,
    WorkspaceState,
    build_preview,
    check_scope_overlap,
    validate_proof,
)
from coverage_model import (
    CoverageObservation,
    CoverageRecord,
    Inventory,
    InventoryItem,
    assess_coverage,
)
from digest import DigestManifest, IncompleteDigestError, run_digest
from ledger import LedgerStatus, parse_ledger
from miner_contract import (
    BatchSpec,
    EvidenceRef,
    Finding,
    MinerReport,
    ReportValidationError,
    parse_report,
    merge_reports,
)
from runtime import RuntimeSpec, Scope, discover_sessions, resolve_runtime
from scoring import CandidateScore, compute_metrics, rank_candidates, score_candidate
from verification import FixVerification, verify_fix


UTC = timezone.utc


def _frozen_dataclass(cls):
    kwargs = {"frozen": True}
    # The project keeps a small Python 3.9 compatibility seam while retaining
    # slots on interpreters that support dataclass slots.
    if sys.version_info >= (3, 10):
        kwargs["slots"] = True
    return dataclass(**kwargs)(cls)


class ApplyApprovalError(ValueError):
    """Raised when Apply is requested without explicit per-cluster approval."""


@_frozen_dataclass
class ReflectionRun:
    manifest: DigestManifest
    report_path: Path
    ranked_candidates: tuple[CandidateScore, ...]
    coverage: tuple[CoverageRecord, ...]
    apply_preview: ApplyPreview | None
    apply_previews: tuple[ApplyPreview, ...] = ()


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("since must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _manifest_digest_paths(manifest: DigestManifest) -> tuple[str, ...]:
    return tuple(
        source.digest_path
        for source in manifest.source_files
        if source.digest_path is not None
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scope_hash(scope: Scope) -> str:
    value = {
        "since": _utc(scope.since).isoformat(),
        "project_filter": scope.project_filter,
        "include_subagents": scope.include_subagents,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _continuation_path(raw_path: str, out_dir: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve(strict=False)
    direct = path.resolve(strict=False)
    if direct.is_file():
        return direct
    return (out_dir / path).resolve(strict=False)


def _validate_continuation_manifest(
    manifest: DigestManifest,
    spec: RuntimeSpec,
    out_dir: Path,
) -> None:
    """Fail closed if persisted source or digest inputs changed since mining."""
    if manifest.schema_version != 1:
        raise ReportValidationError("continuation manifest schema_version must be 1")
    if not manifest.complete:
        raise IncompleteDigestError(manifest)
    source_root = spec.source_root.expanduser().resolve(strict=False)
    run_root = out_dir.expanduser().resolve(strict=False)
    manifest_json_path = out_dir / "manifest.json"
    try:
        manifest_json = json.loads(manifest_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ReportValidationError("cannot reread continuation manifest") from exc
    digest_hashes = manifest_json.get("digest_hashes")
    if digest_hashes is None:
        raise ReportValidationError("continuation manifest lacks digest hashes")
    if not isinstance(digest_hashes, dict):
        raise ReportValidationError("continuation manifest digest_hashes must be an object")
    expected_digest_keys = {
        source.digest_path
        for source in manifest.source_files
        if source.digest_path is not None
    }
    if set(digest_hashes) != expected_digest_keys or any(
        not isinstance(value, str) for value in digest_hashes.values()
    ):
        raise ReportValidationError("continuation manifest digest_hashes do not match digest paths")
    scope_hash = manifest_json.get("scope_hash")
    if scope_hash is None:
        raise ReportValidationError("continuation manifest lacks scope hash")
    if scope_hash != _scope_hash(manifest.scope):
        raise ReportValidationError("continuation manifest scope hash changed")
    expected_sessions = {
        source.source_path
        for source in manifest.source_files
        if source.scanned
    }
    current_sessions = {
        session.relative_path
        for session in discover_sessions(spec, manifest.scope)
    }
    if current_sessions != expected_sessions:
        added = sorted(current_sessions - expected_sessions)
        removed = sorted(expected_sessions - current_sessions)
        details = []
        if added:
            details.append("added=" + ",".join(added))
        if removed:
            details.append("removed=" + ",".join(removed))
        raise ReportValidationError(
            "continuation session set changed: " + "; ".join(details)
        )
    for source in manifest.source_files:
        if not isinstance(source.source_path, str) or not source.source_path:
            raise ReportValidationError("continuation manifest source_path must be a non-empty string")
        if not isinstance(source.sha256, str) or not source.sha256:
            raise ReportValidationError("continuation manifest source hash must be a non-empty string")
        if source.digest_path is not None and not isinstance(source.digest_path, str):
            raise ReportValidationError("continuation manifest digest_path must be a string")
        source_path = Path(source.source_path)
        if source_path.is_absolute():
            raise ReportValidationError("continuation manifest source path must be relative")
        try:
            current_path = (source_root / source_path).resolve(strict=False)
            current_path.relative_to(source_root)
        except (OSError, ValueError) as exc:
            raise ReportValidationError(
                f"continuation source path escapes source root: {source.source_path}"
            ) from exc
        if not current_path.is_file():
            raise ReportValidationError(
                f"continuation source is missing from manifest: {source.source_path}"
            )
        try:
            current_hash = _sha256(current_path)
        except OSError as exc:
            raise ReportValidationError(
                f"cannot read continuation source: {source.source_path}"
            ) from exc
        if current_hash != source.sha256:
            raise ReportValidationError(
                f"continuation source hash changed: {source.source_path}"
            )
        if source.digest_path is None:
            continue
        digest_path = _continuation_path(source.digest_path, out_dir)
        try:
            digest_path.relative_to(run_root)
        except ValueError as exc:
            raise ReportValidationError(
                f"continuation digest path escapes run directory: {source.digest_path}"
            ) from exc
        if not digest_path.is_file():
            raise ReportValidationError(
                f"continuation digest is missing from manifest: {source.digest_path}"
            )
        try:
            current_digest_hash = _sha256(digest_path)
        except OSError as exc:
            raise ReportValidationError(
                f"cannot read continuation digest: {source.digest_path}"
            ) from exc
        if digest_hashes[source.digest_path] != current_digest_hash:
            raise ReportValidationError(
                f"continuation digest hash changed: {source.digest_path}"
            )


def _load_manifest(out_dir: Path) -> DigestManifest | None:
    """Load a prior digest manifest for the host's miner continuation phase."""
    path = out_dir / "manifest.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ReportValidationError("continuation manifest must be an object")
        if value.get("schema_version") != 1:
            raise ReportValidationError("continuation manifest schema_version must be 1")
        if not isinstance(value.get("complete"), bool):
            raise ReportValidationError("continuation manifest complete must be boolean")
        scope_value = value["scope"]
        if not isinstance(scope_value, dict):
            raise ReportValidationError("continuation manifest scope must be an object")
        if not isinstance(scope_value.get("include_subagents"), bool):
            raise ReportValidationError("continuation manifest include_subagents must be boolean")
        since = str(scope_value["since"])
        if since.endswith("Z"):
            since = since[:-1] + "+00:00"
        scope = Scope(
            _utc(datetime.fromisoformat(since)),
            scope_value.get("project_filter"),
            bool(scope_value["include_subagents"]),
        )
        from digest import SourceFile
        from runtime import Runtime

        runtime = Runtime(value["runtime"])
        raw_source_files = value["source_files"]
        if not isinstance(raw_source_files, list):
            raise ReportValidationError("continuation manifest source_files must be an array")
        source_files = tuple(
            SourceFile(
                source_path=item["source_path"],
                sha256=item["sha256"],
                scanned=bool(item["scanned"]),
                readable=bool(item["readable"]),
                json_lines=int(item["json_lines"]),
                malformed_lines=int(item["malformed_lines"]),
                untimestamped_lines=int(item["untimestamped_lines"]),
                in_scope_events=int(item["in_scope_events"]),
                project=item.get("project", ""),
                digest_path=item.get("digest_path"),
            )
            for item in raw_source_files
        )
        manifest = DigestManifest(
            schema_version=int(value["schema_version"]),
            run_id=value["run_id"],
            runtime=runtime,
            scope=scope,
            source_files=source_files,
            sessions_scanned=int(value["sessions_scanned"]),
            sessions_with_signals=int(value["sessions_with_signals"]),
            signal_counts=dict(value["signal_counts"]),
            complete=bool(value["complete"]),
        )
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        raise ReportValidationError(f"invalid digest manifest {path}: {exc}") from exc
    if not manifest.complete:
        raise IncompleteDigestError(manifest)
    return manifest


def _inventory(spec: RuntimeSpec) -> Inventory:
    """Snapshot declared runtime artifacts without interpreting their behavior."""
    items: list[InventoryItem] = []
    seen_roots: set[Path] = set()
    seen_paths: set[Path] = set()

    def inventory_paths(root: Path) -> tuple[Path, ...]:
        if root.is_file():
            return (root,)
        if not root.is_dir():
            return ()
        if root.name == "skills":
            # A skill is declared by its entrypoint, not by every bundled
            # reference, cache, or nested repository file.
            return tuple(
                entrypoint
                for skill_dir in sorted(root.iterdir())
                if not skill_dir.name.startswith(".")
                for entrypoint in (skill_dir / "SKILL.md",)
                if entrypoint.is_file()
            )
        try:
            return tuple(
                path
                for path in sorted(root.iterdir())
                if path.is_file() and not path.name.startswith(".")
            )
        except OSError:
            return ()

    for configured_root in spec.inventory_roots:
        root = configured_root if configured_root.is_absolute() else Path.cwd() / configured_root
        root = root.expanduser()
        if root in seen_roots:
            continue
        seen_roots.add(root)
        candidates = inventory_paths(root)
        for path in candidates:
            if path == root:
                artifact_id = path.as_posix()
                kind = path.name
            else:
                try:
                    relative = path.relative_to(root).as_posix()
                except ValueError:
                    relative = path.name
                artifact_id = relative
                kind = root.name or "artifact"
            if path in seen_paths:
                continue
            seen_paths.add(path)
            items.append(InventoryItem(artifact_id, kind, path, True))
    items.sort(key=lambda item: (item.artifact_id, item.artifact_kind, item.path.as_posix()))
    return Inventory(tuple(items))


def _coverage(
    inventory: Inventory,
    observations: Iterable[CoverageObservation] | Mapping[str, CoverageObservation] | None,
    supplied_records: Iterable[CoverageRecord]
    | Mapping[str, CoverageRecord]
    | None = None,
) -> tuple[CoverageRecord, ...]:
    """Derive coverage only from typed host observations.

    Inventory declaration is local state; eligibility and operating evidence
    belong to the host that can observe its trigger and outcome behavior.
    Missing observations therefore produce no coverage record rather than a
    guessed ineligible record.
    """
    if observations is not None and supplied_records is not None:
        raise ReportValidationError(
            "provide coverage_observations or coverage_records, not both"
        )
    if observations is None and supplied_records is None:
        return ()
    if supplied_records is not None:
        if isinstance(supplied_records, Mapping):
            raw_records = []
            for supplied_id, record in supplied_records.items():
                if not isinstance(supplied_id, str) or not supplied_id.strip():
                    raise ReportValidationError("coverage record mapping keys must be non-empty strings")
                if not isinstance(record, CoverageRecord):
                    raise ReportValidationError(
                        "coverage_records must contain CoverageRecord values"
                    )
                if supplied_id != record.artifact_id:
                    raise ReportValidationError(
                        "coverage record mapping key does not match artifact_id"
                    )
                raw_records.append(record)
            records = tuple(raw_records)
        else:
            records = tuple(supplied_records)
        inventory_by_key = {
            (item.artifact_id, item.artifact_kind): item for item in inventory.items
        }
        seen: set[tuple[str, str]] = set()
        validated: list[CoverageRecord] = []
        for record in records:
            if not isinstance(record, CoverageRecord):
                raise ReportValidationError("coverage_records must contain CoverageRecord values")
            key = (record.artifact_id, record.artifact_kind)
            item = inventory_by_key.get(key)
            if item is None:
                raise ReportValidationError(
                    "coverage record references unknown artifact id/type: "
                    f"{record.artifact_id} ({record.artifact_kind})"
                )
            if record.declared != item.declared:
                raise ReportValidationError(
                    f"coverage record declaration does not match inventory for {record.artifact_id}"
                )
            if key in seen:
                raise ReportValidationError(
                    f"duplicate coverage record for {record.artifact_id} ({record.artifact_kind})"
                )
            if not isinstance(record.eligible, bool) or not isinstance(record.triggered, bool):
                raise ReportValidationError("coverage record states must be booleans")
            if record.prevented is not None and not isinstance(record.prevented, bool):
                raise ReportValidationError("coverage record prevented must be boolean or null")
            evidence = tuple(record.evidence) + tuple(record.trigger_evidence) + tuple(record.prevention_evidence)
            if any(not isinstance(ref, EvidenceRef) for ref in evidence):
                raise ReportValidationError("coverage record evidence must contain EvidenceRef values")
            seen.add(key)
            validated.append(record)
        return tuple(validated)
    if isinstance(observations, Mapping):
        raw_values = []
        for supplied_id, observation in observations.items():
            if not isinstance(supplied_id, str) or not supplied_id.strip():
                raise ReportValidationError("coverage observation mapping keys must be non-empty strings")
            if not isinstance(observation, CoverageObservation):
                raise ReportValidationError(
                    "coverage_observations must contain CoverageObservation values"
                )
            if supplied_id != observation.artifact_id:
                raise ReportValidationError(
                    "coverage observation mapping key does not match artifact_id"
                )
            raw_values.append(observation)
        raw_values = tuple(raw_values)
    else:
        raw_values = tuple(observations)
    inventory_by_key: dict[tuple[str, str], InventoryItem] = {}
    for item in inventory.items:
        if not isinstance(item, InventoryItem):
            raise ReportValidationError("inventory items must be InventoryItem values")
        key = (item.artifact_id, item.artifact_kind)
        if key in inventory_by_key:
            raise ReportValidationError(
                "inventory contains duplicate artifact id/type: "
                f"{item.artifact_id} ({item.artifact_kind})"
            )
        inventory_by_key[key] = item
    seen: set[tuple[str, str]] = set()
    records: list[CoverageRecord] = []
    for observation in raw_values:
        if not isinstance(observation, CoverageObservation):
            raise ReportValidationError(
                "coverage_observations must contain CoverageObservation values"
            )
        key = (observation.artifact_id, observation.artifact_kind)
        item = inventory_by_key.get(key)
        if item is None:
            raise ReportValidationError(
                "coverage observation references unknown artifact id/type: "
                f"{observation.artifact_id} ({observation.artifact_kind})"
            )
        if key in seen:
            raise ReportValidationError(
                "duplicate coverage observation for "
                f"{observation.artifact_id} ({observation.artifact_kind})"
            )
        seen.add(key)
        records.append(assess_coverage(observation=observation, exists=item.declared))
    return tuple(records)


def _report_metadata(raw: str, path: Path) -> BatchSpec:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ReportValidationError(f"miner report {path} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ReportValidationError(f"miner report {path} must contain one JSON object")
    batch_id = value.get("batch_id")
    digest_paths = value.get("digest_paths")
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ReportValidationError(f"miner report {path} has no batch_id")
    if not isinstance(digest_paths, list) or any(
        not isinstance(item, str) or not item.strip() for item in digest_paths
    ):
        raise ReportValidationError(f"miner report {path} has invalid digest_paths")
    return BatchSpec(batch_id, tuple(digest_paths))


def _load_reports(paths: tuple[Path, ...], manifest: DigestManifest) -> tuple[MinerReport, ...]:
    if not paths:
        if _manifest_digest_paths(manifest):
            raise ReportValidationError(
                "manifest has digest paths but no miner reports cover them"
            )
        return ()
    if not _manifest_digest_paths(manifest):
        raise ReportValidationError("miner reports were supplied but the manifest has no digest paths")
    if len(set(paths)) != len(paths):
        raise ReportValidationError("miner report paths must not contain duplicates")
    reports: list[MinerReport] = []
    for path in paths:
        path = Path(path).expanduser()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ReportValidationError(f"cannot read miner report {path}: {exc}") from exc
        batch = _report_metadata(raw, path)
        reports.append(parse_report(raw, manifest, batch))
    return tuple(reports)


def _group_findings(findings: Iterable[Finding]) -> dict[str, tuple[Finding, ...]]:
    grouped: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        grouped[finding.cluster_key].append(finding)
    return {key: tuple(values) for key, values in grouped.items()}


def _cluster_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _canonical_cluster_id(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApplyApprovalError(f"{label} must be a non-empty cluster ID")
    canonical = _cluster_id(value)
    if not canonical:
        raise ApplyApprovalError(f"{label} must contain letters or digits")
    return canonical


def _approved_ids(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        canonical = _canonical_cluster_id(value, label="approved cluster")
        if canonical in result:
            raise ApplyApprovalError(
                f"ambiguous approved cluster IDs normalize to {canonical!r}"
            )
        result.append(canonical)
    return tuple(result)


def _candidate_map(candidates: Iterable[CandidateScore]) -> dict[str, CandidateScore]:
    result: dict[str, CandidateScore] = {}
    for candidate in candidates:
        canonical = _canonical_cluster_id(candidate.cluster_key, label="candidate cluster")
        previous = result.get(canonical)
        if previous is not None and previous.cluster_key != candidate.cluster_key:
            raise ApplyApprovalError(
                f"ambiguous candidate cluster IDs normalize to {canonical!r}"
            )
        result[canonical] = candidate
    return result


def _request_map(values: Any) -> dict[str, ApplyRequest]:
    if values is None:
        return {}
    if isinstance(values, Mapping):
        raw_items = tuple(values.items())
    else:
        raw_items = tuple((None, value) for value in values)
    result: dict[str, ApplyRequest] = {}
    for supplied_id, request in raw_items:
        if not isinstance(request, ApplyRequest):
            raise ApplyApprovalError("apply_requests must contain ApplyRequest values")
        canonical = _canonical_cluster_id(request.cluster_id, label="ApplyRequest cluster")
        if supplied_id is not None and _canonical_cluster_id(
            supplied_id, label="ApplyRequest mapping key"
        ) != canonical:
            raise ApplyApprovalError("ApplyRequest mapping key does not match request cluster_id")
        if canonical in result:
            raise ApplyApprovalError(
                f"ambiguous ApplyRequest IDs normalize to {canonical!r}"
            )
        result[canonical] = request
    return result


def _workspace_map(values: Any) -> dict[str, WorkspaceState]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise ApplyApprovalError("apply_workspaces must map cluster IDs to WorkspaceState")
    result: dict[str, WorkspaceState] = {}
    for key, workspace in values.items():
        canonical = _canonical_cluster_id(key, label="workspace cluster")
        if not isinstance(workspace, WorkspaceState):
            raise ApplyApprovalError("apply_workspaces must contain WorkspaceState values")
        if canonical in result:
            raise ApplyApprovalError(
                f"ambiguous workspace IDs normalize to {canonical!r}"
            )
        result[canonical] = workspace
    return result


def _proof_map(values: Any) -> dict[str, FixProof]:
    if values is None:
        return {}
    if isinstance(values, Mapping):
        raw_items = tuple(values.items())
    else:
        raw_items = tuple((None, value) for value in values)
    result: dict[str, FixProof] = {}
    for supplied_id, proof in raw_items:
        if not isinstance(proof, FixProof):
            raise ApplyApprovalError("apply_proofs must contain FixProof values")
        canonical = _canonical_cluster_id(proof.cluster_id, label="FixProof cluster")
        if supplied_id is not None and _canonical_cluster_id(
            supplied_id, label="FixProof mapping key"
        ) != canonical:
            raise ApplyApprovalError("FixProof mapping key does not match proof cluster_id")
        if canonical in result:
            raise ApplyApprovalError(
                f"ambiguous FixProof IDs normalize to {canonical!r}"
            )
        result[canonical] = FixProof(
            canonical,
            proof.changed_paths,
            proof.command_output,
            proof.verification_output,
            proof.passed,
        )
    return result


def _rank(
    findings: tuple[Finding, ...],
    analyzed_sessions: int,
    regression_counts: Mapping[str, int] | None = None,
) -> tuple[CandidateScore, ...]:
    regression_counts = regression_counts or {}
    candidates: list[CandidateScore] = []
    for cluster_key, cluster_findings in _group_findings(findings).items():
        confidence = max(finding.confidence for finding in cluster_findings)
        metrics = compute_metrics(
            cluster_findings,
            analyzed_sessions,
            regression_count=regression_counts.get(_cluster_id(cluster_key), 0),
            confidence=confidence,
            implementation_cost="M",
        )
        candidates.append(score_candidate(cluster_key, metrics))
    return rank_candidates(candidates)


def _verify_ledger(
    findings: tuple[Finding, ...],
    coverage: tuple[CoverageRecord, ...],
    ledger_path: Path | None,
) -> tuple[FixVerification, ...]:
    if ledger_path is None:
        return ()
    entries = parse_ledger(ledger_path)
    clusters = _group_findings(findings)
    normalized_clusters = {
        _cluster_id(key): values for key, values in clusters.items()
    }
    results: list[FixVerification] = []
    for entry in entries:
        if entry.status not in {
            LedgerStatus.FIX_APPLIED,
            LedgerStatus.BUILT_NOT_OPERATING,
            LedgerStatus.RESOLVED,
        }:
            continue
        results.append(
            verify_fix(
                entry,
                symptom_evidence=(),
                invocation_evidence=(),
                outcome_evidence=(),
                merged_findings=normalized_clusters.get(_cluster_id(entry.cluster_id), ()),
                coverage_records=coverage,
                target_artifact_ids=entry.artifact_ids,
            )
        )
    return tuple(results)


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            descriptor = -1
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = ""
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def _persist_digest_hashes(out_dir: Path, manifest: DigestManifest) -> None:
    path = out_dir / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["digest_hashes"] = {
        source.digest_path: _sha256(_continuation_path(source.digest_path, out_dir))
        for source in manifest.source_files
        if source.digest_path is not None
    }
    value["scope_hash"] = _scope_hash(manifest.scope)
    _atomic_json(path, value)


def _report_payload(
    result_manifest: DigestManifest,
    ranked: tuple[CandidateScore, ...],
    coverage: tuple[CoverageRecord, ...],
    verification: tuple[FixVerification, ...],
    inventory: Inventory,
    apply_requested: bool,
    approved_clusters: tuple[str, ...],
    apply_previews: tuple[ApplyPreview, ...],
    validated_proofs: tuple[str, ...],
    ledger_path: Path | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": result_manifest.run_id,
        "runtime": result_manifest.runtime.value,
        "manifest": result_manifest,
        "inventory": inventory,
        "coverage": coverage,
        "verification": verification,
        "ranked_candidates": ranked,
        "apply": {
            "requested": apply_requested,
            "approved_clusters": approved_clusters,
            "preview_created": bool(apply_previews),
            "previews": apply_previews,
            "validated_proofs": validated_proofs,
        },
        "ledger_path": ledger_path,
    }


def run_reflection(
    *,
    runtime_name: str | None,
    home: Path,
    source_root: Path | None,
    since: datetime,
    project_filter: str | None,
    include_subagents: bool,
    out_dir: Path,
    miner_report_paths: tuple[Path, ...],
    apply: bool,
    approved_clusters: tuple[str, ...] = (),
    apply_requests: Iterable[ApplyRequest] | Mapping[str, ApplyRequest] | None = None,
    apply_workspaces: Mapping[str, WorkspaceState] | None = None,
    apply_proofs: Iterable[FixProof] | Mapping[str, FixProof] | None = None,
    coverage_observations: Iterable[CoverageObservation]
    | Mapping[str, CoverageObservation]
    | None = None,
    coverage_records: Iterable[CoverageRecord]
    | Mapping[str, CoverageRecord]
    | None = None,
    ledger_path: Path | None = None,
) -> ReflectionRun:
    """Run one complete reflection pass and write an atomic JSON report.

    The Python core never dispatches a host task or fixer. ``approved_clusters``
    is only the explicit CLI approval boundary; host workflows own any later
    Apply preview/execution using ``scripts/apply.py``.
    """
    if not isinstance(home, Path):
        raise TypeError("home must be a pathlib.Path")
    if not isinstance(out_dir, Path):
        raise TypeError("out_dir must be a pathlib.Path")
    if not isinstance(apply, bool):
        raise TypeError("apply must be a bool")
    approvals = tuple(approved_clusters)
    if not apply and approvals:
        raise ApplyApprovalError("--approve-cluster is only valid with --apply")
    if apply and not approvals:
        raise ApplyApprovalError("Apply requires explicit cluster approval")
    if not apply and any(value is not None for value in (apply_requests, apply_workspaces, apply_proofs)):
        raise ApplyApprovalError("Apply inputs are only valid with --apply")
    if apply and (apply_requests is None or apply_workspaces is None):
        raise ApplyApprovalError(
            "Apply requires matching ApplyRequest and WorkspaceState inputs"
        )
    if ledger_path is not None and not isinstance(ledger_path, Path):
        raise TypeError("ledger_path must be a pathlib.Path")

    normalized_approvals = _approved_ids(approvals)
    requests = _request_map(apply_requests) if apply else {}
    workspaces = _workspace_map(apply_workspaces) if apply else {}
    proofs = _proof_map(apply_proofs) if apply else {}

    spec = resolve_runtime(
        runtime_name,
        home=home,
        env=os.environ,
        source_root=source_root,
    )
    scope = Scope(_utc(since), project_filter, bool(include_subagents))
    existing_manifest = _load_manifest(out_dir) if out_dir.is_dir() else None
    if existing_manifest is not None:
        if existing_manifest.runtime is not spec.runtime:
            raise ReportValidationError("existing manifest runtime does not match this run")
        if (
            existing_manifest.scope.project_filter != scope.project_filter
            or existing_manifest.scope.include_subagents != scope.include_subagents
        ):
            raise ReportValidationError("existing manifest scope does not match this run")
        _validate_continuation_manifest(existing_manifest, spec, out_dir)
        # The host may need a few seconds between digest and miner phases;
        # retain the persisted cutoff as the authoritative continuation scope.
        scope = existing_manifest.scope
    manifest = existing_manifest or run_digest(spec, scope, out_dir)
    if existing_manifest is None:
        _persist_digest_hashes(out_dir, manifest)
    # IncompleteDigestError is intentionally raised by run_digest after the
    # manifest is persisted, and must stop before any miner report is read.
    reports = _load_reports(tuple(Path(path) for path in miner_report_paths), manifest)
    findings = merge_reports(reports, manifest) if reports else ()
    inventory = _inventory(spec)
    coverage = _coverage(inventory, coverage_observations, coverage_records)
    verification = _verify_ledger(findings, coverage, ledger_path)
    regression_counts = {
        _cluster_id(item.cluster_id): 1
        for item in verification
        if item.overall.value == "fail"
    }
    ranked = _rank(findings, manifest.sessions_scanned, regression_counts)

    previews: list[ApplyPreview] = []
    validated_proofs: list[str] = []
    selected = _candidate_map(ranked)
    if apply:
        unknown = sorted(set(normalized_approvals) - set(selected))
        if unknown:
            raise ApplyApprovalError(
                "approved cluster is not a ranked candidate: " + ", ".join(unknown)
            )
        extra_requests = sorted(set(requests) - set(normalized_approvals))
        if extra_requests:
            raise ApplyApprovalError(
                "ApplyRequest supplied for unapproved cluster: " + ", ".join(extra_requests)
            )
        missing_requests = sorted(set(normalized_approvals) - set(requests))
        missing_workspaces = sorted(set(normalized_approvals) - set(workspaces))
        if missing_requests or missing_workspaces:
            missing = sorted(set(missing_requests) | set(missing_workspaces))
            raise ApplyApprovalError(
                "Apply requires matching ApplyRequest and WorkspaceState for: "
                + ", ".join(missing)
            )
        extra_workspaces = sorted(set(workspaces) - set(normalized_approvals))
        if extra_workspaces:
            raise ApplyApprovalError(
                "WorkspaceState supplied for unapproved cluster: "
                + ", ".join(extra_workspaces)
            )
        extra_proofs = sorted(set(proofs) - set(normalized_approvals))
        if extra_proofs:
            raise ApplyApprovalError(
                "FixProof supplied for unapproved cluster: " + ", ".join(extra_proofs)
            )
        for cluster_id in normalized_approvals:
            request = requests[cluster_id]
            normalized_request = ApplyRequest(
                cluster_id,
                request.target_paths,
                request.commands,
                request.verification_commands,
            )
            preview = build_preview(
                normalized_request,
                inventory=inventory,
                workspace=workspaces[cluster_id],
            )
            for previous in previews:
                overlap = check_scope_overlap(previous, preview)
                if overlap is not None:
                    raise ApplyApprovalError(
                        "approved Apply previews overlap and must run serially: "
                        + " and ".join(overlap)
                    )
            previews.append(preview)
        for cluster_id, proof in proofs.items():
            validate_proof(
                next(preview for preview in previews if preview.request.cluster_id == cluster_id),
                proof,
            )
            validated_proofs.append(cluster_id)

    report_path = Path(out_dir) / "reflection-report.json"
    _atomic_json(
        report_path,
        _json_value(
            _report_payload(
                manifest,
                ranked,
                coverage,
                verification,
                inventory,
                apply,
                normalized_approvals,
                tuple(previews),
                tuple(validated_proofs),
                ledger_path,
            )
        ),
    )
    preview_tuple = tuple(previews)
    return ReflectionRun(
        manifest,
        report_path,
        ranked,
        coverage,
        preview_tuple[0] if preview_tuple else None,
        preview_tuple,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a dual-runtime reflect-setup pass")
    parser.add_argument("--runtime", choices=("auto", "claude", "codex"), default="auto")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--since", type=int, default=30, metavar="DAYS")
    parser.add_argument("--project-filter")
    parser.add_argument("--include-subagents", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--miner-report", type=Path, action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve-cluster", action="append", default=[])
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.since < 0:
        parser.error("--since must be non-negative")
    if args.approve_cluster and not args.apply:
        print("reflect-setup: --approve-cluster is only valid with --apply", file=sys.stderr)
        return 2
    if args.apply and not args.approve_cluster:
        print("reflect-setup: Apply requires explicit cluster approval", file=sys.stderr)
        return 2
    since = datetime.now(UTC) - timedelta(days=args.since)
    try:
        result = run_reflection(
            runtime_name=None if args.runtime == "auto" else args.runtime,
            home=Path.home(),
            source_root=args.source_root,
            since=since,
            project_filter=args.project_filter,
            include_subagents=args.include_subagents,
            out_dir=args.out,
            miner_report_paths=tuple(args.miner_report),
            apply=args.apply,
            approved_clusters=tuple(args.approve_cluster),
            ledger_path=args.ledger,
        )
    except (ApplyApprovalError, IncompleteDigestError, ReportValidationError, OSError, RuntimeError, ValueError) as exc:
        print(f"reflect-setup: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(result.report_path.read_text(encoding="utf-8"), end="")
    else:
        print(
            f"runtime={result.manifest.runtime.value} "
            f"sessions={result.manifest.sessions_scanned} "
            f"signals={result.manifest.sessions_with_signals} "
            f"candidates={len(result.ranked_candidates)} "
            f"report={result.report_path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ApplyApprovalError", "ReflectionRun", "main", "run_reflection"]
