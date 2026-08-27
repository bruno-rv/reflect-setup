import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from digest import IncompleteDigestError
from apply import ApplyRequest, FixProof, WorkspaceState
from coverage_model import CoverageObservation, CoverageRecord, Inventory, InventoryItem, assess_coverage
from miner_contract import EvidenceRef, FindingType, ReportValidationError
from reflect_setup import ApplyApprovalError, _coverage, _inventory, run_reflection
from runtime import Runtime, RuntimeSpec
from workflow_contract import ApplyInput, HostInput, RunStage, WorkspaceBinding


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "scripts/reflect_setup.py", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _signal_source(root, project="project-a", session="session.jsonl"):
    source = root / "projects" / project
    source.mkdir(parents=True)
    (source / session).write_text(
        '{"type":"user","timestamp":"2026-08-23T10:00:00Z",'
        '"message":{"role":"user","content":"please fix this"}}\n'
    )
    return source


def _base_kwargs(root, *, apply=False, out="run"):
    return dict(
        runtime_name="claude",
        home=root,
        source_root=root / "projects",
        since=datetime(2026, 8, 23, tzinfo=timezone.utc),
        project_filter=None,
        include_subagents=False,
        out_dir=root / out,
        apply=apply,
    )


def _miner_payload(manifest, digest_path, *, cluster_key="repeat-fix", source_line=1, session_id="session", project="project-a", kind="failure", timestamp="2026-08-23T10:00:00Z"):
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
                "session_id": session_id,
                "paraphrase": "the same fix is requested",
                "occurrence_count": 1,
                "confidence": 0.9,
                "evidence": [
                    {
                        "digest_path": digest_path,
                        "source_line": source_line,
                        "timestamp": timestamp,
                        "kind": kind,
                        "project": project,
                    }
                ],
            }
        ],
        "themes": [],
    }


def _write_miner_report(out_dir, manifest, digest_path, **overrides):
    plan = json.loads((out_dir / "dispatch-plan.json").read_text())
    batch = next(
        item for item in plan["batches"] if digest_path in item["digest_paths"]
    )
    payload = _miner_payload(manifest, digest_path, **overrides)
    payload["batch_id"] = batch["batch_id"]
    payload["digest_paths"] = batch["digest_paths"]
    report_path = Path(batch["report_path"])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload))
    return report_path


def _prepare_ranked_continuation(root, *, apply=False):
    _signal_source(root)
    kwargs = _base_kwargs(root, apply=False)
    first = run_reflection(**kwargs)
    assert first.stage is RunStage.AWAITING_MINERS
    manifest = json.loads((root / "run" / "manifest.json").read_text())
    digest_path = manifest["source_files"][0]["digest_path"]
    report_path = _write_miner_report(root / "run", manifest, digest_path)
    if apply:
        kwargs["apply"] = True
    return kwargs, report_path, manifest


def _host_input_payload(approved, requests, workspaces, proofs, coverage=()):
    return {
        "schema_version": 1,
        "coverage_observations": list(coverage),
        "apply": {
            "approved_clusters": approved,
            "requests": requests,
            "workspaces": workspaces,
            "proofs": proofs,
        },
    }


def _apply_request_payload(cluster_id="repeat-fix", target="fix.md"):
    return {
        "cluster_id": cluster_id,
        "target_paths": [target],
        "commands": ["fix command"],
        "verification_commands": ["verify command"],
    }


def _workspace_payload(cluster_id="repeat-fix", project_root=None, dirty=()):
    return {
        "cluster_id": cluster_id,
        "workspace": {
            "project_root": str(project_root),
            "dirty_paths": list(dirty),
            "conflicted": False,
        },
    }


def _proof_payload(cluster_id="repeat-fix", changed=("fix.md",)):
    return {
        "cluster_id": cluster_id,
        "changed_paths": list(changed),
        "command_output": ["fix command completed"],
        "verification_output": ["verify command :: PASS"],
        "passed": True,
    }


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
        result = run_reflection(**_base_kwargs(root))
        assert result.manifest.complete is True
        assert result.stage is RunStage.DIAGNOSIS_COMPLETE
        assert result.report_path == (root / "run" / "reflection-report.json").resolve()
        assert result.ranked_candidates == ()
        assert result.apply_preview is None


