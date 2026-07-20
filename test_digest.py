#!/usr/bin/env python3
"""Runnable check for digest.py. Assert-based, no pytest.

python3 test_digest.py
"""
import json
import os
import shutil
import tempfile

import digest


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
            fh.write(make_line({"type": "user", "timestamp": "t1", "message": {"role": "user", "content": "hello"}}))
            fh.write(
                make_line(
                    {
                        "type": "assistant",
                        "timestamp": "t2",
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
                        "timestamp": "t3",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "just thinking"}]},
                    }
                )
            )

        # A memory/ path must never be descended into.
        memory_dir = os.path.join(proj_a, "memory")
        os.makedirs(memory_dir)
        with open(os.path.join(memory_dir, "session-in-memory.jsonl"), "w") as fh:
            fh.write(make_line({"type": "user", "timestamp": "t4", "message": {"role": "user", "content": "secret"}}))

        scanned, written, n_projects, counts = digest.run(projects_dir, since_days=3650, out_dir=out_dir, project_filter=None)

        assert scanned == 2, scanned  # memory session never yielded
        assert written == 1, written
        assert n_projects == 1, n_projects
        assert counts["user"] == 1, counts

        digest_files = os.listdir(out_dir)
        assert len(digest_files) == 1, digest_files
        assert digest_files[0] == "proj-a__session-with-signal.md", digest_files
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
