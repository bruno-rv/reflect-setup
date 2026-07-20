# reflect-setup

Claude Code skill that runs a diagnostic-only scan of your `.claude/projects/` session
transcripts to find recurring friction — repeated corrections, manual workarounds, failures,
explicit complaints — and ranks improvement candidates (new skill / automation / fix / nothing)
with cited evidence from the sessions that surfaced them. Through diagnosis it never edits or
fixes anything itself; it only proposes.

Each run first calls `scripts/digest.py` (stdlib-only) to stream session transcripts line-by-line
and pre-extract only user text, error tool results, and interrupt markers into a scratch
directory — miner subagents then work over those compact digests instead of raw multi-MB
transcripts, using the canonical prompt in `references/miner-prompt.md`. Clusters are tracked
across runs in a local `clusters.yaml` ledger (schema: `clusters.example.yaml`), including a
fix-wiring verification step that flags fixes which were applied but never actually run.
Finally, an optional opt-in Apply phase can dispatch fixer subagents for actionable clusters,
each required to return proof its fix works.

## Structure

```
reflect-setup/
├── SKILL.md              # entry point: quick start + numbered workflow (< 100 lines)
├── REFERENCE.md          # full detail behind every workflow step
├── references/
│   └── miner-prompt.md   # canonical prompt dispatched to miner subagents
├── scripts/
│   ├── digest.py         # pre-extraction digest (stdlib-only)
│   └── test_digest.py    # runnable checks for digest.py
├── clusters.example.yaml # clusters.yaml ledger schema
├── README.md
└── .gitignore
```

## Install

```bash
git clone https://github.com/bruno-rv/reflect-setup.git
ln -s "$(pwd)/reflect-setup" ~/.claude/skills/reflect-setup
```

## Usage

```
/reflect-setup [days] [project-filter]
```

Defaults to the last 30 days across all projects if no arguments are given.

## Notes

`reflection-notes.md` and `clusters.yaml` are generated/updated locally by each run and are
both gitignored — they hold personal, session-derived data and never get committed or pushed.
