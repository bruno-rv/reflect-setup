#!/usr/bin/env python3
"""Write the fix artifact for one approved reflect-setup cluster.

Each artifact is the durable, installable content for the cluster's fix:
a rule snippet, policy note, or install record. The host installs artifacts
to their real homes (~/.claude/CLAUDE.md, ~/.codex/agents/*.toml, settings)
after the Apply pass validates them.

Usage: python3 fixes/fix.py <cluster_key>
Idempotent: overwrites the artifact directory for the cluster.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"

ARTIFACTS_SPEC: dict[str, dict[str, str]] = {
    "safety-classifier-unavailable": {
        "policy.md": """# safety-classifier-unavailable — fix record (2026-08-27)

Root cause: auto-mode safety classification is model-backed. With
`permissions.defaultMode: auto`, Claude Code asks a model to determine the
safety of Bash/Edit actions; when that model is unavailable or rate-limited
(OpenRouter backend, `stealth/ox-alpha` until 2026-08-27), every gated action
fails with "temporarily unavailable, so auto mode cannot determine the safety
of Bash right now". 662 occurrences across 23 sessions and 11 projects in the
2026-08-27 run.

Fix applied:
- Settings migrated from `stealth/ox-alpha` to `anthropic/claude-*` models on
  2026-08-27 (see ~/.claude/settings.json.bak-2026-08-27 for the prior state).
- Retry policy: on "temporarily unavailable", wait briefly and retry the same
  action with backoff (2-3 attempts) before changing approach.
- Fallback policy: on UNAVAILABILITY only, the host may ask the user for
  explicit approval to run the action. NEVER auto-approve on a classifier
  DENIAL — denial and unavailability are distinct (see
  safety-classifier-denied/policy-note.md).

Verification: rerun reflect-setup after 2 weeks; this cluster's occurrence
count must drop materially (provider migration) and the remaining
occurrences must be transient rate-limits, not sustained outages.
""",
    },
    "read-before-mutation": {
        "claude-rule.md": """## Read before mutation (reflection 2026-08-27)

A file must be Read in this turn before it is Edited or Written. Edit/Write
against a target that was not read first is a bug: the harness rejects it and
the turn is wasted. When an edit is rejected for a stale or unread target,
re-read the exact file region and recompose the edit from the fresh content.
This rule applies to the main agent and to every subagent prompt (subagents do
not inherit CLAUDE.md — state the rule verbatim in dispatch prompts).
""",
    },
    "safety-classifier-denied": {
        "policy-note.md": """# safety-classifier-denied — policy note (2026-08-27)

Investigation result: denials ("Permission for this action was denied by the
Claude Code auto mode classifier. Reason: Blocked by classifier.") are the
classifier working as designed. 25 occurrences across 12 sessions and 11
projects; the sampled denials show no pattern of over-blocking.

Policy: no change. Denials are final for the action as phrased — do not
retry the identical action, do not auto-approve, and do not attempt to
bypass. If a denial looks wrong, rephrase the action with a narrower scope or
ask the user. This is distinct from safety-classifier-unavailable (outage),
which has its own retry policy.
""",
    },
    "edit-target-not-found": {
        "agent-rule.md": """## Edit target absent (reflection 2026-08-27)

When an edit fails because the target text is absent ("String to replace not
found"), the anchor is stale or the file drifted. Re-read the exact current
file region and recompose ONE minimal edit from the fresh content; never
resend the stale anchor, never guess a new anchor from memory. This extends
the 2026-08-23 worker.toml rule (reread/recompose after patch context
failure) from implementation agents to ALL agents: document_reader, explorer,
planner, summarizer, web_researcher, worker, and every Claude-side dispatch.
""",
    },
    "command-timeout": {
        "timeout-policy.md": """## Command timeouts (reflection 2026-08-27)

Long-running commands (workspace scans, review commands, background
automation) must not run unbounded in the foreground. Policy:
- Commands expected to exceed ~10 minutes: run in background with a hard
  timeout and a periodic liveness check (heartbeat rule, CLAUDE.md).
- Review/scan commands that repeatedly time out: split into smaller scoped
  runs rather than retrying the same oversized command.
- On timeout, report the timeout explicitly and the partial state; do not
  silently re-run the identical command unchanged.
""",
    },
    "missing-project-path": {
        "path-rule.md": """## Path existence before use (reflection 2026-08-27)

Verify a path exists (glob/read) before cd-ing into it, reading it, or
building commands on it. When a referenced path is missing, search for the
real location (the file may have moved, been renamed, or live in a worktree)
before reporting failure. Never assume a path from memory — confirm against
the current tree.
""",
    },
    "blocked-sleep-wait": {
        "dispatch-rule.md": """## No sleep-polling (reflection 2026-08-27)

The harness blocks foreground sleep-based waiting. NEVER sleep-poll
(sleep N then check); use run_in_background, Monitor, or event-driven waits.
This rule must be stated VERBATIM in every subagent dispatch prompt —
subagents do not inherit CLAUDE.md, and the 2026-07-20 CLAUDE.md rule alone
did not stop the pattern (25 occurrences Aug 8-25, 16 sessions, 9 projects).
""",
    },
    "model-provider-unavailable": {
        "provider-policy.md": """# model-provider-unavailable — fix record (2026-08-27)

Root cause: identical error class to safety-classifier-unavailable — the
auto-mode classifier's backing model ("claude-sonnet-5[1m]",
"stealth/ox-alpha[1m]") is temporarily unavailable or rate-limited through
the OpenRouter backend. 318 occurrences, concentrated in 9 sessions and 6
projects (Aug 2-25).

Fix applied:
- Provider migration on 2026-08-27: `stealth/ox-alpha` → `anthropic/claude-*`
  (settings.json; prior state in settings.json.bak-2026-08-27).
- Retry with backoff on rate-limit/unavailable errors; fail-fast to the
  alternate configured model when the primary is down.

Correlation: missing-visible-response (blank turns, Aug 21-23) overlaps this
failure window — provider failures produced silent blank turns. See
missing-visible-response/visible-response-rule.md.
""",
    },
    "missing-visible-response": {
        "visible-response-rule.md": """## Visible output every turn (reflection 2026-08-27)

Every turn must end with user-visible output. When a provider or tool
failure would otherwise produce a blank turn, emit an explicit error line
naming the failure and the next step. Blank turns (56 occurrences Aug 21-23,
8 sessions, 8 projects) correlate with provider unavailability — a silent
failure is worse than a stated one because the user cannot tell whether work
is progressing.
""",
    },
    "shell-glob-no-match": {
        "glob-rule.md": """## Glob results before use (reflection 2026-08-27)

Check glob results before building commands on them. A glob that matches
nothing is an error to investigate (path moved, renamed, or never existed) —
not a silent pass. When a glob fails, search for the real path and report
what was found. Applies to shell globs and tool globs alike.
""",
    },
}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python3 fixes/fix.py <cluster_key>")
        return 2
    cluster = sys.argv[1]
    spec = ARTIFACTS_SPEC.get(cluster)
    if spec is None:
        print(f"unknown cluster: {cluster}")
        return 2
    target = ARTIFACTS / cluster
    target.mkdir(parents=True, exist_ok=True)
    for name, content in spec.items():
        (target / name).write_text(content, encoding="utf-8")
        print(f"wrote {target.relative_to(ROOT.parent)}/{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