def test_end_to_end_honest_digest_evidence_passes_and_digest_local_line_fails():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "projects" / "project-a"
        source.mkdir(parents=True)
        context = "".join(
            '{{"type":"assistant","timestamp":"2026-08-23T10:0{0}:00Z",'
            '"message":{{"role":"assistant","content":[{{"type":"text",'
            '"text":"context"}}]}}}}\n'.format(index)
            for index in range(6)
        )
        (source / "session.jsonl").write_text(
            context
            + '{"type":"user","timestamp":"2026-08-23T10:06:00Z",'
            '"message":{"role":"user","content":"please fix this"}}\n'
        )
        kwargs = _base_kwargs(root)
        first = run_reflection(**kwargs)
        assert first.stage is RunStage.AWAITING_MINERS
        assert first.report_path is None
        assert first.dispatch_plan_path is not None
        assert len(first.missing_paths) == 1

        manifest = json.loads((root / "run" / "manifest.json").read_text())
        source_entry = manifest["source_files"][0]
        digest_path = Path(source_entry["digest_path"])
        signal_lines = [
            (index, json.loads(line))
            for index, line in enumerate(digest_path.read_text().splitlines(), 1)
            if line.startswith("{")
        ]
        local_line, visible = signal_lines[0]
        assert visible["source_kind"] == "user"
        report_path = _write_miner_report(
            root / "run",
            manifest,
            source_entry["digest_path"],
            source_line=visible["source_line"],
            session_id=visible["session_id"],
            project=visible["project"],
            timestamp=visible["timestamp"],
        )
        result = run_reflection(**kwargs)
        assert result.stage is RunStage.DIAGNOSIS_COMPLETE
        assert result.ranked_candidates[0].cluster_key == "repeat-fix"

        dishonest = json.loads(report_path.read_text())
        dishonest["findings"][0]["evidence"][0]["source_line"] = local_line
        report_path.write_text(json.dumps(dishonest))
        try:
            run_reflection(**kwargs)
        except ReportValidationError as exc:
            assert "retained source line" in str(exc)
        else:
            raise AssertionError("digest-local source lines must fail evidence validation")


def test_inventory_ids_namespace_runtime_and_global_project_root_origin():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        global_skill = root / ".codex" / "skills" / "fixture" / "SKILL.md"
        project_skill = root / "project" / ".codex" / "skills" / "fixture" / "SKILL.md"
        global_skill.parent.mkdir(parents=True)
        project_skill.parent.mkdir(parents=True)
        global_skill.write_text("global")
        project_skill.write_text("project")
        spec = RuntimeSpec(
            Runtime.CODEX,
            root,
            root / "sessions",
            root / ".codex" / "skills" / "reflect-setup",
            (root / ".codex" / "skills", Path("project/.codex/skills")),
        )
        old_cwd = Path.cwd()
        os.chdir(root)
        try:
            inventory = _inventory(spec)
        finally:
            os.chdir(old_cwd)
        ids = {item.artifact_id for item in inventory.items}
        assert "codex:global:.codex/skills/fixture/SKILL.md" in ids
        assert "codex:project:project/.codex/skills/fixture/SKILL.md" in ids


def test_inventory_ids_include_claude_runtime_namespace():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        global_skill = root / ".claude" / "skills" / "fixture" / "SKILL.md"
        global_skill.parent.mkdir(parents=True)
        global_skill.write_text("global")
        spec = RuntimeSpec(
            Runtime.CLAUDE,
            root,
            root / "projects",
            root / ".claude" / "skills" / "reflect-setup",
            (root / ".claude" / "skills",),
        )
        inventory = _inventory(spec)
        assert {
            item.artifact_id for item in inventory.items
        } == {"claude:global:.claude/skills/fixture/SKILL.md"}


