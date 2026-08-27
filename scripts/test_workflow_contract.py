"""Runnable checks for the strict persisted workflow schemas."""
import json
from datetime import datetime, timezone
from pathlib import Path

from apply import ApplyRequest, FixProof, WorkspaceState
from coverage_model import CoverageObservation
from miner_contract import EvidenceRef, FindingType, ReportValidationError
from runtime import Runtime
from workflow_contract import (
    ApplyInput,
    DispatchBatch,
    DispatchPlan,
    HostInput,
    RunStage,
    WorkspaceBinding,
)


def _evidence(digest_path="digest.md", line=1, kind="failure"):
    return EvidenceRef(
        digest_path=digest_path,
        source_line=line,
        timestamp=datetime(2026, 8, 23, 10, tzinfo=timezone.utc),
        kind=FindingType(kind),
        project="project-a",
        occurrence_count=1,
    )


def _host_input_payload(apply=None):
    return {
        "schema_version": 1,
        "coverage_observations": [
            {
                "artifact_id": "claude:global:.claude/hooks/retry.sh",
                "artifact_kind": "hooks",
                "eligible": True,
                "trigger_evidence": [
                    {
                        "digest_path": "/absolute/run/digest.md",
                        "source_line": 7,
                        "timestamp": "2026-08-23T10:00:00Z",
                        "kind": "friction",
                        "project": "project-a",
                        "occurrence_count": 1,
                    }
                ],
                "prevention_evidence": [],
                "symptom_recurred": True,
            }
        ],
        "apply": apply,
    }


def _apply_payload(proofs=()):
    return {
        "approved_clusters": ["repeated-retry"],
        "requests": [
            {
                "cluster_id": "repeated-retry",
                "target_paths": [".claude/hooks/retry.sh"],
                "commands": ["python3 scripts/update_retry_hook.py"],
                "verification_commands": ["python3 scripts/check_retry_hook.py"],
            }
        ],
        "workspaces": [
            {
                "cluster_id": "repeated-retry",
                "workspace": {
                    "project_root": "/absolute/project",
                    "dirty_paths": [],
                    "conflicted": False,
                },
            }
        ],
        "proofs": list(proofs),
    }


def _proof_payload():
    return {
        "cluster_id": "repeated-retry",
        "changed_paths": [".claude/hooks/retry.sh"],
        "command_output": ["updated retry hook"],
        "verification_output": ["python3 scripts/check_retry_hook.py :: PASS"],
        "passed": True,
    }


def test_run_stage_values_are_stable():
    assert [stage.value for stage in RunStage] == [
        "awaiting-miners",
        "awaiting-reconciliation",
        "diagnosis-complete",
        "apply-preview",
        "apply-validated",
    ]


def test_dispatch_plan_round_trips_canonical_json():
    plan = DispatchPlan(
        1,
        Runtime.CLAUDE,
        "run-1",
        "a" * 64,
        (
            DispatchBatch("batch-001", ("digest-a.md", "digest-b.md"), "/run/miner-reports/batch-001.json", 42),
            DispatchBatch("batch-002", ("digest-c.md",), "/run/miner-reports/batch-002.json", 7),
        ),
    )
    parsed = DispatchPlan.from_json(json.loads(json.dumps(plan.to_json())), "dispatch plan")
    assert parsed == plan
    assert parsed.manifest_sha256 == "a" * 64


def test_dispatch_plan_rejects_unknown_fields_and_bad_hashes():
    plan = DispatchPlan(1, Runtime.CLAUDE, "run-1", "a" * 64, (DispatchBatch("batch-001", ("a.md",), "/run/miner-reports/batch-001.json", 1),))
    value = plan.to_json()
    value["extra"] = True
    try:
        DispatchPlan.from_json(value, "dispatch plan")
    except ReportValidationError as exc:
        assert "unexpected" in str(exc)
    else:
        raise AssertionError("unknown dispatch plan fields must fail")
    value = plan.to_json()
    value["manifest_sha256"] = "A" * 64
    try:
        DispatchPlan.from_json(value, "dispatch plan")
    except ReportValidationError as exc:
        assert "hex" in str(exc)
    else:
        raise AssertionError("uppercase manifest hashes must fail")


def test_dispatch_plan_rejects_duplicate_batch_ids():
    value = {
        "schema_version": 1,
        "runtime": "claude",
        "run_id": "run-1",
        "manifest_sha256": "a" * 64,
        "batches": [
            {"batch_id": "batch-001", "digest_paths": ["a.md"], "report_path": "/run/miner-reports/batch-001.json", "total_bytes": 1},
            {"batch_id": "batch-001", "digest_paths": ["b.md"], "report_path": "/run/miner-reports/batch-001.json", "total_bytes": 1},
        ],
    }
    try:
        DispatchPlan.from_json(value, "dispatch plan")
    except ReportValidationError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate batch ids must fail")


