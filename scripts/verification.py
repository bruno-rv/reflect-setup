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


@_frozen_dataclass
class VerificationEvidence:
    """One current-window reference with a semantic verification label."""

    ref: EvidenceRef
    label: str


@_frozen_dataclass
class VerificationCheck:
    name: str
    status: CheckStatus
    evidence: tuple[VerificationEvidence, ...]
    detail: str


@_frozen_dataclass
class FixVerification:
    cluster_id: str
    symptom: VerificationCheck
    invocation: VerificationCheck
    outcome: VerificationCheck
    overall: CheckStatus
    recommended_status: str


_WORD_RE = re.compile(r"[a-z0-9]+")


def _phrase(label: str, *parts: str) -> bool:
    normalized = " ".join(_WORD_RE.findall(label.casefold().replace("_", " ")))
    return any(
        " ".join(_WORD_RE.findall(part.casefold().replace("_", " "))) in normalized
        for part in parts
    )


def _coerce_evidence(
    values: Iterable[VerificationEvidence] | None,
    label: str,
) -> tuple[VerificationEvidence, ...]:
    if values is None:
        raise TypeError(f"{label} must be a sequence of VerificationEvidence")
    evidence = tuple(values)
    for item in evidence:
        if not isinstance(item, VerificationEvidence):
            raise TypeError(f"{label} must contain VerificationEvidence instances")
        if not isinstance(item.ref, EvidenceRef):
            raise TypeError(f"{label} references must contain EvidenceRef instances")
        if not isinstance(item.label, str) or not item.label.strip():
            raise ValueError(f"{label} evidence labels must be non-empty")
    return evidence


def _check_evidence(
    name: str,
    evidence: tuple[VerificationEvidence, ...],
    status: CheckStatus,
    detail: str,
    wired_check: str,
) -> VerificationCheck:
    return VerificationCheck(
        name=name,
        status=status,
        evidence=evidence,
        detail=f"wired_check={wired_check}; {detail}",
    )


def _symptom_status(evidence: tuple[VerificationEvidence, ...]) -> tuple[CheckStatus, str]:
    if not evidence:
        return CheckStatus.INSUFFICIENT, "no explicit non-recurrence evidence"
    labels = tuple(item.label for item in evidence)
    if any(
        _phrase(label, "recurred", "recurrence", "returned", "reappeared", "regression")
        or _phrase(label, "symptom present", "symptom fail")
        for label in labels
    ):
        return CheckStatus.FAIL, "current symptom recurrence is present"
    if any(
        _phrase(
            label,
            "absent",
            "cleared",
            "gone",
            "non-recurred",
            "non recurrence",
            "nonrecurrence",
            "not recurred",
            "no recurrence",
            "not present",
            "no symptom",
        )
        or _phrase(label, "symptom pass", "symptom resolved")
        for label in labels
    ):
        return CheckStatus.PASS, "explicit non-recurrence evidence is present"
    return CheckStatus.INSUFFICIENT, "evidence does not explicitly establish non-recurrence"


def _invocation_status(evidence: tuple[VerificationEvidence, ...]) -> tuple[CheckStatus, str]:
    if not evidence:
        return CheckStatus.INSUFFICIENT, "no transcript or tool invocation evidence"
    labels = tuple(item.label for item in evidence)
    if any(
        _phrase(label, "not invoked", "never invoked", "not loaded", "never loaded", "not called")
        or _phrase(label, "invocation fail", "invocation missing")
        for label in labels
    ):
        return CheckStatus.FAIL, "artifact was not shown as loaded or called"
    if any(
        _phrase(label, "invoked", "loaded", "called", "triggered")
        or _phrase(label, "invocation pass")
        for label in labels
    ):
        return CheckStatus.PASS, "transcript or tool evidence shows the artifact was loaded or called"
    return CheckStatus.INSUFFICIENT, "evidence does not show the artifact was loaded or called"


def _evidence_key(item: VerificationEvidence) -> tuple[str, int]:
    return item.ref.digest_path, item.ref.source_line


