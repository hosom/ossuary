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

## One session per context window

The batch pipeline this replaced ran one agent per session — a fresh context
window per transcript, enforced by a `for` loop. That isolation was not
incidental. Outlines are large, and two of them in one context means the second
session is read by an agent already holding opinions about the first: you start
pattern-matching session twelve against what went wrong in session seven.
Findings stop being independent, and once the window fills, recall degrades with
nothing to tell you it did. It is the same failure the tool avoids by refusing to
hand the investigator a taxonomy of known problems.

A prompt-driven design has to earn that property rather than inherit it, so it
is arranged in three places:

- The **`investigate` skill** is the coordinator. For a single session it works
  inline; for more than one it spawns a `session-investigator` per session, in
  one message so they run concurrently.
- The **`session-investigator`** is granted `outline`, `read_events`,
  `search_session`, `read_event_slice`, `tool_stats` and `report_issue` — and
  nothing else. With no `ossuary_sources` it cannot widen its own scope to a
  second transcript, and with no `ossuary_write_run` it cannot publish everyone
  else's partial findings by finishing early.
- **`tests/test_plugins.py`** asserts those grants, so widening one fails the
  suite rather than quietly costing recall six months from now.

The MCP server is one process per host session, shared by every subagent, so
findings from all of them accumulate in a single buffer. The coordinator then
does the corpus-level pass the per-session investigators structurally cannot —
`ossuary_known_clusters`, `ossuary_propose_cluster`, `ossuary_write_run` — which
is the same split the old two-agent pipeline had, for the same reason.

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
| `.mcp.json` | Starts `ossuary-mcp` via `uvx --from ${CLAUDE_PLUGIN_ROOT}/../../..` — the bundled repo, no network |
| `skills/investigate` | Model-invoked. The method: read the outline in full, follow the shapes, check corpus stats before calling a tool abnormal |
| `/ossuary:report` | Renders HTML from recorded findings; no inference |
| `agents/session-investigator` | Read tools plus `report_issue`, nothing else — one spawned per session, so each transcript gets its own context window |

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

To run it outside a plugin, from a checkout:

```bash
uvx --from /path/to/ossuary ossuary-mcp
```

or add it to a project's `.mcp.json` directly.

### Why the manifests point at a path

Neither plugin fetches anything. `${CLAUDE_PLUGIN_ROOT}` is the plugin's own
directory, and both plugins ship inside the repository that provides the
package, so `../../..` from there is the project itself — whether you loaded it
with `--plugin-dir` from a working copy or installed it from the marketplace,
which clones the same repository.

The two alternatives were tried and both shipped broken. A bare `ossuary`
installs an unrelated PyPI project of that name (a dice analysis toolkit), and a
`git+` URL to the default branch installs whatever that branch happens to
contain, which is not necessarily this package. In both cases the only symptom
is a plugin whose tools never appear, and a JSON-RPC error with no detail.
`tests/test_plugins.py` now resolves the source and checks it lands on a
pyproject declaring the entry point the manifest goes on to run.

### Why `uv run --project` and not `uvx --from`

`uvx --from <path>` builds the project into a cached environment keyed on the
path and the version. When the source changes underneath it, that cache is not
invalidated — not by `--refresh`, and not by `--reinstall`. Editing
`src/ossuary/` and restarting the host gets you the old code, silently, with a
handshake that looks entirely healthy. This is not hypothetical: an
investigation was lost to it, the server answering with a build from hours
earlier, and the only way out was deleting `~/.cache/uv/environments-v2` by
hand.

`uv run --project` runs the checkout itself. There is no build artifact to go
stale, so a fix is live the moment the host reconnects.

It also resolves against `uv.lock` rather than re-resolving from scratch, which
turns out to be the same bug wearing a different hat. `pyproject.toml` asks for
`mcp>=1.2` with no upper bound, so a fresh resolve is free to pick up a new SDK
major; one did, and the code of the day imported a module that major had
removed. The lockfile pins a version known to work. Locking the dependency and
skipping the build cache are one decision, not two.

The server also fills in `serverInfo.version` from the installed distribution.
An empty version string is what made the stale build take twelve shell commands
to identify instead of one.

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
