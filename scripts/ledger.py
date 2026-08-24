"""Strict, dependency-free parsing and updates for the clusters YAML ledger."""
from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping


def _frozen_dataclass(cls):
    kwargs = {"frozen": True}
    if sys.version_info >= (3, 10):
        kwargs["slots"] = True
    return dataclass(**kwargs)(cls)


class LedgerStatus(str, Enum):
    NEW = "new"
    FIX_APPLIED = "fix-applied"
    BUILT_NOT_OPERATING = "built-not-operating"
    MONITOR = "monitor"
    WONT_FIX = "wont-fix"
    RESOLVED = "resolved"


@_frozen_dataclass
class LedgerEntry:
    cluster_id: str
    status: LedgerStatus
    wired_check: str


class LedgerParseError(ValueError):
    """Raised when the supported clusters YAML subset is malformed."""


class LedgerTransitionError(ValueError):
    """Raised when a verification cannot justify a ledger transition."""


_ENTRY_START = re.compile(r"^(\s*)-\s+(.*)$")
_PROPERTY = re.compile(r"^  ([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INT = re.compile(r"^[+-]?\d+$")
_REQUIRED_WIRED_CHECK = frozenset(
    {
        LedgerStatus.FIX_APPLIED,
        LedgerStatus.BUILT_NOT_OPERATING,
        LedgerStatus.RESOLVED,
    }
)


@dataclass
class _LedgerBlock:
    start: int
    end: int
    fields: dict[str, tuple[int, Any, str]]
    cluster_id: str


def _source_text(source: str | Path) -> tuple[str, Path | None]:
    if isinstance(source, Path):
        try:
            return source.read_text(encoding="utf-8"), source
        except OSError as exc:
            raise LedgerParseError(f"cannot read ledger {source}: {exc}") from exc
    if not isinstance(source, str):
        raise TypeError("ledger source must be YAML text or a pathlib.Path")
    if "\n" not in source and "\r" not in source:
        candidate = Path(source)
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8"), candidate
        except OSError:
            pass
    return source, None


def _strip_comment(value: str) -> str:
    quoted: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quoted == '"' and char == "\\":
            escaped = True
            continue
        if char in "'\"" and (quoted is not None or index == 0 or value[index - 1].isspace()):
            if quoted is None:
                quoted = char
            elif quoted == char:
                quoted = None
            continue
        if char == "#" and quoted is None and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.rstrip()


def _split_list(value: str) -> list[str]:
    if not (value.startswith("[") and value.endswith("]")):
        raise LedgerParseError("list fields must use bracketed YAML syntax")
    inner = value[1:-1].strip()
    if not inner:
        return []
    result: list[str] = []
    start = 0
    quoted: str | None = None
    escaped = False
    for index, char in enumerate(inner):
        if escaped:
            escaped = False
            continue
        if quoted == '"' and char == "\\":
            escaped = True
            continue
        if char in "'\"" and (quoted is not None or index == start or inner[index - 1].isspace()):
            if quoted is None:
                quoted = char
            elif quoted == char:
                quoted = None
        elif char == "," and quoted is None:
            item = inner[start:index].strip()
            if not item:
                raise LedgerParseError("bracketed string lists cannot contain empty items")
            result.append(_parse_string(item, "list item"))
            start = index + 1
    if quoted is not None:
        raise LedgerParseError("unterminated quote in bracketed string list")
    item = inner[start:].strip()
    if not item:
        raise LedgerParseError("bracketed string lists cannot contain empty items")
    result.append(_parse_string(item, "list item"))
    return result


def _parse_string(value: str, label: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise LedgerParseError(f"{label} has invalid quoted string") from exc
        if not isinstance(parsed, str):
            raise LedgerParseError(f"{label} must be a string")
        return parsed
    if not value:
        return ""
    return value


def _parse_value(raw: str, label: str) -> Any:
    value = _strip_comment(raw).strip()
    if value.startswith("["):
        if not value.endswith("]"):
            raise LedgerParseError(f"{label} has an unterminated list")
        return _split_list(value)
    if value.startswith(("\"", "'")):
        return _parse_string(value, label)
    if _INT.fullmatch(value):
        return int(value)
    if _DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise LedgerParseError(f"{label} has invalid date: {value}") from exc
    return _parse_string(value, label)


def _validate_fields(fields: Mapping[str, tuple[int, Any, str]], entry_index: int) -> LedgerEntry:
    prefix = f"entry {entry_index}"
    if "id" not in fields:
        raise LedgerParseError(f"{prefix} is missing id")
    cluster_id = fields["id"][1]
    if not isinstance(cluster_id, str) or not cluster_id.strip():
        raise LedgerParseError(f"{prefix} id must be a non-empty string")
    if "status" not in fields:
        raise LedgerParseError(f"{prefix} is missing status")
    raw_status = fields["status"][1]
    if not isinstance(raw_status, str):
        raise LedgerParseError(f"{prefix} status must be a string")
    try:
        status = LedgerStatus(raw_status)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in LedgerStatus)
        raise LedgerParseError(f"{prefix} has unknown status {raw_status!r}; expected {allowed}") from exc
    if status in _REQUIRED_WIRED_CHECK:
        if "wired_check" not in fields:
            raise LedgerParseError(f"{prefix} with status {status.value} requires wired_check")
        wired_check = fields["wired_check"][1]
        if not isinstance(wired_check, str) or not wired_check.strip():
            raise LedgerParseError(f"{prefix} with status {status.value} requires non-empty wired_check")
    else:
        wired_check = fields.get("wired_check", (0, "", ""))[1]
        if not isinstance(wired_check, str):
            raise LedgerParseError(f"{prefix} wired_check must be a string")

    scalar_fields = {"id", "title", "status", "fix", "wired_check"}
    for key in scalar_fields & fields.keys():
        if not isinstance(fields[key][1], str):
            raise LedgerParseError(f"{prefix} {key} must be a scalar string")
    for key in ("first_seen", "last_seen"):
        if key in fields and not isinstance(fields[key][1], date):
            raise LedgerParseError(f"{prefix} {key} must be an ISO date")
    if "sessions" in fields and (isinstance(fields["sessions"][1], bool) or not isinstance(fields["sessions"][1], int)):
        raise LedgerParseError(f"{prefix} sessions must be an integer")
    for key in ("projects", "evidence"):
        if key in fields and (
            not isinstance(fields[key][1], list)
            or any(not isinstance(item, str) for item in fields[key][1])
        ):
            raise LedgerParseError(f"{prefix} {key} must be a bracketed string list")
    return LedgerEntry(cluster_id.strip(), status, wired_check)


def _parse_document(text: str) -> tuple[tuple[LedgerEntry, ...], list[_LedgerBlock]]:
    lines = text.splitlines(keepends=True)
    blocks: list[_LedgerBlock] = []
    current: _LedgerBlock | None = None
    for index, line_with_ending in enumerate(lines):
        line = line_with_ending.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        start_match = _ENTRY_START.match(line)
        if start_match:
            if start_match.group(1):
                raise LedgerParseError(f"entry at line {index + 1} must start at column zero")
            if current is not None:
                current.end = index
                blocks.append(current)
            first_field = start_match.group(2)
            property_match = re.fullmatch(r"id\s*:\s*(.*?)\s*", first_field)
            if property_match is None:
                current = _LedgerBlock(index, len(lines), {}, "")
                raise LedgerParseError(f"entry at line {index + 1} must begin with id")
            raw_id = property_match.group(1)
            current = _LedgerBlock(index, len(lines), {}, "")
            current.fields["id"] = (index, _parse_value(raw_id, "id"), raw_id)
            continue
        if current is None:
            raise LedgerParseError(f"unexpected content at line {index + 1}")
        property_match = _PROPERTY.match(line)
        if property_match is None:
            raise LedgerParseError(f"unsupported YAML syntax at line {index + 1}")
        key, raw_value = property_match.groups()
        if key in current.fields:
            raise LedgerParseError(f"entry at line {index + 1} repeats field {key}")
        current.fields[key] = (index, _parse_value(raw_value, key), raw_value)
    if current is not None:
        current.end = len(lines)
        blocks.append(current)

    entries: list[LedgerEntry] = []
    seen: set[str] = set()
    for entry_index, block in enumerate(blocks, start=1):
        entry = _validate_fields(block.fields, entry_index)
        if entry.cluster_id in seen:
            raise LedgerParseError(f"duplicate ledger id: {entry.cluster_id}")
        seen.add(entry.cluster_id)
        block.cluster_id = entry.cluster_id
        entries.append(entry)
    return tuple(entries), blocks


def parse_ledger(source: str | Path) -> tuple[LedgerEntry, ...]:
    """Parse the supported list-of-mappings YAML subset without PyYAML."""
    text, _ = _source_text(source)
    entries, _ = _parse_document(text)
    return entries


def _check_status(check: Any) -> str:
    status = getattr(check, "status", None)
    value = getattr(status, "value", status)
    if isinstance(value, Enum):
        value = value.value
    if value not in {"pass", "fail", "insufficient"}:
        raise LedgerTransitionError(f"verification has unknown check status: {value!r}")
    return value


def _verification_checks(verification: Any) -> tuple[str, str, str]:
    try:
        return (
            _check_status(verification.symptom),
            _check_status(verification.invocation),
            _check_status(verification.outcome),
        )
    except AttributeError as exc:
        raise LedgerTransitionError("verification must provide symptom, invocation, and outcome checks") from exc


def validate_transition(current: LedgerStatus, verification: Any) -> LedgerStatus:
    """Return the only status justified by the three-part verification.

    Insufficient symptom or outcome evidence is an error rather than a silent
    resolution.  A missing invocation is explicitly classified as
    ``built-not-operating`` so the ledger cannot mistake a declared fix for a
    running fix.
    """
    if not isinstance(current, LedgerStatus):
        try:
            current = LedgerStatus(current)
        except (TypeError, ValueError) as exc:
            raise LedgerTransitionError(f"unknown current ledger status: {current!r}") from exc
    symptom, invocation, outcome = _verification_checks(verification)
    if symptom == "insufficient":
        raise LedgerTransitionError("symptom evidence is insufficient")
    if outcome == "insufficient":
        raise LedgerTransitionError("outcome evidence is insufficient")

    operating_failure = symptom == "fail" or invocation != "pass" or outcome == "fail"
    if current in {LedgerStatus.FIX_APPLIED, LedgerStatus.BUILT_NOT_OPERATING}:
        if operating_failure:
            return LedgerStatus.BUILT_NOT_OPERATING
        return LedgerStatus.RESOLVED
    if current is LedgerStatus.RESOLVED:
        return LedgerStatus.BUILT_NOT_OPERATING if symptom == "fail" else LedgerStatus.RESOLVED
    return current


def _normalize_cluster_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().casefold()).strip("-")


