# Backends

Ossuary's two agents are the same shape: a system prompt, one user turn, a
handful of Python tools, and a turn cap. Nothing about that shape is specific to
an API vendor, so it is described once in `ossuary.backends.base` and implemented
three ways. The `model` field in `agents.yaml` names the backend as its prefix.

| Prefix | Backend | Authenticates as | Temperature |
| --- | --- | --- | --- |
| `claude-code:` | Claude Agent SDK | whatever `claude` is logged in as — including a Pro or Max subscription | not settable |
| `copilot:` | GitHub Copilot SDK | whatever `gh` is logged in as — including a Copilot subscription | not settable |
| anything else | Pydantic AI | a provider API key, or a local endpoint | settable |

```yaml
agents:
  scanner:
    model: claude-code:haiku      # or copilot:gpt-5, anthropic:claude-haiku-4-5
```

`ossuary backends` shows which SDKs are installed on this machine.
`ossuary agents show` shows what the current config resolves to, including
whether the configured temperature is going to be ignored.

## claude-code — the Claude Agent SDK

```bash
uv pip install -e '.[claude-code]'
```

The Agent SDK runs the Claude Code harness as a library and inherits whatever
credentials that harness already has. Ossuary holds no credential of its own and
never sees one.

Two things are pinned deliberately in `backends/claude_agent.py`:

- **`setting_sources=[]`.** Without it the harness loads the operator's
  `CLAUDE.md`, skills, and plugins into the scan. Ossuary's premise is that
  Agent A is given no taxonomy of known problems; inheriting project
  instructions would smuggle one in and make findings differ machine to machine.
- **`permission_mode="dontAsk"` with only Ossuary's own tools pre-approved.**
  The scanner has no business reading the filesystem directly. Every byte it
  sees arrives through the store, which redacts and elides on the way out.

Sampling parameters are not exposed by this backend. A configured `temperature`
is reported as ignored rather than silently dropped — if you need run-to-run
determinism, use the Pydantic AI backend.

**On distribution.** Anthropic's Agent SDK documentation states that, unless
previously approved, third-party developers may not *offer* claude.ai login or
subscription rate limits to the users of their products. Running Ossuary on your
own machine against your own Claude Code login is ordinary local tool use, the
same as any script that shells out to `claude -p`. Shipping a hosted product
where *other people* sign in with their Claude subscriptions is the thing that
needs approval. If you are packaging Ossuary for other people to run as a
service, use API-key auth, or ship the [plugin](plugins.md) — inside Claude Code
the question does not arise, because the host is doing the inference.

## copilot — the GitHub Copilot SDK

```bash
uv pip install -e '.[copilot]'
```

The Copilot SDK exposes the same agent runtime as the Copilot CLI as a library
and authenticates the same way: the signed-in `gh` user, a `github_token`, or a
bring-your-own-key provider. The same two lockdowns apply as above — repository
instructions, skills, and plugin directories are switched off, and built-in
tools are switched off so everything the agent reads arrives through Ossuary.

Backend-specific knobs go under `extra`:

```yaml
agents:
  scanner:
    model: copilot:gpt-5
    extra:
      reasoning_effort: medium    # low | medium | high | xhigh
      timeout_seconds: 900        # a full session investigation outlasts the 60s default
      github_token: ...           # usually unnecessary; the signed-in user is used
```

## pydantic-ai — API key or local

The original path, and the only one that can pin a temperature. Any model string
Ossuary does not claim is handed to Pydantic AI unchanged, so every provider
string that worked before the backend split still works.

```yaml
model: anthropic:claude-haiku-4-5   # ANTHROPIC_API_KEY
model: ollama:qwen2.5-coder         # OLLAMA_BASE_URL, default http://localhost:11434/v1
model: openai-compatible:my-model   # OSSUARY_OPENAI_BASE_URL, any proxy
```

`ollama:` and `openai-compatible:` are the fully-local configurations. Running a
redaction pass and then shipping transcripts to a hosted API is a weaker privacy
story than never sending them at all.

## Adding a backend

Implement `AgentBackend.run` in `ossuary/backends/`, register the prefix in
`BACKENDS`, and add it to the `rows` table in the `backends` CLI command. Tools
and prompts need no changes: they are described once, in `agents/tools.py` and
`agents.yaml`, precisely so that switching where inference runs cannot change
what the agent is able to see.

Two contracts a backend must honour:

- **A turn-cap hit is a result, not an exception.** Return
  `AgentRunResult(hit_turn_cap=True)`. Issues are reported incrementally through
  tools so that a cutoff yields partial findings; raising throws that away.
- **Never swallow a `ToolSpec` error yourself.** `ToolSpec.call` already turns a
  failure into a `[[ossuary:tool-error ...]]` result the model can read and
  recover from. Backends disagree about what a raised exception means — retry,
  abort, swallow — and that disagreement would otherwise show up as different
  findings from the same transcript.
