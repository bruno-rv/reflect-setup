#!/usr/bin/env python3
"""Runnable checks for inventory coverage states."""
from coverage_model import (
    CoverageObservation,
    CoverageRecord,
    Inventory,
    InventoryItem,
    assess_coverage,
)
from test_support import make_evidence
from miner_contract import EvidenceRef
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


def test_reusing_the_same_evidence_cannot_prove_prevention():
    evidence = make_evidence("session.md", 8, "outcome")
    record = assess_coverage(
        artifact_id="skill:reflect-setup",
        artifact_kind="skill",
        exists=True,
        eligible=True,
        trigger_evidence=(evidence,),
        prevention_evidence=(evidence,),
        symptom_recurred=False,
    )
    assert isinstance(evidence, EvidenceRef)
    assert record.triggered is True
    assert record.prevented is None


def test_coverage_rejects_untyped_evidence():
    try:
        assess_coverage(
            artifact_id="skill:reflect-setup",
            artifact_kind="skill",
            exists=True,
            eligible=True,
            trigger_evidence=(object(),),
            prevention_evidence=(),
            symptom_recurred=False,
        )
    except TypeError as exc:
        assert "EvidenceRef" in str(exc)
    else:
        raise AssertionError("untyped coverage evidence must fail")


def test_inventory_records_declared_artifacts_and_paths():
    item = InventoryItem("skill:reflect-setup", "skill", "SKILL.md", True)
    inventory = Inventory((item,))
    assert inventory.items == (item,)


def test_host_observation_is_typed_and_preserves_explicit_eligibility():
    triggered = make_evidence("trigger.md", project="fixture-project")
    prevented = make_evidence("prevented.md", project="fixture-project")
    observation = CoverageObservation(
        artifact_id="skill:fixture",
        artifact_kind="skill",
        eligible=True,
        trigger_evidence=(triggered,),
        prevention_evidence=(prevented,),
        symptom_recurred=False,
    )
    record = assess_coverage(observation=observation, exists=True)
    assert record.eligible is True
    assert record.triggered is True
    assert record.trigger_evidence == (triggered,)
    assert record.prevention_evidence == (prevented,)


def test_host_observation_rejects_unknown_artifact_type():
    try:
        CoverageObservation(
            artifact_id="skill:fixture",
            artifact_kind="",
            eligible=True,
            trigger_evidence=(),
            prevention_evidence=(),
            symptom_recurred=False,
        )
    except ValueError as exc:
        assert "artifact_kind" in str(exc)
    else:
        raise AssertionError("empty artifact kinds must fail closed")


def run_all():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    run_all()
