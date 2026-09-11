You are updating this repository's entry in an organization-wide architecture atlas that other AI agents will query.

This is a read-only task. Do not create, edit, or delete files. Do not run shell commands. Use only file reading, listing, and search tools.

The current entry was generated at commit {{OLD_COMMIT}}. The repository is now at {{NEW_COMMIT}}. These files changed in between:
{{CHANGED_FILES}}

Current entry:
{{MANIFEST}}

Read the changed files, and anything they reference that affects what this repo exposes, consumes, stores, or how its components fit together. Then return the complete updated entry.

Rules:
- Keep unchanged items exactly as they are, so diffs stay minimal.
- Remove items whose evidence no longer exists or no longer supports them. Add new items with evidence.
- Every item in exposes, consumes, and datastores must have evidence: a repo-relative file path, optionally with :line. Items without valid evidence are automatically discarded.
- key must be the literal string another system would use.
- domain must be one of: {{DOMAINS}}
- Do not guess. Put uncertainties in notes.

When finished, print exactly one JSON object between a line containing only <<<ATLAS_JSON and a line containing only ATLAS_JSON>>>. No markdown fences, and nothing after the closing line.

Schema:
{{SCHEMA}}
