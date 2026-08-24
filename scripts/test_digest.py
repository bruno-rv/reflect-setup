#!/usr/bin/env python3
"""Runnable check for digest.py. Assert-based, no pytest.

python3 test_digest.py
"""
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import digest
from digest import (
    IncompleteDigestError,
    LegacyDigestWorkflowError,
    SignalKind,
    run_digest,
    signals_from_event,
)
from runtime import Scope, resolve_runtime


def make_line(entry):
    return json.dumps(entry) + "\n"


def test_user_message_kept():
    line = make_line(
        {
            "type": "user",
            "timestamp": "2026-07-20T10:00:00Z",
            "message": {"role": "user", "content": "actually that's wrong, use python3"},
        }
    )
    signals = list(digest.signals_from_line(line))
    assert signals == [("user", "2026-07-20T10:00:00Z", "actually that's wrong, use python3")], signals


def test_error_result_kept():
    line = make_line(
        {
            "type": "user",
            "timestamp": "2026-07-20T10:01:00Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "is_error": True,
                        "content": "Error: permission denied",
                    }
                ],
            },
        }
    )
    signals = list(digest.signals_from_line(line))
    assert signals == [("error", "2026-07-20T10:01:00Z", "Error: permission denied")], signals


def test_non_error_tool_result_with_fake_error_string_ignored():
    # A file-read result whose *content* happens to contain "Error:" must
    # never be mistaken for a real error signal -- this is the core
    # false-positive guard.
    line = make_line(
        {
            "type": "user",
            "timestamp": "2026-07-20T10:02:00Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_2",
                        "is_error": False,
                        "content": "def handler():\n    raise ValueError('Error: permission denied')\n",
                    }
                ],
            },
        }
    )
    signals = list(digest.signals_from_line(line))
    assert signals == [], signals


def test_assistant_text_never_scanned():
    line = make_line(
        {
            "type": "assistant",
            "timestamp": "2026-07-20T10:03:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Error: this looks like a signal but it's assistant text"}],
            },
        }
    )
    signals = list(digest.signals_from_line(line))
    assert signals == [], signals


def test_interrupt_marker_detected():
    line = make_line(
        {
            "type": "user",
            "timestamp": "2026-07-20T10:04:00Z",
            "message": {"role": "user", "content": "[Request interrupted by user]"},
        }
    )
    signals = list(digest.signals_from_line(line))
    assert len(signals) == 1 and signals[0][0] == "interrupt", signals


def test_truncation_applied():
    long_text = "x" * 900
    line = make_line(
        {
            "type": "user",
            "timestamp": "2026-07-20T10:05:00Z",
            "message": {"role": "user", "content": long_text},
        }
    )
    signals = list(digest.signals_from_line(line))
    kind, ts, text = signals[0]
    assert len(text) <= digest.USER_TRUNCATE + 1, len(text)  # +1 for ellipsis char
    assert text.endswith("…"), text


def test_malformed_json_line_skipped():
    signals = list(digest.signals_from_line("{not json"))
    assert signals == []


def test_event_timestamp_controls_scope_even_when_mtime_is_stale():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source_root = root / "projects" / "project-a"
        source_root.mkdir(parents=True)
        source = source_root / "session.jsonl"
        source.write_text(
            '{"type":"user","timestamp":"2026-08-23T10:00:00Z",'
            '"message":{"role":"user","content":"recent event"}}\n'
        )
        os.utime(source, (1, 1))
        spec = resolve_runtime("claude", home=root, env={}, source_root=source_root.parent)
        scope = Scope(datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc), None, False)
        manifest = run_digest(spec, scope, root / "out")
        assert manifest.sessions_with_signals == 1
        assert manifest.signal_counts["user"] == 1


def test_old_event_is_excluded_from_recent_file_and_output_is_collision_free():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source_root = root / "projects" / "project-a" / "nested"
        source_root.mkdir(parents=True)
        first = source_root / "same.jsonl"
        second = root / "projects" / "project-a" / "same.jsonl"
        second.parent.mkdir(parents=True, exist_ok=True)
        line = '{"type":"user","timestamp":"2026-08-01T10:00:00Z",' \
               '"message":{"role":"user","content":"old"}}\n'
        first.write_text(line)
        second.write_text(line)
        spec = resolve_runtime("claude", home=root, env={}, source_root=root / "projects")
        scope = Scope(datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc), None, False)
        manifest = run_digest(spec, scope, root / "out")
        assert manifest.sessions_with_signals == 0
        assert len(list((root / "out").glob("*.md"))) == 0
        assert len(manifest.source_files) == 2


