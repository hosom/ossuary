# Transcript formats as found on disk

These formats are undocumented internal implementation details of the CLIs that
write them. They change between releases and carry no version field. This file
records what was actually observed, what was derived from source, and what is
still unverified — so the next person can tell the difference.

Verified date: 2026-07-28.

---

## Claude Code — verified against real files

**Layout**

```
~/.claude/projects/<project-slug>/<session-id>.jsonl
```

`<project-slug>` is the working directory with every non-alphanumeric character
replaced by `-` (`/home/user/ossuary` → `-home-user-ossuary`).

**There is no `sessions/` subdirectory under `projects/`.** The brief noted that
sources disagree on this. On disk the `.jsonl` files sit directly in the project
directory. `~/.claude/sessions/` does exist but holds unrelated small JSON files
(`621.json`), not transcripts. The adapter globs `**/*.jsonl` recursively rather
than hardcoding a depth, so either layout works.

`CLAUDE_CONFIG_DIR` overrides `~/.claude` if set.

**Line types observed**

| `type` | Meaning |
|---|---|
| `user` | User message, or a tool result being returned to the model |
| `assistant` | Model response |
| `attachment` | Injected context — skill listings, tool deltas, MCP instructions |
| `queue-operation` | A prompt being enqueued or dequeued |
| `last-prompt` | Bookkeeping pointer to the most recent prompt |
| `summary` | Conversation summary (seen in other versions) |

All of them are kept as events. A `queue-operation` that enqueues and never
dequeues is a health signal; dropping "uninteresting" line types would discard it.

**Structure notes that shaped the adapter**

1. **One line can be several events.** `message.content` is either a string or a
   list of blocks (`text`, `tool_use`, `tool_result`, `thinking`). A line with
   one text block and two `tool_use` blocks becomes three `NormalizedEvent`s.
   `index` is the ordinal over events, not lines.

2. **`tool_use` and `tool_result` are on separate lines and are not adjacent.**
   Results arrive in completion order, not call order. In the observed session,
   the result for `toolu_01CUW…` appeared *after* an unrelated later call.
   Pairing is strictly by `tool_use_id`. Anything positional is wrong.

3. **`toolUseResult` is a sibling of `message`,** not inside it. It holds the
   harness's structured record. For `Bash` its keys are `stdout`, `stderr`,
   `interrupted`, `isImage`, `noOutputExpected`. Other tools carry different
   keys. The adapter reads the *block's* `content` as `text` — that is what the
   model actually saw — and summarises `toolUseResult` into `meta`.

4. **The result line does not name the tool.** The name is recovered by joining
   on `tool_use_id`. Results with no matching call keep `tool_name=None`, are
   flagged `orphan_result`, and are counted under an explicit `<unknown>` bucket
   rather than being attributed to a neighbouring tool.

5. **Thinking blocks are often stored with a signature and no text**
   (`{"type":"thinking","thinking":"","signature":"CAIStQQ…"}`). This is the CLI
   not persisting reasoning, not the model failing to reason. Flagged as
   `thinking_signature_only` and shown as `S` in the outline so the distinction
   survives to the agent.

### Discrepancy: `duration_ms` is usually not recorded

`ShapeRecord.duration_ms` matters — the brief's own example (exit code 0, empty
body, 30-second duration) depends on it. But **Claude Code does not write a
`durationMs` for `Bash` results**; the observed `toolUseResult` has no timing
field at all. Some other tools do record one.

Rather than leave the field mostly empty, it is **derived** from the wall-clock
gap between the `tool_use` line's timestamp and its `tool_result` line's
timestamp, and `ShapeRecord.duration_source` records which happened:

- `recorded` — the CLI supplied it
- `derived` — computed from timestamps; includes any time the harness spent
  elsewhere between the two lines
- `unavailable` — no timestamps to work from

Derived durations are marked `~` in the outline and the legend explains what
they mean. The agent is never told a derived number came from the tool.

### Discrepancy: exit codes are rarely present

