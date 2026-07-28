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

## What to do when a format changes

1. Add a fixture to `tests/golden/` showing the new shape.
2. Change the one adapter. Nothing downstream of `NormalizedEvent` should need
   to move.
3. If the *normalized* shape has to change, bump `SCHEMA_VERSION` in
   `models.py`. It is part of the issue cache key, so bumping it re-derives
   stored artifacts instead of silently mixing schemas.

Never make a parser stricter to handle a format change. Whatever is on disk is
the only record that will ever exist of the session it describes.
