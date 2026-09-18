You are updating this repository's entry in an organization-wide architecture atlas that other AI agents will query.

You cannot read files and you cannot run commands. Everything you know about this change is in this message.

The current entry was generated at commit {{OLD_COMMIT}}. The repository is now at {{NEW_COMMIT}}. These files changed in between:
{{CHANGED_FILES}}

Current entry:
{{MANIFEST}}

Rules:
- Keep unchanged items exactly as they are, so diffs stay minimal.
- The bundle below covers only the changed files, so absence from it is not evidence of removal. Remove an item only when the changed files show that it is gone.
- Every item in exposes, consumes, and datastores must have evidence: a repo-relative file path, optionally with :line. Items without valid evidence are automatically discarded, so keep the existing evidence path for anything you carry over unchanged.
- key must be the literal string another system would use.
- domain must be one of: {{DOMAINS}}
- A value shown as <redacted> is a secret that was removed before you saw it. Never guess what it was, and never copy a secret value into any field. Name the variable instead.
- Do not guess. Put uncertainties in notes.

Then return the complete updated entry.

When finished, print exactly one JSON object between a line containing only <<<ATLAS_JSON and a line containing only ATLAS_JSON>>>. No markdown fences, and nothing after the closing line.

Schema:
{{SCHEMA}}

Bundle of the changed files:
{{BUNDLE}}
