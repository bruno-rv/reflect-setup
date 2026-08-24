import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from digest import IncompleteDigestError
from apply import ApplyRequest, FixProof, WorkspaceState
from coverage_model import CoverageObservation, assess_coverage
from miner_contract import EvidenceRef, FindingType, ReportValidationError
from reflect_setup import ApplyApprovalError, run_reflection


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "scripts/reflect_setup.py", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_run_reflection_produces_manifest_report_and_ranked_candidates():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "projects" / "project-a"
        source.mkdir(parents=True)
        (source / "session.jsonl").write_text(
            '{"type":"assistant","timestamp":"2026-08-23T10:00:00Z",'
            '"message":{"role":"assistant","content":[{"type":"text",'
            '"text":"no signal"}]}}\n'
        )
        result = run_reflection(
            runtime_name="claude",
            home=root,
            source_root=root / "projects",
            since=datetime(2026, 8, 23, tzinfo=timezone.utc),
            project_filter=None,
            include_subagents=False,
            out_dir=root / "run",
            miner_report_paths=(),
            apply=False,
        )
        assert result.manifest.complete is True
        assert result.report_path == root / "run" / "reflection-report.json"
        assert result.ranked_candidates == ()
        assert result.apply_preview is None


def test_cli_rejects_apply_without_explicit_cluster_approval():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "projects"
        source.mkdir()
        completed = run_cli(
            "--runtime", "claude",
            "--source-root", str(source),
            "--apply",
            "--out", str(root / "run"),
        )
        assert completed.returncode != 0
        assert "cluster approval" in completed.stderr


def test_incomplete_digest_cannot_dispatch_miners():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "projects" / "project-a"
        source.mkdir(parents=True)
        (source / "broken.jsonl").write_text("{not valid json\n")
        try:
            run_reflection(
                runtime_name="claude",
                home=root,
                source_root=root / "projects",
                since=datetime(2026, 8, 23, tzinfo=timezone.utc),
                project_filter=None,
                include_subagents=False,
                out_dir=root / "run",
                miner_report_paths=(),
                apply=False,
            )
        except IncompleteDigestError as exc:
            assert "manifest" in str(exc)
        else:
            raise AssertionError("incomplete input must stop the run")


def test_reflection_continuation_validates_reports_and_writes_ranked_report():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "projects" / "project-a"
        source.mkdir(parents=True)
        (source / "session.jsonl").write_text(
            '{"type":"user","timestamp":"2026-08-23T10:00:00Z",'
            '"message":{"role":"user","content":"please fix this"}}\n'
        )
        kwargs = dict(
            runtime_name="claude",
            home=root,
            source_root=root / "projects",
            since=datetime(2026, 8, 23, tzinfo=timezone.utc),
            project_filter=None,
            include_subagents=False,
            out_dir=root / "run",
            apply=False,
        )
        try:
            run_reflection(miner_report_paths=(), **kwargs)
        except ReportValidationError:
            pass
        else:
            raise AssertionError("a signal-bearing run needs miner coverage")
        manifest = json.loads((root / "run" / "manifest.json").read_text())
        digest_path = manifest["source_files"][0]["digest_path"]
        report = root / "miner-report.json"
        report.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "runtime": "claude",
                    "run_id": manifest["run_id"],
                    "batch_id": "batch-a",
                    "digest_paths": [digest_path],
                    "findings": [
                        {
                            "cluster_key": "repeat-fix",
                            "finding_type": "failure",
                            "session_id": "session",
                            "paraphrase": "the same fix is requested",
                            "occurrence_count": 1,
                            "confidence": 0.9,
                            "evidence": [
                                {
                                    "digest_path": digest_path,
                                    "source_line": 1,
                                    "timestamp": "2026-08-23T10:00:00Z",
                                    "kind": "failure",
                                    "project": "project-a",
                                }
                            ],
                        }
                    ],
                    "themes": [],
                }
            )
        )
        result = run_reflection(miner_report_paths=(report,), **kwargs)
        assert result.report_path.is_file()
        assert [candidate.cluster_key for candidate in result.ranked_candidates] == ["repeat-fix"]
        payload = json.loads(result.report_path.read_text())
        assert payload["manifest"]["run_id"] == manifest["run_id"]
        assert payload["ranked_candidates"][0]["metrics"]["projects"] == 1


