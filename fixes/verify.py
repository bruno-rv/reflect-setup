#!/usr/bin/env python3
"""Verify the fix artifact for one approved reflect-setup cluster.

Checks: artifact directory exists, contains the expected files, each file is
non-empty, and the content carries the cluster's fix marker. Prints the
`<declared command> :: PASS` line the Apply proof requires.

Usage: python3 fixes/verify.py <cluster_key>
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"

EXPECTED_FILES: dict[str, tuple[str, ...]] = {
    "safety-classifier-unavailable": ("policy.md",),
    "read-before-mutation": ("claude-rule.md",),
    "safety-classifier-denied": ("policy-note.md",),
    "edit-target-not-found": ("agent-rule.md",),
    "command-timeout": ("timeout-policy.md",),
    "missing-project-path": ("path-rule.md",),
    "blocked-sleep-wait": ("dispatch-rule.md",),
    "model-provider-unavailable": ("provider-policy.md",),
    "missing-visible-response": ("visible-response-rule.md",),
    "shell-glob-no-match": ("glob-rule.md",),
}

MARKERS: dict[str, str] = {
    "safety-classifier-unavailable": "Fix applied",
    "read-before-mutation": "Read before mutation",
    "safety-classifier-denied": "no change",
    "edit-target-not-found": "recompose ONE minimal edit",
    "command-timeout": "Command timeouts",
    "missing-project-path": "Verify a path exists",
    "blocked-sleep-wait": "VERBATIM",
    "model-provider-unavailable": "Provider migration",
    "missing-visible-response": "user-visible output",
    "shell-glob-no-match": "Glob results",
}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python3 fixes/verify.py <cluster_key>")
        return 2
    cluster = sys.argv[1]
    expected = EXPECTED_FILES.get(cluster)
    if expected is None:
        print(f"unknown cluster: {cluster}")
        return 2
    target = ARTIFACTS / cluster
    if not target.is_dir():
        print(f"missing artifact directory: {target}")
        return 1
    for name in expected:
        path = target / name
        if not path.is_file():
            print(f"missing artifact file: {path}")
            return 1
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            print(f"empty artifact file: {path}")
            return 1
        if MARKERS[cluster] not in content:
            print(f"artifact {path} lacks marker {MARKERS[cluster]!r}")
            return 1
    print(f"python3 fixes/verify.py {cluster} :: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
