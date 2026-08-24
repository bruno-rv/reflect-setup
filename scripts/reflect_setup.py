#!/usr/bin/env python3
"""End-to-end, host-neutral orchestration for reflect-setup."""
from __future__ import annotations

import argparse
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

from apply import ApplyPreview
from coverage_model import CoverageRecord, Inventory, InventoryItem, assess_coverage
from digest import DigestManifest, IncompleteDigestError, run_digest
from ledger import LedgerStatus, parse_ledger
from miner_contract import (
    BatchSpec,
    Finding,
    MinerReport,
    ReportValidationError,
    parse_report,
    merge_reports,
)
from runtime import RuntimeSpec, Scope, resolve_runtime
from scoring import CandidateScore, compute_metrics, rank_candidates, score_candidate
from verification import (
    FixVerification,
    VerificationEvidence,
    VerificationLabel,
    verify_fix,
)


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


def _load_manifest(out_dir: Path) -> DigestManifest | None:
    """Load a prior digest manifest for the host's miner continuation phase."""
    path = out_dir / "manifest.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        scope_value = value["scope"]
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
            for item in value["source_files"]
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


def _coverage(inventory: Inventory) -> tuple[CoverageRecord, ...]:
    records = []
    for item in inventory.items:
        records.append(
            assess_coverage(
                artifact_id=item.artifact_id,
                artifact_kind=item.artifact_kind,
                exists=item.declared,
                eligible=False,
                trigger_evidence=(),
                prevention_evidence=(),
                symptom_recurred=False,
            )
        )
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


def _ledger_path(out_dir: Path) -> Path | None:
    candidates = (Path.cwd() / "clusters.yaml", out_dir.parent / "clusters.yaml")
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


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
        symptom = tuple(
            VerificationEvidence(ref, VerificationLabel.SYMPTOM_RECURRED)
            for finding in normalized_clusters.get(_cluster_id(entry.cluster_id), ())
            for ref in finding.evidence
        )
        results.append(
            verify_fix(
                entry,
                symptom_evidence=symptom,
                invocation_evidence=(),
                outcome_evidence=(),
                merged_findings=(),
                coverage_records=coverage,
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


def _report_payload(
    result_manifest: DigestManifest,
    ranked: tuple[CandidateScore, ...],
    coverage: tuple[CoverageRecord, ...],
    verification: tuple[FixVerification, ...],
    inventory: Inventory,
    apply_requested: bool,
    approved_clusters: tuple[str, ...],
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
            "preview_created": False,
        },
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
    if any(not isinstance(value, str) or not value.strip() for value in approvals):
        raise ApplyApprovalError("cluster approvals must be non-empty IDs")
    if apply and not approvals:
        raise ApplyApprovalError("Apply requires explicit cluster approval")

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
        # The host may need a few seconds between digest and miner phases;
        # retain the persisted cutoff as the authoritative continuation scope.
        scope = existing_manifest.scope
    manifest = existing_manifest or run_digest(spec, scope, out_dir)
    # IncompleteDigestError is intentionally raised by run_digest after the
    # manifest is persisted, and must stop before any miner report is read.
    reports = _load_reports(tuple(Path(path) for path in miner_report_paths), manifest)
    findings = merge_reports(reports, manifest) if reports else ()
    inventory = _inventory(spec)
    coverage = _coverage(inventory)
    verification = _verify_ledger(findings, coverage, _ledger_path(out_dir))
    regression_counts = {
        _cluster_id(item.cluster_id): 1
        for item in verification
        if item.overall.value == "fail"
    }
    ranked = _rank(findings, manifest.sessions_scanned, regression_counts)

    approved = tuple(dict.fromkeys(value.strip() for value in approvals))
    selected = {candidate.cluster_key for candidate in ranked}
    if apply:
        unknown = sorted(set(approved) - selected)
        if unknown:
            raise ApplyApprovalError(
                "approved cluster is not a ranked candidate: " + ", ".join(unknown)
            )

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
                approved,
            )
        ),
    )
    return ReflectionRun(manifest, report_path, ranked, coverage, None)


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
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.since < 0:
        parser.error("--since must be non-negative")
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
