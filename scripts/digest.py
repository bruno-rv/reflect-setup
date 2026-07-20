#!/usr/bin/env python3
"""Deterministic pre-extraction digest for reflect-setup.

Streams Claude Code session .jsonl transcripts line-by-line (never loads a
whole file into memory) and extracts ONLY:
  - user-role message text (truncated to ~500 chars), with timestamp
  - tool_result content blocks with is_error:true (truncated to ~300 chars)
  - interrupt markers ("[Request interrupted")

In Claude Code transcripts, tool results arrive as type:"user" lines with a
tool_result content block -- there is no separate "tool" role. Both real
user text and error results live inside type=="user" lines; assistant lines
and non-error tool_result content are never scanned, so a source-code
string like "permission denied" inside a file-read result is never mistaken
for a real signal.

Usage:
  python3 digest.py --projects-dir ~/.claude/projects --since 30 --out <dir> [--project-filter substr]
"""
import argparse
import json
import os
import sys
import time

USER_TRUNCATE = 500
ERROR_TRUNCATE = 300
INTERRUPT_MARKER = "[Request interrupted"


def truncate(text, limit):
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _blocks_text(content):
    """Extract joined text from a list of {"type": "text", "text": ...} blocks."""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(parts)


def signals_from_line(raw_line):
    """Yield (kind, timestamp, text) signals from one raw jsonl line.

    kind is one of: user, error, interrupt. Only type=="user" lines are ever
    inspected -- this is the core false-positive guard (assistant text and
    tool-call args are never scanned for signal-like strings).
    """
    try:
        entry = json.loads(raw_line)
    except (json.JSONDecodeError, ValueError):
        return
    if entry.get("type") != "user":
        return
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return
    timestamp = entry.get("timestamp", "")
    content = message.get("content")

    if isinstance(content, str):
        text = content.strip()
        if not text:
            return
        if INTERRUPT_MARKER in text:
            yield ("interrupt", timestamp, truncate(text, USER_TRUNCATE))
        else:
            yield ("user", timestamp, truncate(text, USER_TRUNCATE))
        return

    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = (block.get("text") or "").strip()
                if not text:
                    continue
                if INTERRUPT_MARKER in text:
                    yield ("interrupt", timestamp, truncate(text, USER_TRUNCATE))
                else:
                    yield ("user", timestamp, truncate(text, USER_TRUNCATE))
            elif btype == "tool_result" and block.get("is_error"):
                inner = block.get("content")
                if isinstance(inner, str):
                    text = inner.strip()
                elif isinstance(inner, list):
                    text = _blocks_text(inner).strip()
                else:
                    text = ""
                if text:
                    yield ("error", timestamp, truncate(text, ERROR_TRUNCATE))
            # non-error tool_result content, and any other block type, is
            # deliberately never inspected: that's where source-code noise
            # ("Error:", "permission denied" inside a file read) lives.


def iter_session_files(projects_dir, since_days, project_filter):
    """Yield (project, path) for session .jsonl files modified within since_days.

    Any 'memory' directory is skipped entirely (never descended into), per
    the requirement to skip paths containing /memory/.
    """
    cutoff = time.time() - since_days * 86400
    try:
        project_names = sorted(os.listdir(projects_dir))
    except OSError as exc:
        print(f"digest: cannot read {projects_dir}: {exc}", file=sys.stderr)
        return
    for project in project_names:
        if project_filter and project_filter not in project:
            continue
        project_path = os.path.join(projects_dir, project)
        if not os.path.isdir(project_path):
            continue
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d != "memory"]
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(root, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                yield project, path


def digest_session(path):
    """Return the list of (kind, timestamp, text) signals in one session file."""
    signals = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                signals.extend(signals_from_line(line))
    except OSError as exc:
        print(f"digest: skipping unreadable {path}: {exc}", file=sys.stderr)
    return signals


def write_digest(out_dir, project, session_basename, signals):
    os.makedirs(out_dir, exist_ok=True)
    fname = f"{project}__{session_basename}.md"
    fpath = os.path.join(out_dir, fname)
    with open(fpath, "w", encoding="utf-8") as fh:
        fh.write(f"# {session_basename} ({project})\n\n")
        for kind, ts, text in signals:
            fh.write(f"- {ts} [{kind}] {text}\n")
    return fpath


def run(projects_dir, since_days, out_dir, project_filter):
    sessions_scanned = 0
    sessions_written = 0
    counts = {"user": 0, "error": 0, "interrupt": 0}
    projects_seen = set()

    for project, path in iter_session_files(projects_dir, since_days, project_filter):
        sessions_scanned += 1
        projects_seen.add(project)
        signals = digest_session(path)
        if not signals:
            continue
        for kind, _, _ in signals:
            counts[kind] = counts.get(kind, 0) + 1
        session_basename = os.path.basename(path)
        if session_basename.endswith(".jsonl"):
            session_basename = session_basename[: -len(".jsonl")]
        write_digest(out_dir, project, session_basename, signals)
        sessions_written += 1

    return sessions_scanned, sessions_written, len(projects_seen), counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects-dir", required=True, help="e.g. ~/.claude/projects")
    parser.add_argument("--since", type=int, default=30, help="days back (default 30)")
    parser.add_argument("--out", required=True, help="directory to write digests into")
    parser.add_argument("--project-filter", default=None, help="substring match on project dir name")
    args = parser.parse_args()

    projects_dir = os.path.expanduser(args.projects_dir)
    out_dir = os.path.expanduser(args.out)

    scanned, written, n_projects, counts = run(projects_dir, args.since, out_dir, args.project_filter)

    print(f"digest: scanned {scanned} session file(s) across {n_projects} project(s) (since {args.since}d)")
    print(f"digest: {written} session(s) had signals -> {out_dir}")
    print(
        "digest: signals extracted -> "
        f"user={counts['user']} error={counts['error']} interrupt={counts['interrupt']}"
    )


if __name__ == "__main__":
    main()
