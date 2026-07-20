# reflect-setup — Reference

Full detail behind each `SKILL.md` workflow step. This file links nowhere deeper — it and
`references/miner-prompt.md` are the only two hops from `SKILL.md`.

## Scope

Parse `$ARGUMENTS` for an optional day window (default: **last 30 days**; an explicit window in
`$ARGUMENTS` overrides this) and an optional project-path filter. The last-run date in
`reflection-notes.md`, if present, is still used for the "Since last run" comparison in the notes
step — it just no longer sets the mining window itself. State the resolved scope at the top of
your output.

## Inventory

Before mining anything, catalog current coverage so you never propose something that's already
built:

- Skills: `~/.claude/skills/*/SKILL.md`, `.claude/skills/*/SKILL.md`
- Commands (legacy): `~/.claude/commands/*.md`, `.claude/commands/*.md`
- Subagents: `.claude/agents/*.md`
- Hooks and permission rules: `.claude/settings.json`, `.claude/settings.local.json`

Keep this as a lookup table for the decide step.

## Digest

`scripts/digest.py` (repo root of this skill, `scripts/` subdir) deterministically streams every
in-scope session `.jsonl` line-by-line and keeps only user-role text, `is_error:true` tool
results, and interrupt markers, so no subagent ever has to stream raw multi-MB transcripts itself
or risk matching noise inside file-read content.

### CLI usage

```bash
python3 scripts/digest.py --projects-dir ~/.claude/projects --since <days> --out <scratch-dir> [--project-filter <substr>]
```

### Flags

| Flag | Required | Default | Meaning |
|---|---|---|---|
| `--projects-dir` | yes | — | Root to scan, e.g. `~/.claude/projects` |
| `--since` | no | `30` | Days back from now |
| `--out` | yes | — | Directory to write digest `.md` files into |
| `--project-filter` | no | none | Substring match on project dir name |

Any directory literally named `memory` is skipped entirely (never descended into) — it holds
auto-memory, not session transcripts. Read the stdout summary line for a sanity count (sessions
scanned, sessions with signal, per-type totals) before proceeding to mining.

## Mining

Partition the resulting digest files in `<scratch-dir>` into batches (by week, or by a fixed file
count — pick whichever keeps each subagent's context manageable; a few dozen digest files per
batch is a reasonable starting point, fewer if individual sessions are signal-heavy) and dispatch
one subagent per batch via the Task tool. Each miner subagent uses the canonical prompt at
`references/miner-prompt.md` in this skill directory, filled in with its digest batch — it
extracts only the signal types and false-positive rules defined there (correction / friction /
failure / complaint), each tagged with session basename, timestamp, and a short paraphrase (never
verbatim sensitive content). Dispatch batches in parallel; there is no dependency between them.

## Clustering

In the main agent, merge subagent outputs into clusters by underlying cause (not surface wording).
For each cluster, record: session list, per-session count, first-seen date, last-seen date.

## Decision thresholds

For each cluster, cross-check against the inventory step, then decide using explicit thresholds:

| Recurrence | Nature of the fix | Verdict |
|---|---|---|
| ≥3 sessions across ≥2 distinct days | Same repeated task/workflow | **New skill** — only if no existing skill covers it |
| ≥3 sessions | Mechanical, deterministic (formatting, repeated bash sequence) | **Automation** (hook or script) |
| Any recurrence | One-line CLAUDE.md rule, config tweak, or permission entry | **Fix** |
| 1–2 sessions, or already covered by an existing skill/command | — | **Nothing** — note why, so it doesn't get re-proposed next run |

Never propose a skill for something that hasn't recurred, even if it looks high-value.

## Fix-wiring verification

For every entry in the local `clusters.yaml` ledger with `status: fix-applied`, check this
window's digests for evidence the fix actually **operates**, per that entry's `wired_check` (e.g.
"digests contain zero 'is the shell stuck' user messages," or "recording_health.py appears
invoked in digests, not just present in the repo"). A fix that never ran, or whose symptom
recurred anyway, gets flagged **built-not-operating** in the notes and re-ranked — built code is
not the same claim as an operating fix.

### Ledger schema and status semantics

Schema: `clusters.example.yaml`. Each entry has an `id` (kebab-slug), `status`, `sessions`,
`last_seen`, and (once a fix is applied) a `wired_check`.

Status values:

- `new` — just clustered this run, not yet decided or acted on.
- `fix-applied` — Apply phase dispatched a fixer and it returned proof; the *next* run's
  fix-wiring verification is what turns this into `resolved`. Applying is not the same claim as
  operating.
- `built-not-operating` — verification found the fix was applied but never actually ran, or its
  symptom recurred anyway. Re-ranked, not silently dropped.
- `resolved` — a fix-wiring verification pass found explicit non-recurrence evidence in-window.
  Never set `resolved` merely because no new evidence was mined that run (absence of evidence is
  not evidence of absence); it requires the digests to positively show the symptom is gone.

Update `clusters.yaml` for every touched entry: bump `last_seen` and `sessions` for anything still
recurring, and move `status` to `resolved` only under the rule above. New clusters from the decide
step get a fresh ledger `id` and `status: new`.

## Notes format

Append (don't overwrite) a dated section to `reflection-notes.md`, ranked most-leverage-first.
Each cluster heading carries its `clusters.yaml` ledger id so notes and ledger stay
cross-referenced:

```markdown
## Run: 2026-07-10 (scope: last 21 days, all projects)

### 1. [Cluster name] (`ledger-id`) — New skill — HIGH
- Recurrence: 5 sessions / 8 occurrences (2026-06-22 → 2026-07-09)
- Evidence: session_abc.jsonl, session_def.jsonl, ... (link or path)
- Build cost: S (single SKILL.md, no new tools)
- Rationale: ...
- Status: new

### 2. ...
```

At the end, add a **Since last run** subsection: what's resolved (no longer recurring), what's
still open, what's newly recurring, and any **built-not-operating** flags from the fix-wiring
verification step.

## Apply phase

Diagnosis-only governs every step above; this phase only runs if the user opts in, and never runs
unasked.

After writing `reflection-notes.md`, use `AskUserQuestion` to ask whether to apply the actionable
fixes from this run. If yes:

1. Dispatch one fixer subagent per actionable cluster, in parallel (independent clusters, no
   shared state).
2. Require each fixer to return **proof** — actual command output, not a claim — that its fix
   works.
3. Append an "Applied `<date>`" addendum to `reflection-notes.md` summarizing what each fixer did.
4. Set the corresponding `clusters.yaml` entries to `status: fix-applied` with a `wired_check`
   defined for each — the next run's fix-wiring verification step is what turns "applied" into
   "verified operating."

Pairs well with a monthly `/schedule` routine that runs this skill unattended and surfaces the
notes.