def test_inventory_ids_include_claude_hooks_roots():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        global_hook = root / ".claude" / "hooks" / "retry.sh"
        project_hook = root / "project" / ".claude" / "hooks" / "retry.sh"
        global_hook.parent.mkdir(parents=True)
        project_hook.parent.mkdir(parents=True)
        global_hook.write_text("global hook")
        project_hook.write_text("project hook")
        spec = RuntimeSpec(
            Runtime.CLAUDE,
            root,
            root / "projects",
            root / ".claude" / "skills" / "reflect-setup",
            (root / ".claude" / "hooks", Path("project/.claude/hooks")),
        )
        old_cwd = Path.cwd()
        os.chdir(root)
        try:
            inventory = _inventory(spec)
        finally:
            os.chdir(old_cwd)
        ids = {item.artifact_id for item in inventory.items}
        assert "claude:global:.claude/hooks/retry.sh" in ids
        assert "claude:project:project/.claude/hooks/retry.sh" in ids


def test_coverage_rejects_duplicate_inventory_before_lookup_overwrite():
    inventory = Inventory(
        (
            InventoryItem("codex:global:skills:fixture", "skills", Path("one"), True),
            InventoryItem("codex:global:skills:fixture", "skills", Path("two"), True),
        )
    )
    record = CoverageRecord(
        artifact_id="codex:global:skills:fixture",
        artifact_kind="skills",
        declared=True,
        eligible=False,
        triggered=False,
        prevented=None,
        evidence=(),
        detail="fixture",
    )
    try:
        _coverage(inventory, None, (record,))
    except ReportValidationError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate inventory identities must fail closed")


def test_coverage_rejects_duplicate_supplied_records():
    record = CoverageRecord(
        artifact_id="codex:global:skills:fixture",
        artifact_kind="skills",
        declared=True,
        eligible=False,
        triggered=False,
        prevented=None,
        evidence=(),
        detail="fixture",
    )
    inventory = Inventory((InventoryItem(record.artifact_id, record.artifact_kind, Path("one"), True),))
    try:
        _coverage(inventory, None, (record, record))
    except ReportValidationError as exc:
        assert "duplicate coverage record" in str(exc)
    else:
        raise AssertionError("duplicate supplied coverage records must fail closed")


def test_ranking_denominator_uses_canonical_project_selected_manifest_sessions():
    for project_filter, include_subagents, extra_path, extra_project in (
        (None, False, "project-a/subagents/agent.jsonl", "project-a"),
        ("project-a", False, "project-b/session-b.jsonl", "project-b"),
    ):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            source_root = root / "projects"
            canonical = source_root / "project-a" / "session-a.jsonl"
            canonical.parent.mkdir(parents=True)
            canonical.write_text(
                '{"type":"user","timestamp":"2026-08-23T10:00:00Z",'
                '"message":{"role":"user","content":"please fix this"}}\n'
            )
            extra = source_root / extra_path
            extra.parent.mkdir(parents=True)
            extra.write_text(
                '{"type":"user","timestamp":"2026-08-23T10:00:00Z",'
                '"message":{"role":"user","content":"another signal"}}\n'
            )
            kwargs = dict(
                runtime_name="claude",
                home=root,
                source_root=source_root,
                since=datetime(2026, 8, 23, tzinfo=timezone.utc),
                project_filter=project_filter,
                include_subagents=include_subagents,
                out_dir=root / "run",
                apply=False,
            )
            first = run_reflection(**kwargs)
            assert first.stage is RunStage.AWAITING_MINERS
            manifest = json.loads((root / "run" / "manifest.json").read_text())
            selected = [
                item for item in manifest["source_files"] if item["digest_path"] is not None
            ]
            assert len(selected) == 1
            item = selected[0]
            entry = item["evidence_index"][0]
            _write_miner_report(
                root / "run",
                manifest,
                item["digest_path"],
                source_line=entry["source_line"],
                session_id=entry["session_id"],
                project=entry["project"],
            )
            result = run_reflection(**kwargs)
            assert result.stage is RunStage.DIAGNOSIS_COMPLETE
            assert result.ranked_candidates[0].metrics.analyzed_sessions == 1


def test_cli_rejects_apply_without_host_input_approval():
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
        assert "host input" in completed.stderr


def test_incomplete_digest_cannot_dispatch_miners():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "projects" / "project-a"
        source.mkdir(parents=True)
        (source / "broken.jsonl").write_text("{not valid json\n")
        try:
            run_reflection(**_base_kwargs(root))
        except IncompleteDigestError as exc:
            assert "manifest" in str(exc)
        else:
            raise AssertionError("incomplete input must stop the run")


