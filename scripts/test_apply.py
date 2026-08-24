"""Focused safety and proof checks for the opt-in Apply phase."""
from pathlib import Path

from apply import ApplyScopeError, build_preview, check_scope_overlap, validate_proof
from test_support import make_inventory, make_preview, make_proof, make_request, make_workspace


def test_preview_lists_paths_commands_and_preexisting_dirty_paths():
    request = make_request(
        cluster_id="shell-retry",
        target_paths=(Path(".claude/hooks/retry.sh"),),
        commands=("python3 scripts/check_retry.py",),
        verification_commands=("python3 scripts/test_retry.py",),
    )
    preview = build_preview(
        request,
        inventory=make_inventory(),
        workspace=make_workspace(dirty=(Path("README.md"),)),
    )
    assert preview.disjoint_scope is True
    assert preview.workspace_state.dirty_paths == (Path("README.md"),)
    assert preview.request.commands == ("python3 scripts/check_retry.py",)


def test_preview_rejects_path_outside_project_scope():
    request = make_request(
        cluster_id="shell-retry",
        target_paths=(Path("../other-project/file.py"),),
        commands=("true",),
        verification_commands=("true",),
    )
    try:
        build_preview(request, inventory=make_inventory(), workspace=make_workspace())
    except ApplyScopeError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("out-of-scope Apply must fail")


def test_proof_requires_only_new_in_scope_paths_and_command_output():
    preview = make_preview(target_paths=(Path(".claude/hooks/retry.sh"),))
    proof = make_proof(
        changed_paths=(Path(".claude/hooks/retry.sh"),),
        command_output=("updated hook",),
        verification_output=("1 test passed",),
        passed=True,
    )
    validate_proof(preview, proof)


def test_proof_accepts_nonempty_fixer_output_without_status_marker():
    preview = make_preview()
    proof = make_proof(
        changed_paths=(Path("SKILL.md"),),
        command_output=("changed one file",),
        verification_output=("ok",),
        passed=True,
    )
    validate_proof(preview, proof)


def test_proof_rejects_new_path_outside_scope():
    preview = make_preview(target_paths=(Path(".claude/hooks/retry.sh"),))
    proof = make_proof(
        changed_paths=(Path(".claude/hooks/retry.sh"), Path("AGENTS.md")),
        command_output=("updated",),
        verification_output=("pass",),
        passed=True,
    )
    try:
        validate_proof(preview, proof)
    except ApplyScopeError as exc:
        assert "AGENTS.md" in str(exc)
    else:
        raise AssertionError("unapproved changed path must fail")


def test_two_approved_clusters_with_disjoint_paths_are_independent():
    first = make_preview(cluster_id="first", target_paths=(Path("a.py"),))
    second = make_preview(cluster_id="second", target_paths=(Path("b.py"),))
    assert check_scope_overlap(first, second) is None


def test_overlapping_clusters_cannot_run_in_parallel():
    first = make_preview(cluster_id="first", target_paths=(Path("a.py"),))
    second = make_preview(cluster_id="second", target_paths=(Path("a.py"),))
    assert check_scope_overlap(first, second) == ("first", "second")


def run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    run_all()
