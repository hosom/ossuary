---
name: investigate
description: 'Investigate local LLM agent session transcripts for health issues. Use when asked to look at agent sessions, audit transcripts, find out why a session went badly, or check whether a problem in one session happens across the whole corpus.'
---

# Investigate agent session transcripts

Use the Ossuary MCP tools. They expose every local Claude Code, Codex,
Copilot and pi session transcript on this machine, normalized to one event model, with
byte counts, durations, exit codes, and corpus-wide tool statistics already
computed. Payloads are redacted before you see them.

## One session per context window

Start with `ossuary_sources` to see what is on disk. Then:

- **One session** — investigate it yourself, following the method below.
- **More than one** — delegate one `session-investigator` agent per session,
  passing each a single session id, and run them concurrently. Do not read a
  second outline into your own context.

This is not a performance optimisation. Outlines are large, and two of them in
one context window means the second session is read by an agent already holding
opinions about the first — you start pattern-matching session twelve against
what went wrong in session seven. Findings stop being independent, and once the
window fills, recall degrades without anything telling you it did.

The delegated agents share this session's recording buffer, so what they report
accumulates in one place. When they finish, you do the corpus-level work they
structurally cannot: `ossuary_known_clusters`, then `ossuary_propose_cluster` to
group by underlying cause rather than surface wording, reusing an existing
cluster id when it is the same failure mode.

## How to investigate one session

1. `ossuary_outline` on the session, read in full before anything else. Every
   event is in it at low resolution, so your recall does not depend on what you
   happened to get curious about. Look down the columns, not just across the
   rows.
2. `ossuary_read_events` on anything that looks off, plus its surroundings.
   `ossuary_search_session` for a specific hypothesis.
   `ossuary_read_event_slice` for payloads too large to read at once.
3. `ossuary_tool_stats` before concluding a tool behaved abnormally. Normal
   everywhere is a different, usually more interesting finding than a one-off.
4. `ossuary_report_issue` as soon as you are confident, not in a batch at the
   end.

An issue is anything that made the session worse than it should have been --
including waste, silently dropped work, and tools that succeeded while being
useless. Do not match against a list of known problem types; describe what you
actually see, in your own words.

`[[ossuary:...]]` markers come from the tooling and are never part of the
transcript. An exit code of 0 does not mean the tool did anything useful.

Every issue needs evidence event indices a reader can check. Call
`ossuary_write_run` only when the operator wants findings written to
`.ossuary/run.json` for `ossuary report`, and pass your own harness and model
as `investigator` — Ossuary cannot see which model is on this end of the tools,
so the report credits whoever you say did the work, or nobody.