def test_reflection_continuation_validates_reports_and_writes_ranked_report():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report_path, manifest = _prepare_ranked_continuation(root)
        result = run_reflection(**kwargs)
        assert result.stage is RunStage.DIAGNOSIS_COMPLETE
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
            first = run_reflection(**kwargs)
            second = run_reflection(**kwargs)
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
            first = run_reflection(**kwargs)
            assert first.stage is RunStage.AWAITING_MINERS
            manifest = json.loads((root / "run" / "manifest.json").read_text())
            assert manifest["source_files"][0]["project"] == (
                "codex:2026/08/23/canonical.jsonl"
            )


def test_apply_requires_host_input_and_cli_rejects_bare_approval():
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
        assert "host input" in completed.stderr
        try:
            run_reflection(**_base_kwargs(root, apply=True))
        except ApplyApprovalError as exc:
            assert "host input" in str(exc)
        else:
            raise AssertionError("Apply without host input must fail closed")


def test_apply_builds_previews_for_exact_approved_ids_and_validates_proof():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report_path, manifest = _prepare_ranked_continuation(root, apply=True)
        project_root = root / "projects" / "project-a"
        host_input = HostInput.parse(
            json.dumps(
                _host_input_payload(
                    ["repeat-fix"],
                    [_apply_request_payload()],
                    [_workspace_payload(project_root=project_root)],
                    [],
                )
            )
        )
        result = run_reflection(host_input=host_input, **kwargs)
        assert result.stage is RunStage.APPLY_PREVIEW
        assert len(result.apply_previews) == 1
        assert result.apply_preview == result.apply_previews[0]
        assert result.apply_previews[0].request.cluster_id == "repeat-fix"

        proven_input = HostInput.parse(
            json.dumps(
                _host_input_payload(
                    ["repeat-fix"],
                    [_apply_request_payload()],
                    [_workspace_payload(project_root=project_root)],
                    [_proof_payload()],
                )
            )
        )
        proven = run_reflection(host_input=proven_input, **kwargs)
        assert proven.stage is RunStage.APPLY_VALIDATED
        payload = json.loads(proven.report_path.read_text())
        assert payload["apply"]["validated_proofs"] == ["repeat-fix"]


def test_apply_rejects_approval_id_collisions_after_kebab_normalization():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report_path, manifest = _prepare_ranked_continuation(root, apply=True)
        project_root = root / "projects" / "project-a"
        host_input = HostInput.parse(
            json.dumps(
                _host_input_payload(
                    ["repeat fix", "repeat-fix"],
                    [_apply_request_payload()],
                    [_workspace_payload(project_root=project_root)],
                    [],
                )
            )
        )
        try:
            run_reflection(host_input=host_input, **kwargs)
        except ApplyApprovalError as exc:
            assert "ambiguous" in str(exc)
        else:
            raise AssertionError("normalized approval collisions must fail closed")


def test_continuation_rejects_changed_source_before_trusting_reports():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report_path, manifest = _prepare_ranked_continuation(root)
        source_file = root / "projects" / "project-a" / "session.jsonl"
        source_file.write_text(source_file.read_text() + "\n")
        try:
            run_reflection(**kwargs)
        except ReportValidationError as exc:
            assert "source" in str(exc)
        else:
            raise AssertionError("changed source must invalidate continuation")


def test_continuation_rejects_tampered_digest_path():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report_path, manifest = _prepare_ranked_continuation(root)
        manifest_path = root / "run" / "manifest.json"
        value = json.loads(manifest_path.read_text())
        value["source_files"][0]["digest_path"] = str(root / "outside.md")
        manifest_path.write_text(json.dumps(value))
        try:
            run_reflection(**kwargs)
        except ReportValidationError as exc:
            assert "digest" in str(exc)
        else:
            raise AssertionError("tampered digest path must invalidate continuation")


def test_continuation_rejects_changed_digest_bytes():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report_path, manifest = _prepare_ranked_continuation(root)
        digest_path = Path(manifest["source_files"][0]["digest_path"])
        digest_path.write_text(digest_path.read_text() + "tampered\n")
        try:
            run_reflection(**kwargs)
        except ReportValidationError as exc:
            assert "digest hash" in str(exc)
        else:
            raise AssertionError("changed digest bytes must invalidate continuation")


