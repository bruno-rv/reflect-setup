#!/usr/bin/env python3
"""Runnable checks for three-part fix-wiring verification."""

from verification import CheckStatus, verify_fix
from test_support import make_entry, make_evidence_set


def test_all_checks_are_required_for_resolution():
    result = verify_fix(
        make_entry("background-task-opacity"),
        symptom_evidence=make_evidence_set("symptom-absent"),
        invocation_evidence=make_evidence_set("invoked"),
        outcome_evidence=make_evidence_set("outcome-pass"),
    )
    assert result.overall is CheckStatus.PASS
    assert result.symptom.status is CheckStatus.PASS
    assert result.invocation.status is CheckStatus.PASS
    assert result.outcome.status is CheckStatus.PASS


def test_recurrence_marks_built_not_operating():
    result = verify_fix(
        make_entry("background-task-opacity"),
        symptom_evidence=make_evidence_set("symptom-recurred"),
        invocation_evidence=make_evidence_set("invoked"),
        outcome_evidence=make_evidence_set("outcome-pass"),
    )
    assert result.symptom.status is CheckStatus.FAIL
    assert result.overall is CheckStatus.FAIL
    assert result.recommended_status == "built-not-operating"


def test_absence_of_evidence_is_insufficient():
    result = verify_fix(
        make_entry("background-task-opacity"),
        symptom_evidence=(),
        invocation_evidence=(),
        outcome_evidence=(),
    )
    assert result.overall is CheckStatus.INSUFFICIENT


def test_resolved_fix_regresses_when_symptom_returns():
    result = verify_fix(
        make_entry("recording-health-check-not-wired", status="resolved"),
        symptom_evidence=make_evidence_set("symptom-recurred"),
        invocation_evidence=make_evidence_set("invoked"),
        outcome_evidence=make_evidence_set("outcome-fail"),
    )
    assert result.overall is CheckStatus.FAIL
    assert result.recommended_status == "built-not-operating"


def test_applied_entry_without_wired_check_is_rejected():
    try:
        verify_fix(
            make_entry("bad-entry", wired_check=""),
            symptom_evidence=(),
            invocation_evidence=(),
            outcome_evidence=(),
        )
    except ValueError as exc:
        assert "wired_check" in str(exc)
    else:
        raise AssertionError("applied entries need a wired check")


def test_checks_preserve_exact_evidence_references_and_details():
    symptom = make_evidence_set("symptom-absent")
    invocation = make_evidence_set("invoked")
    outcome = make_evidence_set("outcome-pass")
    result = verify_fix(
        make_entry("fixture", wired_check="fixture evidence"),
        symptom_evidence=symptom,
        invocation_evidence=invocation,
        outcome_evidence=outcome,
    )
    assert result.symptom.evidence == tuple(item.ref for item in symptom)
    assert result.invocation.evidence == tuple(item.ref for item in invocation)
    assert result.outcome.evidence == tuple(item.ref for item in outcome)
    assert "fixture evidence" in result.symptom.detail
    assert result.symptom.detail
    assert result.invocation.detail
    assert result.outcome.detail


def test_outcome_reusing_invocation_evidence_is_insufficient():
    from verification import VerificationEvidence

    invocation = make_evidence_set("invoked")
    outcome = tuple(VerificationEvidence(item.ref, "outcome-pass") for item in invocation)
    result = verify_fix(
        make_entry("fixture"),
        symptom_evidence=make_evidence_set("symptom-absent"),
        invocation_evidence=invocation,
        outcome_evidence=outcome,
    )
    assert result.invocation.status is CheckStatus.PASS
    assert result.outcome.status is CheckStatus.INSUFFICIENT
    assert result.overall is CheckStatus.INSUFFICIENT


def test_merged_finding_for_cluster_overrides_non_recurrence_claim():
    from test_support import make_findings

    result = verify_fix(
        make_entry("fixture"),
        symptom_evidence=make_evidence_set("symptom-absent"),
        invocation_evidence=make_evidence_set("invoked"),
        outcome_evidence=make_evidence_set("outcome-pass"),
        merged_findings=make_findings(
            1,
            ("session-a",),
            ("project-a",),
            ("2026-08-23",),
            cluster_key="fixture",
        ),
    )
    assert result.symptom.status is CheckStatus.FAIL
    assert result.overall is CheckStatus.FAIL


