---
description: Run a reproducible Ossuary scan across the whole transcript corpus and render the HTML report.
disable-model-invocation: true
---

# Scan the corpus

Run the batch pipeline rather than investigating interactively. Use this when
the operator wants a number they can compare against next week's, not an
exploration: `ossuary scan` caps turns, hashes the prompt, and caches per
transcript by content, so an unchanged session costs nothing on a re-scan.

$ARGUMENTS is passed through to `ossuary scan` (e.g. `--limit 50`,
`--source claude-code`, `--model claude-code:sonnet`).

Steps:

1. Check what is installed and what it will authenticate as:

   ```bash
   uvx --from 'ossuary[claude-code]' ossuary backends
   uvx --from 'ossuary[claude-code]' ossuary agents show
   ```

   If `agents.yaml` names a `claude-code:` or `copilot:` model, no Anthropic API
   key is needed -- the scan uses whatever the corresponding CLI is logged in
   as. If it names `anthropic:` and `ANTHROPIC_API_KEY` is unset, say so and
   offer to switch the config rather than letting the run fail on credentials.

2. Run the scan, passing through whatever the operator asked for:

   ```bash
   uvx --from 'ossuary[claude-code]' ossuary scan $ARGUMENTS
   ```

   This is the expensive step. It writes `.ossuary/run.json`.

3. Render the report:

   ```bash
   uvx --from 'ossuary[claude-code]' ossuary report --no-open
   ```

4. Read the scan's own summary line and tell the operator what came out: how
   many sessions, how many issues, how many clusters, and which of those
   clusters are new this run. If the run reported errors, quote them rather
   than summarizing.