def test_continuation_rejects_tampered_scope_metadata():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report_path, manifest = _prepare_ranked_continuation(root)
        manifest_path = root / "run" / "manifest.json"
        value = json.loads(manifest_path.read_text())
        value["scope"]["since"] = "2026-08-22T00:00:00Z"
        manifest_path.write_text(json.dumps(value))
        try:
            run_reflection(**kwargs)
        except ReportValidationError as exc:
            assert "scope hash" in str(exc)
        else:
            raise AssertionError("tampered scope metadata must invalidate continuation")


def test_continuation_rejects_tampered_evidence_index_metadata():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report_path, manifest = _prepare_ranked_continuation(root)
        manifest_path = root / "run" / "manifest.json"
        value = json.loads(manifest_path.read_text())
        value["source_files"][0]["evidence_index"][0]["timestamp"] = (
            "2026-08-23T11:00:00Z"
        )
        manifest_path.write_text(json.dumps(value))
        try:
            run_reflection(**kwargs)
        except ReportValidationError as exc:
            assert "evidence index" in str(exc)
        else:
            raise AssertionError("tampered evidence index must invalidate continuation")


def test_continuation_rejects_changed_miner_batch_count():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        for project in ("project-a", "project-b", "project-c"):
            _signal_source(root, project=project, session=f"session-{project}.jsonl")
        kwargs = _base_kwargs(root)
        first = run_reflection(**kwargs)
        assert first.stage is RunStage.AWAITING_MINERS
        assert len(first.missing_paths) == 3
        manifest = json.loads((root / "run" / "manifest.json").read_text())
        for source in manifest["source_files"]:
            if source["digest_path"] is None:
                continue
            _write_miner_report(
                root / "run",
                manifest,
                source["digest_path"],
                session_id=source["evidence_index"][0]["session_id"],
                project=source["evidence_index"][0]["project"],
            )
        try:
            run_reflection(miner_batch_count=2, **kwargs)
        except ReportValidationError as exc:
            assert "batch count" in str(exc)
        else:
            raise AssertionError("changed --miner-batches must invalidate continuation")


def test_continuation_rejects_unplanned_miner_report_file():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report_path, manifest = _prepare_ranked_continuation(root)
        (root / "run" / "miner-reports" / "unplanned.json").write_text("{}")
        try:
            run_reflection(**kwargs)
        except ReportValidationError as exc:
            assert "unplanned" in str(exc)
        else:
            raise AssertionError("unplanned miner report files must fail closed")


def test_ledger_is_only_read_when_explicitly_supplied():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report_path, manifest = _prepare_ranked_continuation(root)
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
            implicit = run_reflection(**kwargs)
        finally:
            os.chdir(previous)
        assert json.loads(implicit.report_path.read_text())["verification"] == []
        explicit = run_reflection(ledger_path=ledger, **kwargs)
        assert len(json.loads(explicit.report_path.read_text())["verification"]) == 1


def test_orchestration_requires_typed_host_coverage_for_operating_states():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report_path, manifest = _prepare_ranked_continuation(root)
        skill = root / ".claude" / "skills" / "fixture" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("fixture\n")
        ledger = root / "clusters.yaml"
        ledger.write_text(
            "- id: repeat-fix\n"
            "  status: fix-applied\n"
            "  wired_check: next run invokes the fixture skill\n"
            "  artifact_ids: [claude:global:.claude/skills/fixture/SKILL.md]\n"
        )
        digest_path = manifest["source_files"][0]["digest_path"]
        coverage = CoverageObservation(
            artifact_id="claude:global:.claude/skills/fixture/SKILL.md",
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
            coverage_observations=(coverage,),
            ledger_path=ledger,
            **kwargs,
        )
        payload = json.loads(result.report_path.read_text())
        assert payload["coverage"][0]["eligible"] is True
        assert payload["verification"][0]["invocation"]["status"] == "pass"
        assert payload["verification"][0]["outcome"]["status"] == "pass"
        record_result = run_reflection(
            coverage_records=(assess_coverage(observation=coverage, exists=True),),
            ledger_path=ledger,
            **kwargs,
        )
        assert json.loads(record_result.report_path.read_text())["coverage"][0]["eligible"] is True


