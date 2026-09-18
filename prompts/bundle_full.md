You are mapping this repository for an organization-wide architecture atlas that other AI agents will query.

You cannot read files and you cannot run commands. Everything you know about this repository is in the bundle at the end of this message. It was assembled for you: a file tree, excerpts of the files that describe the repo, and the source lines that name other systems.

Rules:
- Every item in exposes, consumes, and datastores must have evidence: a repo-relative file path that appears in the bundle, optionally with :line. Items without valid evidence are automatically discarded, so never invent a path.
- key must be the literal string another system would use. For consumes, copy the target you found (hostname, service name, default value of an env var, topic name, package name). If only an env var name is known, use the env var name as key and say so in detail.
- identifiers must list every name this repo is known by, so other repos' consumes can be matched to it.
- domain must be one of: {{DOMAINS}}
- components: 3 to 12 top-level modules, taken from the tree. Keep component_edges between those components only.
- A value shown as <redacted> is a secret that was removed before you saw it. Never guess what it was, and never copy a secret value (token, password, key, or a connection string containing credentials) into any field. Name the variable instead.
- Do not guess. The bundle is a sample, not the whole repo, so record what is missing or uncertain in notes rather than inferring it.

When finished, print exactly one JSON object between a line containing only <<<ATLAS_JSON and a line containing only ATLAS_JSON>>>. No markdown fences, and nothing after the closing line.

Schema:
{{SCHEMA}}

Bundle:
{{BUNDLE}}