def test_codex_extracts_canonical_user_and_true_tool_error_only():
    scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False)
    user = make_line(
        {
            "type": "response_item",
            "timestamp": "2026-08-23T10:00:00Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "please correct this"}],
            },
        }
    )
    tool_error = make_line(
        {
            "type": "response_item",
            "timestamp": "2026-08-23T10:01:00Z",
            "payload": {
                "type": "function_call_output",
                "is_error": True,
                "output": "Error: command failed",
            },
        }
    )
    assistant = make_line(
        {
            "type": "response_item",
            "timestamp": "2026-08-23T10:02:00Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Error: not a user signal"}],
            },
        }
    )
    successful_tool = make_line(
        {
            "type": "response_item",
            "timestamp": "2026-08-23T10:03:00Z",
            "payload": {
                "type": "function_call_output",
                "is_error": False,
                "output": "Error: source text only",
            },
        }
    )
    assert signals_from_event(
        user,
        runtime="codex",
        session_id="session-1",
        source_line=1,
        scope=scope,
    )[0].kind is SignalKind.USER
    assert signals_from_event(
        tool_error,
        runtime="codex",
        session_id="session-1",
        source_line=2,
        scope=scope,
    )[0].kind is SignalKind.ERROR
    assert signals_from_event(
        assistant,
        runtime="codex",
        session_id="session-1",
        source_line=3,
        scope=scope,
    ) == ()
    assert signals_from_event(
        successful_tool,
        runtime="codex",
        session_id="session-1",
        source_line=4,
        scope=scope,
    ) == ()


def test_codex_custom_tool_output_with_explicit_failure_status_is_error():
    scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False)
    line = make_line(
        {
            "type": "response_item",
            "timestamp": "2026-08-23T10:01:00Z",
            "payload": {
                "type": "custom_tool_call_output",
                "output": [
                    {"type": "input_text", "text": "Process exited with code 1"},
                    {"type": "input_text", "text": "details"},
                ],
            },
        }
    )
    signals = signals_from_event(
        line,
        runtime="codex",
        session_id="session-1",
        source_line=1,
        scope=scope,
    )
    assert len(signals) == 1 and signals[0].kind is SignalKind.ERROR, signals
    assert signals[0].text == "Process exited with code 1\ndetails", signals[0].text


def test_codex_function_output_with_explicit_failure_status_is_error():
    scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False)
    line = make_line(
        {
            "type": "response_item",
            "timestamp": "2026-08-23T10:01:00Z",
            "payload": {
                "type": "function_call_output",
                "output": "Command failed\nexit code: 1\ndetails",
            },
        }
    )
    signals = signals_from_event(
        line,
        runtime="codex",
        session_id="session-1",
        source_line=1,
        scope=scope,
    )
    assert len(signals) == 1 and signals[0].kind is SignalKind.ERROR, signals
    assert signals[0].text == "Command failed\nexit code: 1\ndetails", signals[0].text


def test_codex_successful_tool_output_with_error_looking_detail_is_ignored():
    scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False)
    line = make_line(
        {
            "type": "response_item",
            "timestamp": "2026-08-23T10:01:00Z",
            "payload": {
                "type": "custom_tool_call_output",
                "output": [
                    {"type": "input_text", "text": "Script completed"},
                    {"type": "input_text", "text": "Error: source text only"},
                ],
            },
        }
    )
    assert signals_from_event(
        line,
        runtime="codex",
        session_id="session-1",
        source_line=1,
        scope=scope,
    ) == ()


def test_codex_error_looking_first_status_line_is_not_failure_metadata():
    scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False)
    line = make_line(
        {
            "type": "response_item",
            "timestamp": "2026-08-23T10:01:00Z",
            "payload": {
                "type": "custom_tool_call_output",
                "output": [
                    {"type": "input_text", "text": "Error: source text only"},
                    {"type": "input_text", "text": "successful tool details"},
                ],
            },
        }
    )
    assert signals_from_event(
        line,
        runtime="codex",
        session_id="session-1",
        source_line=1,
        scope=scope,
    ) == ()


def test_codex_successful_output_takes_precedence_over_error_looking_content():
    scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False)
    line = make_line(
        {
            "type": "response_item",
            "timestamp": "2026-08-23T10:01:00Z",
            "payload": {
                "type": "function_call_output",
                "output": "Script completed",
                "content": "Error: source text only",
            },
        }
    )
    assert signals_from_event(
        line,
        runtime="codex",
        session_id="session-1",
        source_line=1,
        scope=scope,
    ) == ()


