<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/brand/ossuary-mark.svg">
  <img src="docs/brand/ossuary-mark-inverse.svg" alt="Ossuary" width="120">
</picture>

# OSSUARY

**Where the remains are sorted**

</div>

Reads local LLM agent session transcripts, finds health issues in them, clusters
those issues across the corpus, and writes a self-contained HTML report.

A postmortem tool reads what is left behind. Ossuary takes a finished agent
session — every turn, tool call, retry and dead end — and lays the remains out in
order, so the engineer who owns the agent can see exactly where it went wrong.

Built for hundreds to low thousands of sessions on a laptop.

```
discover sessions
      ↓
adapters → normalized events (+ shape records)   [deterministic]
      ↓
outline, corpus-wide tool stats, redaction        [deterministic]
      ↓
── MCP ───────────────────────────────────────────────────────
      ↓
your agent reads, notices, records                [Claude Code / Copilot CLI]
      ↓
── MCP ───────────────────────────────────────────────────────
      ↓
issues + clusters, reconciled against the taxonomy
      ↓
Jinja2 → single-file HTML → open browser
```

Ossuary supplies everything above and below the line. The reasoning in the
middle is done by the coding agent you already have open, under whatever
credentials it already holds.

## Install

```bash
uv venv && uv pip install -e .
source .venv/bin/activate    # the bare `ossuary ...` commands below need this
```

Then install the plugin for whichever agent you use:

```
/plugin marketplace add hosom/ossuary          # Claude Code
/plugin install ossuary@ossuary
```

```bash
copilot plugin install --path ./plugins/copilot/ossuary    # Copilot CLI
```

**There is no API key to set.** Ossuary runs no inference of its own — it hands
your transcripts to the agent you are already talking to, which is already
authenticated. See [docs/plugins.md](docs/plugins.md).

## Use

Ask your agent to look at your sessions:

> *Have a look at my recent Claude Code sessions and tell me what went wrong.*

It reads the outline, follows what looks odd, checks the corpus statistics
before calling anything abnormal, and records what it finds. When you want the
findings written down it calls `ossuary_write_run`, and then:

```bash
ossuary report                   # renders .ossuary/run.json to a single HTML file
```

The Claude Code plugin ships the same two steps as commands:

```
/ossuary:investigate             # audit what is on disk, one subagent per session
/ossuary:report                  # render the findings that were written down
```

Nothing reaches disk until `ossuary_write_run` runs, so an investigation you
abandon leaves no artifacts behind. Writing a run archives the previous one
under `.ossuary/runs/` rather than replacing it.

The CLI is the deterministic surface around that — nothing here calls a model:

```bash
ossuary sources [PATHS...] [--source claude-code|codex|copilot]
ossuary outline <session-id|path>          # one session, by hand
ossuary report [--open/--no-open] [--out report.html]
ossuary taxonomy [--show/--clear]
ossuary export --out issues.jsonl
ossuary-mcp                                # the MCP server, for wiring up by hand
```

## Design decisions worth not undoing

**Issue discovery is the model's job, not a regex's.** There are no heuristic
detectors that look for tracebacks or known failure strings and call those
"issues". A deterministic detector can only find what its author already knew
about, which makes the tool a fancy grep. Deterministic code here *measures and
indexes*; the agent *interprets*. The investigating agent is given no taxonomy
of issue types for the same reason — a menu of expected failure modes turns
discovery into recognition.

**Nothing is truncated silently.** Every path that shortens a payload routes
through `elide.py` and inserts `[[ossuary:elided N of M bytes]]`. The invariant
the agent relies on is the contrapositive: *a payload with no marker ended that
way on disk*. Unlabelled truncation would manufacture the exact artifact the
tool exists to detect. This is a correctness requirement, and
`tests/test_elide.py` treats it as one.

**The outline comes before anything else.** The agent navigates from there with
tools, but it has seen every event at low resolution before it chooses where to
look. Recall does not depend on the model's curiosity. The outline is also where
most anomalies actually surface — as rows rather than as text buried in a
payload — which is why the plugin prompts insist on reading it in full first.

**One session per context window.** Auditing several sessions spawns one
`session-investigator` per transcript rather than sweeping the corpus in a single
context. Two outlines in one window means the second session is read by an agent
already holding opinions about the first, and findings stop being independent.
The per-session agent is granted the read tools and `report_issue` and nothing
else — no way to reach a second transcript, no way to end the run — and
`tests/test_plugins.py` asserts that, so widening it fails the suite instead of
quietly costing recall. That check normalises the `mcp__<server>__` prefix away
before comparing, so it constrains *which* tools an agent may hold but not
whether the names resolve under the namespace the host actually uses: a plugin
server the host exposes as `mcp__plugin_ossuary_ossuary__*` will not match an
agent asking for `mcp__ossuary__*`, and the suite passes either way while every
spawn gets an empty toolset. See [`docs/plugins.md`](docs/plugins.md).

**Adapters parse like archaeologists, not validators.** No line is ever rejected.
A malformed line becomes a degraded event carrying its raw text and the parse
error, and parsing continues. Whatever is on disk is the only record that will
ever exist of that session. See [`docs/formats.md`](docs/formats.md).