def _outcome_status(
    evidence: tuple[VerificationEvidence, ...],
    invocation_evidence: tuple[VerificationEvidence, ...],
) -> tuple[CheckStatus, str]:
    if not evidence:
        return CheckStatus.INSUFFICIENT, "no independent success or behavioral evidence"
    labels = tuple(item.label for item in evidence)
    if any(
        _phrase(label, "outcome fail", "failed", "failure", "error", "unsuccessful", "regressed")
        for label in labels
    ):
        return CheckStatus.FAIL, "behavioral outcome shows failure"
    passing = tuple(
        item
        for item in evidence
        if _phrase(item.label, "outcome pass", "success", "successful", "prevented", "behavior pass")
    )
    if not passing:
        return CheckStatus.INSUFFICIENT, "evidence does not show a successful behavioral outcome"
    if any(_phrase(item.label, "inventory coverage") for item in passing):
        return CheckStatus.PASS, "independent success is certified by inventory coverage"
    invocation_keys = {_evidence_key(item) for item in invocation_evidence}
    if not any(_evidence_key(item) not in invocation_keys for item in passing):
        return CheckStatus.INSUFFICIENT, "success evidence is not separate from invocation evidence"
    return CheckStatus.PASS, "independent success or behavioral evidence is present"


def _cluster_key(value: str) -> str:
    return "-".join(_WORD_RE.findall(value.casefold()))


def _coverage_evidence(
    records: tuple[CoverageRecord, ...],
) -> tuple[tuple[VerificationEvidence, ...], tuple[VerificationEvidence, ...]]:
    invocation: list[VerificationEvidence] = []
    outcome: list[VerificationEvidence] = []
    for record in records:
        if record.triggered:
            invocation.extend(VerificationEvidence(ref, "invoked (inventory coverage)") for ref in record.evidence)
        if record.prevented is True:
            outcome.extend(VerificationEvidence(ref, "outcome-pass (inventory coverage)") for ref in record.evidence)
        elif record.prevented is False:
            outcome.extend(VerificationEvidence(ref, "outcome-fail (inventory coverage)") for ref in record.evidence)
    return tuple(invocation), tuple(outcome)


def verify_fix(
    entry: LedgerEntry,
    symptom_evidence: Iterable[VerificationEvidence],
    invocation_evidence: Iterable[VerificationEvidence],
    outcome_evidence: Iterable[VerificationEvidence],
    *,
    merged_findings: Iterable[Finding] = (),
    coverage_records: Iterable[CoverageRecord] = (),
) -> FixVerification:
    """Verify symptom absence, invocation, and independent outcome separately.

    ``merged_findings`` and ``coverage_records`` are optional adapters for the
    preceding merge and inventory phases.  A finding for this ledger cluster
    is explicit recurrence evidence; coverage records can supply invocation or
    outcome evidence when their typed references are available.
    """
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
    for finding in findings:
        if not isinstance(finding, Finding):
            raise TypeError("merged_findings must contain Finding instances")
        if _cluster_key(finding.cluster_key) == _cluster_key(entry.cluster_id):
            for ref in finding.evidence:
                if not isinstance(ref, EvidenceRef):
                    raise TypeError("merged_findings evidence must contain EvidenceRef instances")
                symptom.append(VerificationEvidence(ref, "symptom-recurred (merged finding)"))
    for record in records:
        if not isinstance(record, CoverageRecord):
            raise TypeError("coverage_records must contain CoverageRecord instances")
    coverage_invocation, coverage_outcome = _coverage_evidence(records)
    if not invocation:
        invocation.extend(coverage_invocation)
    if not outcome:
        outcome.extend(coverage_outcome)

    symptom_values = tuple(symptom)
    invocation_values = tuple(invocation)
    outcome_values = tuple(outcome)
    symptom_status, symptom_detail = _symptom_status(symptom_values)
    invocation_status, invocation_detail = _invocation_status(invocation_values)
    outcome_status, outcome_detail = _outcome_status(outcome_values, invocation_values)
    symptom_check = _check_evidence("symptom", symptom_values, symptom_status, symptom_detail, entry.wired_check)
    invocation_check = _check_evidence(
        "invocation", invocation_values, invocation_status, invocation_detail, entry.wired_check
    )
    outcome_check = _check_evidence("outcome", outcome_values, outcome_status, outcome_detail, entry.wired_check)
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
    "CheckStatus",
    "FixVerification",
    "VerificationCheck",
    "VerificationEvidence",
    "verify_fix",
]
