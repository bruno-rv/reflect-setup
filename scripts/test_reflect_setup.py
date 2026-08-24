import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from digest import IncompleteDigestError
from miner_contract import ReportValidationError
from reflect_setup import run_reflection


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


if __name__ == "__main__":
    tests = tuple(
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    )
    for test in tests:
        test()
    print(f"{len(tests)} tests passed")