**Ossuary does not bring its own model.** The hard part here was never calling
one: it was turning four incompatible transcript formats into a single event
model, measuring payloads without editorialising, computing the corpus-wide
statistics no single session can show, and never shortening anything without
saying so. A coding agent already has a model, a context window and a turn loop.
It needs the transcripts laid out properly, not a second agent bolted on beside
it. So the whole inference layer is somebody else's — which is also why there is
no API key, no provider SDK, and no question about whose subscription is being
spent. See [`docs/plugins.md`](docs/plugins.md).

**The method lives in the plugin prompts, not in Python.** What to look for, how
to read the instrumentation, and what counts as an issue are in `SKILL.md` and
`*.agent.md`, where they are readable and editable by the person running the
investigation. Nothing in the code decides what a problem looks like.

## Shape records

Every `tool_result` carries measurements, not verdicts:

| Field | Why |
|---|---|
| `byte_length` | Repeated identical lengths across calls are visible in a column |
| `is_round_number` | Exactly 30000 bytes is a cap, not a coincidence |
| `terminates_cleanly` | Ends at a boundary, or was cut mid-flow |
| `duration_ms` + `duration_source` | Whether the CLI reported it or we derived it |
| `exit_code` | Only when genuinely recorded — never synthesised from an error flag |
| `content_hash` | Byte-identical repeated results |
| `is_empty`, `has_error_field` | |

Exit code 0, an empty body, and a 30-second duration together are a timeout
swallowed by the harness. No amount of reading the payload shows that as clearly
as those three fields side by side.

Corpus-wide statistics do the part no single session can: a tool that returns
exactly 30000 bytes on 40% of calls is unremarkable once and damning across two
hundred sessions. Nothing inside one session shows it, so `ossuary_tool_stats`
computes it across the corpus and hands it over on request.

## Privacy

Transcripts contain source code, file contents, and not infrequently credentials.
A redaction pass runs **before anything leaves the MCP boundary** — common secret
shapes, key-shaped strings, and the literal values of your own credential-ish
environment variables. Placeholders are padded toward the original length so
shape records stay honest.

The host agent is a model too, and is treated as one. `OSSUARY_NO_REDACT`
disables the pass, and then transcripts reach it verbatim.

Nothing else leaves the machine: Ossuary opens no sockets and makes no API
calls. Whatever the transcripts are shown to is whatever you have already
chosen to run.

## Taxonomy

Named clusters persist to `.ossuary/taxonomy.json`. Later runs assign to existing
clusters where they fit and only propose genuinely new ones — which stops reports
reshuffling between runs and makes "new issue types this run" a real signal
rather than an artifact of the model choosing different words.

## The report

`ossuary report` renders one self-contained HTML file — inline CSS and JS, no
CDN links, no network — so it survives being attached to a ticket. Sections:
run summary, corpus trace, new issue types this run, clusters (severity-filtered,
expandable to member issues and verbatim evidence), distribution by phase and
tool, full tool statistics, and every session examined.

### Brand

The mark is a niche — the arched recess a set of remains is filed into — holding
three strips of sorted bone. The strips carry the meaning: cream for the record,
verdigris for what held, rust for what failed. The arch springs from a circle of
radius 18 centred at (32,27) on a 64-unit grid, drawn in a single 2.4-unit
stroke, and reads down to 16px. Clear space equals the plinth height on all four
sides.

| Colour | Hex | Used for |
|---|---|---|
| Void | `#12100E` | Every canvas. Never used for type. |
| Bone | `#E6DFD1` | Primary type, the mark, rules of consequence |
| Verdigris | `#4E8F7C` | Passed, held, resolved. Section numerals. |
| Rust | `#9C4A3C` | Failure, severity. Used sparingly. |
| Ash | `#5A554D` | Secondary type, skipped sessions, inert data |
| Brass | `#C8A24A` | Warnings — measurements that want a second look |

Cinzel for display (inscriptional caps only, never a paragraph), Spectral for
body, JetBrains Mono for every measurement, label, ID and code fragment. The
report requests all three and falls back down stacks that keep their character,
because a file that must work offline cannot fetch a webfont.

The recurring device is the dot strip: a run of circles where each dot is one
session, one tool call, one attempt. It is the interior of the mark and it is the
corpus trace in the report. Where a strip stops being one dot per unit it says so
rather than quietly standing for the wrong number — the same rule as
`[[ossuary:elided]]`, applied to pixels.

Findings are stated flatly, past tense, with the number attached. The funerary
register lives in the naming and the surfaces, never in the diagnosis: the
engineer reading this is debugging at 6pm and wants the cause of death and the
line number, not a metaphor.

## Tests

```bash
python -m pytest
```

Golden-file tests run over fixtures that include deliberately malformed lines,
out-of-order tool pairing, an orphaned result, a payload capped at exactly 30000
bytes, and an empty result with a 30-second duration.

## Not in v1

No regex/heuristic issue detectors. No embeddings or vector store. No web
service, daemon, or TUI. No database — JSON and JSONL on disk are sufficient at
this scale. No model SDK, no API client, and no inference of Ossuary's own.
