#!/usr/bin/env python3
"""Runnable checks for safe source installation."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import runtime
from runtime import Runtime, install_skill, resolve_runtime


def _source_tree(root):
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("# reflect-setup\n")
    (root / "scripts").mkdir()
    (root / "scripts" / "runtime.py").write_text("print('runtime')\n")


def test_install_symlink_points_to_source_and_is_idempotent():
    with TemporaryDirectory() as raw:
        home = Path(raw) / "home"
        source = Path(raw) / "repo"
        _source_tree(source)
        spec = resolve_runtime("claude", home=home, env={})
        first = install_skill(spec, source)
        second = install_skill(spec, source)
        assert first.mode == "symlink"
        assert first.runtime is Runtime.CLAUDE
        assert second.source_hash == first.source_hash
        assert spec.skill_root.is_symlink()
        assert spec.skill_root.resolve() == source.resolve()


def test_install_refuses_unmanaged_existing_directory():
    with TemporaryDirectory() as raw:
        home = Path(raw) / "home"
        source = Path(raw) / "repo"
        _source_tree(source)
        spec = resolve_runtime("codex", home=home, env={})
        spec.skill_root.mkdir(parents=True)
        (spec.skill_root / "user-file.md").write_text("keep me\n")
        try:
            install_skill(spec, source)
        except FileExistsError as exc:
            assert "user-file.md" in str(exc)
        else:
            raise AssertionError("unmanaged directory must not be replaced")
        assert (spec.skill_root / "user-file.md").read_text() == "keep me\n"


def test_copy_install_refuses_existing_regular_file():
    with TemporaryDirectory() as raw:
        home = Path(raw) / "home"
        source = Path(raw) / "repo"
        _source_tree(source)
        spec = resolve_runtime("codex", home=home, env={})
        spec.skill_root.parent.mkdir(parents=True)
        spec.skill_root.write_text("keep this file\n")
        try:
            install_skill(spec, source, mode="copy")
        except FileExistsError as exc:
            assert "existing file" in str(exc)
        else:
            raise AssertionError("existing regular file must not be replaced")
        assert spec.skill_root.read_text() == "keep this file\n"


def test_install_refuses_different_existing_symlink():
    with TemporaryDirectory() as raw:
        home = Path(raw) / "home"
        source = Path(raw) / "repo"
        other = Path(raw) / "other"
        _source_tree(source)
        _source_tree(other)
        spec = resolve_runtime("codex", home=home, env={})
        spec.skill_root.parent.mkdir(parents=True)
        spec.skill_root.symlink_to(other, target_is_directory=True)
        try:
            install_skill(spec, source)
        except FileExistsError as exc:
            assert "mismatch" in str(exc)
        else:
            raise AssertionError("different symlink must not be replaced")
        assert spec.skill_root.resolve() == other.resolve()


def test_copy_install_writes_manifest_and_is_idempotent():
    with TemporaryDirectory() as raw:
        home = Path(raw) / "home"
        source = Path(raw) / "repo"
        _source_tree(source)
        spec = resolve_runtime("codex", home=home, env={})
        first = install_skill(spec, source, mode="copy")
        second = install_skill(spec, source, mode="copy")
        assert first.mode == "copy"
        assert second.source_hash == first.source_hash
        assert not spec.skill_root.is_symlink()
        assert (spec.skill_root / "SKILL.md").read_text() == "# reflect-setup\n"
        manifest = json.loads((spec.skill_root / ".reflect-setup-source.json").read_text())
        assert manifest["source_hash"] == first.source_hash
        assert manifest["runtime"] == "codex"


def test_copy_install_records_source_commit_provenance():
    with TemporaryDirectory() as raw:
        home = Path(raw) / "home"
        source = Path(raw) / "repo"
        _source_tree(source)
        git_dir = source / ".git"
        (git_dir / "refs" / "heads").mkdir(parents=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        (git_dir / "refs" / "heads" / "main").write_text("abc123def456\n")
        spec = resolve_runtime("claude", home=home, env={})
        install_skill(spec, source, mode="copy")
        manifest = json.loads((spec.skill_root / ".reflect-setup-source.json").read_text())
        assert manifest["source_commit"] == "abc123def456"


def test_copy_install_refuses_different_manifest_hash_without_overwriting():
    with TemporaryDirectory() as raw:
        home = Path(raw) / "home"
        source = Path(raw) / "repo"
        other = Path(raw) / "other"
        _source_tree(source)
        _source_tree(other)
        (other / "SKILL.md").write_text("# other\n")
        spec = resolve_runtime("claude", home=home, env={})
        install_skill(spec, source, mode="copy")
        original = (spec.skill_root / "SKILL.md").read_text()
        try:
            install_skill(spec, other, mode="copy")
        except FileExistsError as exc:
            assert "manifest" in str(exc)
        else:
            raise AssertionError("different manifest hash must fail")
        assert (spec.skill_root / "SKILL.md").read_text() == original


def test_source_hash_is_order_independent_and_changes_with_file_bytes():
    with TemporaryDirectory() as raw:
        home = Path(raw) / "home"
        source = Path(raw) / "repo"
        _source_tree(source)
        spec = resolve_runtime("claude", home=home, env={})
        first = install_skill(spec, source, mode="copy")
        (source / "SKILL.md").write_text("# changed\n")
        second = install_skill(resolve_runtime("codex", home=Path(raw) / "other-home", env={}), source, mode="copy")
        assert first.source_hash != second.source_hash


def test_source_hash_uses_one_consistent_file_snapshot():
    with TemporaryDirectory() as raw:
        source = Path(raw) / "repo"
        _source_tree(source)
        files = runtime._source_files(source)
        before = runtime._source_hash(files)
        (source / "SKILL.md").write_text("# changed after scan\n")
        assert runtime._source_hash(files) == before


def test_install_rejects_non_directory_source():
    with TemporaryDirectory() as raw:
        home = Path(raw) / "home"
        source = Path(raw) / "repo"
        source.write_text("not a directory\n")
        spec = resolve_runtime("claude", home=home, env={})
        try:
            install_skill(spec, source)
        except ValueError as exc:
            assert "directory" in str(exc)
        else:
            raise AssertionError("file source must fail")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