def test_codex_continuation_matches_canonical_session_scope_with_subagents():
    for include_subagents, expected_paths in (
        (False, ("canonical.jsonl", "subagents/subagent.jsonl")),
        (True, ("canonical.jsonl", "subagents/subagent.jsonl")),
    ):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            source_root = root / "sessions"
            source_root.mkdir()
            (source_root / "canonical.jsonl").write_text(
                '{"type":"session_meta","timestamp":"2026-08-23T10:00:00Z",'
                '"payload":{"id":"canonical-1","thread_source":"user",'
                '"cwd":"/tmp/project-a"}}\n'
            )
            nested = source_root / "subagents"
            nested.mkdir()
            (nested / "subagent.jsonl").write_text(
                '{"type":"session_meta","timestamp":"2026-08-23T10:00:00Z",'
                '"payload":{"id":"subagent-1","thread_source":"user",'
                '"cwd":"/tmp/project-a"}}\n'
            )
            kwargs = dict(
                runtime_name="codex",
                home=root,
                source_root=source_root,
                since=datetime(2026, 8, 23, tzinfo=timezone.utc),
                project_filter=None,
                include_subagents=include_subagents,
                out_dir=root / "run",
                apply=False,
            )
            first = run_reflection(miner_report_paths=(), **kwargs)
            second = run_reflection(miner_report_paths=(), **kwargs)
            assert tuple(
                source.source_path for source in first.manifest.source_files
            ) == expected_paths
            assert tuple(
                source.source_path for source in second.manifest.source_files
            ) == expected_paths


def test_codex_continuation_preserves_missing_project_user_session():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source_root = root / "sessions" / "2026" / "08" / "23"
        source_root.mkdir(parents=True)
        (source_root / "canonical.jsonl").write_text(
            '{"type":"session_meta","timestamp":"2026-08-23T09:00:00Z",'
            '"payload":{"id":"canonical-1","thread_source":"user"}}\n'
            '{"type":"event_msg","timestamp":"2026-08-23T10:00:00Z",'
            '"payload":{"type":"user_message","message":"canonical user"}}\n'
        )
        kwargs = dict(
            runtime_name="codex",
            home=root,
            source_root=root / "sessions",
            since=datetime(2026, 8, 23, tzinfo=timezone.utc),
            project_filter=None,
            include_subagents=False,
            out_dir=root / "run",
            apply=False,
        )
        for _ in range(2):
            try:
                run_reflection(miner_report_paths=(), **kwargs)
            except ReportValidationError as exc:
                assert "miner reports" in str(exc)
            else:
                raise AssertionError("signal-bearing continuation must require miner reports")
            manifest = json.loads((root / "run" / "manifest.json").read_text())
            assert manifest["source_files"][0]["project"] == (
                "codex:2026/08/23/canonical.jsonl"
            )


