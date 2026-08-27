"""Conformance checks: prompt JSON examples must validate with production parsers."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from miner_contract import BatchSpec, ReportValidationError, parse_report
from reconciliation import (
    ReconciliationRequest,
    build_request,
    parse_report as parse_reconciliation_report,
    parse_request,
)
from runtime import Runtime
from test_support import make_manifest


ROOT = Path(__file__).resolve().parents[1]
MINER_PROMPT = ROOT / "references" / "miner-prompt.md"
RECONCILER_PROMPT = ROOT / "references" / "reconciler-prompt.md"


def _json_blocks(markdown: str) -> list[str]:
    blocks = re.findall(r"```json\n(.*?)```", markdown, re.DOTALL)
    return [block.strip() for block in blocks]


def _substitute_placeholders(raw: str) -> str:
    """Replace prompt placeholders with concrete values."""
    replacements = {
        "<run id>": "run-1",
        "<batch id>": "batch-001",
        "<assigned path>": "digest-a.md",
        "<session id>": "session-a",
        "<manifest project>": "fixture-project",
        "<finding id>": "f" * 64,
    }
    for placeholder, value in replacements.items():
        raw = raw.replace(placeholder, value)
    return raw


def test_miner_prompt_example_validates_with_production_parser():
    blocks = _json_blocks(MINER_PROMPT.read_text(encoding="utf-8"))
    assert len(blocks) >= 2, "miner prompt must contain the signal and report examples"
    signal_example = json.loads(blocks[0])
    assert signal_example["source_kind"] == "user"
    report_example = json.loads(_substitute_placeholders(blocks[1]))
    manifest = make_manifest(
        run_id="run-1",
        files=("digest-a.md",),
        lines=(("digest-a.md", 4, "2026-08-23T10:00:00Z", "user"),),
        project="fixture-project",
        sessions=(("digest-a.md", "session-a"),),
    )
    batch = BatchSpec("batch-001", ("digest-a.md",))
    report = parse_report(json.dumps(report_example), manifest, batch)
    assert report.batch_id == "batch-001"
    assert report.findings[0].cluster_key == "repeated-shell-retry"
    assert report.findings[0].evidence[0].source_line == 4


def test_reconciler_prompt_example_validates_with_production_parser():
    blocks = _json_blocks(RECONCILER_PROMPT.read_text(encoding="utf-8"))
    assert len(blocks) >= 1, "reconciler prompt must contain the report example"
    raw = blocks[0]
    # The example lists two distinct findings; give the second placeholder a
    # different identity so the example validates as a real two-member group.
    raw = raw.replace('"<finding id>", "<finding id>"', '"<finding id>", "<second finding id>"')
    raw = _substitute_placeholders(raw)
    raw = raw.replace("<second finding id>", "e" * 64)
    report_example = json.loads(raw)
    from miner_contract import EvidenceRef, Finding, FindingType

    finding_value = Finding(
        cluster_key="repeated shell retry",
        finding_type=FindingType.FAILURE,
        session_id="session-a",
        paraphrase="A command needed repeated retries.",
        occurrence_count=1,
        confidence=0.9,
        evidence=(
            EvidenceRef(
                "digest-a.md",
                4,
                datetime(2026, 8, 23, 10, tzinfo=timezone.utc),
                FindingType.FAILURE,
                "fixture-project",
            ),
        ),
    )
    request = build_request((finding_value,), runtime=Runtime.CLAUDE, run_id="run-1")
    report = parse_reconciliation_report(json.dumps(report_example), request)
    assert report.groups[0].cluster_key == "repeated-shell-retry"
    assert report.groups[0].member_finding_ids == ("f" * 64, "e" * 64)


def test_prompt_examples_are_strict_json_objects():
    for prompt_path in (MINER_PROMPT, RECONCILER_PROMPT):
        for block in _json_blocks(prompt_path.read_text(encoding="utf-8")):
            value = json.loads(_substitute_placeholders(block))
            assert isinstance(value, dict), f"{prompt_path} example must be an object"


def run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    run_all()
