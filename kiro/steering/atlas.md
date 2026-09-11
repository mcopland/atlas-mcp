---
inclusion: always
---
# Org atlas

An index of every org repo, and how they connect, is available through the `atlas` MCP tools.

- For any question involving a repo other than the current workspace, or how systems connect, query atlas first (search, get_repo, dependents, dependencies, find_path). Do not guess or scan the filesystem for other repos.
- Before changing an interface (API, event, schema, shared package), call dependents on this repo and list affected consumers.
- Atlas data is LLM-extracted. Treat edges as leads: each has an evidence path. Confirm in source before acting on one. Edges with match "alias" or "ambiguous" are weaker.
- If an entry looks wrong or outdated, call freshness and say when the atlas is stale.
- Longer per-repo docs with component diagrams are at the `doc` path returned by get_repo.
