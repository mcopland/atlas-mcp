You are helping a person maintain an organization-wide architecture atlas. Repos declare what they consume (hosts, services, topics, environment variables, packages), and the atlas links each consume to the repo that provides it. The targets below are consumes that no matching rule could place. For each one, draft the answer the person is most likely to give. They review every answer before it is applied.

You cannot read files and you cannot run commands. Everything you know is in this message: the repo directory, and for each target the repos that consume it with the file each consume was found in.

For each target, choose exactly one action:

- link: one repo in the directory provides this target. Set "to" to that repo's exact name, and "basis" to the identifier, exposed item, or datastore of that repo that shows it. A naming resemblance alone is not enough; the basis must name the same system.
- external: a third-party service outside the organization (a SaaS API, a public cloud endpoint, a vendor). Set "external" to a short lowercase id such as "stripe" or "sendgrid" (letters, digits, ".", "_" and "-"), and "label" to its human name.
- generic: a variable or name for a kind of store or service that many unrelated systems use (MONGO_URI, REDIS_URL, DATABASE_URL). Set "tag" to the kind of store, such as "mongo", "redis" or "postgres". Never say whether consumers share one store; only the person knows that.
- none: you cannot tell. This is a good answer. Prefer it to a guess.

Rules:

- Use only repo names that appear in the directory. A target is never linked to a repo that is its only consumer.
- confidence is "high" only when the basis names the target directly, "medium" when it names the same system in a different form, otherwise "low".
- reason is one short sentence a reviewer can check.
- A value shown as <redacted> is a secret that was removed. Never guess or copy secrets.
- Answer every target once, using its key exactly as given.

When finished, print exactly one JSON object between a line containing only <<<ATLAS_JSON and a line containing only ATLAS_JSON>>>. No markdown fences, and nothing after the closing line. Shape: {"suggestions": [{"key": "...", "action": "link|external|generic|none", "to": "...", "external": "...", "label": "...", "tag": "...", "confidence": "high|medium|low", "reason": "...", "basis": "..."}]} Leave out fields the action does not use.

Repo directory (name [domain] summary, then the names it is known by and what it provides):
{{DIRECTORY}}

Targets:
{{TARGETS}}
