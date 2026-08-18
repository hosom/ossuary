# Supporting Agent Zero — investigation

This is the study behind a possible fifth source, written to the same standard as
[`formats.md`](formats.md) and [`pi-investigation.md`](pi-investigation.md): what
was verified, what was derived from source, and what is still unknown.

**Nothing has shipped.** No adapter exists yet. This document exists so the
decision to write one — and the three decisions that have to be made *before* it
is written — can be made from evidence rather than from a guess.

Verified date: 2026-08-18, against `agent0ai/agent-zero` (also published as
`frdel/agent-zero`) at commit `baadd0d`, `main` as of 2026-08-12, which the docs
call v2.0. Everything below comes from reading that source. **No Agent Zero
install existed on the machine this was written on**, so this is the same
evidence class as the Codex adapter: source, not observation. Where a real file
disagrees with this document, the file wins.

Verdict: **the parse is easy and the discovery is hard.** The format is a single
JSON object per chat with a clean, timestamped event list inside it — closer to
Copilot's VS Code blobs than to anything JSONL. What has no precedent among the
four current sources is that Agent Zero keeps *two* records of the same session
which disagree with each other, throws parts of both away on purpose, and by
default stores the result inside a Docker volume rather than under `$HOME`.

---

## Layout and discovery

```
<agent-zero-root>/usr/chats/<context-id>/chat.json
<agent-zero-root>/usr/chats/<context-id>/backups/pre-compact-<YYYYmmdd-HHMMSS>.json
```

`<agent-zero-root>` is the directory the application was installed into —
`helpers/files.py` computes it from `__file__`, so it is the repository root, and
inside the official image it is `/a0`. **There is no `~/.agent-zero`, no
`AGENT_ZERO_HOME`, and no environment override of any kind.** Every one of the
four current adapters starts from a known directory under `$HOME`; this one
cannot.

It gets worse before it gets better. The documented install is

```bash
docker run -p 80:80 -v a0_usr:/a0/usr agent0ai/agent-zero
```

— a *named Docker volume*. On Linux that is reachable at
`/var/lib/docker/volumes/a0_usr/_data/chats`; on macOS and Windows it is inside
the Docker VM and is not a host path at all. Users who follow the "map a local
directory" section of the install docs (`-v /path/to/work_dir:/a0/usr`) or who
run from source do have an ordinary host path. So the population splits:

| Install shape | Where Ossuary can read chats |
|---|---|
| Source checkout / dev setup | `<checkout>/usr/chats` |
| Bind mount (`-v /host/path:/a0/usr`) | `/host/path/chats` |
| Named volume (the documented default) | Linux: `/var/lib/docker/volumes/a0_usr/_data/chats`. Elsewhere: `docker cp` first |
| A0 Launcher | mounts user data to `/a0/usr`; the host side is the launcher's data directory |

**Recommendation.** Do not guess. `default_roots()` returns
`$OSSUARY_AGENT_ZERO_DIR` (an Ossuary-side variable, since Agent Zero has none)
if set, then `./usr/chats` relative to the current directory, then
`~/agent-zero/usr/chats`, and nothing else — no filesystem sweep, no Docker
socket. Everything else is an explicit path:
`ossuary sources /path/to/usr/chats --source agent-zero`. The docs carry the
`docker cp` recipe. An adapter that finds nothing by default and says so is
better than one that scans `$HOME` looking for a directory named `chats`.

Discovery globs `**/chat.json` under whichever root resolves, plus
`**/backups/*.json`, plus any explicitly named `.json` file (see below).
`session_id` is the containing directory's name — the context id — and for a
backup, the id with the backup's stem appended, because a pre-compaction backup
and the chat it was cut from carry the same `id` field and would otherwise
collide.

`project` comes from `data.project` — the project folder the chat is bound to,
which is Agent Zero's nearest thing to a working directory — falling back to the
context's `name`, which is a human chat title rather than a path. Say which one
it is in `meta`; a title in a column that reads as a cwd everywhere else is a
lie by column position.

### Three other places a session can be

- **Exported chats.** The web UI's export (`api/chat_export.py`) writes exactly
  the same serialization to a file the user names. Those should parse, which
  means `claims()` cannot depend on the filename `chat.json`.
