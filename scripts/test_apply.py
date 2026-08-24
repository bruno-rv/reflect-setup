"""Focused safety and proof checks for the opt-in Apply phase."""
from pathlib import Path
from tempfile import TemporaryDirectory

from apply import ApplyProofError, ApplyRequest, ApplyScopeError, FixProof, WorkspaceState, build_preview, check_scope_overlap, validate_proof
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
        verification_output=("true :: PASS",),
        passed=True,
    )
    validate_proof(preview, proof)


def test_proof_accepts_nonempty_fixer_output_without_status_marker():
    preview = make_preview()
    proof = make_proof(
        changed_paths=(Path("SKILL.md"),),
        command_output=("0 failures", "error handling updated"),
        verification_output=("true :: PASS",),
        passed=True,
    )
    validate_proof(preview, proof)


def test_proof_accepts_descendant_only_for_directory_target_established_at_preview():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "hooks").mkdir()
        request = ApplyRequest("fixture", (Path("hooks"),), ("true",), ("true",))
        preview = build_preview(
            request,
            inventory=make_inventory(),
            workspace=WorkspaceState(root, (), False),
        )
        proof = FixProof(
            "fixture",
            (Path("hooks/retry.sh"),),
            ("updated retry",),
            ("true :: PASS",),
            True,
        )
        validate_proof(preview, proof)


def test_proof_rejects_file_target_siblings_and_descendants():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "SKILL.md").write_text("fixture")
        request = ApplyRequest("fixture", (Path("SKILL.md"),), ("true",), ("true",))
        preview = build_preview(
            request,
            inventory=make_inventory(),
            workspace=WorkspaceState(root, (), False),
        )
        for changed in (Path("SKILL.md.bak"), Path("SKILL.md/child")):
            proof = FixProof(
                "fixture",
                (changed,),
                ("updated",),
                ("true :: PASS",),
                True,
            )
            try:
                validate_proof(preview, proof)
            except ApplyScopeError:
                pass
            else:
                raise AssertionError("file target boundaries must reject sibling and descendant paths")


def test_preview_rejects_symlinked_target_even_when_link_resolves_inside_root():
    with TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "real").mkdir()
        (root / "link").symlink_to(root / "real", target_is_directory=True)
        request = ApplyRequest("fixture", (Path("link"),), ("true",), ("true",))
        try:
            build_preview(
                request,
                inventory=make_inventory(),
                workspace=WorkspaceState(root, (), False),
            )
        except ApplyScopeError as exc:
            assert "symlink" in str(exc)
        else:
            raise AssertionError("symlinked Apply targets must fail closed")


def test_proof_rejects_new_path_outside_scope():
    preview = make_preview(target_paths=(Path(".claude/hooks/retry.sh"),))
    proof = make_proof(
        changed_paths=(Path(".claude/hooks/retry.sh"), Path("AGENTS.md")),
        command_output=("updated",),
        verification_output=("true :: PASS",),
        passed=True,
    )
    try:
        validate_proof(preview, proof)
    except ApplyScopeError as exc:
        assert "AGENTS.md" in str(exc)
    else:
        raise AssertionError("unapproved changed path must fail")


def test_proof_rejects_not_ok_even_when_generic_ok_is_present():
    preview = make_preview()
    proof = make_proof(
        changed_paths=(Path("SKILL.md"),),
        command_output=("ok\nnot ok",),
        verification_output=("true :: PASS",),
        passed=True,
    )
    try:
        validate_proof(preview, proof)
    except ApplyProofError as exc:
        assert "failure" in str(exc)
    else:
        raise AssertionError("explicit not ok status must fail closed")


def test_proof_rejects_explicit_false_and_no_statuses():
    preview = make_preview()
    for status in (
        "status: false",
        "status: no",
        "result: false",
        "outcome: no",
        "ok: false",
        "pass: no",
        "success: 0",
        "status: 1",
    ):
        proof = make_proof(
            changed_paths=(Path("SKILL.md"),),
            command_output=(status,),
            verification_output=("true :: PASS",),
            passed=True,
        )
        try:
            validate_proof(preview, proof)
        except ApplyProofError as exc:
            assert "failure" in str(exc)
        else:
            raise AssertionError(f"explicit negative status must fail: {status}")


def test_proof_accepts_zero_status_result_and_outcome_with_exact_pass():
    preview = make_preview()
    for status in ("status: 0", "result: 0", "outcome: 0"):
        proof = make_proof(
            changed_paths=(Path("SKILL.md"),),
            command_output=(status,),
            verification_output=("true :: PASS",),
            passed=True,
        )
        validate_proof(preview, proof)


def test_proof_rejects_traceback_header():
    preview = make_preview()
    proof = make_proof(
        changed_paths=(Path("SKILL.md"),),
        command_output=("Traceback (most recent call last):",),
        verification_output=("true :: PASS",),
        passed=True,
    )
    try:
        validate_proof(preview, proof)
    except ApplyProofError as exc:
        assert "failure" in str(exc)
    else:
        raise AssertionError("traceback header must fail closed")


def test_proof_rejects_failed_test_status():
    preview = make_preview()
    proof = make_proof(
        changed_paths=(Path("SKILL.md"),),
        command_output=("FAILED test_apply.py::test_status",),
        verification_output=("true :: PASS",),
        passed=True,
    )
    try:
        validate_proof(preview, proof)
    except ApplyProofError as exc:
        assert "failure" in str(exc)
    else:
        raise AssertionError("failed test status must fail closed")


