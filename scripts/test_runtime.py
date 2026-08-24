#!/usr/bin/env python3
"""Runnable checks for runtime selection and session discovery."""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from runtime import Runtime, Scope, discover_sessions, resolve_runtime


def test_explicit_runtime_resolves_default_roots_without_writing():
    with TemporaryDirectory() as raw:
        home = Path(raw)
        spec = resolve_runtime("codex", home=home, env={})
        assert spec.runtime is Runtime.CODEX
        assert spec.source_root == home / ".codex" / "sessions"
        assert spec.skill_root == home / ".codex" / "skills" / "reflect-setup"
        assert not (home / ".codex").exists()


def test_source_root_override_changes_only_session_root():
    with TemporaryDirectory() as raw:
        home = Path(raw)
        override = Path(raw) / "fixture-sessions"
        spec = resolve_runtime("claude", home=home, env={}, source_root=override)
        assert spec.source_root == override
        assert spec.skill_root == home / ".claude" / "skills" / "reflect-setup"


def test_claude_inventory_includes_global_and_project_locations():
    with TemporaryDirectory() as raw:
        home = Path(raw)
        spec = resolve_runtime("claude", home=home, env={})
        assert set(spec.inventory_roots) == {
            home / ".claude" / "skills",
            home / ".claude" / "commands",
            home / ".claude" / "agents",
            home / ".claude" / "settings.json",
            home / ".claude" / "settings.local.json",
            Path(".claude/skills"),
            Path(".claude/commands"),
            Path(".claude/agents"),
            Path(".claude/settings.json"),
            Path(".claude/settings.local.json"),
        }


def test_codex_inventory_includes_global_and_project_locations():
    with TemporaryDirectory() as raw:
        home = Path(raw)
        spec = resolve_runtime("codex", home=home, env={})
        assert set(spec.inventory_roots) == {
            home / ".codex" / "skills",
            home / ".codex" / "agents",
            home / ".codex" / "config.toml",
            Path(".codex/skills"),
            Path(".codex/agents"),
        }


def test_auto_runtime_rejects_ambiguous_existing_roots():
    with TemporaryDirectory() as raw:
        home = Path(raw)
        (home / ".claude" / "projects").mkdir(parents=True)
        (home / ".codex" / "sessions").mkdir(parents=True)
        try:
            resolve_runtime(None, home=home, env={})
        except RuntimeError as exc:
            assert "--runtime claude" in str(exc)
            assert "--runtime codex" in str(exc)
        else:
            raise AssertionError("ambiguous auto selection must fail")


def test_auto_runtime_selects_the_only_readable_existing_root():
    with TemporaryDirectory() as raw:
        home = Path(raw)
        source_root = home / ".claude" / "projects"
        source_root.mkdir(parents=True)
        spec = resolve_runtime(None, home=home, env={})
        assert spec.runtime is Runtime.CLAUDE
        assert spec.source_root == source_root


def test_auto_runtime_requires_an_existing_root():
    with TemporaryDirectory() as raw:
        try:
            resolve_runtime(None, home=Path(raw), env={})
        except RuntimeError as exc:
            assert "--runtime claude" in str(exc)
            assert "--runtime codex" in str(exc)
        else:
            raise AssertionError("auto selection must not create a runtime root")


def test_claude_discovery_skips_subagents_by_default_and_applies_project_filter():
    with TemporaryDirectory() as raw:
        home = Path(raw)
        root = home / ".claude" / "projects"
        (root / "project-a").mkdir(parents=True)
        (root / "project-a" / "session-a.jsonl").write_text(
            '{"type":"user","message":{"role":"user","content":"hello"}}\n'
        )
        (root / "project-a" / "subagents").mkdir()
        (root / "project-a" / "subagents" / "session-sub.jsonl").write_text("{}\n")
        (root / "project-b").mkdir()
        (root / "project-b" / "session-b.jsonl").write_text("{}\n")
        spec = resolve_runtime("claude", home=home, env={})
        scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), "project-a", False)
        sessions = discover_sessions(spec, scope)
        assert [item.session_id for item in sessions] == ["session-a"]
        assert sessions[0].project == "project-a"
        assert sessions[0].relative_path == "project-a/session-a.jsonl"


