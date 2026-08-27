# Canonical reconciler prompt

You reconcile one run's merged findings into named clusters. The
`reconciliation-request.json` file is the source of truth for this run.
Never reopen the raw `.jsonl` transcripts or the digest files, and never
return prose outside the JSON object below.

## Input

The request contains `schema_version`, `runtime`, `run_id`, and one `items`
entry per merged finding. Each item has:

- `finding_id`: the deterministic SHA-256 identity of the finding, bound to its
  manifest-validated evidence, classification, proposed key, runtime, and
  session.
- `proposed_key`: the miner's normalized cluster key for that finding.
- `finding_type`: `correction`, `friction`, `failure`, or `complaint`.
- `paraphrase`: the miner's one-line description of the signal.
- `occurrence_count`: how many distinct evidence references the finding
  aggregates.
- `projects`: the explicit project identities cited by the finding.
- `first_seen` / `last_seen`: the UTC evidence window.

## Rules

- Group findings that describe the same recurring problem under one
  `cluster_key`, even when their proposed keys differ. Different names for the
  same root cause are exactly what you are here to reconcile.
- Keep findings separate when the symptoms look similar but the causes differ.
  A generic failure symptom with different paraphrases is two clusters, not
  one.
- Every `finding_id` from the request must appear in exactly one group.
  Never invent, drop, duplicate, or rename a `finding_id`.
- `cluster_key` must be a kebab slug: lowercase letters, digits, and single
  hyphens only (`[a-z0-9]+(?:-[a-z0-9]+)*`). Choose the most specific stable
  name for the cause.
- `summary` is one line and describes the recurring problem, not the fix.
- `rationale` is one line and names the evidence or reasoning that justifies
  the grouping (for example, which paraphrases or projects agree).
- You may group or rename findings, but you cannot change counts,
  classifications, projects, or evidence. The request's `finding_id` values
  are the only identities the run accepts.

## Required JSON output

Return exactly one JSON object with exactly these top-level fields:

```json
{
  "schema_version": 1,
  "runtime": "claude",
  "run_id": "<run id>",
  "groups": [
    {
      "cluster_key": "repeated-shell-retry",
      "summary": "A command needed repeated retries across projects.",
      "rationale": "Both findings cite the same retry loop with matching evidence.",
      "member_finding_ids": ["<finding id>", "<finding id>"]
    }
  ]
}
```

Rules for the fields:

- `schema_version` is `1`; `runtime` is exactly `claude` or `codex` and must
  match the request.
- `run_id` must exactly equal the request's `run_id`.
- `groups` is an array of one or more objects. Each group has exactly
  `cluster_key`, `summary`, `rationale`, and `member_finding_ids`.
- `member_finding_ids` is a non-empty array of finding IDs copied exactly
  from the request. The union of all groups must equal the request's item set
  exactly once each.
- `cluster_key` is a non-empty kebab slug. Normalized cluster keys must be
  unique across groups.
- `summary` and `rationale` are non-empty, one line each, and never copy raw
  transcript text.

## Claude dispatch

For Claude, return the JSON contract above with `runtime: "claude"` and the
provided `run_id`. The same strict field, identity, and no-raw-transcript
rules apply. Return the JSON object only.

## Codex dispatch

For Codex, return the JSON contract above with `runtime: "codex"` and the
provided `run_id`. The same strict field, identity, and no-raw-transcript
rules apply. Return the JSON object only.
