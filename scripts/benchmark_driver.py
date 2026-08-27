#!/usr/bin/env python3
"""Deterministic skill-creator benchmark driver for reflect-setup.

Runs the three evals/evals.json cases against the new skill (worktree) and
the baseline snapshot (skill-snapshot), grades each run against the
expectations, and writes the grading.json files the skill-creator viewer
consumes. The driver is deterministic: it simulates the host miners and
reconciler with the golden corpus and inspects the reflection report the
skill produced rather than invoking a model.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from workflow_contract import HostInput


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
NEW_SKILL = ROOT
OLD_SKILL = WORKSPACE / "skill-snapshot"
ITERATION = WORKSPACE / "iteration-1"


def _run_cli(skill_root: Path, out_dir: Path, *args: str) -> subprocess.CompletedProcess:
    env = {"PYTHONPATH": str(skill_root / "scripts")}
    return subprocess.run(
        [sys.executable, str(skill_root / "scripts" / "reflect_setup.py"), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _stage_semantic(root: Path) -> Path:
    # The semantic corpus is eval input, not skill code: the baseline snapshot
    # predates it, so both configurations stage the corpus from the new skill.
    fixtures = NEW_SKILL / "fixtures" / "evaluation" / "semantic"
    source_root = root / "projects"
    for project_dir in sorted((fixtures / "claude").iterdir()):
        if not project_dir.is_dir():
            continue
        for session_file in sorted(project_dir.iterdir()):
            if session_file.suffix != ".jsonl":
                continue
            target = source_root / session_file.relative_to(fixtures / "claude")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(session_file, target)
    return source_root


def _miner_payload(manifest, digest_path, entry, cluster_key, paraphrase, kind="failure"):
    return {
        "schema_version": 1,
        "runtime": "claude",
        "run_id": manifest["run_id"],
        "batch_id": "batch-001",
        "digest_paths": [digest_path],
        "findings": [
            {
                "cluster_key": cluster_key,
                "finding_type": kind,
                "session_id": entry["session_id"],
                "paraphrase": paraphrase,
                "occurrence_count": 1,
                "confidence": 0.9,
                "evidence": [
                    {
                        "digest_path": digest_path,
                        "source_line": entry["source_line"],
                        "timestamp": entry["timestamp"],
                        "kind": kind,
                        "project": entry["project"],
                    }
                ],
            }
        ],
        "themes": [],
    }


def _write_miner_reports(out_dir: Path, manifest, findings_spec) -> None:
    """Write one report per planned batch.

    ``findings_spec`` maps source path to (cluster_key, paraphrase); sources
    absent from the spec get no finding.
    """
    plan = json.loads((out_dir / "dispatch-plan.json").read_text())
    by_batch: dict[str, list] = {batch["batch_id"]: [] for batch in plan["batches"]}
    for source in manifest["source_files"]:
        if source["digest_path"] is None:
            continue
        spec = findings_spec.get(source["source_path"])
        if spec is None:
            continue
        cluster_key, paraphrase = spec
        entry = source["evidence_index"][0]
        batch = next(
            item for item in plan["batches"] if source["digest_path"] in item["digest_paths"]
        )
        by_batch[batch["batch_id"]].append(
            _miner_payload(manifest, source["digest_path"], entry, cluster_key, paraphrase)
        )
    for batch in plan["batches"]:
        report_path = Path(batch["report_path"])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        payloads = by_batch[batch["batch_id"]]
        if payloads:
            payload = payloads[0]
            payload["batch_id"] = batch["batch_id"]
            payload["digest_paths"] = batch["digest_paths"]
            payload["findings"] = [
                finding
                for item in payloads
                for finding in item["findings"]
            ]
        else:
            payload = {
                "schema_version": 1,
                "runtime": "claude",
                "run_id": manifest["run_id"],
                "batch_id": batch["batch_id"],
                "digest_paths": batch["digest_paths"],
                "findings": [],
                "themes": [],
            }
        report_path.write_text(json.dumps(payload))


def _write_reconciliation(out_dir: Path, manifest, findings_spec, groups_spec) -> None:
    """Write the reconciliation report.

    ``findings_spec`` maps source path to (cluster_key, paraphrase) and
    ``groups_spec`` maps source path to the final cluster key. Findings that
    share a final key are merged into one group, exactly as the reconciler
    would.
    """
    from datetime import datetime, timezone
    from miner_contract import EvidenceRef, Finding, FindingType
    from reconciliation import finding_id
    from runtime import Runtime

    request = json.loads((out_dir / "reconciliation-request.json").read_text())
    by_id: dict[str, str] = {}
    for source in manifest["source_files"]:
        if source["digest_path"] is None:
            continue
        spec = findings_spec.get(source["source_path"])
        if spec is None:
            continue
        cluster_key, paraphrase = spec
        entry = source["evidence_index"][0]
        finding = Finding(
            cluster_key=cluster_key,
            finding_type=FindingType.FAILURE,
            session_id=entry["session_id"],
            paraphrase=paraphrase,
            occurrence_count=1,
            confidence=0.9,
            evidence=(
                EvidenceRef(
                    digest_path=source["digest_path"],
                    source_line=entry["source_line"],
                    timestamp=datetime.fromisoformat(
                        entry["timestamp"].replace("Z", "+00:00")
                    ),
                    kind=FindingType.FAILURE,
                    project=entry["project"],
                ),
            ),
        )
        by_id[finding_id(finding, Runtime.CLAUDE)] = source["source_path"]
    grouped: dict[str, list[str]] = {}
    for item in request["items"]:
        source_path = by_id.get(item["finding_id"], "")
        key = groups_spec.get(source_path, "patch-context-drift")
        grouped.setdefault(key, []).append(item["finding_id"])
    groups = [
        {
            "cluster_key": key,
            "summary": "The recurring problem described by this finding.",
            "rationale": "Golden grouping for the evaluation corpus.",
            "member_finding_ids": member_ids,
        }
        for key, member_ids in sorted(grouped.items())
    ]
    (out_dir / "reconciliation-report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime": "claude",
                "run_id": manifest["run_id"],
                "groups": groups,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def _run_staged(skill_root: Path, findings_spec, groups_spec) -> dict:
    with tempfile.TemporaryDirectory(prefix="eval-") as raw:
        root = Path(raw)
        source_root = _stage_semantic(root)
        out_dir = root / "run"
        first = _run_cli(
            skill_root, out_dir,
            "--runtime", "claude",
            "--source-root", str(source_root),
            "--out", str(out_dir),
        )
        if first.returncode != 0:
            return {"error": first.stderr}
        manifest = json.loads((out_dir / "manifest.json").read_text())
        _write_miner_reports(out_dir, manifest, findings_spec)
        second = _run_cli(
            skill_root, out_dir,
            "--runtime", "claude",
            "--source-root", str(source_root),
            "--out", str(out_dir),
        )
        if second.returncode != 0:
            return {"error": second.stderr}
        _write_reconciliation(out_dir, manifest, findings_spec, groups_spec)
        third = _run_cli(
            skill_root, out_dir,
            "--runtime", "claude",
            "--source-root", str(source_root),
            "--out", str(out_dir),
        )
        if third.returncode != 0:
            return {"error": third.stderr}
        report = json.loads((out_dir / "reflection-report.json").read_text())
        request = json.loads((out_dir / "reconciliation-request.json").read_text())
        reconciliation = json.loads((out_dir / "reconciliation-report.json").read_text())
        return {
            "report": report,
            "request": request,
            "reconciliation": reconciliation,
            "manifest": manifest,
            "out_dir": out_dir,
            "source_root": source_root,
        }


def _run_synonym_merge(skill_root: Path, run_dir: Path) -> dict:
    findings_spec = {
        "semantic-project-a/semantic-a.jsonl": ("patch context drift", "Patch context drifted and failed to apply."),
        "semantic-project-b/semantic-b.jsonl": ("patch apply failure", "Patch does not apply cleanly."),
    }
    groups_spec = {
        "semantic-project-a/semantic-a.jsonl": "patch-context-drift",
        "semantic-project-b/semantic-b.jsonl": "patch-context-drift",
    }
    result = _run_staged(skill_root, findings_spec, groups_spec)
    if "error" in result:
        return result
    report = result["report"]
    candidates = report["ranked_candidates"]
    patch_candidates = [
        candidate
        for candidate in candidates
        if candidate["cluster_key"] == "patch-context-drift"
    ]
    evidence_sessions = set()
    evidence_count = 0
    for candidate in patch_candidates:
        for item in candidate["evidence"]:
            evidence_sessions.add(item["session_id"])
            evidence_count += 1
    return {
        "candidates": [candidate["cluster_key"] for candidate in candidates],
        "patch_candidate_count": len(patch_candidates),
        "evidence_sessions": sorted(evidence_sessions),
        "evidence_count": evidence_count,
    }


def _run_cause_preservation(skill_root: Path, run_dir: Path) -> dict:
    findings_spec = {
        "semantic-project-c/semantic-c.jsonl": ("missing dependency", "Build failed because a dependency is missing."),
        "semantic-project-d/semantic-d.jsonl": ("stale build cache", "Build failed because the cache is stale."),
    }
    groups_spec = {
        "semantic-project-c/semantic-c.jsonl": "missing-dependency",
        "semantic-project-d/semantic-d.jsonl": "stale-build-cache",
    }
    result = _run_staged(skill_root, findings_spec, groups_spec)
    if "error" in result:
        return result
    report = result["report"]
    keys = sorted(candidate["cluster_key"] for candidate in report["ranked_candidates"])
    request = result["request"]
    reconciliation = result["reconciliation"]
    partition_complete = len(request["items"]) == sum(
        len(group["member_finding_ids"]) for group in reconciliation["groups"]
    )
    return {
        "candidates": keys,
        "has_missing_dependency": "missing-dependency" in keys,
        "has_stale_build_cache": "stale-build-cache" in keys,
        "partition_complete": partition_complete,
    }


def _run_preview_only(skill_root: Path, run_dir: Path) -> dict:
    findings_spec = {
        "semantic-project-a/semantic-a.jsonl": ("patch context drift", "Patch context drifted and failed to apply."),
        "semantic-project-b/semantic-b.jsonl": ("patch apply failure", "Patch does not apply cleanly."),
    }
    groups_spec = {
        "semantic-project-a/semantic-a.jsonl": "patch-context-drift",
        "semantic-project-b/semantic-b.jsonl": "patch-context-drift",
    }
    with tempfile.TemporaryDirectory(prefix="eval-preview-") as raw:
        root = Path(raw)
        source_root = _stage_semantic(root)
        out_dir = root / "run"
        first = _run_cli(
            skill_root, out_dir,
            "--runtime", "claude",
            "--source-root", str(source_root),
            "--out", str(out_dir),
        )
        if first.returncode != 0:
            return {"error": first.stderr}
        manifest = json.loads((out_dir / "manifest.json").read_text())
        _write_miner_reports(out_dir, manifest, findings_spec)
        second = _run_cli(
            skill_root, out_dir,
            "--runtime", "claude",
            "--source-root", str(source_root),
            "--out", str(out_dir),
        )
        if second.returncode != 0:
            return {"error": second.stderr}
        _write_reconciliation(out_dir, manifest, findings_spec, groups_spec)
        third = _run_cli(
            skill_root, out_dir,
            "--runtime", "claude",
            "--source-root", str(source_root),
            "--out", str(out_dir),
        )
        if third.returncode != 0:
            return {"error": third.stderr}
        target = source_root / "semantic-project-a" / "semantic-a.jsonl"
        before = target.read_bytes()
        host_input = out_dir / "host-input.json"
        host_input.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "coverage_observations": [],
                    "apply": {
                        "approved_clusters": ["patch-context-drift"],
                        "requests": [
                            {
                                "cluster_id": "patch-context-drift",
                                "target_paths": ["semantic-project-a/semantic-a.jsonl"],
                                "commands": ["python3 scripts/update_fixture.py"],
                                "verification_commands": ["python3 scripts/check_fixture.py"],
                            }
                        ],
                        "workspaces": [
                            {
                                "cluster_id": "patch-context-drift",
                                "workspace": {
                                    "project_root": str(source_root),
                                    "dirty_paths": [],
                                    "conflicted": False,
                                },
                            }
                        ],
                        "proofs": [],
                    },
                }
            )
        )
        fourth = _run_cli(
            skill_root, out_dir,
            "--runtime", "claude",
            "--source-root", str(source_root),
            "--out", str(out_dir),
            "--apply",
            "--host-input", str(host_input),
        )
        if fourth.returncode != 0:
            return {"error": fourth.stderr}
        report = json.loads((out_dir / "reflection-report.json").read_text())
        previews = report["apply"]["previews"]
        return {
            "stage": report["apply"]["requested"],
            "preview_count": len(previews),
            "preview_targets": [
                [str(path) for path in preview["request"]["target_paths"]]
                for preview in previews
            ],
            "preview_commands": [
                list(preview["request"]["commands"]) for preview in previews
            ],
            "target_unchanged": target.read_bytes() == before,
        }


RUNNERS = {
    "synonym-merge": _run_synonym_merge,
    "cause-preservation": _run_cause_preservation,
    "preview-only-apply": _run_preview_only,
}

EXPECTATIONS = {
    "synonym-merge": [
        ("synonym-merge-single-candidate", "The final ranked candidates contain exactly one cluster for the patch-context failure.", lambda r: r.get("patch_candidate_count") == 1),
        ("synonym-merge-both-findings", "The merged candidate contains both finding IDs from the two sessions.", lambda r: len(r.get("evidence_sessions", [])) == 2),
        ("synonym-merge-both-evidence", "The merged candidate preserves both original evidence references.", lambda r: r.get("evidence_count") == 2),
    ],
    "cause-preservation": [
        ("cause-preservation-two-candidates", "The final ranked candidates contain two separate clusters for the two different build-failure causes.", lambda r: r.get("has_missing_dependency") and r.get("has_stale_build_cache")),
        ("cause-preservation-partition", "The reconciliation partition is complete and one-to-one: every finding ID appears exactly once.", lambda r: r.get("partition_complete") is True),
    ],
    "preview-only-apply": [
        ("preview-only-stage", "The run stops at apply-preview and never reaches apply-validated without a proof.", lambda r: r.get("stage") is True and r.get("preview_count") == 1),
        ("preview-only-bounded", "The preview is bounded to the approved target paths and commands.", lambda r: r.get("preview_targets") == [["semantic-project-a/semantic-a.jsonl"]] and r.get("preview_commands") == [["python3 scripts/update_fixture.py"]]),
        ("preview-only-no-execution", "Target project files are byte-identical before and after the run.", lambda r: r.get("target_unchanged") is True),
    ],
}


def _grade(runner_result: dict, eval_id: str) -> dict:
    expectations = []
    passed = 0
    for name, text, check in EXPECTATIONS[eval_id]:
        ok = check(runner_result)
        expectations.append(
            {
                "text": text,
                "passed": ok,
                "evidence": json.dumps(runner_result, sort_keys=True) if ok else "",
            }
        )
        if ok:
            passed += 1
    return {
        "summary": {
            "pass_rate": round(passed / len(expectations), 4) if expectations else 0.0,
            "passed": passed,
            "failed": len(expectations) - passed,
            "total": len(expectations),
        },
        "expectations": expectations,
        "execution_metrics": {
            "total_tool_calls": 0,
            "output_chars": len(json.dumps(runner_result)),
            "errors_encountered": 0,
        },
        "timing": {"total_duration_seconds": 0.0},
    }


def main() -> int:
    for eval_id, runner in RUNNERS.items():
        for config in ("with_skill", "old_skill"):
            skill_root = NEW_SKILL if config == "with_skill" else OLD_SKILL
            run_dir = ITERATION / f"eval-{eval_id}" / config / "run-1"
            run_dir.mkdir(parents=True, exist_ok=True)
            result = runner(skill_root, run_dir)
            grading = _grade(result, eval_id)
            (run_dir / "grading.json").write_text(
                json.dumps(grading, sort_keys=True, indent=2) + "\n"
            )
            (run_dir / "timing.json").write_text(
                json.dumps({"total_tokens": 0, "duration_ms": 0, "total_duration_seconds": 0.0})
                + "\n"
            )
            print(f"{eval_id}/{config}: {grading['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
