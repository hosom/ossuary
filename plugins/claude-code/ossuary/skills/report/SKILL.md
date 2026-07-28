---
description: Render the HTML report from the findings an investigation recorded. Runs no inference.
disable-model-invocation: true
---

# Render the report

Turn `.ossuary/run.json` into a single self-contained HTML file. This involves
no model, which is why it is a separate step from investigating — the report
gets looked at far more often than it gets produced.

```bash
uvx --from "$CLAUDE_PLUGIN_ROOT/../../.." ossuary report --no-open
```

If there are no artifacts yet, the command says so. Findings only reach disk
when `ossuary_write_run` is called, so the usual cause is an investigation that
was never written down: offer to run one with the `investigate` skill, or to
call `ossuary_write_run` now if you have findings recorded in this session.

After rendering, tell the operator the path and summarize the top clusters by
issue count.
