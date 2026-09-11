You are mapping this repository for an organization-wide architecture atlas that other AI agents will query.

This is a read-only task. Do not create, edit, or delete files. Do not run shell commands. Use only file reading, listing, and search tools.

Explore efficiently, in roughly this order:
1. README, build manifests (package.json, go.mod, pyproject.toml, pom.xml, build.gradle), CODEOWNERS.
2. Deployment and runtime config: Dockerfile, docker-compose, k8s manifests, Helm charts, Terraform, serverless configs, env example files.
3. API definitions: OpenAPI or Swagger, .proto, GraphQL schemas, AsyncAPI, route registration.
4. Entrypoints, then code that talks to other systems: HTTP clients, gRPC stubs, queue producers and consumers, database connections, SDK clients for other internal services.

Rules:
- Every item in exposes, consumes, and datastores must have evidence: a repo-relative file path you actually opened, optionally with :line. Items without valid evidence are automatically discarded.
- key must be the literal string another system would use. For consumes, copy the target you found (hostname, service name, default value of an env var, topic name, package name). If only an env var name is known, use the env var name as key and say so in detail.
- identifiers must list every name this repo is known by, so other repos' consumes can be matched to it.
- domain must be one of: {{DOMAINS}}
- components: 3 to 12 top-level modules. Keep component_edges between those components only.
- Do not guess. Put uncertainties in notes.

When finished, print exactly one JSON object between a line containing only <<<ATLAS_JSON and a line containing only ATLAS_JSON>>>. No markdown fences, and nothing after the closing line.

Schema:
{{SCHEMA}}
