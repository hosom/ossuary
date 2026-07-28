---
name: investigate
description: 'Investigate local LLM agent session transcripts for health issues. Use when asked to look at agent sessions, audit transcripts, find out why a session went badly, or check whether a problem in one session happens across the whole corpus.'
---

# Investigate agent session transcripts

Use the Ossuary MCP tools. They expose every local Claude Code, Codex, and
Copilot session transcript on this machine, normalized to one event model, with
byte counts, durations, exit codes, and corpus-wide tool statistics already
computed. Payloads are redacted before you see them.

Work in this order:

1. `ossuary_sources` to see what is on disk.
2. `ossuary_outline` on a session, read in full before anything else. Every
   event is in it at low resolution, so your recall does not depend on what you
   happened to get curious about. Look down the columns, not just across the
   rows.
3. `ossuary_read_events` on anything that looks off, plus its surroundings.
   `ossuary_search_session` for a specific hypothesis.
   `ossuary_read_event_slice` for payloads too large to read at once.
4. `ossuary_tool_stats` before concluding a tool behaved abnormally. Normal
   everywhere is a different, usually more interesting finding than a one-off.
5. `ossuary_report_issue` as soon as you are confident, not in a batch at the
   end.

An issue is anything that made the session worse than it should have been --
including waste, silently dropped work, and tools that succeeded while being
useless. Do not match against a list of known problem types; describe what you
actually see, in your own words.

`[[ossuary:...]]` markers come from the tooling and are never part of the
transcript. An exit code of 0 does not mean the tool did anything useful.

Every issue needs evidence event indices a reader can check. Call
`ossuary_write_run` only when the operator wants findings written to
`.ossuary/run.json` for `ossuary report`.