def _finding_cluster_key(finding: Any) -> str | None:
    value = getattr(finding, "cluster_key", None)
    return value if isinstance(value, str) and value.strip() else None


def _finding_session_id(finding: Any) -> str | None:
    value = getattr(finding, "session_id", None)
    return value if isinstance(value, str) and value.strip() else None


def _finding_dates(findings: Iterable[Any]) -> tuple[date | None, date | None]:
    dates: list[date] = []
    for finding in findings:
        for evidence in getattr(finding, "evidence", ()):
            timestamp = getattr(evidence, "timestamp", None)
            if isinstance(timestamp, datetime):
                dates.append(timestamp.astimezone(timezone.utc).date() if timestamp.tzinfo else timestamp.date())
            elif isinstance(timestamp, date):
                dates.append(timestamp)
    if not dates:
        return None, None
    return min(dates), max(dates)


def _format_scalar(value: Any) -> str:
    if isinstance(value, LedgerStatus):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        if value and re.fullmatch(r"[A-Za-z0-9_./:@+\-]+", value):
            return value
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _format_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_format_scalar(item) for item in value) + "]"
    return _format_scalar(value)


def _replace_field(lines: list[str], block: _LedgerBlock, key: str, value: Any) -> None:
    rendered = _format_value(value)
    field = block.fields.get(key)
    if field is not None:
        line_index = field[0]
        original = lines[line_index]
        ending = "\n" if original.endswith("\n") else ""
        if original.endswith("\r\n"):
            ending = "\r\n"
        raw_line = original[: -len(ending)] if ending else original
        prefix_match = re.match(rf"^(\s*{re.escape(key)}\s*:\s*)(.*)$", raw_line)
        suffix = ""
        if prefix_match is not None:
            raw_value = prefix_match.group(2)
            suffix = raw_value[len(_strip_comment(raw_value)) :]
        lines[line_index] = f"  {key}: {rendered}{suffix}{ending}"
        block.fields[key] = (line_index, value, rendered)
        return
    insert_at = block.end
    ending = "\n"
    if lines and lines[-1].endswith("\r\n"):
        ending = "\r\n"
    lines.insert(insert_at, f"  {key}: {rendered}{ending}")
    block.end += 1
    block.fields[key] = (insert_at, value, rendered)