def test_claude_discovery_can_include_subagents():
    with TemporaryDirectory() as raw:
        home = Path(raw)
        root = home / ".claude" / "projects" / "project-a" / "subagents"
        root.mkdir(parents=True)
        (root / "session-sub.jsonl").write_text("{}\n")
        spec = resolve_runtime("claude", home=home, env={})
        scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, True)
        sessions = discover_sessions(spec, scope)
        assert [item.session_id for item in sessions] == ["session-sub"]


def test_codex_discovery_keeps_canonical_user_threads_only():
    with TemporaryDirectory() as raw:
        home = Path(raw)
        root = home / ".codex" / "sessions" / "2026" / "08" / "23"
        root.mkdir(parents=True)
        (root / "user.jsonl").write_text(
            '{"timestamp":"2026-08-23T10:00:00Z","type":"session_meta",'
            '"payload":{"id":"user-1","thread_source":"user"}}\n'
        )
        (root / "agent.jsonl").write_text(
            '{"timestamp":"2026-08-23T10:00:00Z","type":"session_meta",'
            '"payload":{"id":"agent-1","thread_source":"subagent"}}\n'
        )
        spec = resolve_runtime("codex", home=home, env={})
        scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False)
        sessions = discover_sessions(spec, scope)
        assert [item.session_id for item in sessions] == ["user-1"]


def test_codex_discovery_uses_source_context_when_user_project_is_missing():
    with TemporaryDirectory() as raw:
        home = Path(raw)
        root = home / ".codex" / "sessions" / "2026" / "08" / "23"
        root.mkdir(parents=True)
        (root / "user.jsonl").write_text(
            '{"type":"session_meta","payload":{"id":"user-1",'
            '"thread_source":"user"}}\n'
        )
        spec = resolve_runtime("codex", home=home, env={})
        scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False)
        sessions = discover_sessions(spec, scope)
        assert len(sessions) == 1
        assert sessions[0].project == "codex:2026/08/23/user.jsonl"


def test_codex_discovery_can_include_subagent_threads_and_uses_cwd_project():
    with TemporaryDirectory() as raw:
        home = Path(raw)
        root = home / ".codex" / "sessions"
        root.mkdir(parents=True)
        (root / "agent.jsonl").write_text(
            '{"type":"session_meta","payload":{"id":"agent-1",'
            '"thread_source":"subagent","cwd":"/tmp/project-a"}}\n'
        )
        spec = resolve_runtime("codex", home=home, env={})
        scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, True)
        sessions = discover_sessions(spec, scope)
        assert [item.session_id for item in sessions] == ["agent-1"]
        assert sessions[0].project == "project-a"


def test_discovery_sort_order_is_runtime_project_then_relative_path():
    with TemporaryDirectory() as raw:
        home = Path(raw)
        root = home / ".claude" / "projects"
        for project, name in (("z-project", "b.jsonl"), ("a-project", "z.jsonl"), ("a-project", "a.jsonl")):
            path = root / project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n")
        spec = resolve_runtime("claude", home=home, env={})
        scope = Scope(datetime(2026, 8, 23, tzinfo=timezone.utc), None, False)
        sessions = discover_sessions(spec, scope)
        assert [item.relative_path for item in sessions] == [
            "a-project/a.jsonl",
            "a-project/z.jsonl",
            "z-project/b.jsonl",
        ]


def test_invalid_runtime_name_is_rejected():
    with TemporaryDirectory() as raw:
        try:
            resolve_runtime("unknown", home=Path(raw), env={})
        except ValueError as exc:
            assert "claude" in str(exc) and "codex" in str(exc)
        else:
            raise AssertionError("unknown runtime must fail")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
