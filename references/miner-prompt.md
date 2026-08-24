# Canonical miner prompt

You mine one assigned batch of pre-extracted digest files for recurring
friction signals. The digest files are the source of truth for this batch.
Never reopen the raw `.jsonl` transcripts, and never return prose outside the
JSON object below.

## Signal types

- `correction`: the user corrects, rejects, or re-explains something already
  stated in project guidance or configuration.
- `friction`: a repeated manual step or recurring multi-turn back-and-forth.
- `failure`: a tool-call error, retry, permission denial, or abandoned/reverted
  work.
- `complaint`: an explicit user complaint that the problem keeps recurring.

Interrupt markers are context for `failure` or `friction`; they are not a
standalone finding type.

Each retained signal line has labeled digest-visible metadata in this stable
shape:

```text
- source_line=7 timestamp=2026-08-23T10:00:00Z kind=user project=project-a session_id=session-a :: signal text
```

`source_line` is the original JSONL source line, not the line number of the
rendered digest file. Copy it exactly; never substitute the digest-local line
number or invent a value. The `timestamp`, `kind`, `project`, and `session_id`
labels are the signal's identity and must remain consistent with the cited
evidence.

Only digest lines with `kind=user` or `kind=error` are signal candidates.
Discount skill-listing boilerplate, background-agent notifications, and any
text that looks like source code or raw file contents.

## Assigned batch

The dispatch provides `runtime`, `run_id`, `batch_id`, and an ordered list of
assigned `digest_paths`. Copy that exact list into the report. Report every
assigned path even when it contains no finding. Do not add paths from another
batch.

## Required JSON output

Return exactly one JSON object with exactly these top-level fields:

```json
{
  "schema_version": 1,
  "runtime": "claude",
  "run_id": "<run id>",
  "batch_id": "<batch id>",
  "digest_paths": ["<assigned path>"],
  "findings": [
    {
      "cluster_key": "repeated-shell-retry",
      "finding_type": "failure",
      "session_id": "<session id>",
      "paraphrase": "A command needed repeated retries.",
      "occurrence_count": 1,
      "confidence": 0.9,
      "evidence": [
        {
          "digest_path": "<assigned path>",
          "project": "<manifest project>",
          "source_line": 4,
          "timestamp": "2026-08-23T10:00:00Z",
          "kind": "failure",
          "occurrence_count": 1
        }
      ]
    }
  ],
  "themes": ["one dominant pattern"]
}
```

Rules for the fields:

- `schema_version` is `1`; `runtime` is exactly `claude` or `codex`.
- `run_id`, `batch_id`, `cluster_key`, `session_id`, and every path are
  non-empty strings. `digest_paths` must exactly equal the assigned list.
- `finding_type` and evidence `kind` are one of `correction`, `friction`,
  `failure`, or `complaint`; evidence `kind` must match its finding type.
- Each finding has exactly `cluster_key`, `finding_type`, `session_id`,
  `paraphrase`, `occurrence_count`, `confidence`, and `evidence` fields. Each
  evidence item has exactly `digest_path`, `project`, `source_line`,
  `timestamp`, `kind`, and `occurrence_count` fields. `project` must be copied
  from the digest manifest metadata for that path; never infer it from a
  digest filename. `finding.occurrence_count` must equal the sum of distinct
  evidence `occurrence_count` values. Repeated evidence references with the
  same path and line are counted once.
- `paraphrase` is non-empty, one line, and paraphrases the signal without
  copying sensitive transcript text.
- `occurrence_count` is a positive integer. `confidence` is a number from
  `0.0` through `1.0`, inclusive.
- Each evidence reference names an assigned digest path, uses a positive
  digest `source_line`, and includes an RFC 3339 timestamp with timezone.
  Evidence is typed, has a positive `occurrence_count`, and must point to the
  signal supporting that finding. Copy `source_line`, timestamp, kind, and
  project exactly from the labeled digest metadata and verify them against the
  manifest evidence index; do not invent metadata or use a digest-local line
  number. Copy the digest `session_id` into the finding's `session_id`.
  Every cited index entry must belong to the finding's `session_id`, and a
  finding must not mix sessions.
- `themes` is an array of zero, one, or two short strings. Do not include more
  than two themes.
- Use `findings: []` when the batch has no retained signals. Do not fabricate
  evidence, paths, sessions, or themes.

## Claude dispatch

For Claude, return the JSON contract above with `runtime: "claude"`, the
provided `run_id`, `batch_id`, and assigned `digest_paths`. The same strict
field, evidence, count, confidence, and no-raw-transcript rules apply. Return
the JSON object only.

## Codex dispatch

For Codex, return the JSON contract above with `runtime: "codex"`, the
provided `run_id`, `batch_id`, and assigned `digest_paths`. The same strict
field, evidence, count, confidence, and no-raw-transcript rules apply. Return
the JSON object only.
