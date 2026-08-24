"""Safety boundaries for the opt-in, per-cluster Apply phase."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from coverage_model import Inventory, InventoryItem


def _frozen_dataclass(cls):
    kwargs = {"frozen": True}
    if sys.version_info >= (3, 10):
        kwargs["slots"] = True
    return dataclass(**kwargs)(cls)


@_frozen_dataclass
class WorkspaceState:
    project_root: Path
    dirty_paths: tuple[Path, ...]
    conflicted: bool


@_frozen_dataclass
class ApplyRequest:
    cluster_id: str
    target_paths: tuple[Path, ...]
    commands: tuple[str, ...]
    verification_commands: tuple[str, ...]


@_frozen_dataclass
class ApplyPreview:
    request: ApplyRequest
    workspace_state: WorkspaceState
    disjoint_scope: bool
    expected_diff: tuple[Path, ...]


@_frozen_dataclass
class FixProof:
    cluster_id: str
    changed_paths: tuple[Path, ...]
    command_output: tuple[str, ...]
    verification_output: tuple[str, ...]
    passed: bool


class ApplyScopeError(ValueError):
    """Raised when an Apply request or proof escapes its approved scope."""


class ApplyProofError(ValueError):
    """Raised when a fixer cannot prove a successful, bounded Apply."""


_FAILURE_LINE = re.compile(
    r"(?:\b(?:failed|failure|error|traceback)\b|"
    r"\b(?:exit|status|code)\s*[:=]?\s*[1-9]\d*\b|"
    r"\b(?:non[- ]?zero|unsuccessful)\b)",
    re.IGNORECASE,
)
_SUCCESS_LINE = re.compile(
    r"(?:\bpass(?:ed)?\b|\bok\b|\bsuccess(?:ful|fully)?\b|"
    r"\b(?:exit|status|code)\s*[:=]?\s*0\b|"
    r"\b(?:updated|created|applied|fixed|completed)\b)",
    re.IGNORECASE,
)


def _root(workspace: WorkspaceState) -> Path:
    if not isinstance(workspace, WorkspaceState):
        raise TypeError("workspace must be a WorkspaceState")
    if not isinstance(workspace.project_root, Path):
        raise TypeError("workspace project_root must be a pathlib.Path")
    try:
        return workspace.project_root.expanduser().resolve(strict=False)
    except OSError as exc:
        raise ApplyScopeError(f"cannot resolve project root {workspace.project_root}") from exc


def _relative_path(path: Path, root: Path, *, label: str) -> Path:
    if not isinstance(path, Path):
        raise TypeError(f"{label} must contain pathlib.Path values")
    if path.is_absolute():
        raise ApplyScopeError(f"path {path} is outside the selected project scope")
    try:
        resolved = (root / path).resolve(strict=False)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ApplyScopeError(f"path {path} is outside the selected project scope") from exc
    if relative == Path("."):
        raise ApplyScopeError(f"path {path} resolves to the project root")
    return relative


def _paths(values: Iterable[Path], root: Path, *, label: str) -> tuple[Path, ...]:
    normalized = tuple(_relative_path(path, root, label=label) for path in values)
    if len(set(normalized)) != len(normalized):
        raise ApplyScopeError(f"{label} contains duplicate paths")
    return normalized


def _nonempty_strings(values: Iterable[str], *, label: str) -> tuple[str, ...]:
    result = tuple(values)
    if any(not isinstance(value, str) or not value.strip() for value in result):
        raise ValueError(f"{label} must contain non-empty strings")
    return result


def _validate_inventory(inventory: Inventory) -> None:
    if not isinstance(inventory, Inventory):
        raise TypeError("inventory must be an Inventory")
    for item in inventory.items:
        if not isinstance(item, InventoryItem):
            raise TypeError("inventory items must be InventoryItem values")


def _validated_request(request: ApplyRequest, root: Path) -> ApplyRequest:
    if not isinstance(request, ApplyRequest):
        raise TypeError("request must be an ApplyRequest")
    if not isinstance(request.cluster_id, str) or not request.cluster_id.strip():
        raise ValueError("request cluster_id must be non-empty")
    target_paths = _paths(request.target_paths, root, label="target_paths")
    if not target_paths:
        raise ApplyScopeError("Apply request must declare at least one target path")
    commands = _nonempty_strings(request.commands, label="commands")
    verification_commands = _nonempty_strings(
        request.verification_commands,
        label="verification_commands",
    )
    if not commands:
        raise ApplyProofError("Apply request must declare at least one command")
    if not verification_commands:
        raise ApplyProofError("Apply request must declare at least one verification command")
    return ApplyRequest(request.cluster_id.strip(), target_paths, commands, verification_commands)


def build_preview(
    request: ApplyRequest,
    *,
    inventory: Inventory,
    workspace: WorkspaceState,
) -> ApplyPreview:
    """Create one immutable, project-relative preview for a selected cluster."""
    _validate_inventory(inventory)
    root = _root(workspace)
    if not isinstance(workspace.conflicted, bool):
        raise TypeError("workspace conflicted must be a bool")
    if workspace.conflicted:
        raise ApplyScopeError("workspace has unresolved conflicts")
    dirty_paths = _paths(workspace.dirty_paths, root, label="dirty_paths")
    normalized_request = _validated_request(request, root)
    normalized_workspace = WorkspaceState(workspace.project_root, dirty_paths, workspace.conflicted)
    return ApplyPreview(
        normalized_request,
        normalized_workspace,
        True,
        normalized_request.target_paths,
    )


def _preview_paths(preview: ApplyPreview) -> tuple[Path, ...]:
    if not isinstance(preview, ApplyPreview):
        raise TypeError("preview must be an ApplyPreview")
    root = _root(preview.workspace_state)
    return _paths(preview.request.target_paths, root, label="preview target_paths")


def check_scope_overlap(first: ApplyPreview, second: ApplyPreview) -> tuple[str, str] | None:
    """Return both cluster ids when two approved previews share a real path."""
    first_paths = _preview_paths(first)
    second_paths = _preview_paths(second)
    first_root = _root(first.workspace_state)
    second_root = _root(second.workspace_state)
    first_absolute = {first_root / path for path in first_paths}
    second_absolute = {second_root / path for path in second_paths}
    if first_absolute.intersection(second_absolute):
        return (first.request.cluster_id, second.request.cluster_id)
    return None


def _nonempty_output(values: Iterable[str], *, label: str) -> tuple[str, ...]:
    result = tuple(values)
    if not result or any(not isinstance(value, str) or not value.strip() for value in result):
        raise ApplyProofError(f"{label} must contain non-empty output")
    return result


def _successful_lines(output: tuple[str, ...]) -> tuple[str, ...]:
    lines = tuple(line.strip() for item in output for line in item.splitlines() if line.strip())
    if any(_FAILURE_LINE.search(line) for line in lines):
        raise ApplyProofError("fixer output contains a failure")
    return tuple(line for line in lines if _SUCCESS_LINE.search(line))


def validate_proof(preview: ApplyPreview, proof: FixProof) -> None:
    """Fail closed unless a selected cluster proves a bounded successful Apply."""
    if not isinstance(preview, ApplyPreview):
        raise TypeError("preview must be an ApplyPreview")
    if not isinstance(proof, FixProof):
        raise TypeError("proof must be a FixProof")
    if proof.cluster_id != preview.request.cluster_id:
        raise ApplyProofError("proof cluster_id does not match the approved preview")
    if proof.passed is not True:
        raise ApplyProofError("fix proof must report passed=True")
    command_output = _nonempty_output(proof.command_output, label="command_output")
    verification_output = _nonempty_output(
        proof.verification_output,
        label="verification_output",
    )
    _successful_lines(command_output)
    verification_lines = _successful_lines(verification_output)
    required = len(preview.request.verification_commands)
    if len(verification_lines) < required:
        raise ApplyProofError("verification_output lacks a successful line for every command")

    root = _root(preview.workspace_state)
    changed_paths = _paths(proof.changed_paths, root, label="changed_paths")
    baseline = set(_paths(preview.workspace_state.dirty_paths, root, label="dirty_paths"))
    approved = set(_preview_paths(preview))
    new_paths = set(changed_paths) - baseline
    outside = sorted(new_paths - approved, key=lambda path: path.as_posix())
    if outside:
        raise ApplyScopeError(f"changed path {outside[0]} is outside approved Apply scope")


__all__ = [
    "ApplyProofError",
    "ApplyPreview",
    "ApplyRequest",
    "ApplyScopeError",
    "FixProof",
    "WorkspaceState",
    "build_preview",
    "check_scope_overlap",
    "validate_proof",
]
