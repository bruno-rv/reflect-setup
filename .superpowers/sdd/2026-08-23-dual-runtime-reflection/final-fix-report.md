# Final fix wave

## RED

Focused regression tests were added before production changes. The expected
failures were observed:

- `test_coverage_model.py`: `CoverageObservation` is not yet implemented.
- `test_miner_contract.py`: `EvidenceRef` has no per-evidence count field.
- `test_install.py`: a missing managed copy file is incorrectly accepted as
  idempotent.
- `test_apply.py`: directory/descendant scopes are incorrectly treated as
  disjoint.
- `test_digest.py`: the fail-closed legacy CLI boundary is not implemented.
- `test_reflect_setup.py`: typed host coverage and continuation/proof fixes are
  not yet wired.

The failures identify missing behavior rather than test syntax or collection
errors.

## GREEN

Implemented and verified the focused fixes:

- Host coverage is typed (`CoverageObservation` or validated
  `CoverageRecord`), inventory IDs/types are checked, eligibility is never
  inferred, and trigger/prevention evidence flows into ledger verification and
  regression scoring.
- The legacy digest writer/CLI is unavailable and directs callers to the typed
  `reflect_setup.py` manifest workflow; `signals_from_line` remains import
  compatible.
- Evidence supports explicit per-evidence occurrence counts, validates finding
  aggregates, and merges only distinct evidence keys.
- Copy installs rehash manifest-listed files and reject tampered, missing, or
  extra files; continuation rejects added/removed in-scope sessions.
- Apply detects directory/descendant scope overlap and canonicalizes proof IDs;
  host-neutral skill metadata and generated-run ignores are corrected.

Fresh checks:

```text
test_apply.py          20 passed
test_coverage_model.py 8 passed
test_digest.py         22 passed
test_evaluate.py       6 passed
test_install.py        16 passed
test_ledger.py         14 passed
test_miner_contract.py 18 passed
test_reflect_setup.py  16 passed
test_runtime.py        13 passed
test_scoring.py        11 passed
test_verification.py   15 passed
python3 -m py_compile scripts/*.py       passed
reflect_setup.py --help                  passed
install.py --help                        passed
temporary copy-install drift check       passed
git diff --check                          passed
```

No repository skill-specific quick validator is present; that optional check
was skipped.
