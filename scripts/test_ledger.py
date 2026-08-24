#!/usr/bin/env python3
"""Runnable checks for strict ledger parsing and transitions."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from ledger import LedgerStatus, LedgerTransitionError, parse_ledger, update_ledger, validate_transition


def make_verification(symptom, invocation, outcome):
    def check(name, status):
        return SimpleNamespace(name=name, status=SimpleNamespace(value=status), evidence=(), detail=status)

    return SimpleNamespace(
        cluster_id="fixture",
        symptom=check("symptom", symptom),
        invocation=check("invocation", invocation),
        outcome=check("outcome", outcome),
    )


def test_fix_applied_without_invocation_becomes_built_not_operating():
    verification = make_verification(symptom="fail", invocation="insufficient", outcome="pass")
    assert validate_transition(LedgerStatus.FIX_APPLIED, verification) is LedgerStatus.BUILT_NOT_OPERATING


def test_missing_evidence_never_resolves():
    verification = make_verification(symptom="insufficient", invocation="pass", outcome="pass")
    try:
        validate_transition(LedgerStatus.FIX_APPLIED, verification)
    except LedgerTransitionError as exc:
        assert "insufficient" in str(exc)
    else:
        raise AssertionError("insufficient symptom evidence cannot resolve")


def test_all_three_passes_resolve():
    verification = make_verification(symptom="pass", invocation="pass", outcome="pass")
    assert validate_transition(LedgerStatus.FIX_APPLIED, verification) is LedgerStatus.RESOLVED


def test_parser_accepts_example_subset_and_statuses():
    source = """# comment\n- id: fixture\n  title: \"Fixture title\"\n  status: built-not-operating\n  first_seen: 2026-08-23\n  last_seen: 2026-08-24\n  sessions: 2\n  projects: [one, two]\n  evidence: [session-a, session-b]\n  fix: \"wire the hook\"\n  wired_check: \"next run invokes the hook\"\n"""
    entries = parse_ledger(source)
    assert entries[0].cluster_id == "fixture"
    assert entries[0].status is LedgerStatus.BUILT_NOT_OPERATING
    assert entries[0].wired_check == "next run invokes the hook"


def test_parser_rejects_duplicate_ids_and_missing_wired_check():
    duplicate = "- id: fixture\n  status: new\n- id: fixture\n  status: monitor\n"
    try:
        parse_ledger(duplicate)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate ids must fail")

    missing_check = "- id: fixture\n  status: resolved\n"
    try:
        parse_ledger(missing_check)
    except ValueError as exc:
        assert "wired_check" in str(exc)
    else:
        raise AssertionError("resolved entries require wired_check")


def test_update_ledger_touches_one_entry_and_preserves_comments_and_fields():
    source = """# header\n- id: fixture\n  title: \"Fixture title\"\n  status: fix-applied\n  first_seen: 2026-08-23\n  last_seen: 2026-08-23\n  sessions: 1\n  projects: [one]\n  evidence: [session-a]\n  fix: \"wire the hook\"\n  wired_check: \"next run invokes the hook\"\n- id: unrelated\n  title: \"Keep me\"\n  status: monitor\n  sessions: 4\n  projects: [two]\n  evidence: [session-z]\n  fix: \"none\"\n  wired_check: \"observe\"\n"""
    findings = [SimpleNamespace(cluster_key="fixture", session_id="session-b", evidence=[SimpleNamespace(timestamp=__import__("datetime").datetime(2026, 8, 24))])]
    verification = make_verification("pass", "pass", "pass")
    updated = update_ledger(source, {"fixture": verification}, merged_findings=findings)
    assert "# header" in updated
    assert 'title: "Keep me"' in updated
    assert "status: resolved" in updated
    assert "sessions: 2" in updated
    assert "last_seen: 2026-08-24" in updated
    assert "status: monitor" in updated


def test_update_ledger_can_write_a_path():
    source = "- id: fixture\n  status: new\n  wired_check: \"\"\n"
    with TemporaryDirectory() as raw:
        path = Path(raw) / "clusters.yaml"
        path.write_text(source)
        updated = update_ledger(path, {"fixture": LedgerStatus.MONITOR})
        assert path.read_text() == updated
        assert "status: monitor" in updated


def run_all():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    run_all()
