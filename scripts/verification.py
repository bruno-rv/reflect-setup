"""Typed, evidence-gated verification for applied fixes."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from coverage_model import CoverageRecord
from ledger import LedgerEntry, LedgerStatus
from miner_contract import EvidenceRef, Finding


def _frozen_dataclass(cls):
    kwargs = {"frozen": True}
    if sys.version_info >= (3, 10):
        kwargs["slots"] = True
    return dataclass(**kwargs)(cls)


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT = "insufficient"


class CheckName(str, Enum):
    SYMPTOM = "symptom"
    INVOCATION = "invocation"
    OUTCOME = "outcome"


class VerificationLabel(str, Enum):
    SYMPTOM_ABSENT = "symptom-absent"
    SYMPTOM_RECURRED = "symptom-recurred"
    INVOKED = "invoked"
    OUTCOME_PASS = "outcome-pass"
    OUTCOME_FAIL = "outcome-fail"


@_frozen_dataclass
class VerificationEvidence:
    """One current-window reference with a constrained semantic label."""

    ref: EvidenceRef
    label: VerificationLabel


@_frozen_dataclass
class VerificationCheck:
    name: CheckName
    status: CheckStatus
    evidence: tuple[EvidenceRef, ...]
    detail: str


@_frozen_dataclass
class FixVerification:
    cluster_id: str
    symptom: VerificationCheck
    invocation: VerificationCheck
    outcome: VerificationCheck
    overall: CheckStatus
    recommended_status: str


_ALLOWED_LABELS = frozenset(item.value for item in VerificationLabel)


def _label_value(label: VerificationLabel | str, field: str) -> str:
    value = label.value if isinstance(label, VerificationLabel) else label
    if not isinstance(value, str) or value not in _ALLOWED_LABELS:
        allowed = ", ".join(sorted(_ALLOWED_LABELS))
        raise ValueError(f"{field} label must be one of: {allowed}")
    return value


def _coerce_evidence(
    values: Iterable[VerificationEvidence] | None,
    field: str,
) -> tuple[VerificationEvidence, ...]:
    if values is None:
        raise TypeError(f"{field} must be a sequence of VerificationEvidence")
    evidence = tuple(values)
    for item in evidence:
        if not isinstance(item, VerificationEvidence):
            raise TypeError(f"{field} must contain VerificationEvidence instances")
        if not isinstance(item.ref, EvidenceRef):
            raise TypeError(f"{field} references must contain EvidenceRef instances")
        _label_value(item.label, field)
    return evidence


def _check_evidence(
    name: CheckName,
    evidence: tuple[VerificationEvidence, ...],
    status: CheckStatus,
    detail: str,
    wired_check: str,
) -> VerificationCheck:
    return VerificationCheck(
        name=name,
        status=status,
        evidence=tuple(item.ref for item in evidence),
        detail=f"wired_check={wired_check}; {detail}",
    )


def _symptom_status(
    evidence: tuple[VerificationEvidence, ...],
    coverage_recurred: bool = False,
) -> tuple[CheckStatus, str]:
    if coverage_recurred:
        return CheckStatus.FAIL, "target coverage record reports current symptom recurrence"
    if not evidence:
        return CheckStatus.INSUFFICIENT, "no explicit non-recurrence evidence"
    labels = {_label_value(item.label, "symptom_evidence") for item in evidence}
    if VerificationLabel.SYMPTOM_RECURRED.value in labels:
        return CheckStatus.FAIL, "current symptom recurrence is present"
    if VerificationLabel.SYMPTOM_ABSENT.value in labels:
        return CheckStatus.PASS, "explicit non-recurrence evidence is present"
    return CheckStatus.INSUFFICIENT, "evidence does not explicitly establish non-recurrence"


def _invocation_status(evidence: tuple[VerificationEvidence, ...]) -> tuple[CheckStatus, str]:
    if not evidence:
        return CheckStatus.INSUFFICIENT, "no transcript or tool invocation evidence"
    labels = {_label_value(item.label, "invocation_evidence") for item in evidence}
    if VerificationLabel.OUTCOME_FAIL.value in labels or VerificationLabel.SYMPTOM_RECURRED.value in labels:
        return CheckStatus.FAIL, "artifact was not shown as loaded or called"
    if VerificationLabel.INVOKED.value in labels:
        return CheckStatus.PASS, "transcript or tool evidence shows the artifact was loaded or called"
    return CheckStatus.INSUFFICIENT, "evidence does not show the artifact was loaded or called"


def _evidence_key(ref: EvidenceRef) -> tuple[str, int]:
    return ref.digest_path, ref.source_line


def _outcome_status(
    evidence: tuple[VerificationEvidence, ...],
    invocation_evidence: tuple[VerificationEvidence, ...],
) -> tuple[CheckStatus, str]:
    if not evidence:
        return CheckStatus.INSUFFICIENT, "no independent success or behavioral evidence"
    outcome_keys = {_evidence_key(item.ref) for item in evidence}
    invocation_keys = {_evidence_key(item.ref) for item in invocation_evidence}
    if outcome_keys.intersection(invocation_keys):
        return CheckStatus.INSUFFICIENT, "outcome evidence is not separate from invocation evidence"
    labels = {_label_value(item.label, "outcome_evidence") for item in evidence}
    if VerificationLabel.OUTCOME_FAIL.value in labels:
        return CheckStatus.FAIL, "behavioral outcome shows failure"
    if VerificationLabel.OUTCOME_PASS.value in labels:
        return CheckStatus.PASS, "independent success or behavioral evidence is present"
    return CheckStatus.INSUFFICIENT, "evidence does not show a successful behavioral outcome"


def _cluster_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _target_ids(entry: LedgerEntry, explicit: Iterable[str]) -> frozenset[str]:
    target_values = (explicit,) if isinstance(explicit, str) else tuple(explicit)
    if not target_values:
        if not isinstance(entry.wired_check, str) or not entry.wired_check.strip():
            return frozenset()
        target_values = (entry.wired_check.strip(),)
    if any(not isinstance(value, str) or not value.strip() for value in target_values):
        raise ValueError("target_artifact_ids must contain non-empty strings")
    return frozenset(value.strip() for value in target_values)


def _coverage_contribution(
    records: tuple[CoverageRecord, ...],
    targets: frozenset[str],
    invocation_evidence: tuple[VerificationEvidence, ...],
    outcome_evidence: tuple[VerificationEvidence, ...],
) -> tuple[tuple[VerificationEvidence, ...], tuple[VerificationEvidence, ...], bool]:
    """Use each aggregate coverage record on one side of the evidence split.

    ``CoverageRecord.evidence`` is a combined sequence in the preceding
    coverage interface. It may contribute invocation evidence when no explicit
    invocation exists, or certified outcome evidence when invocation evidence
    already exists, but never both. This preserves disjoint evidence keys.
    """
    invocation_keys = {_evidence_key(item.ref) for item in invocation_evidence}
    outcome_keys = {_evidence_key(item.ref) for item in outcome_evidence}
    invocation: list[VerificationEvidence] = []
    outcome: list[VerificationEvidence] = []
    symptom_recurred = False
    for record in records:
        if record.artifact_id not in targets or not record.declared or not record.eligible:
            continue
        trigger_refs = tuple(record.trigger_evidence)
        prevention_refs = tuple(record.prevention_evidence)
        # Records created before the split fields remain import-compatible;
        # their combined evidence can prove invocation only.
        if not trigger_refs and record.evidence:
            trigger_refs = tuple(record.evidence)
        if any(
            not isinstance(ref, EvidenceRef)
            for ref in trigger_refs + prevention_refs
        ):
            raise TypeError("coverage record evidence must contain EvidenceRef instances")
        if record.prevented is False:
            symptom_recurred = True
        if record.triggered and not invocation_evidence:
            invocation.extend(
                VerificationEvidence(ref, VerificationLabel.INVOKED)
                for ref in trigger_refs
            )
            invocation_keys.update(_evidence_key(ref) for ref in trigger_refs)
        if record.triggered and record.prevented is True and not outcome_evidence:
            for ref in prevention_refs:
                key = _evidence_key(ref)
                if key not in invocation_keys and key not in outcome_keys:
                    outcome.append(VerificationEvidence(ref, VerificationLabel.OUTCOME_PASS))
        elif record.triggered and record.prevented is False and not outcome_evidence:
            for ref in prevention_refs:
                key = _evidence_key(ref)
                if key not in invocation_keys and key not in outcome_keys:
                    outcome.append(VerificationEvidence(ref, VerificationLabel.OUTCOME_FAIL))
    return tuple(invocation), tuple(outcome), symptom_recurred


def verify_fix(
    entry: LedgerEntry,
    *,
    symptom_evidence: tuple[VerificationEvidence, ...],
    invocation_evidence: tuple[VerificationEvidence, ...],
    outcome_evidence: tuple[VerificationEvidence, ...],
    merged_findings: Iterable[Finding] = (),
    coverage_records: Iterable[CoverageRecord] = (),
    target_artifact_ids: Iterable[str] = (),
) -> FixVerification:
    """Verify symptom absence, invocation, and independent outcome separately."""
    if not isinstance(entry, LedgerEntry):
        raise TypeError("entry must be a LedgerEntry")
    if not isinstance(entry.cluster_id, str) or not entry.cluster_id.strip():
        raise ValueError("entry cluster_id must be non-empty")
    if entry.status in {
        LedgerStatus.FIX_APPLIED,
        LedgerStatus.BUILT_NOT_OPERATING,
        LedgerStatus.RESOLVED,
    } and (not isinstance(entry.wired_check, str) or not entry.wired_check.strip()):
        raise ValueError("applied entries require a non-empty wired_check")

    symptom = list(_coerce_evidence(symptom_evidence, "symptom_evidence"))
    invocation = list(_coerce_evidence(invocation_evidence, "invocation_evidence"))
    outcome = list(_coerce_evidence(outcome_evidence, "outcome_evidence"))
    findings = tuple(merged_findings)
    records = tuple(coverage_records)
    targets = _target_ids(entry, target_artifact_ids)
    for finding in findings:
        if not isinstance(finding, Finding):
            raise TypeError("merged_findings must contain Finding instances")
        if _cluster_key(finding.cluster_key) == _cluster_key(entry.cluster_id):
            for ref in finding.evidence:
                if not isinstance(ref, EvidenceRef):
                    raise TypeError("merged_findings evidence must contain EvidenceRef instances")
                symptom.append(VerificationEvidence(ref, VerificationLabel.SYMPTOM_RECURRED))
    for record in records:
        if not isinstance(record, CoverageRecord):
            raise TypeError("coverage_records must contain CoverageRecord instances")
    coverage_invocation, coverage_outcome, coverage_recurred = _coverage_contribution(
        records, targets, tuple(invocation), tuple(outcome)
    )
    if not invocation:
        invocation.extend(coverage_invocation)
    if not outcome:
        outcome.extend(coverage_outcome)

    symptom_values = tuple(symptom)
    invocation_values = tuple(invocation)
    outcome_values = tuple(outcome)
    symptom_status, symptom_detail = _symptom_status(symptom_values, coverage_recurred)
    invocation_status, invocation_detail = _invocation_status(invocation_values)
    outcome_status, outcome_detail = _outcome_status(outcome_values, invocation_values)
    symptom_check = _check_evidence(
        CheckName.SYMPTOM, symptom_values, symptom_status, symptom_detail, entry.wired_check
    )
    invocation_check = _check_evidence(
        CheckName.INVOCATION, invocation_values, invocation_status, invocation_detail, entry.wired_check
    )
    outcome_check = _check_evidence(
        CheckName.OUTCOME, outcome_values, outcome_status, outcome_detail, entry.wired_check
    )
    checks = (symptom_check, invocation_check, outcome_check)
    if any(check.status is CheckStatus.FAIL for check in checks):
        overall = CheckStatus.FAIL
    elif any(check.status is CheckStatus.INSUFFICIENT for check in checks):
        overall = CheckStatus.INSUFFICIENT
    else:
        overall = CheckStatus.PASS
    recommended_status = {
        CheckStatus.PASS: "resolved",
        CheckStatus.FAIL: "built-not-operating",
        CheckStatus.INSUFFICIENT: "fix-applied",
    }[overall]
    return FixVerification(
        cluster_id=entry.cluster_id,
        symptom=symptom_check,
        invocation=invocation_check,
        outcome=outcome_check,
        overall=overall,
        recommended_status=recommended_status,
    )


__all__ = [
    "CheckName",
    "CheckStatus",
    "FixVerification",
    "VerificationCheck",
    "VerificationEvidence",
    "VerificationLabel",
    "verify_fix",
]