def test_orchestration_rejects_unknown_host_coverage_artifact():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report_path, manifest = _prepare_ranked_continuation(root)
        unknown = CoverageObservation("not-in-inventory", "skill", True, (), (), False)
        try:
            run_reflection(coverage_observations=(unknown,), **kwargs)
        except ReportValidationError as exc:
            assert "artifact" in str(exc)
        else:
            raise AssertionError("unknown coverage artifacts must fail closed")


def test_continuation_rejects_new_or_removed_in_scope_sessions():
    for mutation in ("new", "removed"):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            kwargs, report_path, manifest = _prepare_ranked_continuation(root)
            session = root / "projects" / "project-a" / "session.jsonl"
            if mutation == "new":
                (session.parent / "new.jsonl").write_text(session.read_text())
            else:
                session.unlink()
            try:
                run_reflection(**kwargs)
            except ReportValidationError as exc:
                assert "session set" in str(exc)
            else:
                raise AssertionError(f"{mutation} in-scope session must invalidate continuation")


def test_noncanonical_fix_proof_id_is_normalized_before_validation():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        kwargs, report_path, manifest = _prepare_ranked_continuation(root, apply=True)
        project_root = root / "projects" / "project-a"
        host_input = HostInput.parse(
            json.dumps(
                _host_input_payload(
                    ["repeat-fix"],
                    [_apply_request_payload(cluster_id="repeat fix")],
                    [_workspace_payload(cluster_id="Repeat Fix", project_root=project_root)],
                    [_proof_payload(cluster_id="Repeat Fix")],
                )
            )
        )
        result = run_reflection(host_input=host_input, **kwargs)
        assert result.stage is RunStage.APPLY_VALIDATED
        payload = json.loads(result.report_path.read_text())
        assert payload["apply"]["validated_proofs"] == ["repeat-fix"]


def test_cli_reaches_apply_preview_with_host_input():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source = root / "projects" / "project-a"
        source.mkdir(parents=True)
        target = source / "fix.md"
        target.write_text("original\n")
        (source / "session.jsonl").write_text(
            '{"type":"user","timestamp":"2026-08-23T10:00:00Z",'
            '"message":{"role":"user","content":"please fix this"}}\n'
        )
        out_dir = root / "run"
        first = run_cli(
            "--runtime", "claude",
            "--source-root", str(root / "projects"),
            "--out", str(out_dir),
            "--json",
        )
        assert first.returncode == 0
        envelope = json.loads(first.stdout)
        assert envelope["stage"] == "awaiting-miners"
        assert envelope["dispatch_plan_path"] is not None
        assert envelope["report_path"] is None
        assert len(envelope["missing_paths"]) == 1

        manifest = json.loads((out_dir / "manifest.json").read_text())
        digest_path = manifest["source_files"][0]["digest_path"]
        plan = json.loads((out_dir / "dispatch-plan.json").read_text())
        batch = plan["batches"][0]
        report_path = Path(batch["report_path"])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                _miner_payload(
                    manifest,
                    digest_path,
                    source_line=1,
                    session_id="session",
                    project="project-a",
                )
            )
        )
        second = run_cli(
            "--runtime", "claude",
            "--source-root", str(root / "projects"),
            "--out", str(out_dir),
            "--json",
        )
        assert second.returncode == 0
        assert json.loads(second.stdout)["stage"] == "diagnosis-complete"

        host_input = root / "host-input.json"
        host_input.write_text(
            json.dumps(
                _host_input_payload(
                    ["repeat-fix"],
                    [_apply_request_payload()],
                    [_workspace_payload(project_root=source)],
                    [],
                )
            )
        )
        before = target.read_bytes()
        third = run_cli(
            "--runtime", "claude",
            "--source-root", str(root / "projects"),
            "--out", str(out_dir),
            "--apply",
            "--host-input", str(host_input),
            "--json",
        )
        assert third.returncode == 0, third.stderr
        assert json.loads(third.stdout)["stage"] == "apply-preview"
        assert target.read_bytes() == before


if __name__ == "__main__":
    tests = tuple(
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} tests passed")