- **Pre-compaction backups.** `/compact` writes the full pre-compaction chat to
  `backups/pre-compact-<ts>.json` *and then destroys the live record* (see
  [Silent losses](#silent-losses)). An adapter that reads only `chat.json` sees
  nothing of a compacted session but its summary. These are not optional.
- **Task contexts.** Scheduled tasks are contexts too and save to the same
  folder. `type` distinguishes them (`user` / `task`). Both are real sessions.
  `background` contexts are never saved at all and are invisible to any tool
  reading disk — worth one line in the docs, because "Ossuary found fewer
  sessions than the UI shows" otherwise reads as a bug.

### `claims()`

`chat.json` is a single JSON object, so `Adapter.head_records` — which reads
JSONL — does not apply. The sniff reads the first few KB and looks for the keys
`_serialize_context` writes first, in order: `id`, `name`, `created_at`, `type`,
`last_message`, `agents`. Requiring `agents` together with `created_at` is
unambiguous.

No collision with the four existing adapters:

- **Copilot** is the only one that claims `.json`, and only under a directory
  named `chatSessions`, `chatEditingSessions`, `chat-sessions` or
  `interactive-sessions`. `usr/chats/<id>/chat.json` matches none of those.
- **Claude Code**, **Codex** and **pi** all glob `*.jsonl`.

So today every Agent Zero chat is discovered by nobody, and adding the adapter
takes no sessions away from another one. The reverse also holds: the sniff above
requires `agents` to be a list of objects with `number` and `history`, which no
other supported format has.

---

## File schema

One JSON object, written atomically (`os.replace` after `fsync`) at the end of
every message-loop iteration by
`extensions/python/message_loop_end/_90_save_chat.py`. A session read while it is
running is therefore consistent but up to one turn stale, and a session whose
process died mid-turn is missing that turn.

```json
{
  "id": "b2e1…", "name": "Fix the failing test",
  "created_at": "2026-08-18T09:14:02.114+02:00",
  "type": "user",
  "last_message": "2026-08-18T09:31:44.902+02:00",
  "agents": [ { "number": 0, "agent_profile": "default", "data": {…},
                "history": "{\"_cls\":\"History\", …}" } ],
  "streaming_agent": 0,
  "agent_profile": "default",
  "log": { "guid": "…", "logs": [ … ], "progress": "…", "progress_no": 41 },
  "data": {…}, "output_data": {…}
}
```

Two things to note before anything else.

**`agents[].history` is a JSON string inside the JSON.** `History.serialize()`
returns `json.dumps(...)`, and `_serialize_agent` stores that string. It is a
second parse, on untrusted content, and it must degrade to a `meta` event
carrying the raw string rather than failing the session.

**`created_at` and `last_message` are in the user's configured timezone**, ISO
with an offset (`Localization.serialize_datetime`). Log item timestamps are
`time.time()` floats — epoch seconds, UTC. `Adapter.parse_timestamp` already
handles both spellings correctly and normalises to UTC, which is exactly the
mixed-awareness case the pi work fixed; there is nothing new to do here, but do
not "helpfully" reinterpret the floats.

### The log item

`log.logs` is a list of `LogItem.output()` dictionaries, in creation order:

```json
{"no": 12, "id": "9f3c…", "type": "tool", "heading": "icon://construction A0: Using tool 'search_engine'",
 "content": "…the tool result…", "kvps": {"query": "…", "_tool_name": "search_engine"},
 "timestamp": 1787041243.882, "agentno": 0}
```

| Field | Notes |
|---|---|
| `no` | the item's ordinal **as it was when written** — see [Silent losses](#silent-losses) |
| `id` | often a uuid, often `null`; where present it **joins to a history message id** |
| `type` | one of 15 values, table below |
| `heading` | UI line, ≤120 chars, may start with an `icon://name ` sentinel |
| `content` | the payload; truncated at 15000 chars (250000 for `response`) |
| `kvps` | ordered dict — tool arguments, parsed model output, reasoning text |
| `timestamp` | epoch seconds, **set at creation and never updated** |
| `agentno` | which agent in the chain emitted it; older files spell it `agent_number` |

`Type` is a closed literal in `helpers/log.py`, and the mapping that fits
Ossuary's model is:

| `type` | → kind | Notes |
|---|---|---|
| `user` | `message` / user | the user's turn, also injected by the queue, Telegram and email integrations |
| `agent` | **several events** | one LLM turn: see below |
| `response` | `message` / assistant | the final answer to the user; also what compaction leaves behind |
| `tool` | `tool_call` + `tool_result` | one item, both halves — see below |
| `code_exe` | `tool_call` + `tool_result` | the code execution tool and its `input` sibling |
| `browser` | `tool_call` + `tool_result` | browser tool |
| `mcp` | `tool_call` + `tool_result` | MCP tool; `kvps.tool_name` is the real name |
| `subagent` | `tool_call` + `tool_result` | `call_subordinate`; the child's own events are in this same log under a higher `agentno` |
| `error` | `meta` (or `message`/system) | exceptions, Docker failures, compaction failures |
| `warning` | `meta` | rate limits, repaired model output, intervention notices |
| `hint`, `info`, `util`, `progress`, `input` | `meta` | harness bookkeeping |

**A `type: "agent"` item is one LLM turn and expands to several events**, the
same way a Claude Code assistant line does. `content` is the raw streamed text;
`kvps` holds the *parsed* fields, written by
`extensions/python/response_stream/_10_log_from_stream.py`:

| `kvps` key | Meaning | → |
|---|---|---|
| `reasoning` | the provider's reasoning stream, verbatim | `thinking` |
| `thoughts` | the `thoughts` array from the model's own JSON | `thinking` |
| `headline` | the model's one-line summary of what it is about to do | into the `message` event |
| `tool_name`, `tool_args` | what the model decided to call | part of the assistant turn, **not** the call event |
| `step` | UI progress string, derived | `meta`, or dropped |

That last row is the one worth being careful about. The model's *intent* to call
a tool and the *record of the call* are different facts in different items, and
only the second one has a payload to measure.

### Tool calls and their results are the same log item

`Tool.get_log_object()` creates the item with `kvps=self.args` and `content=""`;
`Tool.after_execution()` then does `self.log.update(content=text)` on that same
item. There is no second line, no `toolCallId`, and nothing to join.

This is a gift and a trap. The gift: pairing is by identity, so the
call-and-result correspondence cannot be wrong — emit two events from the one
item, the `tool_call` carrying `kvps` and the `tool_result` carrying `content`,
and record the source item's `no` in `meta` on both. The trap: **the item's
`timestamp` is the moment the call started, and nothing records when it
finished.** See [Shape records](#shape-records).

A tool item whose `content` is still empty is a call that never returned — the
process died, the container stopped, the user hit stop. That is a real signal and
`is_empty` already carries it.

### The history

`agents[].history` parses to `{_cls: "History", counter, bulks: [], topics: [],
current: {…}}`, where a `Topic` is `{_cls, summary, messages: []}`, a `Bulk` is
`{_cls, summary, records: []}`, and a `Message` is:

```json
{"_cls": "Message", "id": "9f3c…", "ai": false, "content": {…}, "metadata": {…},
 "sequence": 14, "summary": "", "tokens": 812}
```

`content` is structured, not rendered: a tool result is
`{"tool_name": "search_engine", "tool_result": "…"}`, a user message is
`{"system_message": …, "user_message": …, "attachments": […]}`, an assistant
response is the raw string the model emitted. `metadata` on assistant messages
carries the LLM result — token usage and provider response ids.

Conversation order is `bulks`, then `topics`, then `current`; `sequence` is a
monotonic counter across the whole session and survives compression, so it is the
reliable ordering key.

---

## Decision 1: two records, and which one is the transcript

Agent Zero keeps the log and the history side by side. They are not two views of
one thing — they are two lossy records that lose *different* things:

| | Log | History |
|---|---|---|
| Ordering | chronological, timestamped | `sequence`, no timestamps at all |
| Tool calls | args and result, as one item | result only, as a `{tool_name, tool_result}` message |
| Reasoning | `kvps.reasoning`, verbatim | not present |
| Token usage | not present | `metadata` on assistant messages |
| Errors, warnings, subagent calls | present | mostly not |
| What it loses | everything past 1000 items | anything compression has summarised away |
| What it is | what the user saw | what the model was given |

**Recommendation: the log is the event stream; the history is joined onto it.**

`LogItem.id` is passed as the history message id at every site that creates both
(`hist_add_user_message`, `hist_add_ai_response`, `hist_add_tool_result`,
`hist_add_warning`), so the join is by recorded id — no positional guessing. That
join buys three things nothing else gives:

1. **The real tool name.** Most tools put `_tool_name` in `kvps`, but the six
   that override `get_log_object()` — code execution, browser, subagent, skills,
   wait, office — do not, and their name survives only in the heading string
   (`Using tool 'x'`) and in the history message's `tool_name` field. Take it
   from the history. Regex-matching a human-readable heading for a fact the
   corpus statistics then aggregate is the same mistake as parsing an exit code
   out of prose, and `docs/pi-investigation.md` already argues that case.
2. **Token usage** on assistant turns, from `metadata`.
3. **Evidence of compression** — the part that matters most.

Because the second half of the recommendation is: **emit a `meta` event for every
history record that has been summarised.** A `Topic` with a non-empty `summary`,
a `Bulk` with one, a `Message` whose `summary` is set — each of those is a place
where the model's own record of this session was overwritten with a shorter one,
in place, destructively (`Topic.compress_attention` literally does
`self.messages[1:n] = [sum_msg]`). An investigation into why an agent forgot
something it had been told needs to see that the telling was compressed away, and
no other source in the corpus has a comparable event. Do not try to interleave
these into the log's timeline — they have no timestamps and belong nowhere in it.
Emit them as a trailing block, ordered by `sequence`, and say in the outline
legend what they are.

The alternative — parse the history and ignore the log — throws away every
timestamp, every duration, every error item and the entire reasoning stream. The
other alternative — parse the log and ignore the history — throws away the tool
names for six tools, all token accounting, and any evidence that compression
happened. Neither is defensible on its own.

## Decision 2: subordinate agents

`call_subordinate` spawns a chain of agents that all write to the *same* log,
distinguished only by `agentno`. A busy session interleaves A0's and A1's events
in one column with nothing to separate them, and `NormalizedEvent` has no field
for "which agent".

This is the same shape of problem as pi's off-path entries: an adapter can record
it in `meta`, but if nothing surfaces it the agent never sees it.
`store._render_event` prints role, kind, tool, ts, shape, parse errors and the
orphan note, and nothing else from `meta`.

The cheapest honest answer: put `agent_no` in `meta`, and render a non-zero one
in the outline's preview column as a `A1:` prefix — no new flag letter, no change
to the shared legend vocabulary beyond one explanatory line that only appears for
sessions that actually delegated. Reading a transcript where a subordinate's tool
calls are silently attributed to the top-level agent produces confident, wrong
findings, which is worse than not reading it.

This is a decision about the shared surface, like pi's `B` flag was, and it
should be made deliberately rather than discovered.

## Decision 3: what to do about the losses

See below. The question is not whether to record them — it is whether Ossuary
states them as events the agent can see, or leaves them as facts about the file
that only this document knows.

---

## Silent losses

Four places where the on-disk record is shorter than what happened. Ossuary's
invariant — *a payload with no marker ended that way on disk* — survives contact
with three of them, and the fourth needs work.

**1. The log is capped at 1000 items.** `_serialize_log` writes
`log.logs[-LOG_SIZE:]`. A long session's beginning is simply absent from the
file, with no marker of any kind.

It is detectable, sometimes exactly: `no` is the item's original ordinal, so a
first item with `no: 812` proves 812 items were dropped. The adapter should emit
a synthetic `meta` event saying so, in the `[[ossuary:elided]]` spirit — the
number is recorded, not estimated.

The catch, and it is a real one: `_deserialize_log` **renumbers from zero**
(`no=i`, with `item_data["no"]` commented out beside it) when Agent Zero loads a
chat at startup. So any chat that has survived a restart has had the evidence
erased, and the next save writes `no: 0` at the head. **A non-zero first `no` is
proof of loss; a zero first `no` is not proof of completeness.** Say exactly that
in the event text.

**2. `/compact` destroys the log.** `plugins/_chat_compaction/helpers/compactor.py`
calls `context.log.reset()`, replaces the history with a single summary message,
and saves. The chat.json of a compacted session contains one `response` item and
nothing else. The full record is in `backups/pre-compact-<ts>.json`, which is why
discovery must read that directory. Where both exist, they are two sessions in
Ossuary's terms, and the summary event should name the backup it came from.

**3. History compression rewrites messages in place** — covered under Decision 1.
Detectable through the `summary` fields; invisible if the history is not read.

**4. Three different truncation markers, meaning three different things.** All
three are Agent Zero's, none is Ossuary's, and none should be stripped:

| Marker | Written by | Means |
|---|---|---|
| `<< N Characters hidden >>` | `helpers/log.py` | the *display* record was cut (content over 15000 chars, or a `kvps` value over 5000). The model saw the whole thing. |
| `<< N CHARACTERS REMOVED TO SAVE SPACE >>` | `prompts/fw.msg_truncated.md` | the *payload* was cut before the model saw it — terminal output over 1000000 chars |
| `...` at 120 / 60 chars | `helpers/log.py` | headings and kvps keys, cosmetic |

The first is the one that changes what a shape record means. `byte_length` on a
truncated log item measures the truncated text, and the truncation is a fixed
cap of exactly 15000 *characters*, so **`is_round_number` will fire across the
corpus on a length that is an artifact of the UI, not of any tool** — 15000 is a
multiple of 1000. Worse for anything non-ASCII: those payloads land a few bytes
*above* 15000 and read as natural lengths, so the same cap is loud on some
sessions and invisible on others. Carry the marker's own number
into `meta` — as the pi adapter does with `details.truncation` — so "this payload
looks capped" becomes "this payload was capped, from N chars, by the log's
display limit". Without that, Agent Zero sessions will pollute
`ossuary_tool_stats` with a phantom 15000-byte cap that reads exactly like Claude
Code's real 30000-byte one.

Also worth knowing: Agent Zero masks its own configured secrets on the way into
the log (`Log._mask_recursive`). That is not a substitute for Ossuary's redaction
pass — it only covers values the user registered — but it means some payloads
arrive already altered.

---

## Shape records

| Field | Where it comes from | Quality |
|---|---|---|
| `byte_length`, `content_hash`, `is_empty`, `terminates_cleanly`, `is_round_number` | the log item's `content` | as elsewhere, with the caveat above |
| `has_error_field` | only `type == "error"` on the item itself | there is no per-result error flag — see below |
| `duration_ms` | nothing records it | see below |
| `exit_code` | never recorded | leave null |

**Durations.** The tool item's `timestamp` is stamped at creation and never
updated, so the only available estimate is the gap to the *next* log item, which
is "when the harness next wrote something", not "when the tool returned". That is
weaker than the derived durations already in the corpus, which at least span a
call line and its result line. Two options: leave `duration_ms` null and let the
outline say the CLI records no timing, or derive it and mark it `derived` like the
others. **Recommendation: leave it null**, and put `next_item_gap_ms` in `meta`
instead. A number in the duration column is read as a tool's duration; this one
would be the gap until the model finished thinking about the result, and the `~`
flag does not say enough to fix that. The last item in a session has no next item
either way.

**Exit codes.** The code execution tool retrieves an exit code when its shell
terminates and renders it into a prose framework message (`fw.code.shell_exit.md`
→ `" with exit code 1"`). Recovering it means regex-matching a sentence that
changes between releases, and a wrong parse is indistinguishable from a recorded
code. Leave `exit_code` null. This is precisely the case `_exit_code_for` was
written to avoid in the Claude Code adapter.

**Errors.** Agent Zero has no per-result error flag. Failures appear as separate
`error` and `warning` log items, and framework error text is folded into the
tool result payload itself (`fw.code.info.md` and friends). So `has_error_field`
is honestly false almost everywhere, and the signal lives in the neighbouring
`error` item — which the outline shows as its own row. That is adequate, and it
is better than inventing a flag by pattern-matching payload text.

---

## What Agent Zero records that nothing else does

Worth carrying into `meta` even though no existing field wants them — and worth
reading [`pi-investigation.md`](pi-investigation.md)'s note that `meta` is
currently invisible to the agent unless deliberately surfaced:

- **The reasoning stream, verbatim**, in `kvps.reasoning`. Claude Code frequently
  stores a thinking signature with no text; this stores the text.
- **`headline` and `thoughts`** — the model's own statement of what it is doing,
  as structured fields rather than prose to be inferred.
- **A subordinate agent's whole run**, inline, under a higher `agentno`. No other
  supported source records the inside of a subagent at all.
- **Token usage and cost** per assistant turn, in the history `metadata`.
- **`error` and `warning` as first-class items** — retries, rate limits, repaired
  model output. In the other four sources these are either absent or buried.
- **`type` itself** — `browser`, `code_exe`, `mcp` and `subagent` classify a call
  by *kind of work* before any tool name is read, which is a grouping the corpus
  statistics cannot currently express.

---

## Fixtures

There is no writer to borrow, the way pi's `SessionManager` was borrowed.
`helpers/persist_chat.py` imports `agent`, `initialize` and the whole model
stack; running it needs an API key and a container.

`helpers/log.py` does not. It imports only `helpers.secrets` and
`helpers.strings`, so a fixture's `log` block can be produced by Agent Zero's own
`Log`/`LogItem` code — real truncation, real masking, real `no` sequencing — and
the surrounding context object and the history string, which are both small and
fully specified above, written by hand to match `_serialize_context` and
`History.to_dict`. That is one notch below the pi fixtures and one above the
Codex ones.

Then damage it deliberately, as the other golden fixtures are: a log item with a
malformed `kvps`, a history string that is not valid JSON, a tool item with empty
`content` (the call that never returned), a first item with `no: 812` (the
1000-item cap), a content payload sitting exactly on the 15000-char truncation,
an item with `agent_number` instead of `agentno` (the older spelling), and a
`backups/pre-compact-*.json` beside a `chat.json` that has been compacted down to
one item.

---

## Cost

| Change | Size |
|---|---|
| `src/ossuary/adapters/agent_zero.py` | ~600–700 lines; closest sibling is `copilot.py` (single-JSON parsing), but the log/history join makes it the largest adapter |
| `Source` literal in `models.py`, registry in `adapters/__init__.py` | 2 lines |
| `tests/golden/agent-zero/…` fixtures | half generated, half hand-written, then damaged |
| `tests/test_adapters_agent_zero.py`, `conftest.py` fixtures | ~200 lines |
| `tests/test_discovery_and_cli.py` — the assertions that enumerate the sources | ~4 lines |
| Outline: the subordinate-agent prefix and its legend line | ~8 lines, shared surface |
| `docs/formats.md` — an Agent Zero section | ~70 lines |
| `README.md` — one `--source` line, plus the discovery caveat and the `docker cp` recipe | ~10 lines |
| Both plugins' `investigate/SKILL.md` — the sentence naming the CLIs | 2 lines |

No `SCHEMA_VERSION` bump: `NormalizedEvent` does not change shape, and adding a
`Source` value does not invalidate artifacts derived from the other four.
Everything downstream is source-agnostic — `ALL_SOURCES` drives the CLI, the MCP
server, the store and the report.

The honest total is larger than pi's, and the reason is not the parse. It is that
this is the first source whose sessions Ossuary cannot find on its own, and the
first where deciding *what the transcript is* takes an argument.

---

## Unknowns

- **No real corpus, and no install.** Every claim here is read out of source. The
  shape of `kvps` in the wild, how often the 1000-item cap actually bites, and
  whether `id` is populated as consistently as the code suggests are all
  unverified.
- **Plugins are load-bearing and user-installable.** Code execution, browser,
  compaction and branching all ship as plugins under `plugins/`, and a user can
  add more. A plugin can log any `type` it likes and put anything in `kvps`.
  Unknown types must degrade to `meta`, never to a parse failure.
- **Attachments and images.** Chat media is written under
  `usr/chats/<id>/{messages,images,screenshots}/` and referenced by path, but
  `RawMessage` content in the history can carry inline base64. As with pi, name
  non-text items rather than inlining them, or every shape record on those events
  measures base64.
- **File size.** A chat is one JSON object holding up to 1000 items of up to
  15000 chars plus a full history. `json.load` of a 30MB file is fine; a
  streaming parse is not available for this format if it turns out not to be.
- **Chat branching** (`plugins/_chat_branching`) copies a chat into a *new*
  context truncated at a cut point, rather than branching in place as pi does. So
  there is no tree to walk — but there are two sessions on disk sharing a
  prefix, with only `"<name> (branch)"` linking them. Whether that duplication
  should be detected and marked is a question for after the first real corpus.

---

## Agent Zero as a host

Out of scope, and — unlike pi — probably cheap. Agent Zero is already an MCP
client (`helpers/mcp_handler.py`, stdio servers configured in settings) and
already reads `SKILL.md` skills from `usr/skills`, which are the two things pi
lacked and which cost that investigation its host half. `call_subordinate` and
`helpers/parallel_tools.py` give it a fan-out primitive.

None of that is needed to answer the question this document exists for. An Agent
Zero user reads their Agent Zero chats from Claude Code or Copilot with the
plugin that already ships, the moment the adapter exists — the same conclusion
pi reached, for the same reason: Ossuary has never required the host to match the
source, and mixing a fifth format into the corpus is a gain rather than a
compromise. Agent Zero's 15000-char display cap and 1000-item log cap become
visible next to Claude Code's 30000 bytes and pi's 51200, which is exactly the
comparison no single session can show.