def test_numeric_timestamp_is_parsed_as_utc_and_old_events_are_excluded():
    scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False)
    recent = make_line(
        {
            "type": "user",
            "timestamp": 1787479200,
            "message": {"role": "user", "content": "numeric timestamp"},
        }
    )
    old = make_line(
        {
            "type": "user",
            "timestamp": "2026-08-22T23:59:59Z",
            "message": {"role": "user", "content": "old event"},
        }
    )
    recent_signals = signals_from_event(
        recent,
        runtime="claude",
        session_id="session-1",
        source_line=1,
        scope=scope,
    )
    assert recent_signals[0].timestamp.tzinfo is timezone.utc
    assert signals_from_event(
        old,
        runtime="claude",
        session_id="session-1",
        source_line=2,
        scope=scope,
    ) == ()


def test_malformed_input_writes_incomplete_manifest_before_raising():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source_root = root / "projects" / "project-a"
        source_root.mkdir(parents=True)
        (source_root / "broken.jsonl").write_text("{not valid json\n")
        spec = resolve_runtime("claude", home=root, env={}, source_root=source_root.parent)
        scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False)
        try:
            run_digest(spec, scope, root / "out")
        except IncompleteDigestError as exc:
            assert exc.manifest.complete is False
            source = exc.manifest.source_files[0]
            assert source.malformed_lines == 1
            assert source.readable is True
            assert json.loads((root / "out" / "manifest.json").read_text())["complete"] is False
        else:
            raise AssertionError("malformed input must fail after writing the manifest")


def test_non_empty_output_directory_is_rejected_without_overwriting():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source_root = root / "projects"
        source_root.mkdir(parents=True)
        out_dir = root / "out"
        out_dir.mkdir()
        sentinel = out_dir / "sentinel"
        sentinel.write_text("keep me")
        spec = resolve_runtime("claude", home=root, env={}, source_root=source_root)
        scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False)
        try:
            run_digest(spec, scope, out_dir)
        except FileExistsError:
            assert sentinel.read_text() == "keep me"
        else:
            raise AssertionError("non-empty output directories must be rejected")


def test_codex_run_filters_subagent_threads_and_records_manifest_paths():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source_root = root / "sessions"
        source_root.mkdir()
        canonical = source_root / "canonical.jsonl"
        canonical.write_text(
            make_line(
                {
                    "type": "session_meta",
                    "timestamp": "2026-08-23T09:00:00Z",
                    "payload": {"id": "user-1", "thread_source": "user", "project": "project__with__underscores", "cwd": "/tmp/ignored"},
                }
            )
            + make_line(
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-23T10:00:00Z",
                    "payload": {"type": "user_message", "message": "canonical user"},
                }
            )
        )
        subagent = source_root / "subagent.jsonl"
        subagent.write_text(
            make_line(
                {
                    "type": "session_meta",
                    "timestamp": "2026-08-23T09:00:00Z",
                    "payload": {"id": "agent-1", "thread_source": "subagent", "project": "project__with__underscores", "cwd": "/tmp/ignored"},
                }
            )
            + make_line(
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-23T10:00:00Z",
                    "payload": {"type": "user_message", "message": "subagent user"},
                }
            )
        )
        spec = resolve_runtime("codex", home=root, env={}, source_root=source_root)
        scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False)
        manifest = run_digest(spec, scope, root / "out")
        assert manifest.sessions_scanned == 2
        assert manifest.sessions_with_signals == 1
        assert manifest.signal_counts["user"] == 1
        assert [source.source_path for source in manifest.source_files] == [
            "canonical.jsonl",
            "subagent.jsonl",
        ]
        assert manifest.source_files[0].digest_path is not None
        assert manifest.source_files[1].digest_path is None
        assert manifest.source_files[0].project == "project__with__underscores"
        assert json.loads((root / "out" / "manifest.json").read_text())["source_files"][0]["project"] == "project__with__underscores"
        assert "# project: project__with__underscores" in next((root / "out").glob("*.md")).read_text()
        assert json.loads((root / "out" / "manifest.json").read_text())["runtime"] == "codex"


