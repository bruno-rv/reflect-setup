#!/usr/bin/env python3
"""Runtime adapters and safe source installation for reflect-setup."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal, Mapping


def _frozen_dataclass(cls):
    kwargs = {"frozen": True}
    if sys.version_info >= (3, 10):
        kwargs["slots"] = True
    return dataclass(**kwargs)(cls)


class Runtime(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"


@_frozen_dataclass
class Scope:
    since: datetime
    project_filter: str | None
    include_subagents: bool


@_frozen_dataclass
class RuntimeSpec:
    runtime: Runtime
    home: Path
    source_root: Path
    skill_root: Path
    inventory_roots: tuple[Path, ...]


@_frozen_dataclass
class SessionRef:
    runtime: Runtime
    source_path: Path
    relative_path: str
    session_id: str
    project: str


@_frozen_dataclass
class InstallResult:
    runtime: Runtime
    mode: Literal["symlink", "copy"]
    target: Path
    source_hash: str


@_frozen_dataclass
class _SourceFile:
    relative: str
    path: Path
    sha256: str
    size: int
    mtime_ns: int


@_frozen_dataclass
class _SourceSnapshot:
    files: tuple[_SourceFile, ...]
    source_hash: str

    def __iter__(self):
        return iter(self.files)


def _runtime_from_name(name: str) -> Runtime:
    try:
        return Runtime(name.lower())
    except (AttributeError, ValueError) as exc:
        raise ValueError("runtime must be one of: claude, codex, auto") from exc


def _default_source_root(runtime: Runtime, home: Path, env: Mapping[str, str]) -> Path:
    key = "CLAUDE_PROJECTS_DIR" if runtime is Runtime.CLAUDE else "CODEX_SESSIONS_DIR"
    configured = env.get(key)
    if configured:
        return Path(configured).expanduser()
    if runtime is Runtime.CLAUDE:
        return home / ".claude" / "projects"
    return home / ".codex" / "sessions"


def _runtime_spec(runtime: Runtime, home: Path, env: Mapping[str, str], source_root: Path | None) -> RuntimeSpec:
    root = source_root if source_root is not None else _default_source_root(runtime, home, env)
    if runtime is Runtime.CLAUDE:
        base = home / ".claude"
        skill_root = base / "skills" / "reflect-setup"
        inventory_roots = (
            base / "skills",
            base / "commands",
            base / "agents",
            base / "hooks",
            base / "settings.json",
            base / "settings.local.json",
            Path(".claude/skills"),
            Path(".claude/commands"),
            Path(".claude/agents"),
            Path(".claude/hooks"),
            Path(".claude/settings.json"),
            Path(".claude/settings.local.json"),
        )
    else:
        base = home / ".codex"
        skill_root = base / "skills" / "reflect-setup"
        inventory_roots = (
            base / "skills",
            base / "agents",
            base / "config.toml",
            Path(".codex/skills"),
            Path(".codex/agents"),
        )
    return RuntimeSpec(runtime, home, Path(root), skill_root, inventory_roots)


def resolve_runtime(
    name: str | None,
    *,
    home: Path,
    env: Mapping[str, str],
    source_root: Path | None = None,
) -> RuntimeSpec:
    """Resolve a runtime without creating or modifying any filesystem path."""
    home = Path(home).expanduser()
    if name is not None and name.lower() != "auto":
        runtime = _runtime_from_name(name)
        return _runtime_spec(runtime, home, env, source_root)

    candidates = []
    for runtime in (Runtime.CLAUDE, Runtime.CODEX):
        root = _default_source_root(runtime, home, env)
        if root.is_dir() and os.access(root, os.R_OK):
            candidates.append(runtime)
    if len(candidates) == 1:
        return _runtime_spec(candidates[0], home, env, source_root)
    if len(candidates) > 1:
        raise RuntimeError(
            "auto runtime selection is ambiguous; use --runtime claude or --runtime codex"
        )
    raise RuntimeError(
        "auto runtime selection found no readable session root; use --runtime claude or --runtime codex"
    )


def _relative_path(source_root: Path, source_path: Path) -> str:
    return source_path.relative_to(source_root).as_posix()


def _project_filter_matches(project: str, scope: Scope) -> bool:
    return not scope.project_filter or scope.project_filter in project


def subagent_path_excluded(relative_path: str, include_subagents: bool) -> bool:
    """Return whether a path-component marks an intentionally excluded subagent."""
    return not include_subagents and "subagents" in Path(relative_path).parts


def _skip_subagent_path(relative_path: str, scope: Scope) -> bool:
    parts = Path(relative_path).parts
    return "memory" in parts or subagent_path_excluded(relative_path, scope.include_subagents)


def _read_codex_metadata(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for raw_line in stream:
                try:
                    entry = json.loads(raw_line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(entry, dict) and entry.get("type") == "session_meta":
                    return entry
    except OSError:
        return None
    return None


def codex_project(payload: Mapping[str, object], source_context: str = "") -> str:
    """Return Codex project metadata or a stable source-context identity."""
    for key in ("project", "project_name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        project = Path(cwd.strip().rstrip("/\\")).name
        if project:
            return project
    context = source_context.strip().replace("\\", "/")
    if context:
        return f"codex:{context}"
    return "codex:unknown"


def discover_sessions(spec: RuntimeSpec, scope: Scope) -> tuple[SessionRef, ...]:
    """Return sorted session references in the selected runtime's canonical scope."""
    if not spec.source_root.is_dir():
        return ()

    sessions: list[SessionRef] = []
    try:
        paths = sorted(spec.source_root.rglob("*.jsonl"))
    except OSError:
        return ()
    for path in paths:
        if not path.is_file():
            continue
        relative_path = _relative_path(spec.source_root, path)
        if _skip_subagent_path(relative_path, scope):
            continue

        if spec.runtime is Runtime.CLAUDE:
            parts = Path(relative_path).parts
            project = parts[0] if len(parts) > 1 else ""
            session_id = path.stem
        else:
            metadata = _read_codex_metadata(path)
            if not metadata:
                continue
            payload = metadata.get("payload")
            if not isinstance(payload, dict):
                continue
            thread_source = payload.get("thread_source")
            if not scope.include_subagents and thread_source != "user":
                continue
            session_id = payload.get("id")
            if not isinstance(session_id, str) or not session_id:
                continue
            project = codex_project(payload, relative_path)

        if _project_filter_matches(project, scope):
            sessions.append(SessionRef(spec.runtime, path, relative_path, session_id, project))

    return tuple(sorted(sessions, key=lambda item: (item.runtime.value, item.project, item.relative_path)))


