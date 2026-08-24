#!/usr/bin/env python3
"""Runtime-aware, deterministic pre-extraction digest for reflect-setup.

The typed path in this module scans every candidate JSONL source and filters
events by their timestamps. The older ``signals_from_line`` and ``run``
functions remain available for the original Claude-only command and tests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping

from runtime import Runtime, RuntimeSpec, Scope


USER_TRUNCATE = 500
ERROR_TRUNCATE = 300
INTERRUPT_MARKER = "[Request interrupted"
UTC = timezone.utc
FAILURE_STATUS = re.compile(
    r"^(?:(?:script|command|process)\s+(?:failed|error)\b|"
    r"(?:script|command|process)\s+exited\s+with\s+(?:code|status)\s+[1-9]\d*\b|"
    r"(?:exit\s+code|status)\s*[:=]\s*[1-9]\d*\b|"
    r"(?:non[- ]?zero)\s+exit\b)",
    re.IGNORECASE,
)


def _frozen_dataclass(cls):
    """Use slots on supported interpreters while retaining Python 3.9 support."""
    if sys.version_info >= (3, 10):
        return dataclass(frozen=True, slots=True)(cls)
    return dataclass(frozen=True)(cls)


class SignalKind(str, Enum):
    USER = "user"
    ERROR = "error"
    INTERRUPT = "interrupt"


@_frozen_dataclass
class Signal:
    kind: SignalKind
    timestamp: datetime
    session_id: str
    source_line: int
    text: str


@_frozen_dataclass
class SourceFile:
    source_path: str
    sha256: str
    scanned: bool
    readable: bool
    json_lines: int
    malformed_lines: int
    untimestamped_lines: int
    in_scope_events: int
    digest_path: str | None


@_frozen_dataclass
class DigestManifest:
    schema_version: int
    run_id: str
    runtime: Runtime
    scope: Scope
    source_files: tuple[SourceFile, ...]
    sessions_scanned: int
    sessions_with_signals: int
    signal_counts: Mapping[str, int]
    complete: bool


class IncompleteDigestError(RuntimeError):
    """Raised after an incomplete source scan has written its manifest."""

    def __init__(self, manifest: DigestManifest):
        self.manifest = manifest
        super().__init__("digest incomplete; see manifest.json for source accounting")


def truncate(text, limit):
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _blocks_text(content):
    """Extract joined text from text-like content blocks."""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("text", "input_text", "output_text"):
            value = block.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def _legacy_claude_signals(entry):
    """Return compatibility tuples from one parsed Claude event."""
    if not isinstance(entry, dict) or entry.get("type") != "user":
        return ()
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return ()
    timestamp = entry.get("timestamp", "")
    content = message.get("content")
    result = []

    def append_text(text):
        if not isinstance(text, str):
            return
        text = (text or "").strip()
        if not text:
            return
        kind = "interrupt" if INTERRUPT_MARKER in text else "user"
        result.append((kind, timestamp, truncate(text, USER_TRUNCATE)))

    if isinstance(content, str):
        append_text(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                append_text(block.get("text"))
            elif block_type == "tool_result" and block.get("is_error") is True:
                inner = block.get("content")
                if isinstance(inner, str):
                    text = inner.strip()
                elif isinstance(inner, list):
                    text = _blocks_text(inner).strip()
                else:
                    text = ""
                if text:
                    result.append(("error", timestamp, truncate(text, ERROR_TRUNCATE)))
    return tuple(result)


def signals_from_line(raw_line):
    """Yield legacy ``(kind, timestamp, text)`` tuples for Claude JSONL."""
    try:
        entry = json.loads(raw_line)
    except (json.JSONDecodeError, ValueError, TypeError):
        return
    for signal in _legacy_claude_signals(entry):
        yield signal


def _utc_timestamp(value):
    """Parse an RFC 3339 or numeric Unix timestamp into UTC."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _scope_since(scope: Scope):
    since = scope.since
    if since.tzinfo is None:
        return since.replace(tzinfo=UTC)
    return since.astimezone(UTC)


def _text_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return _blocks_text(value)
    if isinstance(value, dict):
        for key in ("text", "message", "content", "output", "result", "error"):
            if key in value:
                text = _text_value(value[key])
                if text:
                    return text
    return ""


