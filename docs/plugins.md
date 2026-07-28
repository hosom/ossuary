# Plugins

Ossuary runs no inference. It exposes its deterministic half — discovery,
normalization, the outline, the shape measurements, the corpus statistics,
redaction — as MCP tools, and the agent you are already talking to does the
investigating.

That is the whole architecture, and it falls out of one observation: the hard
part of this tool was never calling a model. It was turning four incompatible
transcript formats into one event model, measuring payloads without editorializing
about them, computing the corpus-wide statistics no single session can show, and
never truncating anything without saying so. A coding agent already has a model,
a context window, and a turn loop. It does not need Ossuary to bring another one
— it needs the transcripts, laid out properly.

Two consequences worth stating plainly:

- **Ossuary holds no credential.** Inside Claude Code or Copilot CLI the
  inference is already paid for and already authenticated. There is no API key
  to set, no SDK to install, and no question about whose subscription is being
  spent.
- **You can watch it happen.** The investigation is a normal conversation with
  your agent. You can read its reasoning, redirect it mid-run, and stop it.

## Claude Code

```
/plugin marketplace add hosom/ossuary
/plugin install ossuary@ossuary
```

Or, while developing:

```bash
claude --plugin-dir ./plugins/claude-code/ossuary
```

| Component | What it does |
| --- | --- |
| `.mcp.json` | Starts `ossuary-mcp` via `uvx --from ossuary` |
| `skills/investigate` | Model-invoked. The method: read the outline in full, follow the shapes, check corpus stats before calling a tool abnormal |
| `/ossuary:report` | Renders HTML from recorded findings; no inference |
| `agents/session-investigator` | A subagent with only the Ossuary read tools, so auditing several sessions gives each one its own context window |

## Copilot CLI

```bash
copilot plugin install --path ./plugins/copilot/ossuary
```

Same MCP server, same method, in Copilot's plugin format: a
`session-investigator` custom agent and an `investigate` skill.

## The MCP server

`ossuary-mcp` speaks MCP over stdio and exposes ten tools.

**Reading** — everything the agent sees arrives through these, redacted and
elided on the way out:

| Tool | |
| --- | --- |
| `ossuary_sources` | List every transcript found on this machine |
| `ossuary_outline` | Every event in one session at low resolution |
| `ossuary_read_events` | Full events by index range, ≤40 per call |
| `ossuary_search_session` | Regex search within one session |
| `ossuary_read_event_slice` | One oversized payload, by byte offset |
| `ossuary_tool_stats` | Corpus-wide statistics for one tool |

**Recording** — findings accumulate in memory:

| Tool | |
| --- | --- |
| `ossuary_report_issue` | Record a finding, with evidence indices and confidence |
| `ossuary_propose_cluster` | Group findings into a recurring failure mode |
| `ossuary_known_clusters` | The taxonomy from previous runs |
| `ossuary_write_run` | Persist to `.ossuary/run.json` for `ossuary report` |

Nothing is written to disk until `ossuary_write_run`, so an abandoned
exploration cannot leave artifacts that `ossuary report` would render as though
they were a finished investigation.

`ossuary_write_run` takes an optional `investigator` string. Ossuary cannot see
which model is on the other end of these tools, so the report credits whatever
the agent names itself, or nobody. Recorded rather than inferred: a report that
guessed would be worse than one that admits it does not know.

### Configuration

- `OSSUARY_ROOTS` — `os.pathsep`-separated directories to search, instead of the
  default transcript locations.
- `OSSUARY_NO_REDACT` — disable redaction. Transcripts then reach the host agent
  verbatim, credentials included. On by default because the host is a model too.

To run it outside a plugin:

```bash
uvx --from ossuary ossuary-mcp
```

or add it to a project's `.mcp.json` directly.

## What the plugin does not give you

A plugin-driven investigation is a conversation, not a measurement. There is no
turn cap, no prompt version to hash, and the host agent brings its own system
prompt and its own context — so two runs over the same corpus can differ for
reasons that have nothing to do with the transcripts.

That is the right trade for finding out what is wrong with your agents. It is
the wrong trade for tracking a number week over week. If you need the latter,
the pieces are all still here — `.ossuary/run.json` is a stable schema and
`ossuary export` emits JSONL — but the discipline has to come from how you drive
it: same prompt, same corpus, and record the `investigator` so you know what
produced each number.
