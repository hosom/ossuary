# Plugins

The [backends](backends.md) point outward: Ossuary holds the transcripts and
calls a model. The plugins point the other way. Ossuary exposes its
deterministic half — discovery, normalization, the outline, the shape
measurements, the corpus statistics, redaction — as MCP tools, and the agent you
are already talking to does the investigating.

That inversion is the cleanest answer to "I have a subscription, not an API
key". Inside Claude Code or Copilot CLI the inference is already paid for and
already authenticated; Ossuary never holds a credential or has to ask who is
paying. It is also the only arrangement where you can watch the investigation
happen and interrupt it.

**The tradeoff is real.** A run driven this way is not reproducible the way
`ossuary scan` is: no turn cap, no prompt version to hash, and the host agent
brings its own system prompt and its own context. Use the plugin to explore a
corpus; use `ossuary scan` to produce a number you intend to compare against
next week's. Both write the same `.ossuary/run.json`, so `ossuary report`
renders either.

## Claude Code

```
/plugin marketplace add hosom/ossuary
/plugin install ossuary@ossuary
```

Or, while developing:

```bash
claude --plugin-dir ./plugins/claude-code/ossuary
```

What it ships:

| Component | What it does |
| --- | --- |
| `.mcp.json` | Starts `ossuary-mcp` via `uvx --from 'ossuary[mcp]'` |
| `skills/investigate` | Model-invoked. The investigation method: read the outline in full, follow the shapes, check corpus stats before calling a tool abnormal |
| `/ossuary:scan` | Runs the reproducible batch pipeline and renders the report |
| `/ossuary:report` | Renders HTML from existing artifacts; no inference |
| `agents/session-investigator` | A subagent with only the Ossuary read tools, so auditing several sessions gives each one its own context window |

## Copilot CLI

```bash
copilot plugin install --path ./plugins/copilot/ossuary
```

Same MCP server, same method, expressed in Copilot's plugin format: a
`session-investigator` custom agent and an `investigate` skill.

## The MCP server

`ossuary-mcp` speaks MCP over stdio and exposes ten tools:

| Tool | |
| --- | --- |
| `ossuary_sources` | List every transcript found on this machine |
| `ossuary_outline` | Every event in one session at low resolution |
| `ossuary_read_events` | Full events by index range, ≤40 per call |
| `ossuary_search_session` | Regex search within one session |
| `ossuary_read_event_slice` | One oversized payload, by byte offset |
| `ossuary_tool_stats` | Corpus-wide statistics for one tool |
| `ossuary_report_issue` | Record a finding |
| `ossuary_propose_cluster` | Group findings into a recurring failure mode |
| `ossuary_known_clusters` | The taxonomy from previous runs |
| `ossuary_write_run` | Persist to `.ossuary/run.json` for `ossuary report` |

Nothing is written to disk until `ossuary_write_run` is called, so an abandoned
exploration leaves no artifacts behind for `ossuary report` to render as though
they were a scan.

Two environment variables:

- `OSSUARY_ROOTS` — `os.pathsep`-separated directories to search, instead of the
  default transcript locations.
- `OSSUARY_NO_REDACT` — disable redaction. Transcripts then reach the host agent
  verbatim, credentials included. Redaction is on by default for the same reason
  it is in `ossuary scan`: the host is a model too.

To run it outside a plugin:

```bash
uvx --from 'ossuary[mcp]' ossuary-mcp
```

or add it to a project's `.mcp.json` directly.