def _codex_user_text(payload):
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    if content is None:
        content = payload.get("message")
    if content is None:
        content = payload.get("text")
    return _text_value(content).strip()


def _codex_error_text(payload):
    if not isinstance(payload, dict):
        return ""
    for key in ("output", "content", "error", "message", "result"):
        if key in payload:
            text = _text_value(payload[key]).strip()
            if text:
                return text
    return ""


def _codex_failure_text(payload):
    """Return output only when its first status line explicitly signals failure."""
    if not isinstance(payload, dict):
        return ""
    key = "output" if "output" in payload else "content"
    text = _text_value(payload.get(key)).strip()
    if text and FAILURE_STATUS.match(text.splitlines()[0].strip()):
        return text
    return ""


def _codex_signals(entry):
    """Return untyped signal tuples from canonical Codex user/error events."""
    if not isinstance(entry, dict):
        return ()
    event_type = entry.get("type")
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return ()
    result = []
    payload_type = str(payload.get("type", "")).lower()

    is_user = False
    if event_type == "response_item":
        is_user = payload.get("role") == "user" and payload_type in ("message", "user_message", "")
    elif event_type == "event_msg":
        is_user = payload.get("role") == "user" or payload_type in (
            "user_message",
            "user_input",
            "user",
            "input",
        )

    if is_user:
        text = _codex_user_text(payload)
        if text:
            kind = "interrupt" if INTERRUPT_MARKER in text else "user"
            result.append((kind, truncate(text, USER_TRUNCATE)))

    explicit_interrupt = payload_type in ("interrupt", "request_interrupted", "turn_aborted", "aborted")
    if event_type == "event_msg" and explicit_interrupt:
        text = _codex_user_text(payload) or "[Request interrupted]"
        result.append(("interrupt", truncate(text, USER_TRUNCATE)))

    tool_error_type = payload_type in (
        "custom_tool_call_output",
        "tool_error",
        "tool_result",
        "function_call_output",
        "function_call_output_error",
        "function_call_error",
        "tool_failure",
        "error",
    )
    is_true_error = payload.get("is_error") is True or payload_type == "tool_error"
    if tool_error_type:
        if payload.get("is_error") is False:
            text = ""
        elif is_true_error:
            text = _codex_error_text(payload)
        else:
            text = _codex_failure_text(payload)
        if text:
            result.append(("error", truncate(text, ERROR_TRUNCATE)))
    return tuple(result)


def signals_from_event(
    raw_line: str,
    *,
    runtime: Runtime,
    session_id: str,
    source_line: int,
    scope: Scope,
) -> tuple[Signal, ...]:
    """Extract only in-scope typed signals from one runtime event."""
    try:
        entry = json.loads(raw_line)
    except (json.JSONDecodeError, ValueError, TypeError):
        return ()
    if not isinstance(entry, dict):
        return ()
    timestamp = _utc_timestamp(entry.get("timestamp"))
    if timestamp is None or timestamp < _scope_since(scope):
        return ()
    try:
        runtime = Runtime(runtime)
    except (TypeError, ValueError):
        return ()

    if runtime is Runtime.CLAUDE:
        raw_signals = tuple(
            (kind, text) for kind, unused_timestamp, text in _legacy_claude_signals(entry)
        )
    else:
        raw_signals = _codex_signals(entry)
    return tuple(
        Signal(SignalKind(kind), timestamp, session_id, source_line, text)
        for kind, text in raw_signals
    )


def _candidate_paths(spec: RuntimeSpec, scope: Scope):
    if not spec.source_root.is_dir():
        return ()
    try:
        paths = sorted(spec.source_root.rglob("*.jsonl"))
    except OSError:
        return ()
    candidates = []
    for path in paths:
        if not path.is_file():
            continue
        relative = path.relative_to(spec.source_root).as_posix()
        if not scope.include_subagents and "subagents" in Path(relative).parts:
            continue
        if spec.runtime is Runtime.CLAUDE:
            parts = Path(relative).parts
            project = parts[0] if len(parts) > 1 else ""
            if scope.project_filter and scope.project_filter not in project:
                continue
        candidates.append((relative, path))
    return tuple(candidates)