def _status_update(current: LedgerStatus, update: Any) -> tuple[LedgerStatus | None, str | None]:
    if isinstance(update, LedgerEntry):
        return update.status, update.wired_check or None
    if isinstance(update, LedgerStatus):
        return update, None
    if isinstance(update, str):
        try:
            return LedgerStatus(update), None
        except ValueError as exc:
            raise LedgerTransitionError(f"unknown requested ledger status: {update!r}") from exc
    if hasattr(update, "symptom") and hasattr(update, "invocation") and hasattr(update, "outcome"):
        return validate_transition(current, update), None
    if isinstance(update, tuple) and len(update) == 2:
        requested, verification = update
        if isinstance(requested, (LedgerStatus, str)):
            target, wired = _status_update(current, requested)
            if verification is not None:
                target = validate_transition(current, verification)
            return target, wired
    raise LedgerTransitionError(f"unsupported ledger update for {current.value}")


def _group_findings(findings: Iterable[Any]) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    for finding in findings:
        cluster_key = _finding_cluster_key(finding)
        if cluster_key:
            grouped.setdefault(cluster_key, []).append(finding)
    return grouped


def update_ledger(
    source: str | Path,
    touched: Mapping[str, Any] | Iterable[LedgerEntry],
    *,
    merged_findings: Iterable[Any] = (),
    run_date: date | datetime | str | None = None,
) -> str:
    """Update only touched ledger entries and return their serialized YAML.

    ``source`` may be YAML text or a path.  Path inputs are written in place
    after validation; text inputs are returned without filesystem effects.
    Findings are optional typed miner results used to advance session and
    date fields for touched entries.  Existing comments and unrelated fields
    remain byte-for-byte unchanged except for fields that are intentionally
    bumped.
    """
    text, path = _source_text(source)
    entries, blocks = _parse_document(text)
    if isinstance(touched, Mapping):
        updates = dict(touched)
    else:
        updates = {entry.cluster_id: entry for entry in touched}
    by_id = {entry.cluster_id: entry for entry in entries}
    unknown = sorted(set(updates) - set(by_id))
    if unknown:
        raise LedgerTransitionError("unknown ledger ids: " + ", ".join(unknown))

    grouped = _group_findings(merged_findings)
    explicit_date: date | None = None
    if run_date is not None:
        if isinstance(run_date, datetime):
            explicit_date = (run_date.astimezone(timezone.utc) if run_date.tzinfo else run_date).date()
        elif isinstance(run_date, date):
            explicit_date = run_date
        elif isinstance(run_date, str) and _DATE.fullmatch(run_date):
            explicit_date = date.fromisoformat(run_date)
        else:
            raise LedgerTransitionError("run_date must be an ISO date or datetime")

    lines = text.splitlines(keepends=True)
    block_by_id = {block.cluster_id: block for block in blocks}
    # Process blocks from the end so inserting a missing field cannot shift an
    # index used by an earlier touched block.
    for cluster_id in reversed(list(updates)):
        block = block_by_id[cluster_id]
        current = by_id[cluster_id]
        target_status, target_wired_check = _status_update(current.status, updates[cluster_id])
        if target_status is not None and target_status is not current.status:
            _replace_field(lines, block, "status", target_status)
            current = LedgerEntry(current.cluster_id, target_status, current.wired_check)
        if target_wired_check:
            _replace_field(lines, block, "wired_check", target_wired_check)

        findings = grouped.get(cluster_id, ())
        if not findings:
            # Cluster keys are normalized by miners; permit a normalized match
            # when a human ledger id uses a different punctuation style.
            findings = tuple(
                finding
                for key, values in grouped.items()
                if _normalize_cluster_key(key) == _normalize_cluster_key(cluster_id)
                for finding in values
            )
        if findings:
            existing_sessions = block.fields.get("evidence", (0, [], ""))[1]
            if not isinstance(existing_sessions, list):
                existing_sessions = []
            session_ids = [session for session in (_finding_session_id(item) for item in findings) if session]
            new_sessions = list(dict.fromkeys(session for session in session_ids if session not in existing_sessions))
            if new_sessions or "evidence" not in block.fields:
                _replace_field(lines, block, "evidence", existing_sessions + new_sessions)
            if new_sessions or "sessions" in block.fields:
                old_count = block.fields.get("sessions", (0, 0, ""))[1]
                if not isinstance(old_count, int):
                    raise LedgerParseError(f"ledger entry {cluster_id} sessions must be an integer")
                _replace_field(lines, block, "sessions", old_count + len(new_sessions))
            first_seen, last_seen = _finding_dates(findings)
            if first_seen is not None and "first_seen" not in block.fields:
                _replace_field(lines, block, "first_seen", first_seen)
            candidate_last_seen = explicit_date or last_seen
            if candidate_last_seen is not None:
                previous_last_seen = block.fields.get("last_seen", (0, None, ""))[1]
                if not isinstance(previous_last_seen, date) or candidate_last_seen > previous_last_seen:
                    _replace_field(lines, block, "last_seen", candidate_last_seen)

    updated = "".join(lines)
    # Reparse the resulting text before writing so an update never leaves an
    # invalid ledger behind (especially for required wired_check fields).
    _parse_document(updated)
    if path is not None:
        try:
            path.write_text(updated, encoding="utf-8")
        except OSError as exc:
            raise LedgerParseError(f"cannot write ledger {path}: {exc}") from exc
    return updated


__all__ = [
    "LedgerEntry",
    "LedgerParseError",
    "LedgerStatus",
    "LedgerTransitionError",
    "parse_ledger",
    "update_ledger",
    "validate_transition",
]
