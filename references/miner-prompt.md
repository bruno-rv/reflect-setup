# Canonical miner prompt

You are mining pre-extracted digests for recurring friction signals. You
will be given one batch of digest files produced by `digest.py`. Each
digest already contains only three kinds of line -- user-role text,
`is_error:true` tool results, and interrupt markers -- never assistant text
and never non-error tool output. Do not re-open the raw `.jsonl`
transcripts; the digests are the source of truth for this batch.

## Signal types

- **Correction** — user says "no," "that's wrong," "actually," or
  re-explains something already stated in CLAUDE.md, a skill, or an agent
  config.
- **Friction** — a repeated manual step, or the same multi-turn
  back-and-forth recurring across sessions.
- **Failure** — a tool-call error, retry, permission denial, or
  abandoned/reverted work.
- **Complaint** — user explicitly says some version of "this keeps
  happening" / "again?"

`[interrupt]` digest lines are context for Failure/Friction, not a
standalone signal type on their own.

## False-positive rules (binding)

- Only treat `[user]` and `[error]` digest lines as signal candidates.
- Never match a string that appears *inside* file contents being read or
  written. `digest.py` already strips non-error tool output at the source —
  if a batch somehow still contains something that looks like raw file
  content, discount it rather than counting it as a signal.
- Common traps to actively discount:
  - error-looking strings that are source code (e.g. `raise
    ValueError("Error: ...")`, log format strings) — these should never
    have survived into a digest; flag it if one slips through instead of
    treating it as a real failure.
  - skill-listing / tool-catalog boilerplate quoted back at the user.
  - `<task-notification>` / background-agent-completion boilerplate — this
    is auto-generated and delivered as user-role text, but it is not
    something the user said or did.

## Dedupe

Within your batch, collapse near-duplicate occurrences of the same
underlying signal into one line with a count, rather than listing every
occurrence separately.

## Report format

For each retained signal:

```
- <session-basename> | <date> | <type> | <one-line paraphrase, no verbatim sensitive content> | count: <n>
```

End your report with a 2-line theme summary: the one or two dominant
patterns in this batch, in plain language, for the main agent to fold into
cross-batch clustering.