def _codex_metadata(entry):
    if not isinstance(entry, dict) or entry.get("type") != "session_meta":
        return None
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("id")
    thread_source = payload.get("thread_source")
    project = ""
    for key in ("project", "project_name"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            project = value
            break
    if not project:
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            project = Path(cwd.rstrip("/\\")).name
    return (
        session_id if isinstance(session_id, str) and session_id else None,
        thread_source if isinstance(thread_source, str) else None,
        project,
    )


def _safe_filename(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return value or "session"


def _digest_filename(relative_path: str, session_id: str):
    path_hash = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
    path_parts = list(Path(relative_path).with_suffix("").parts)
    readable_name = "__".join(_safe_filename(part) for part in path_parts)
    return f"{readable_name or _safe_filename(session_id)}--{path_hash}.md"


def _atomic_write(path: Path, text: str):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(str(temporary), str(path))
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _signal_timestamp(timestamp: datetime):
    return timestamp.isoformat().replace("+00:00", "Z")


def _write_typed_digest(out_dir: Path, relative_path: str, session_id: str, signals: tuple[Signal, ...]):
    filename = _digest_filename(relative_path, session_id)
    path = out_dir / filename
    if path.exists():
        raise FileExistsError(f"digest output collision: {path}")
    lines = [f"# {session_id}\n", "\n"]
    for signal in signals:
        lines.append(f"- {_signal_timestamp(signal.timestamp)} [{signal.kind.value}] {signal.text}\n")
    _atomic_write(path, "".join(lines))
    return str(path)


def _manifest_json(manifest: DigestManifest):
    return {
        "schema_version": manifest.schema_version,
        "run_id": manifest.run_id,
        "runtime": manifest.runtime.value,
        "scope": {
            "since": _signal_timestamp(_scope_since(manifest.scope)),
            "project_filter": manifest.scope.project_filter,
            "include_subagents": manifest.scope.include_subagents,
        },
        "source_files": [
            {
                "source_path": source.source_path,
                "sha256": source.sha256,
                "scanned": source.scanned,
                "readable": source.readable,
                "json_lines": source.json_lines,
                "malformed_lines": source.malformed_lines,
                "untimestamped_lines": source.untimestamped_lines,
                "in_scope_events": source.in_scope_events,
                "digest_path": source.digest_path,
            }
            for source in manifest.source_files
        ],
        "sessions_scanned": manifest.sessions_scanned,
        "sessions_with_signals": manifest.sessions_with_signals,
        "signal_counts": dict(manifest.signal_counts),
        "complete": manifest.complete,
    }


def _prepare_output(out_dir: Path):
    if out_dir.exists():
        if not out_dir.is_dir():
            raise FileExistsError(f"digest output exists and is not a directory: {out_dir}")
        try:
            next(out_dir.iterdir())
        except StopIteration:
            return
        raise FileExistsError(f"digest output directory must be empty: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=False)


def _scan_typed_source(spec, scope, relative_path, path):
    """Read one source, returning its accounting, signals, and completeness."""
    session_id = path.stem
    metadata_session = None
    metadata_thread_source = None
    metadata_project = ""
    signals = []
    digest = hashlib.sha256()
    json_lines = malformed_lines = untimestamped_lines = in_scope_events = 0
    try:
        with path.open("rb") as stream:
            for source_line, raw_bytes in enumerate(stream, 1):
                digest.update(raw_bytes)
                raw_line = raw_bytes.decode("utf-8", errors="replace").strip()
                if not raw_line:
                    continue
                json_lines += 1
                try:
                    entry = json.loads(raw_line)
                except (json.JSONDecodeError, ValueError, TypeError):
                    malformed_lines += 1
                    continue

                metadata = _codex_metadata(entry) if spec.runtime is Runtime.CODEX else None
                if metadata is not None and metadata_session is None:
                    metadata_session, metadata_thread_source, metadata_project = metadata
                timestamp = _utc_timestamp(entry.get("timestamp")) if isinstance(entry, dict) else None
                if timestamp is None:
                    untimestamped_lines += 1
                    continue
                if timestamp >= _scope_since(scope):
                    in_scope_events += 1
                signals.extend(
                    signals_from_event(
                        raw_line,
                        runtime=spec.runtime,
                        session_id=session_id,
                        source_line=source_line,
                        scope=scope,
                    )
                )
    except OSError:
        source = SourceFile(
            source_path=relative_path,
            sha256="",
            scanned=True,
            readable=False,
            json_lines=json_lines,
            malformed_lines=malformed_lines,
            untimestamped_lines=untimestamped_lines,
            in_scope_events=in_scope_events,
            digest_path=None,
        )
        return source, (), False, (not scope.project_filter or spec.runtime is Runtime.CLAUDE)

    project_match = True
    if spec.runtime is Runtime.CODEX:
        if metadata_session:
            session_id = metadata_session
        project_match = not scope.project_filter or (
            metadata_project and scope.project_filter in metadata_project
        )
        canonical = metadata_session is not None and (
            scope.include_subagents or metadata_thread_source == "user"
        )
        if not project_match or not canonical:
            signals = []

    typed_signals = tuple(
        signal if signal.session_id == session_id else Signal(
            signal.kind, signal.timestamp, session_id, signal.source_line, signal.text
        )
        for signal in signals
    )
    source = SourceFile(
        source_path=relative_path,
        sha256=digest.hexdigest(),
        scanned=True,
        readable=True,
        json_lines=json_lines,
        malformed_lines=malformed_lines,
        untimestamped_lines=untimestamped_lines,
        in_scope_events=in_scope_events,
        digest_path=None,
    )
    return source, typed_signals, malformed_lines == 0, project_match


def run_digest(spec: RuntimeSpec, scope: Scope, out_dir: Path) -> DigestManifest:
    """Scan all candidates, write collision-free digests, and return coverage."""
    out_dir = Path(out_dir).expanduser()
    _prepare_output(out_dir)
    run_id = uuid.uuid4().hex
    source_records = []
    pending = []
    complete = True
    signal_counts = {kind.value: 0 for kind in SignalKind}

    for relative_path, path in _candidate_paths(spec, scope):
        source, signals, source_complete, in_project_scope = _scan_typed_source(
            spec, scope, relative_path, path
        )
        if not in_project_scope:
            continue
        source_records.append(source)
        pending.append((len(source_records) - 1, relative_path, signals))
        complete = complete and source_complete and source.readable

    sessions_with_signals = 0
    for index, relative_path, signals in pending:
        if not signals:
            continue
        sessions_with_signals += 1
        for signal in signals:
            signal_counts[signal.kind.value] += 1
        digest_path = _write_typed_digest(out_dir, relative_path, signals[0].session_id, signals)
        original = source_records[index]
        source_records[index] = SourceFile(
            source_path=original.source_path,
            sha256=original.sha256,
            scanned=original.scanned,
            readable=original.readable,
            json_lines=original.json_lines,
            malformed_lines=original.malformed_lines,
            untimestamped_lines=original.untimestamped_lines,
            in_scope_events=original.in_scope_events,
            digest_path=digest_path,
        )

    manifest = DigestManifest(
        schema_version=1,
        run_id=run_id,
        runtime=spec.runtime,
        scope=scope,
        source_files=tuple(source_records),
        sessions_scanned=len(source_records),
        sessions_with_signals=sessions_with_signals,
        signal_counts=signal_counts,
        complete=complete,
    )
    _atomic_write(
        out_dir / "manifest.json",
        json.dumps(_manifest_json(manifest), sort_keys=True, indent=2) + "\n",
    )
    if not manifest.complete:
        raise IncompleteDigestError(manifest)
    return manifest


# Original Claude-only compatibility command.


def iter_session_files(projects_dir, since_days, project_filter):
    """Yield legacy ``(project, path)`` files filtered by filesystem mtime."""
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
    """Return legacy tuples from one Claude session file."""
    signals = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
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
    with open(fpath, "w", encoding="utf-8") as stream:
        stream.write(f"# {session_basename} ({project})\n\n")
        for kind, timestamp, text in signals:
            stream.write(f"- {timestamp} [{kind}] {text}\n")
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
        for kind, unused_timestamp, unused_text in signals:
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
