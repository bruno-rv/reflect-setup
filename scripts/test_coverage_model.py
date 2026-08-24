#!/usr/bin/env python3
"""Runnable checks for inventory coverage states."""
from coverage_model import CoverageRecord, Inventory, InventoryItem, assess_coverage
from test_support import make_evidence


def test_declared_artifact_is_not_triggered_or_preventing_without_evidence():
    record = assess_coverage(
        artifact_id="skill:reflect-setup",
        artifact_kind="skill",
        exists=True,
        eligible=True,
        trigger_evidence=(),
        prevention_evidence=(),
        symptom_recurred=False,
    )
    assert isinstance(record, CoverageRecord)
    assert record.declared is True
    assert record.eligible is True
    assert record.triggered is False
    assert record.prevented is None


def test_trigger_and_positive_outcome_are_distinct():
    triggered = make_evidence("skill.md", 3, "trigger")
    outcome = make_evidence("session.md", 8, "outcome")
    record = assess_coverage(
        artifact_id="hook:recording-health",
        artifact_kind="hook",
        exists=True,
        eligible=True,
        trigger_evidence=(triggered,),
        prevention_evidence=(outcome,),
        symptom_recurred=False,
    )
    assert record.triggered is True
    assert record.prevented is True
    assert record.evidence == (triggered, outcome)


def test_recurrence_is_negative_prevention_evidence():
    triggered = make_evidence("skill.md", 3, "trigger")
    outcome = make_evidence("session.md", 8, "outcome")
    record = assess_coverage(
        artifact_id="skill:reflect-setup",
        artifact_kind="skill",
        exists=True,
        eligible=True,
        trigger_evidence=(triggered,),
        prevention_evidence=(outcome,),
        symptom_recurred=True,
    )
    assert record.prevented is False


def test_inventory_records_declared_artifacts_and_paths():
    item = InventoryItem("skill:reflect-setup", "skill", "SKILL.md", True)
    inventory = Inventory((item,))
    assert inventory.items == (item,)


def run_all():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    run_all()