def test_independent_inventory_coverage_can_supply_invocation_and_outcome():
    from coverage_model import assess_coverage
    from test_support import make_evidence

    record = assess_coverage(
        artifact_id="skill:fixture",
        artifact_kind="skill",
        exists=True,
        eligible=True,
        trigger_evidence=(make_evidence("trigger.md", 3, "trigger"),),
        prevention_evidence=(make_evidence("outcome.md", 8, "outcome"),),
        symptom_recurred=False,
    )
    result = verify_fix(
        make_entry("fixture", wired_check="skill:fixture"),
        symptom_evidence=make_evidence_set("symptom-absent"),
        invocation_evidence=make_evidence_set("invoked"),
        outcome_evidence=(),
        coverage_records=(record,),
    )
    assert result.invocation.status is CheckStatus.PASS
    assert result.outcome.status is CheckStatus.PASS
    assert set(result.invocation.evidence).isdisjoint(result.outcome.evidence)
    assert result.overall is CheckStatus.PASS


def test_failed_invocation_is_not_operating():
    result = verify_fix(
        make_entry("fixture"),
        symptom_evidence=make_evidence_set("symptom-absent"),
        invocation_evidence=make_evidence_set("outcome-fail"),
        outcome_evidence=make_evidence_set("outcome-pass"),
    )
    assert result.invocation.status is CheckStatus.FAIL
    assert result.overall is CheckStatus.FAIL


def test_failed_outcome_is_not_resolved():
    result = verify_fix(
        make_entry("fixture"),
        symptom_evidence=make_evidence_set("symptom-absent"),
        invocation_evidence=make_evidence_set("invoked"),
        outcome_evidence=make_evidence_set("outcome-fail"),
    )
    assert result.outcome.status is CheckStatus.FAIL
    assert result.overall is CheckStatus.FAIL


def test_ineligible_coverage_cannot_prove_invocation_or_outcome():
    from coverage_model import assess_coverage
    from test_support import make_evidence

    record = assess_coverage(
        artifact_id="skill:fixture",
        artifact_kind="skill",
        exists=False,
        eligible=False,
        trigger_evidence=(make_evidence("trigger.md", 3, "trigger"),),
        prevention_evidence=(make_evidence("outcome.md", 8, "outcome"),),
        symptom_recurred=False,
    )
    result = verify_fix(
        make_entry("fixture", wired_check="skill:fixture"),
        symptom_evidence=make_evidence_set("symptom-absent"),
        invocation_evidence=(),
        outcome_evidence=(),
        coverage_records=(record,),
    )
    assert result.invocation.status is CheckStatus.INSUFFICIENT
    assert result.outcome.status is CheckStatus.INSUFFICIENT
    assert result.overall is CheckStatus.INSUFFICIENT


def test_unrelated_coverage_cannot_contribute_to_a_fix():
    from coverage_model import assess_coverage
    from test_support import make_evidence

    record = assess_coverage(
        artifact_id="skill:other",
        artifact_kind="skill",
        exists=True,
        eligible=True,
        trigger_evidence=(make_evidence("trigger.md", 3, "trigger"),),
        prevention_evidence=(make_evidence("outcome.md", 8, "outcome"),),
        symptom_recurred=False,
    )
    result = verify_fix(
        make_entry("fixture", wired_check="skill:fixture"),
        symptom_evidence=make_evidence_set("symptom-absent"),
        invocation_evidence=(),
        outcome_evidence=(),
        coverage_records=(record,),
    )
    assert result.invocation.status is CheckStatus.INSUFFICIENT
    assert result.outcome.status is CheckStatus.INSUFFICIENT
    assert result.overall is CheckStatus.INSUFFICIENT


def test_verify_fix_evidence_arguments_are_keyword_only():
    try:
        verify_fix(make_entry("fixture"), (), (), ())
    except TypeError as exc:
        assert "positional" in str(exc)
    else:
        raise AssertionError("verification evidence must be keyword-only")


def run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    run_all()