def _stream_file_hash(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _source_files(source_root: Path) -> _SourceSnapshot:
    files: list[_SourceFile] = []
    aggregate = hashlib.sha256()
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source_root).as_posix()
        before = path.stat()
        aggregate.update(relative.encode("utf-8"))
        aggregate.update(b"\0")
        file_digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                file_digest.update(chunk)
                aggregate.update(chunk)
                size += len(chunk)
        aggregate.update(b"\0")
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"source changed while scanning: {path}")
        files.append(_SourceFile(relative, path, file_digest.hexdigest(), size, after.st_mtime_ns))
    return _SourceSnapshot(tuple(files), aggregate.hexdigest())


def _source_hash(files: _SourceSnapshot) -> str:
    return files.source_hash


def _source_commit(source_root: Path) -> str | None:
    git_marker = source_root / ".git"
    if git_marker.is_dir():
        git_dir = git_marker
    elif git_marker.is_file():
        try:
            marker = git_marker.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not marker.startswith("gitdir:"):
            return None
        git_dir = Path(marker.split(":", 1)[1].strip())
        if not git_dir.is_absolute():
            git_dir = source_root / git_dir
    else:
        return None

    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head:
        return None
    if not head.startswith("ref: "):
        return head
    ref = head[5:].strip()
    ref_dirs = [git_dir]
    commondir_marker = git_dir / "commondir"
    if commondir_marker.is_file():
        try:
            common_dir = Path(commondir_marker.read_text(encoding="utf-8").strip())
        except OSError:
            common_dir = git_dir
        if not common_dir.is_absolute():
            common_dir = git_dir / common_dir
        if common_dir != git_dir:
            ref_dirs.append(common_dir)
    for ref_dir in ref_dirs:
        try:
            return (ref_dir / ref).read_text(encoding="utf-8").strip() or None
        except OSError:
            try:
                packed = (ref_dir / "packed-refs").read_text(encoding="utf-8")
            except OSError:
                continue
            for line in packed.splitlines():
                if line and not line.startswith("#") and not line.startswith("^"):
                    parts = line.split(" ", 1)
                    if len(parts) == 2 and parts[1] == ref:
                        return parts[0]
    return None


def _manifest(source_root: Path, spec: RuntimeSpec, source_hash: str, files: _SourceSnapshot) -> dict[str, object]:
    return {
        "source_commit": _source_commit(source_root),
        "source_path": str(source_root.resolve()),
        "runtime": spec.runtime.value,
        "source_hash": source_hash,
        "files": {
            file.relative: file.sha256
            for file in files
        },
    }


