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
Agent A, once per session, tool-using loop        [LLM]
      ↓
issues (per session)  +  corpus-wide tool stats   [deterministic aggregate]
      ↓
Agent B, batched over all issues                  [LLM]
      ↓
clusters (reconciled against stored taxonomy)
      ↓
Jinja2 → single-file HTML → open browser
```

## Install

```bash
uv venv && uv pip install -e .
```

Set a key for whichever provider `agents.yaml` names, e.g. `ANTHROPIC_API_KEY`.
Or run entirely locally — see [Privacy](#privacy).

## Use

```bash
ossuary sources                  # what it found on disk, counts per source
ossuary scan                     # expensive; writes artifacts to .ossuary/
ossuary report                   # cheap; renders HTML from artifacts
```

`scan` and `report` are separate on purpose. The report will be iterated on
dozens of times and must not re-pay for inference each round.

```bash
ossuary scan [PATHS...] [--source claude-code|codex|copilot] [--model ...]
             [--limit N] [--no-cache] [--no-redact] [--no-cluster]
ossuary report [--open/--no-open] [--out report.html]
ossuary agents test scanner --fixture tests/golden/     # add --live to call the model
ossuary agents show
ossuary outline <session-id|path>                       # deterministic, no model
ossuary taxonomy [--show/--clear]
ossuary export --out issues.jsonl
```

## Design decisions worth not undoing

**Issue discovery is the model's job, not a regex's.** There are no heuristic
detectors that look for tracebacks or known failure strings and call those
"issues". A deterministic detector can only find what its author already knew
about, which makes the tool a fancy grep. Deterministic code here *measures and
indexes*; the agent *interprets*. Agent A is given no taxonomy of issue types
for the same reason — a menu of expected failure modes turns discovery into
recognition.

**Nothing is truncated silently.** Every path that shortens a payload routes
through `elide.py` and inserts `[[ossuary:elided N of M bytes]]`. The invariant
the agent relies on is the contrapositive: *a payload with no marker ended that
way on disk*. Unlabelled truncation would manufacture the exact artifact the
tool exists to detect. This is a correctness requirement, and
`tests/test_elide.py` treats it as one.

**Agent A always gets the full outline first.** It navigates from there with
tools, but it has seen every event at low resolution before it chooses where to
look. Recall does not depend on the model's curiosity, and runs are reproducible
enough to diff week over week. The outline is also where most anomalies actually
surface — as rows rather than as text buried in a payload.

**Adapters parse like archaeologists, not validators.** No line is ever rejected.
A malformed line becomes a degraded event carrying its raw text and the parse
error, and parsing continues. Whatever is on disk is the only record that will
ever exist of that session. See [`docs/formats.md`](docs/formats.md).

**Prompts live in config, tools live in code.** Model, temperature, turn cap, and
prompt text are in `agents.yaml`; tool implementations and schemas are in Python.
The prompt's content hash is a cache key, so editing a prompt re-runs inference
without re-paying for I/O.

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
hundred sessions. Agent A structurally cannot see that; Agent B is handed it.

## Privacy

Transcripts contain source code, file contents, and not infrequently credentials.
A redaction pass runs **before any API call** — common secret shapes, key-shaped
strings, and the literal values of your own credential-ish environment variables.
Placeholders are padded toward the original length so shape records stay honest.

`--no-redact` disables it and warns loudly.

A fully local configuration works today, not eventually:

```yaml
agents:
  scanner:
    model: ollama:qwen2.5-coder
```

`OLLAMA_BASE_URL` overrides the default `http://localhost:11434/v1`. For any
other provider, point `openai-compatible:<model>` at a proxy via
`OSSUARY_OPENAI_BASE_URL`.

## Caching

Content-addressed under `.ossuary/cache/`:

- tool responses — `hash(session_file_content) + hash(call_args)`
- issue lists — `hash(session_file_content) + prompt_version + model + source`

A prompt edit re-runs inference but not I/O. An unchanged session costs nothing
on re-scan. Keying on file *content* rather than mtime means a touched-but-
unmodified transcript stays cached.

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
tool, full tool statistics, and every session scanned.

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

No regex/heuristic issue detectors. No embeddings or vector store. No semantic
pre-batching for Agent B. No web service, daemon, or TUI. No database — JSON and
JSONL on disk are sufficient at this scale. No agents beyond the two described.