def _prepare_ranked_continuation(root):
    source = root / "projects" / "project-a"
    source.mkdir(parents=True)
    (source / "session.jsonl").write_text(
        '{"type":"user","timestamp":"2026-08-23T10:00:00Z",'
        '"message":{"role":"user","content":"please fix this"}}\n'
    )
    kwargs = dict(
        runtime_name="claude",
        home=root,
        source_root=root / "projects",
        since=datetime(2026, 8, 23, tzinfo=timezone.utc),
        project_filter=None,
        include_subagents=False,
        out_dir=root / "run",
        apply=True,
    )
    try:
        run_reflection(miner_report_paths=(), apply=False, **{key: value for key, value in kwargs.items() if key != "apply"})
    except ReportValidationError:
        pass
    manifest = json.loads((root / "run" / "manifest.json").read_text())
    digest_path = manifest["source_files"][0]["digest_path"]
    report = root / "miner-report.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime": "claude",
                "run_id": manifest["run_id"],
                "batch_id": "batch-a",
                "digest_paths": [digest_path],
                "findings": [
                    {
                        "cluster_key": "repeat-fix",
                        "finding_type": "failure",
                        "session_id": "session",
                        "paraphrase": "the same fix is requested",
                        "occurrence_count": 1,
                        "confidence": 0.9,
                        "evidence": [
                            {
                                "digest_path": digest_path,
                                "source_line": 1,
                                "timestamp": "2026-08-23T10:00:00Z",
                                "kind": "failure",
                                "project": "project-a",
                            }
                        ],
                    }
                ],
                "themes": [],
            }
        )
    )
    return kwargs, report, manifest


def test_apply_requires_typed_request_and_workspace_and_cli_rejects_bare_approval():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "projects"
        source.mkdir()
        completed = run_cli(
            "--runtime", "claude",
            "--source-root", str(source),
            "--approve-cluster", "candidate",
            "--out", str(root / "run"),
        )
        assert completed.returncode != 0
        assert "--apply" in completed.stderr
        completed = run_cli(
            "--runtime", "claude",
            "--source-root", str(source),
            "--apply",
            "--approve-cluster", "candidate",
            "--out", str(root / "run-without-request"),
        )
        assert completed.returncode != 0
        assert "ApplyRequest" in completed.stderr
        try:
            run_reflection(
                runtime_name="claude",
                home=root,
                source_root=source,
                since=datetime(2026, 8, 23, tzinfo=timezone.utc),
                project_filter=None,
                include_subagents=False,
                out_dir=root / "api-run",
                miner_report_paths=(),
                apply=True,
                approved_clusters=("candidate",),
            )
        except ApplyApprovalError as exc:
            assert "ApplyRequest" in str(exc)
            assert not (root / "api-run").exists()
        else:
            raise AssertionError("Apply without typed request/workspace must fail closed")


def test_apply_builds_previews_for_exact_approved_ids_and_validates_proof():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report, _ = _prepare_ranked_continuation(root)
        workspace = WorkspaceState(root / "projects" / "project-a", (), False)
        request = ApplyRequest(
            "Repeat Fix",
            (Path("fix.md"),),
            ("fix command",),
            ("verify command",),
        )
        result = run_reflection(
            miner_report_paths=(report,),
            approved_clusters=("repeat-fix",),
            apply_requests=(request,),
            apply_workspaces={"repeat-fix": workspace},
            **kwargs,
        )
        assert len(result.apply_previews) == 1
        assert result.apply_preview == result.apply_previews[0]
        assert result.apply_previews[0].request.cluster_id == "repeat-fix"

        proof = FixProof(
            "repeat-fix",
            (Path("fix.md"),),
            ("fix command completed",),
            ("verify command :: PASS",),
            True,
        )
        proven = run_reflection(
            miner_report_paths=(report,),
            approved_clusters=("repeat-fix",),
            apply_requests=(request,),
            apply_workspaces={"repeat-fix": workspace},
            apply_proofs=(proof,),
            **kwargs,
        )
        payload = json.loads(proven.report_path.read_text())
        assert payload["apply"]["validated_proofs"] == ["repeat-fix"]


def test_apply_rejects_approval_id_collisions_after_kebab_normalization():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report, _ = _prepare_ranked_continuation(root)
        workspace = WorkspaceState(root / "projects" / "project-a", (), False)
        request = ApplyRequest("repeat-fix", (Path("fix.md"),), ("fix",), ("verify",))
        try:
            run_reflection(
                miner_report_paths=(report,),
                approved_clusters=("repeat fix", "repeat-fix"),
                apply_requests=(request,),
                apply_workspaces={"repeat-fix": workspace},
                **kwargs,
            )
        except ApplyApprovalError as exc:
            assert "ambiguous" in str(exc)
        else:
            raise AssertionError("normalized approval collisions must fail closed")


