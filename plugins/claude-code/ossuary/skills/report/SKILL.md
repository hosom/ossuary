---
description: Render the HTML report from the artifacts an earlier Ossuary run wrote. Runs no inference.
disable-model-invocation: true
---

# Render the report

Turn `.ossuary/run.json` into a single self-contained HTML file. This is cheap
and involves no model, which is the whole reason it is a separate step from
scanning -- the report design gets iterated on dozens of times and must never
re-pay for inference to do it.

```bash
uvx --from 'ossuary[mcp]' ossuary report --no-open
```

If there are no artifacts yet, the command says so. In that case either run
`/ossuary:scan`, or ask the operator whether they want an interactive
investigation instead -- the `investigate` skill records findings through the
MCP tools and `ossuary_write_run` writes the same artifacts this command reads.

After rendering, tell the operator the path and summarize the top clusters by
issue count.
