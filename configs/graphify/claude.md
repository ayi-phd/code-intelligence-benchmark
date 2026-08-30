## graphify — REQUIRED workflow

This project has a pre-built knowledge graph. You MUST follow this exact workflow. Skipping any step is not allowed.

### Step 1 — Orient (do this first, before anything else)
Call BOTH of these, in order:
1. `mcp__graphify__graph_stats` — get graph size and top files
2. `mcp__graphify__god_nodes` — get the most-connected hub files

### Step 2 — Query before every exploration action
Before reading ANY source file or running ANY grep/find/ls command, you MUST first call `mcp__graphify__query_graph` with a question describing what you are looking for. No exceptions. The query result tells you which files to read and why — only then open them.

### Step 3 — Use graph tools for relationships
- To understand how two components relate: `mcp__graphify__shortest_path` (REQUIRED before reading both files)
- To see callers or dependencies of a node: `mcp__graphify__get_node` or `mcp__graphify__get_neighbors`
- To understand what cluster a file belongs to: `mcp__graphify__get_community`

### MCP tools reference

| Tool | Purpose |
|------|---------|
| `mcp__graphify__graph_stats` | Step 1a — overall graph stats |
| `mcp__graphify__god_nodes` | Step 1b — hub files list |
| `mcp__graphify__query_graph` | Step 2 — REQUIRED before every grep or file read |
| `mcp__graphify__shortest_path` | Step 3 — relationship between two components |
| `mcp__graphify__get_node` | Step 3 — callers and dependencies of one node |
| `mcp__graphify__get_neighbors` | Step 3 — direct neighbors of one node |
| `mcp__graphify__get_community` | Step 3 — cluster membership |