def _existing_manifest(target: Path) -> dict[str, object] | None:
    path = target / ".reflect-setup-source.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _verify_installed_copy(
    target: Path,
    snapshot: _SourceSnapshot,
    installed: Mapping[str, object],
) -> None:
    """Rehash every managed file before accepting copy-install idempotence."""
    expected = {file.relative: file.sha256 for file in snapshot}
    manifest_files = installed.get("files")
    if not isinstance(manifest_files, dict) or manifest_files != expected:
        raise _collision(target, "managed copy manifest file hashes do not match source")

    manifest_path = target / ".reflect-setup-source.json"
    actual: dict[str, str] = {}
    for path in target.rglob("*"):
        if path == manifest_path:
            continue
        relative = path.relative_to(target).as_posix()
        if path.is_file() or path.is_symlink():
            if path.is_symlink() and not path.is_file():
                actual[relative] = "<directory symlink>"
            else:
                try:
                    actual[relative] = _stream_file_hash(path)[0]
                except OSError as exc:
                    raise _collision(target, f"cannot hash managed file {relative}") from exc
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatched = sorted(
        relative
        for relative in set(expected).intersection(actual)
        if actual[relative] != expected[relative]
    )
    if missing or extra or mismatched:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        if mismatched:
            details.append("tampered=" + ",".join(mismatched))
        raise _collision(target, "managed copy files changed (" + "; ".join(details) + ")")


def _collision(target: Path, detail: str) -> FileExistsError:
    return FileExistsError(f"cannot install skill at {target}: {detail}")


def _verify_snapshot_copy(source_root: Path, staging: Path, snapshot: _SourceSnapshot) -> None:
    current = _source_files(source_root)
    if current.source_hash != snapshot.source_hash or current.files != snapshot.files:
        raise RuntimeError("source changed during copy")
    for file in snapshot:
        copied = staging / file.relative
        if not copied.is_file():
            raise RuntimeError(f"copied source diverged from manifest: {file.relative}")
        copied_hash, copied_size = _stream_file_hash(copied)
        if (copied_hash, copied_size) != (file.sha256, file.size):
            raise RuntimeError(f"copied source diverged from manifest: {file.relative}")


def _copy_tree(source_root: Path, staging: Path, snapshot: _SourceSnapshot) -> None:
    shutil.copytree(source_root, staging, symlinks=True, dirs_exist_ok=True)
    _verify_snapshot_copy(source_root, staging, snapshot)


def install_skill(
    spec: RuntimeSpec,
    source_root: Path,
    *,
    mode: Literal["symlink", "copy"] = "symlink",
) -> InstallResult:
    """Install the source tree under the selected runtime's skill root."""
    if mode not in ("symlink", "copy"):
        raise ValueError("mode must be one of: symlink, copy")
    source_root = Path(source_root).expanduser()
    if not source_root.is_dir():
        raise ValueError(f"source must be a directory: {source_root}")
    files = _source_files(source_root)
    source_hash = _source_hash(files)
    target = spec.skill_root

    if target.is_symlink():
        resolved_target = target.resolve(strict=False)
        if mode == "symlink" and resolved_target == source_root.resolve(strict=False):
            return InstallResult(spec.runtime, mode, target, source_hash)
        raise _collision(target, "source/installed mismatch (different symlink)")
    if target.exists():
        if mode == "copy":
            if not target.is_dir():
                raise _collision(target, "existing file")
            installed = _existing_manifest(target)
            if installed is not None and installed.get("source_hash") == source_hash:
                _verify_installed_copy(target, files, installed)
                return InstallResult(spec.runtime, mode, target, source_hash)
            if installed is not None:
                raise _collision(target, "manifest hash mismatch")
            entries = ", ".join(sorted(path.name for path in target.iterdir()))
            raise _collision(target, f"unmanaged directory ({entries or 'no manifest'})")
        if target.is_dir():
            entries = ", ".join(sorted(path.name for path in target.iterdir()))
            detail = f"unmanaged directory ({entries or 'no manifest'})"
        else:
            detail = "existing path is not the requested source symlink"
        raise _collision(target, detail)

    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == "symlink":
        target.symlink_to(source_root.resolve(), target_is_directory=True)
        return InstallResult(spec.runtime, mode, target, source_hash)

    manifest = _manifest(source_root, spec, source_hash, files)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=str(target.parent)))
    try:
        _copy_tree(source_root, staging, files)
        manifest_path = staging / ".reflect-setup-source.json"
        temporary_manifest = staging / ".reflect-setup-source.json.tmp"
        temporary_manifest.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary_manifest, manifest_path)
        os.rename(staging, target)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return InstallResult(spec.runtime, mode, target, source_hash)


def install_result_dict(result: InstallResult) -> dict[str, object]:
    """Return a JSON-friendly representation used by the CLI."""
    data = asdict(result)
    data["runtime"] = result.runtime.value
    data["target"] = str(result.target)
    return data