`Bash` results carry no exit code in the observed data — only `is_error` on the
block and `interrupted`/`stderr` in `toolUseResult`. `_exit_code_for` returns a
code only when one was genuinely recorded and **never synthesises one from an
error flag**: a fabricated `1` would be indistinguishable from a real one, and
the agent reasons about exactly this field. Error signals land on
`has_error_field` instead.

---

## Codex — derived from source, not observed

No `~/.codex` directory existed on the machine this was written on, so this
adapter is written against the Codex source rather than against real files.

**Layout**

```
~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl
~/.codex/archived_sessions/…
```

`CODEX_HOME` overrides `~/.codex`.

**Line schema** — from `RolloutLine` / `RolloutItem` in
`codex-rs/protocol/src/protocol.rs`:

```rust
pub struct RolloutLine {
    pub timestamp: String,
    pub ordinal: Option<u64>,
    #[serde(flatten)] pub item: RolloutItem,
}

#[serde(tag = "type", content = "payload", rename_all = "snake_case")]
pub enum RolloutItem {
    SessionMeta, ResponseItem, InterAgentCommunication,
    InterAgentCommunicationMetadata, Compacted, TurnContext, WorldState, EventMsg,
}
```

so each line is `{"timestamp": …, "ordinal": N, "type": "response_item",
"payload": {…}}`.

`ResponseItem` (`codex-rs/protocol/src/models.rs`) is itself `#[serde(tag =
"type")]` with variants `message`, `agent_message`, `reasoning`,
`local_shell_call`, `function_call`, `tool_search_call`, `function_call_output`,
and more.

**Two shapes that differ from Claude Code**

- `function_call.arguments` is a **JSON string**, not an object — the Responses
  API serialises it that way and Codex stores it verbatim. Kept unchanged so the
  outline shows what the model emitted.
- `function_call_output.output` is **untagged**: either a bare string or
  `{"body": …, "success": bool}` where `body` is a string or a list of content
  items. All spellings are handled; `success: false` sets `has_error_field`.

`session_meta` may appear after other lines, so the resolved session id is
stamped across every event at the end of parsing rather than left mixed.

---

## Copilot — the picture has changed since the brief

The brief says Copilot chat history lives in VS Code `workspaceStorage` as JSON
rather than JSONL, and that the Copilot CLI is a separate thing. That is still
half right, but **the Copilot CLI now writes JSONL**, which makes it structurally
close to the other two:

```
~/.copilot/session-state/<session-id>/events.jsonl      (current, since v0.0.342)
~/.copilot/session-state/<session-id>/workspace.yaml
~/.copilot/history-session-state/                       (legacy)
```

VS Code Copilot Chat remains the different-in-kind case — one JSON *document* per
session with a `requests` array, not a line-per-event log:

```
~/.config/Code/User/workspaceStorage/<hash>/chatSessions/<uuid>.json   (Linux)
~/Library/Application Support/Code/…                                    (macOS)
%APPDATA%/Code/User/workspaceStorage/…                                  (Windows)
```

Also searched: Code - Insiders, VSCodium, Cursor.

The adapter handles both, dispatching on what it finds. Neither was available on
this machine, so **this adapter is unverified against real data.** It is written
defensively: unrecognised structures become events carrying the raw JSON with
`parse_error` set. A whole VS Code document that fails to parse becomes a single
`unparseable` event rather than an empty session, because an empty session reads
downstream as "nothing happened here" — a very different and much more
misleading claim than "this file could not be read".

---

## pi — derived from source and from pi's own writer

No `~/.pi` existed on the machine this was written on either, so this adapter is
written against pi's sources and its shipped `docs/session-format.md`. The golden
fixture goes one step further than the Codex one: it was generated by pi's own
`SessionManager` and then damaged by hand, so the shapes are authentic to the
writer even though no real conversation was observed.
[`pi-investigation.md`](pi-investigation.md) records the full study.

**Layout**

```
~/.pi/agent/sessions/--<cwd>--/<timestamp>_<uuid>.jsonl
```

