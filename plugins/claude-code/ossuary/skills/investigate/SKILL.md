---
description: Investigate local LLM agent session transcripts for health issues -- waste, silently dropped work, capped tool output, wrong turns nobody noticed. Use when asked to look at agent sessions, audit transcripts, find out why a session went badly, or check whether a problem in one session happens everywhere.
---

# Investigate agent session transcripts

You have the Ossuary MCP tools. They give you every local Claude Code, Codex,
and Copilot session transcript on this machine, normalized to one event model,
with byte counts, durations, exit codes, and corpus-wide tool statistics
already computed. Payloads are redacted before you see them.

## How to work

1. `ossuary_sources` to see what is on disk. If the operator named a session,
   an id prefix is enough for every other tool.
2. `ossuary_outline` on a session, and **read it in full before calling
   anything else.** Every event is in there at low resolution, which is what
   makes your recall independent of what you happened to get curious about.
   Look down the columns, not just across the rows: repeated identical byte
   counts, a suspiciously round number, a long duration next to an empty body,
   a gap in the timestamps, a tool called many times in a row.
3. `ossuary_read_events` on anything that looks off, plus enough of its
   surroundings to understand what was happening.
4. `ossuary_search_session` when you have a specific hypothesis to check.
   `ossuary_read_event_slice` for payloads too large to read at once.
5. `ossuary_tool_stats` **before** concluding a tool behaved abnormally. A
   result that looks odd in one session may be normal for that tool everywhere,
   and if it is normal everywhere that is a different and usually more
   interesting finding than a one-off.
6. `ossuary_report_issue` the moment you are confident in a finding. Do not
   save them up.

## What counts as an issue

Anything that made the session worse than it should have been. It may be a
failure, but it may equally be waste, confusion, a misunderstanding, work
silently dropped, a wrong turn the agent never noticed, or a tool that
technically succeeded while being useless. Include problems the agent itself
never noticed, and problems that were recovered from if the recovery cost
something.

Do not report an issue for a session simply being long, or for an agent making
a reasonable choice you would have made differently.

There is deliberately no list of things to look for. Do not try to match what
you see against categories of known problems -- read the transcript, notice
what is strange, and describe it in your own words. The problems worth finding
are the ones nobody thought to look for in advance.

## Reading the instrumentation

Byte lengths, durations, exit codes, and flags are measurements, not verdicts.
They tell you where to look; the transcript tells you what happened.

- A payload marked as not terminating cleanly may have been cut off, or may
  simply end that way. Read it before deciding.
- Text of the form `[[ossuary:...]]` is a marker inserted by these tools --
  elision, redaction, or a tool error. It is never part of the original
  transcript. A payload with no such marker ended exactly the way you see it,
  on disk.
- Durations marked "(derived)" are wall-clock gaps between a call and its
  result, not measurements reported by the tool. Treat them as approximate.
- An exit code of 0 does not mean the tool did anything useful.

## Evidence

Every issue needs `evidence_event_indices` pointing at the events a reader
should look at to check your claim. An issue nobody can verify is not useful.
Set `confidence` honestly: use a low value when you suspect something but the
transcript does not settle it, and say in the description what would settle it.

Assign `phase` to where the problem originates: `prompt` (the instructions the
agent was given), `tool` (a tool returned something wrong, useless, or
malformed), `model` (the model reasoned or wrote badly given what it had),
`harness` (truncation, timeouts, lost state, caps), `user` (the request was
unclear, contradictory, or changed), or `unknown`.

## Finishing

When you have covered the outline, tell the operator what you found in a
sentence or two. Finding no issues is a legitimate result; report it plainly
rather than inventing something.

Only call `ossuary_write_run` when the operator wants the findings persisted --
it writes `.ossuary/run.json`, which `ossuary report` renders as HTML. Until
then nothing is written to disk.