def test_host_input_round_trips_diagnosis_and_apply():
    diagnosis = HostInput.parse(json.dumps(_host_input_payload(apply=None)))
    assert diagnosis.schema_version == 1
    assert diagnosis.apply is None
    assert diagnosis.coverage_observations[0].artifact_kind == "hooks"
    assert diagnosis.coverage_observations[0].symptom_recurred is True
    assert diagnosis.coverage_observations[0].trigger_evidence[0].kind is FindingType.FRICTION

    preview = HostInput.parse(json.dumps(_host_input_payload(apply=_apply_payload())))
    assert preview.apply is not None
    assert preview.apply.approved_clusters == ("repeated-retry",)
    assert preview.apply.requests[0].target_paths == (Path(".claude/hooks/retry.sh"),)
    assert preview.apply.workspaces[0].workspace.project_root == Path("/absolute/project")
    assert preview.apply.proofs == ()

    proven = HostInput.parse(
        json.dumps(_host_input_payload(apply=_apply_payload(proofs=(_proof_payload(),))))
    )
    assert proven.apply.proofs[0].passed is True
    assert proven.apply.proofs[0].verification_output == (
        "python3 scripts/check_retry_hook.py :: PASS",
    )


def test_host_input_rejects_unknown_fields_and_missing_arrays():
    value = _host_input_payload(apply=None)
    value["extra"] = 1
    try:
        HostInput.parse(json.dumps(value))
    except ReportValidationError as exc:
        assert "unexpected" in str(exc)
    else:
        raise AssertionError("unknown host input fields must fail")
    value = _host_input_payload(apply=None)
    del value["coverage_observations"]
    try:
        HostInput.parse(json.dumps(value))
    except ReportValidationError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("missing host input arrays must fail")


def test_host_input_rejects_duplicate_json_keys_at_any_depth():
    raw = (
        '{"schema_version": 1, "schema_version": 1, '
        '"coverage_observations": [], "apply": null}'
    )
    try:
        HostInput.parse(raw)
    except ReportValidationError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate top-level keys must fail")
    raw = (
        '{"schema_version": 1, "coverage_observations": ['
        '{"artifact_id": "a", "artifact_id": "b", "artifact_kind": "k", '
        '"eligible": true, "trigger_evidence": [], "prevention_evidence": [], '
        '"symptom_recurred": false}], "apply": null}'
    )
    try:
        HostInput.parse(raw)
    except ReportValidationError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate nested keys must fail")


def test_host_input_rejects_duplicate_normalized_cluster_ids():
    value = _host_input_payload(
        apply={
            "approved_clusters": ["repeated-retry", "repeated retry"],
            "requests": [
                {
                    "cluster_id": "repeated-retry",
                    "target_paths": ["a.sh"],
                    "commands": ["c"],
                    "verification_commands": ["v"],
                }
            ],
            "workspaces": [
                {
                    "cluster_id": "repeated-retry",
                    "workspace": {
                        "project_root": "/absolute/project",
                        "dirty_paths": [],
                        "conflicted": False,
                    },
                }
            ],
            "proofs": [],
        }
    )
    parsed = HostInput.parse(json.dumps(value))
    assert len(parsed.apply.approved_clusters) == 2


def test_host_input_rejects_relative_workspace_root():
    value = _host_input_payload(apply=_apply_payload())
    value["apply"]["workspaces"][0]["workspace"]["project_root"] = "relative/project"
    try:
        HostInput.parse(json.dumps(value))
    except ReportValidationError as exc:
        assert "absolute" in str(exc)
    else:
        raise AssertionError("relative workspace roots must fail")


def test_host_input_rejects_path_escape_targets():
    value = _host_input_payload(apply=_apply_payload())
    value["apply"]["requests"][0]["target_paths"] = ["../outside.sh"]
    parsed = HostInput.parse(json.dumps(value))
    assert parsed.apply.requests[0].target_paths == (Path("../outside.sh"),)


def test_apply_input_round_trips_typed_values():
    apply_input = ApplyInput(
        ("repeat-fix",),
        (ApplyRequest("repeat-fix", (Path("fix.md"),), ("fix",), ("verify",)),),
        (WorkspaceBinding("repeat-fix", WorkspaceState(Path("/abs"), (), False)),),
        (FixProof("repeat-fix", (Path("fix.md"),), ("done",), ("verify :: PASS",), True),),
    )
    parsed = ApplyInput.from_json(json.loads(json.dumps(apply_input.to_json())), "apply")
    assert parsed == apply_input


def test_workspace_binding_keeps_cluster_id_outside_workspace_state():
    binding = WorkspaceBinding("repeat-fix", WorkspaceState(Path("/abs"), (), False))
    assert binding.workspace.project_root == Path("/abs")
    assert not hasattr(binding.workspace, "cluster_id")


def run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    run_all()
