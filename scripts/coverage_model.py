"""Typed inventory and operating-coverage state models."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from miner_contract import EvidenceRef


def _frozen_dataclass(cls):
    kwargs = {"frozen": True}
    if sys.version_info >= (3, 10):
        kwargs["slots"] = True
    return dataclass(**kwargs)(cls)


@_frozen_dataclass
class CoverageRecord:
    artifact_id: str
    artifact_kind: str
    declared: bool
    eligible: bool
    triggered: bool
    prevented: bool | None
    evidence: tuple[EvidenceRef, ...]
    detail: str
    trigger_evidence: tuple[EvidenceRef, ...] = ()
    prevention_evidence: tuple[EvidenceRef, ...] = ()


@_frozen_dataclass
class CoverageObservation:
    """Typed host evidence for one declared runtime artifact.

    The host owns eligibility, invocation, and independent outcome collection;
    the Python core only validates the observation and derives its record.
    """

    artifact_id: str
    artifact_kind: str
    eligible: bool
    trigger_evidence: tuple[EvidenceRef, ...]
    prevention_evidence: tuple[EvidenceRef, ...]
    symptom_recurred: bool

    def __post_init__(self):
        if not isinstance(self.artifact_id, str) or not self.artifact_id.strip():
            raise ValueError("artifact_id must be a non-empty string")
        if not isinstance(self.artifact_kind, str) or not self.artifact_kind.strip():
            raise ValueError("artifact_kind must be a non-empty string")
        for name in ("eligible", "symptom_recurred"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool")
        for name in ("trigger_evidence", "prevention_evidence"):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)):
                raise TypeError(f"{name} must be a sequence of EvidenceRef")
            if any(not isinstance(value, EvidenceRef) for value in values):
                raise TypeError(f"{name} must contain EvidenceRef instances")


@_frozen_dataclass
class InventoryItem:
    artifact_id: str
    artifact_kind: str
    path: Path
    declared: bool


@_frozen_dataclass
class Inventory:
    items: tuple[InventoryItem, ...]


def assess_coverage(
    *,
    artifact_id: str = "",
    artifact_kind: str = "",
    exists: bool,
    eligible: bool | None = None,
    trigger_evidence: tuple[EvidenceRef, ...] | list[EvidenceRef] = (),
    prevention_evidence: tuple[EvidenceRef, ...] | list[EvidenceRef] = (),
    symptom_recurred: bool = False,
    observation: CoverageObservation | None = None,
) -> CoverageRecord:
    """Separate declaration, eligibility, invocation, and outcome evidence.

    A positive outcome cannot be inferred from an artifact being present or
    eligible.  It requires both invocation evidence and an independent
    outcome reference.  A recurring symptom is explicit negative evidence and
    takes precedence over any otherwise positive outcome reference.
    """
    if observation is not None:
        if not isinstance(observation, CoverageObservation):
            raise TypeError("observation must be a CoverageObservation")
        artifact_id = observation.artifact_id
        artifact_kind = observation.artifact_kind
        eligible = observation.eligible
        trigger_evidence = observation.trigger_evidence
        prevention_evidence = observation.prevention_evidence
        symptom_recurred = observation.symptom_recurred
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ValueError("artifact_id must be a non-empty string")
    if not isinstance(artifact_kind, str) or not artifact_kind.strip():
        raise ValueError("artifact_kind must be a non-empty string")
    if not isinstance(exists, bool):
        raise TypeError("exists must be a bool")
    if not isinstance(eligible, bool):
        raise TypeError("eligible must be a bool")
    if not isinstance(symptom_recurred, bool):
        raise TypeError("symptom_recurred must be a bool")
    trigger_refs = tuple(trigger_evidence)
    prevention_refs = tuple(prevention_evidence)
    trigger_keys: set[tuple[str, int]] = set()
    for ref in trigger_refs:
        if not isinstance(ref, EvidenceRef):
            raise TypeError("trigger_evidence must contain EvidenceRef instances")
        trigger_keys.add((ref.digest_path, ref.source_line))
    prevention_keys: set[tuple[str, int]] = set()
    for ref in prevention_refs:
        if not isinstance(ref, EvidenceRef):
            raise TypeError("prevention_evidence must contain EvidenceRef instances")
        prevention_keys.add((ref.digest_path, ref.source_line))
    independent = not trigger_keys.intersection(prevention_keys)
    triggered = bool(trigger_refs)
    if symptom_recurred:
        prevented: bool | None = False
        result_detail = "symptom recurred"
    elif triggered and prevention_refs and independent:
        prevented = True
        result_detail = "triggered and independent outcome passed"
    elif trigger_refs and prevention_refs:
        prevented = None
        result_detail = "outcome evidence is not independent"
    else:
        prevented = None
        result_detail = "outcome evidence insufficient"

    states = (
        f"declared={bool(exists)}",
        f"eligible={bool(eligible)}",
        f"triggered={triggered}",
        f"prevented={prevented}",
    )
    return CoverageRecord(
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        declared=bool(exists),
        eligible=bool(eligible),
        triggered=triggered,
        prevented=prevented,
        evidence=trigger_refs + prevention_refs,
        detail=f"{result_detail}; " + ", ".join(states),
        trigger_evidence=trigger_refs,
        prevention_evidence=prevention_refs,
    )


__all__ = [
    "CoverageObservation",
    "CoverageRecord",
    "Inventory",
    "InventoryItem",
    "assess_coverage",
]