def test_continuation_rejects_changed_source_before_trusting_reports():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report, _ = _prepare_ranked_continuation(root)
        source_file = root / "projects" / "project-a" / "session.jsonl"
        source_file.write_text(source_file.read_text() + "\n")
        try:
            run_reflection(
                miner_report_paths=(report,),
                apply=False,
                **{key: value for key, value in kwargs.items() if key != "apply"},
            )
        except ReportValidationError as exc:
            assert "source" in str(exc)
        else:
            raise AssertionError("changed source must invalidate continuation")


def test_continuation_rejects_tampered_digest_path():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report, manifest = _prepare_ranked_continuation(root)
        manifest_path = root / "run" / "manifest.json"
        value = json.loads(manifest_path.read_text())
        value["source_files"][0]["digest_path"] = str(root / "outside.md")
        manifest_path.write_text(json.dumps(value))
        try:
            run_reflection(
                miner_report_paths=(report,),
                apply=False,
                **{key: value for key, value in kwargs.items() if key != "apply"},
            )
        except ReportValidationError as exc:
            assert "digest" in str(exc)
        else:
            raise AssertionError("tampered digest path must invalidate continuation")


def test_continuation_rejects_changed_digest_bytes():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report, manifest = _prepare_ranked_continuation(root)
        digest_path = Path(manifest["source_files"][0]["digest_path"])
        digest_path.write_text(digest_path.read_text() + "tampered\n")
        try:
            run_reflection(
                miner_report_paths=(report,),
                apply=False,
                **{key: value for key, value in kwargs.items() if key != "apply"},
            )
        except ReportValidationError as exc:
            assert "digest hash" in str(exc)
        else:
            raise AssertionError("changed digest bytes must invalidate continuation")


def test_continuation_rejects_tampered_scope_metadata():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report, _ = _prepare_ranked_continuation(root)
        manifest_path = root / "run" / "manifest.json"
        value = json.loads(manifest_path.read_text())
        value["scope"]["since"] = "2026-08-22T00:00:00Z"
        manifest_path.write_text(json.dumps(value))
        try:
            run_reflection(
                miner_report_paths=(report,),
                apply=False,
                **{key: value for key, value in kwargs.items() if key != "apply"},
            )
        except ReportValidationError as exc:
            assert "scope hash" in str(exc)
        else:
            raise AssertionError("tampered scope metadata must invalidate continuation")


def test_ledger_is_only_read_when_explicitly_supplied():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report, _ = _prepare_ranked_continuation(root)
        ledger = root / "clusters.yaml"
        ledger.write_text(
            "- id: repeat-fix\n"
            "  status: fix-applied\n"
            "  wired_check: next run invokes the fix\n"
            "  artifact_ids: [repeat-fix]\n"
        )
        previous = Path.cwd()
        os.chdir(root)
        try:
            implicit = run_reflection(
                miner_report_paths=(report,),
                apply=False,
                **{key: value for key, value in kwargs.items() if key != "apply"},
            )
        finally:
            os.chdir(previous)
        assert json.loads(implicit.report_path.read_text())["verification"] == []
        explicit = run_reflection(
            miner_report_paths=(report,),
            ledger_path=ledger,
            apply=False,
            **{key: value for key, value in kwargs.items() if key != "apply"},
        )
        assert len(json.loads(explicit.report_path.read_text())["verification"]) == 1


