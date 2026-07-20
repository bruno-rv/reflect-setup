# reflect-setup

Claude Code skill that runs a diagnostic-only scan of your `.claude/projects/` session
transcripts to find recurring friction — repeated corrections, manual workarounds, failures,
explicit complaints — and ranks improvement candidates (new skill / automation / fix / nothing)
with cited evidence from the sessions that surfaced them. It never edits or fixes anything itself;
it only proposes.

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

`reflection-notes.md` is generated locally by each run and is gitignored — it holds personal,
session-derived data and never gets committed or pushed.