`<cwd>` is the working directory with the leading separator stripped and every
`/`, `\` and `:` replaced by `-`. `PI_CODING_AGENT_DIR` overrides `~/.pi/agent`
and `PI_CODING_AGENT_SESSION_DIR` overrides the sessions directory; a `sessionDir`
in pi's `settings.json` does the same and is not read here.

The project comes from the header's `cwd`, not from the directory name: `-` is
not reversible back into `/`, `\` or `:`.

**Line schema**

Line one is a header (`{"type":"session","version":3,"id":…,"cwd":…}`). Every
later line is an entry with `type`, `id` (8-char hex), `parentId` and an ISO
`timestamp`. Entry types are `message`, `model_change`, `thinking_level_change`,
`compaction`, `branch_summary`, `custom`, `custom_message`, `label` and
`session_info`; all are kept, as messages or as `meta` events.

A `message` entry holds an `AgentMessage` whose `role` is one of `user`,
`assistant`, `toolResult`, `bashExecution`, `custom`, `branchSummary` or
`compactionSummary`. Assistant content is a list of `text`, `thinking` and
`toolCall` blocks, so one line becomes several events. Results pair to calls by
`toolCallId`.

### The tree: file order is not conversation order

pi sessions branch **in place**. `/tree` and `/rewind` move the leaf back to an
earlier entry and append from there, so a file can contain entries that are on no
path at all — the record of an approach that was tried and abandoned.

The live conversation is the walk from the last entry in the file back to the
root along `parentId`; pi's own leaf on load is that same last entry. The adapter
emits **every** entry in file order regardless, and marks the ones that are not on
that walk with `off_path` in `meta` and a `B` in the outline. Emitting only the
active path would delete the evidence that a rewind happened at all, and a
session missing five events between two adjacent indices reads downstream as a
session where those things never occurred.

### Discrepancy: durations are derived, exit codes are almost never present

pi records no per-tool duration. Each message carries a Unix-ms `timestamp`
alongside the entry's ISO one, so durations are the gap between an assistant
message and its `toolResult`, marked `derived`.

Both spellings are UTC on disk, and `Adapter.parse_timestamp` returns every
timestamp as timezone-aware UTC for exactly this reason: reading the epoch-ms
number as local time put a session's meta rows hours away from its conversation
rows while both described the same instant, and mixing an aware time with a naive
one raised rather than measured when a duration spanned the two. The outline says
`time is UTC.` in its legend.

The only genuinely recorded exit code in a pi transcript is on `bashExecution` —
the user's own `!command` — which the adapter splits into a call and a result so
the code lands on a shape record. The bash *tool* reports failure by appending
`Command exited with code N` to the payload text; that is prose, not a field, and
`_exit_code_for`-style parsing of it would fabricate a number indistinguishable
from a recorded one. `has_error_field` carries the signal instead.

### pi says how much it truncated

Tool output is cut at 2000 lines or 51200 bytes, the payload gets a marker
(`[Showing lines 1-41 of 9000 …]`), and `details.truncation` holds the exact
numbers — `totalBytes`, `totalLines`, `truncatedBy`, `maxBytes`. No other
supported CLI does this. The record is carried into `meta` verbatim, so "this
payload looks capped" becomes "this payload was capped, from 900000 bytes, by the
byte limit".

pi's marker and `[[ossuary:elided N of M bytes]]` are different claims — the CLI
cut it before the model saw it, we cut it before the agent saw it — and both may
appear on one payload. Neither is stripped.

### On-disk versions

The header carries `version`. pi migrates old files to v3 **and rewrites them** on
load, so anything a recent pi has opened is v3. Files it has not opened can still
be:

- **v1** — linear, no `id`/`parentId`, compaction points at `firstKeptEntryIndex`.
  A session with no ids cannot branch, so nothing is marked `off_path`.
- **v2** — the tree, with the extension message role spelled `hookMessage`.

---

## What to do when a format changes

1. Add a fixture to `tests/golden/` showing the new shape.
2. Change the one adapter. Nothing downstream of `NormalizedEvent` should need
   to move.
3. If the *normalized* shape has to change, bump `SCHEMA_VERSION` in
   `models.py`. It is part of the issue cache key, so bumping it re-derives
   stored artifacts instead of silently mixing schemas.

Never make a parser stricter to handle a format change. Whatever is on disk is
the only record that will ever exist of the session it describes.