def test_codex_missing_or_invalid_session_metadata_is_recorded_as_incomplete():
    for filename, content in (
        ("malformed.jsonl", "{not valid json\n"),
        ("missing-meta.jsonl", '{"type":"event_msg","timestamp":"2026-08-23T10:00:00Z"}\n'),
        ("invalid-meta.jsonl", '{"type":"session_meta","payload":{"thread_source":"user"}}\n'),
    ):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            source_root = root / "sessions"
            source_root.mkdir()
            (source_root / filename).write_text(content)
            spec = resolve_runtime("codex", home=root, env={}, source_root=source_root)
            scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False)
            try:
                run_digest(spec, scope, root / "out")
            except IncompleteDigestError as exc:
                assert exc.manifest.sessions_scanned == 1
                assert exc.manifest.complete is False
                source = exc.manifest.source_files[0]
                assert source.source_path == filename
                if filename == "malformed.jsonl":
                    assert source.malformed_lines == 1
            else:
                raise AssertionError("invalid Codex metadata must fail closed")


def test_codex_subagents_path_is_filtered_only_when_subagents_are_excluded():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        source_root = root / "sessions"
        nested = source_root / "subagents"
        nested.mkdir(parents=True)
        canonical_line = make_line(
            {
                "type": "session_meta",
                "timestamp": "2026-08-23T09:00:00Z",
                "payload": {"id": "user-1", "thread_source": "user", "project": "project-a"},
            }
        ) + make_line(
            {
                "type": "event_msg",
                "timestamp": "2026-08-23T10:00:00Z",
                "payload": {"type": "user_message", "message": "canonical user"},
            }
        )
        subagent_line = canonical_line.replace("user-1", "nested-user-1").replace(
            "canonical user", "nested path user"
        )
        (source_root / "canonical.jsonl").write_text(canonical_line)
        (nested / "foo.jsonl").write_text(subagent_line)
        spec = resolve_runtime("codex", home=root, env={}, source_root=source_root)

        default = run_digest(
            spec,
            Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False),
            root / "default-out",
        )
        assert default.sessions_scanned == 2
        assert default.sessions_with_signals == 1
        assert default.source_files[1].source_path == "subagents/foo.jsonl"
        assert default.source_files[1].digest_path is None

        included = run_digest(
            spec,
            Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, True),
            root / "included-out",
        )
        assert included.sessions_scanned == 2
        assert included.sessions_with_signals == 2
        assert included.source_files[1].digest_path is not None


def test_end_to_end_writes_digest_and_skips_empty_and_memory():
    tmp = tempfile.mkdtemp()
    try:
        projects_dir = os.path.join(tmp, "projects")
        out_dir = os.path.join(tmp, "out")

        # Project A: one session with signals, one with none.
        proj_a = os.path.join(projects_dir, "proj-a")
        os.makedirs(proj_a)
        with_signal = os.path.join(proj_a, "session-with-signal.jsonl")
        with open(with_signal, "w") as fh:
            fh.write(make_line({"type": "user", "timestamp": "2026-08-23T10:00:00Z", "message": {"role": "user", "content": "hello"}}))
            fh.write(
                make_line(
                    {
                        "type": "assistant",
                        "timestamp": "2026-08-23T10:01:00Z",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]},
                    }
                )
            )

        no_signal = os.path.join(proj_a, "session-no-signal.jsonl")
        with open(no_signal, "w") as fh:
            fh.write(
                make_line(
                    {
                        "type": "assistant",
                        "timestamp": "2026-08-23T10:02:00Z",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "just thinking"}]},
                    }
                )
            )

        # A memory/ path must never be descended into.
        memory_dir = os.path.join(proj_a, "memory")
        os.makedirs(memory_dir)
        with open(os.path.join(memory_dir, "session-in-memory.jsonl"), "w") as fh:
            fh.write(make_line({"type": "user", "timestamp": "2026-08-23T10:03:00Z", "message": {"role": "user", "content": "secret"}}))

        spec = resolve_runtime("claude", home=Path(tmp), env={}, source_root=Path(projects_dir))
        manifest = run_digest(
            spec,
            Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False),
            Path(out_dir),
        )

        assert manifest.sessions_scanned == 2, manifest
        assert manifest.sessions_with_signals == 1
        assert manifest.signal_counts["user"] == 1
        digest_files = os.listdir(out_dir)
        assert len([name for name in digest_files if name.endswith(".md")]) == 1, digest_files
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_legacy_digest_cli_fails_closed_and_points_to_typed_entrypoint():
    completed = __import__("subprocess").run(
        [os.sys.executable, "scripts/digest.py", "--projects-dir", "/tmp", "--out", "/tmp/reflect-setup-test"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "reflect_setup.py" in completed.stderr


def test_legacy_digest_writer_is_unavailable():
    try:
        digest.run("/tmp", 30, "/tmp/reflect-setup-test", None)
    except LegacyDigestWorkflowError as exc:
        assert "typed" in str(exc)
    else:
        raise AssertionError("legacy digest workflow must not remain callable")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