def test_orchestration_requires_typed_host_coverage_for_operating_states():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report, manifest = _prepare_ranked_continuation(root)
        skill = root / ".claude" / "skills" / "fixture" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("fixture\n")
        ledger = root / "clusters.yaml"
        ledger.write_text(
            "- id: repeat-fix\n"
            "  status: fix-applied\n"
            "  wired_check: next run invokes the fixture skill\n"
            "  artifact_ids: [fixture/SKILL.md]\n"
        )
        digest_path = manifest["source_files"][0]["digest_path"]
        coverage = CoverageObservation(
            artifact_id="fixture/SKILL.md",
            artifact_kind="skills",
            eligible=True,
            trigger_evidence=(
                EvidenceRef(
                    digest_path, 1,
                    datetime(2026, 8, 23, tzinfo=timezone.utc),
                    FindingType.FAILURE, "project-a",
                ),
            ),
            prevention_evidence=(
                EvidenceRef(
                    digest_path, 2,
                    datetime(2026, 8, 23, tzinfo=timezone.utc),
                    FindingType.FAILURE, "project-a",
                ),
            ),
            symptom_recurred=False,
        )
        result = run_reflection(
            miner_report_paths=(report,),
            ledger_path=ledger,
            coverage_observations=(coverage,),
            apply=False,
            **{key: value for key, value in kwargs.items() if key != "apply"},
        )
        payload = json.loads(result.report_path.read_text())
        assert payload["coverage"][0]["eligible"] is True
        assert payload["verification"][0]["invocation"]["status"] == "pass"
        assert payload["verification"][0]["outcome"]["status"] == "pass"
        record_result = run_reflection(
            miner_report_paths=(report,),
            ledger_path=ledger,
            coverage_records=(assess_coverage(observation=coverage, exists=True),),
            apply=False,
            **{key: value for key, value in kwargs.items() if key != "apply"},
        )
        assert json.loads(record_result.report_path.read_text())["coverage"][0]["eligible"] is True


def test_orchestration_rejects_unknown_host_coverage_artifact():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report, _ = _prepare_ranked_continuation(root)
        unknown = CoverageObservation("not-in-inventory", "skill", True, (), (), False)
        try:
            run_reflection(
                miner_report_paths=(report,),
                coverage_observations=(unknown,),
                apply=False,
                **{key: value for key, value in kwargs.items() if key != "apply"},
            )
        except ReportValidationError as exc:
            assert "artifact" in str(exc)
        else:
            raise AssertionError("unknown coverage artifacts must fail closed")


def test_continuation_rejects_new_or_removed_in_scope_sessions():
    for mutation in ("new", "removed"):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            kwargs, report, _ = _prepare_ranked_continuation(root)
            session = root / "projects" / "project-a" / "session.jsonl"
            if mutation == "new":
                (session.parent / "new.jsonl").write_text(session.read_text())
            else:
                session.unlink()
            try:
                run_reflection(
                    miner_report_paths=(report,),
                    apply=False,
                    **{key: value for key, value in kwargs.items() if key != "apply"},
                )
            except ReportValidationError as exc:
                assert "session set" in str(exc)
            else:
                raise AssertionError(f"{mutation} in-scope session must invalidate continuation")


def test_noncanonical_fix_proof_id_is_normalized_before_validation():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report, _ = _prepare_ranked_continuation(root)
        workspace = WorkspaceState(root / "projects" / "project-a", (), False)
        request = ApplyRequest("repeat fix", (Path("fix.md"),), ("fix command",), ("verify command",))
        proof = FixProof(
            "Repeat Fix", (Path("fix.md"),), ("fix command completed",), ("verify command :: PASS",), True
        )
        result = run_reflection(
            miner_report_paths=(report,),
            approved_clusters=("repeat-fix",),
            apply_requests=(request,),
            apply_workspaces={"repeat-fix": workspace},
            apply_proofs=(proof,),
            **kwargs,
        )
        payload = json.loads(result.report_path.read_text())
        assert payload["apply"]["validated_proofs"] == ["repeat-fix"]


if __name__ == "__main__":
    tests = tuple(
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    )
    for test in tests:
        test()
    print(f"{len(tests)} tests passed")
