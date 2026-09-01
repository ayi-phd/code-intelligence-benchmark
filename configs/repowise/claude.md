## Codebase Intelligence (Repowise)

### Follow the below rules when working in this repo
Step 1. For unknown repo, run get_overview() once. Do not run it again.
Step 2. For Planning & Implementation stages when exploring files, symbols, and dependencies, MUST use MCP tool get_context. To get symbol bodies, MUST use MCP tool get_symbol. Use Read or Grep to read raw files ONLY if you cannot get the necessary information from MCP tools.
Note: Trust the index created by Repowise. `verified: true` means the bytes were checked against the live tree, so never re-read those lines. Re-read only on `bounds: "approximate"`, `_meta.stale_warning`, `search_method: "bm25"` or `confidence: "low"`; `index_behind: true` alone is informational.

### Tools
| Tool | When and why |
|------|--------------|
| `get_overview()` | Architecture map. Call once, first, in an unfamiliar repo; skip it after that. |                                                          
| `get_context(targets=[...])` | Triage card for files/modules/symbols: docs, signatures, hotspot, fix history. No source bytes — `include=["skeleton"]` for the whole file verified, `["callers"|"callees"]` for dependencies. Batch targets. |
| `get_symbol(id)` | **Follow-up, not an entry point** — one verified body for an id a prior response named (`path.py::Name`, `path.py:140-180`, `repowise#<hex>`). |
| `search_codebase(query)` | Matching only, no semantic search |
