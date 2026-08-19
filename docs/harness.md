# Harness

**Status: design proposal. Nothing in this document is implemented.**

This document considers whether Ossuary should grow a full agent harness —
instrumented so that its analysis arrives via introspection of sessions it ran
itself, rather than archaeology on sessions somebody else's harness wrote down.
The answer proposed here is yes to the direction and no to the pivot: build the
harness as a new layer that *emits* the record Ossuary already analyzes, and
leave the analysis core exactly as model-free as it is today.

## Why a harness at all

Ossuary's whole architecture is a workaround for one fact: it does not control
the emitter. Shape records exist to reverse-engineer what a harness hid — the
payload capped at exactly 30000 bytes, the timeout swallowed into an empty
result with a 30-second duration, the exit code that was never really recorded.
The adapters parse like archaeologists because the record on disk is lossy and
nobody labeled the loss. `docs/formats.md` is, in effect, a catalog of what
harnesses swallow.

A harness we own dissolves that entire problem class at the source. It can emit
the normalized event model directly — real exit codes, real durations,
truncation routed through `elide.py` and labeled at write time. The native
session log becomes one more adapter, except the adapter is the identity
function, and every analytic already built works on it from day one.

That is the test the design is held to throughout: **the harness's log format
is `models.py`, or maps losslessly onto it.** If the harness invents a format
that needs its own archaeology, something has gone wrong.

It also closes a sharp edge the README already documents: findings and session
state living only in the MCP server's memory. An append-only per-session log is
the durable substrate that problem has been asking for.

## The substrate decision

Two options were considered:

1. **Own the loop** — build on Pydantic AI, bring-your-own-key.
2. **Wrap existing harnesses** — Copilot CLI and the Claude Agent SDK behind a
   wrapper layer that papers over their differences and supports multiple
   providers.

The decision is option 1, and it follows from the one feature the harness
exists to provide. Walk the clawback-style hook through each option:

**Owned loop.** The log writer sits inside the event loop. Every tool call,
every result, every retry passes through code we wrote, before and after it
executes. Completeness is a property of the architecture, not a hope.
Multi-provider support is native to Pydantic AI (Anthropic, OpenAI, Gemini,
Bedrock, anything OpenAI-compatible locally), it has an MCP client, and
agents-as-config is natural because its agents are constructed
programmatically — a config-to-`Agent` factory is a small amount of code.

**Wrapped harnesses.** The "first-class" log is assembled from whatever each
vendor's hook system deigns to report. Claude Code's hooks are rich; Copilot's
extensibility is different and weaker; neither guarantees visibility into
everything, and both change between releases with no version field — the exact
property `docs/formats.md` complains about. This option runs Ossuary's current
adapter problem *at runtime*, across two SDKs with different semantics, and
calls the result forensically verifiable. It cannot be: you can hash-chain what
you received, but you cannot attest that what you received is what happened.
That is the shape-record problem wearing a new hat. A project whose thesis is
that existing harnesses' logs are lossy artifacts to be excavated should not
build its harness on top of those same harnesses.

### What owning the loop costs

The README's proudest property — *no API key, no provider SDK, no question
about whose subscription is being spent* — does not survive into the harness.
Bring-your-own-key is API-key economics.

That property is genuinely valuable, but it is valuable to the interactive
plugin audience: people auditing their own Claude Code or Copilot sessions
inside a subscription they already pay for. The harness serves a different
audience — people running fleets of configured agents in automation — and that
audience is API-key-native already. Different users, different economics. A
property that matters to audience A does not get to veto the right
architecture for audience B.

The resolution is to keep both paths, at honestly different guarantee levels:

- The **harness** emits the session log natively, with a completeness
  guarantee.
- The **existing plugins** gain a best-effort hook-based emitter of the same
  format — clawback-style capture for people who will not leave their harness,
  labeled as best-effort because that is what hook-based capture is.

Both feed the same analytics. The plugins stop being the whole product and
become the on-ramp.

## The session log

Every harness session writes an append-only JSONL log: one file per session,
each line a `NormalizedEvent` (or a superset that projects onto one). The log
is the forensic record, so it carries the same invariants the rest of the
codebase does, plus verifiability. Four tensions are load-bearing and need
deciding before any code exists:

**Non-blocking vs. ordered hash chain.** The writer must never sit in the tool
call's critical path: events go onto a queue, and a single writer thread builds
the chain in arrival order, each record carrying the hash of its predecessor.
Records get sequence numbers on the *emitting* side, so a reordering in the
queue is detectable rather than silently absorbed into the chain.

**Backpressure vs. "nothing is truncated silently."** If the queue ever forces
a drop, the gap is labeled in-stream — a record that says events N through M
were lost, chained like any other. Ossuary's core invariant applies to its own
log with full force: an unlabeled gap in a forensic log manufactures exactly
the artifact the tool exists to detect.

**Verifiability vs. redaction.** `redact.py` exists for a reason, and a hash
chain makes after-the-fact rewriting impossible by design. So either redaction
happens at write time, or redactions are stored as overlay records that
reference the original record's hash without rewriting it. This must be picked
before v1; retrofitting either choice onto the other is ugly.

**Tamper-evidence vs. non-repudiation.** Chained SHA-256 per record plus a
sealed session footer gives tamper-evidence, which is what v1 needs: the log
can prove it was not edited after the fact. Signatures and external
timestamping give non-repudiation, and also bring key management. Defer that,
but leave a field for it in the footer.

A restart mid-session leaves a log with no footer — which is itself the
record: an unsealed log is an interrupted session, visible as such, instead of
the current failure mode where a restarted MCP server comes back empty with no
way to know it ever held anything.

## Agents as config

Agents are defined declaratively — name, model, system prompt, toolset, MCP
servers — and the harness constructs them from that config. This is the part
that pays for everything else, because it changes what the corpus statistics
mean. Today the analytics answer *what went wrong in my sessions*. Clustered on
agents, they answer *which agent configuration is degrading* — fleet health
rather than session autopsy.

One rule makes or breaks it: **sessions are tagged by config hash, not just
agent name.** A session header carries:

| Field | Why |
|---|---|
| agent name | The human-facing cluster key |
| config content hash | The name lies the moment the config is edited |
| harness version | The emitter is part of the experiment |
| resolved model id | "Same agent, new model" is a different population |

Tag only by name and every cluster silently mixes behavior from before and
after each config edit — the per-agent regression signal, the best thing this
feature buys, turns to noise. With the hash, the taxonomy's persistence
(`.ossuary/taxonomy.json`) starts doing real longitudinal work: *this cluster
appeared in `triage-bot` after config `abc123`* is a sentence no session-level
tool can currently say.

## Packaging

The analysis core stays exactly as principled as it is: model-free, no provider
SDK, deterministic. The harness lives in an optional extra (`ossuary[harness]`)
or a sibling package that depends on the core. The README's "Ossuary does not
bring its own model" remains true of the analysis layer — the harness *brings
sessions to* the analyzer; it does not put inference inside it. The design
decision flagged as worth not undoing survives intact, because this adds an
emitter rather than undoing anything.

## Non-goals

A harness invites the full Claude Code feature surface: permission systems,
sandboxing, context compaction, human-in-the-loop UX, an ecosystem. Resist all
of it. The differentiated feature here is exactly one thing:

> Every session produces a complete, verifiable, analyzable record, clustered
> by agent identity.

Ship that thin, on Pydantic AI's loop, and let the analytics remain the
product. Anything on the list above gets built only when its absence blocks
someone from producing or analyzing a record — not because harnesses usually
have it.