def test_proof_rejects_nonzero_failed_test_count():
    preview = make_preview()
    proof = make_proof(
        changed_paths=(Path("SKILL.md"),),
        command_output=("tests failed: 1",),
        verification_output=("true :: PASS",),
        passed=True,
    )
    try:
        validate_proof(preview, proof)
    except ApplyProofError as exc:
        assert "failure" in str(exc)
    else:
        raise AssertionError("nonzero failed test count must fail closed")


def test_proof_rejects_explicit_failed_verification_status():
    preview = make_preview()
    proof = make_proof(
        changed_paths=(Path("SKILL.md"),),
        command_output=("ok",),
        verification_output=("true :: FAIL",),
        passed=True,
    )
    try:
        validate_proof(preview, proof)
    except ApplyProofError as exc:
        assert "failure" in str(exc) or "PASS" in str(exc)
    else:
        raise AssertionError("explicit failed verification status must fail")


def test_verification_output_covers_each_declared_command_once():
    request = make_request(
        verification_commands=("true", "python3 scripts/check_retry.py"),
    )
    from apply import build_preview

    preview = build_preview(request, inventory=make_inventory(), workspace=make_workspace())
    proof = make_proof(
        changed_paths=(Path("SKILL.md"),),
        command_output=("changed one file",),
        verification_output=(
            "true :: PASS",
            "python3 scripts/check_retry.py :: PASS",
        ),
        passed=True,
    )
    validate_proof(preview, proof)


def test_verification_output_rejects_missing_declared_command():
    request = make_request(
        verification_commands=("true", "python3 scripts/check_retry.py"),
    )
    from apply import build_preview

    preview = build_preview(request, inventory=make_inventory(), workspace=make_workspace())
    proof = make_proof(
        changed_paths=(Path("SKILL.md"),),
        command_output=("changed one file",),
        verification_output=("true :: PASS",),
        passed=True,
    )
    try:
        validate_proof(preview, proof)
    except ApplyProofError as exc:
        assert "missing" in str(exc) or "every" in str(exc)
    else:
        raise AssertionError("missing command verification must fail")


def test_verification_output_rejects_duplicate_declared_command():
    request = make_request(
        verification_commands=("true", "python3 scripts/check_retry.py"),
    )
    from apply import build_preview

    preview = build_preview(request, inventory=make_inventory(), workspace=make_workspace())
    proof = make_proof(
        changed_paths=(Path("SKILL.md"),),
        command_output=("changed one file",),
        verification_output=("true :: PASS", "true :: PASS"),
        passed=True,
    )
    try:
        validate_proof(preview, proof)
    except ApplyProofError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate command verification must fail")


def test_verification_output_rejects_unknown_declared_command():
    request = make_request(
        verification_commands=("true", "python3 scripts/check_retry.py"),
    )
    from apply import build_preview

    preview = build_preview(request, inventory=make_inventory(), workspace=make_workspace())
    proof = make_proof(
        changed_paths=(Path("SKILL.md"),),
        command_output=("changed one file",),
        verification_output=(
            "true :: PASS",
            "python3 scripts/other_check.py :: PASS",
        ),
        passed=True,
    )
    try:
        validate_proof(preview, proof)
    except ApplyProofError as exc:
        assert "not declared" in str(exc)
    else:
        raise AssertionError("unknown command verification must fail")


def test_verification_command_matching_preserves_byte_exact_whitespace():
    declared = " python3 scripts/check_retry.py "
    request = make_request(verification_commands=(declared,))
    preview = build_preview(request, inventory=make_inventory(), workspace=make_workspace())
    proof = make_proof(
        changed_paths=(Path("SKILL.md"),),
        command_output=("changed one file",),
        verification_output=("python3 scripts/check_retry.py :: PASS",),
        passed=True,
    )
    try:
        validate_proof(preview, proof)
    except ApplyProofError as exc:
        assert "missing" in str(exc) or "declared" in str(exc)
    else:
        raise AssertionError("normalized command must not satisfy byte-exact match")


def test_verification_command_matching_accepts_exact_whitespace():
    declared = " python3 scripts/check_retry.py "
    request = make_request(verification_commands=(declared,))
    preview = build_preview(request, inventory=make_inventory(), workspace=make_workspace())
    proof = make_proof(
        changed_paths=(Path("SKILL.md"),),
        command_output=("changed one file",),
        verification_output=(" python3 scripts/check_retry.py  :: PASS",),
        passed=True,
    )
    validate_proof(preview, proof)


def test_two_approved_clusters_with_disjoint_paths_are_independent():
    first = make_preview(cluster_id="first", target_paths=(Path("a.py"),))
    second = make_preview(cluster_id="second", target_paths=(Path("b.py"),))
    assert check_scope_overlap(first, second) is None


def test_overlapping_clusters_cannot_run_in_parallel():
    first = make_preview(cluster_id="first", target_paths=(Path("a.py"),))
    second = make_preview(cluster_id="second", target_paths=(Path("a.py"),))
    assert check_scope_overlap(first, second) == ("first", "second")


def test_directory_scope_overlaps_descendant_scope():
    first = make_preview(cluster_id="first", target_paths=(Path("hooks"),))
    second = make_preview(cluster_id="second", target_paths=(Path("hooks/retry.sh"),))
    assert check_scope_overlap(first, second) == ("first", "second")


def run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    run_all()
