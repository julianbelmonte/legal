# MCP Readiness

An MCP (Model Context Protocol) server is **out of scope** for this project, but
the pipeline is deliberately structured so one can be added later with **no
changes to the pipeline**. This note records the seam it should consume.

## The seam (same one the API uses)

The FastAPI consumer adds no source-access logic of its own: it discovers
sources from the registry and invokes operations through a single uniform
dispatch function. An MCP server should be added as a **sibling consumer**
(e.g. a new `mcp/` package alongside `api/`) that uses the exact same two seams:

1. **Tool enumeration** — `legal.registry.list_sources()` returns the registry
   of sources and their operations. Map each source/operation pair to an MCP
   tool (and/or expose the generic source-agnostic invocation as a single tool
   taking `source`, `operation`, and a params object).

2. **Tool invocation** — `legal.dispatch.run_operation(source, op, params)`
   runs any registered operation from a params dict and returns the same
   normalized JSON envelope the CLI and API produce. This is the single
   agnostic accessor; the MCP server marshals tool arguments into `params` and
   returns the envelope as the tool result.

For a cross-source tool, `legal.global_search.run_global_search` is available as
well (the same function the `/v1/search` endpoint calls).

## Why no pipeline changes are needed

- `run_operation` already validates the source/operation against the registry,
  synthesizes the argparse namespace, calls the handler, and normalizes errors
  into the envelope — identical behavior to the CLI and API.
- The registry is the single source of truth for what tools exist, so the MCP
  tool list stays in sync automatically as sources are added or changed.
- Captcha/proxy/secret configuration is resolved inside the pipeline from the
  same `LEGAL_*` environment (see `DEPLOYMENT.md`), so an MCP deployment is
  configured exactly like the API deployment.

## Sketch (illustrative only — do not implement here)

```python
# mcp/server.py  (future, sibling to api/)
from legal.registry import list_sources
from legal.dispatch import run_operation

def tools():
    # enumerate one tool per source/operation, or a single generic tool
    return list_sources()

def call_tool(source: str, operation: str, params: dict):
    # returns the normalized envelope, 1:1 with the CLI/API
    return run_operation(source, operation, params)
```

That is the entire integration surface: enumerate from the registry, dispatch
through `run_operation`. No new pipeline code.
