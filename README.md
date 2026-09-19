# Org atlas for Kiro CLI

Maps every repo with headless Kiro (Haiku), joins the results into one dependency graph, and serves it to Kiro through a local MCP server. Everything lives in your home directory. Nothing is committed to any repo, and secrets are stripped from repo content before it reaches the model.

```
atlas-mcp/   this kit (code, prompts, config)
atlas-src/   dedicated clones of every repo, default branch only (recommended)
atlas/       generated output: repos/*.json, graph.json, docs/, index.md, logs/
~/.kiro/     global steering, MCP registration, permissions
```

## 1. Prerequisites

- git, and [uv](https://docs.astral.sh/uv/). Linux or macOS; the paths below are POSIX.
- No Python setup needed. Both scripts carry PEP 723 inline metadata, so `uv` provisions a 3.11+ interpreter (required for `tomllib`) and, for the MCP server, the `mcp` package. `atlas.py` itself is stdlib-only, so `python3 atlas.py` also works on any 3.11+ system.
- Kiro CLI v3, logged in. For unattended runs without a browser session, set `KIRO_API_KEY` (Pro, Pro+, or Power).
- Clones of every repo under one or two root folders. Use dedicated clones (`atlas-src`), not your working copies, because `--pull` fast-forwards them.

## 2. Install

```bash
cd ~/atlas-mcp
cp config.example.json config.json
uv run --locked --script atlas_mcp.py < /dev/null   # warm the dependency cache once
kiro-cli chat --list-models          # confirm the Haiku model id, put it in config.json "model"
mkdir -p ~/.kiro/agents && cp kiro/agents/atlas-bundle.json kiro/agents/atlas-mapper.json ~/.kiro/agents/
```

That warm-up matters: `atlas_mcp.py.lock` pins `mcp` and its 29 transitive dependencies by hash, but the first run still downloads them. Doing it by hand keeps Kiro from having to resolve during MCP startup, where a slow or offline resolve looks like a broken server.

Edit `config.json`:

- `repo_roots`: folders containing clones (scanned 2 levels deep). `repos`: extra individual paths. `exclude_repos`: names to skip (matches either the resolved atlas name or the directory basename).
- `kiro_bin`: absolute path from `which kiro-cli` (cron has a minimal PATH).
- `mapper_mode`: `bundle` (default) or `explore`. See "How repos get mapped" below.
- `bundle_budget_bytes`: how much repo context a full bundle prompt may carry, default 400 KB (roughly 100k tokens). A bigger bundle gives thin entries more to work with, and the ceiling is the model's context window rather than anything in this tool. Kiro's docs describe credit consumption in terms of what the model generates, its thinking depth and tokenizer differences, and do not name input size, so a bigger bundle should cost little; that is the documented behaviour, not a measured result, so compare the dashboard before and after if you raise this a long way. Update prompts keep their own 96 KB cap.
- `explore_repos`: names forced back to `explore` when `mapper_mode` is `bundle`. Use it for the handful of repos the bundle does not describe well.
- `kiro_agent`: agent passed as `--agent`. Leave it `null` and each repo gets the agent matching its mapper mode (`atlas-bundle` or `atlas-mapper`, both installed above). Keep that; see Troubleshooting for why. Set it to `""` to use Kiro's default agent.
- `domains`: fixed list like `["payments", "identity", "platform"]`. Strongly recommended; without it the model invents inconsistent domain names. Anything off the list is forced to `unassigned` and recorded in the manifest's `_meta.domain_rejected`.
- `repo_domains`: pin a domain deterministically, `{"orders-*": "payments"}`. Globs match the atlas name, first match wins, and a pin always beats the model. `_meta.domain_source` records which one decided. Use it wherever domain membership is a fact you already know.
- `parallel`: concurrent Kiro sessions. Start at 3.
- `repo_names`: pin a name for a clone, `{"/abs/path/to/clone": "orders-service"}`. Needed only if you want a name that differs from the directory, or want it stable regardless of what else gets cloned.
- `generic_identifiers`: extra names that must never act as a weak alias (added to a built-in list of `api`, `db`, `gateway`, and similar). Use this when one repo claims a name so generic it starts matching half the org.
- `max_ambiguous_hits`: if a weak (alias or env-var) match lands on more than this many repos, no edge is created; the consume becomes `unresolved` with a `candidates` shortlist instead. Default 3.
- `ignore_changes`: glob patterns whose changes cannot affect the entry, so a commit touching only those restamps instead of spending credits. It defaults to a built-in list covering tests, docs, images and editor config, which is what the table in section 6 assumes. Set it to override that list, or to `[]` to disable restamping entirely.

## 3. How repos get mapped

Two modes, chosen by `mapper_mode`.

**`bundle` (default).** `atlas.py` reads the repo itself and assembles a deterministic context bundle from the files git tracks, so `.gitignore` decides what counts as part of the repo and build output never costs budget: a file tree, excerpts of the files that describe a repo (README, CODEOWNERS, build manifests, Dockerfile and compose, k8s and Helm, Terraform, env examples, OpenAPI, proto, GraphQL, entrypoints), and the source lines that name other systems (URLs, env-var targets, topics and queues, datastore connections, client constructors). Kiro then gets one call, with no tools at all, to turn that bundle into the entry. One model call per repo, no tool loop, so there is no approval prompt to hang on and the spend is a fraction of exploring.

The bundle is capped at `bundle_budget_bytes`: the tree gets up to 10% of it, key files up to 70% cumulative, and the rest is reserved for signal lines so a monorepo full of manifests cannot starve the part that feeds the graph join. The prompt goes to Kiro on stdin, so the budget is bounded by the model's context window rather than by how long a single command-line argument may be. A path git will not list, such as a folder that is not a clone, falls back to walking the directory with a built-in list of vendored and build directories to skip.

**`explore`.** The original behaviour: Kiro explores the repo with its read tool and decides what to look at. Slower and more expensive, but it can follow a trail the bundle missed. Put individual repos in `explore_repos` to use it for them alone.

**Not everything is left to the model.** Whatever mode is in use, `atlas.py` also reads the repo's own structured files itself and merges the result into the entry: owners from CODEOWNERS, names and owners from `catalog-info.yaml`, service names and ingress hosts from Kubernetes manifests, the chart name from `Chart.yaml`, built services from compose files, server hosts from OpenAPI, fully-qualified service names from `.proto`, and owned queues, buckets and tables from Terraform. These items cost no credits, carry `"source": "deterministic"`, and win over the model's version of the same name, which puts the edges they feed on the `exact` tier. Machine-read names are filtered through `generic_identifiers` first, since nothing stops a compose file from calling a service `api`. Deterministic items still have to pass the evidence gate, so a parser that points at a file the repo does not have shows up in `_meta.dropped_without_evidence` like any other bad claim.

Each entry also records where the repo lives and when it last moved: `_meta.remote_url`, normalised to one credential-free https form whatever the clone used, and `_meta.last_commit_at`, the committer date of HEAD. Both are copied into `graph.json` and returned by the `list_repos` and `get_repo` MCP tools, so an agent can link to the hosted repo and see that an entry describes something nobody has touched in a year.

Both modes redact before the model sees anything: private key blocks, assignments to secret-looking keys, credentials inside URLs, tokens whose provider prefix gives them away (AWS access key ids, GitHub, Slack, Google, JWTs), and long opaque quoted literals become `<redacted>`. The prompts tell the model never to copy a secret value into an entry. Redaction is a safety net over the fact that the bundle prefers `.env.example` over `.env`, not a licence to point the mapper at a repo full of live keys.

Both modes record the prompt hash they were generated with. Editing a prompt or the schema therefore regenerates every entry in full on the next run, rather than leaving a mix of old and new shapes in the atlas until the 30-day full regeneration comes round. Entries also record `_meta.facts_version`; changing anything `atlas.py` reads out of a repo itself bumps it, whether that is a deterministic extractor or a new field in `_meta`, and a repo that would otherwise have been skipped restamps instead, which re-reads it all without a model call. That is also why CODEOWNERS staying inside `ignore_changes` costs nothing: an owners-only commit refreshes the owners on the free path.

## 4. Pilot, then full run

```bash
uv run --script atlas.py generate --dry-run          # spends nothing
uv run --script atlas.py generate --only a b c       # a representative mix
```

Each run ends with a summary line: `done: 12 repos in 5m12s | full 4, update 6, restamp 1, skip 1 | 0 errors | 1.2 MB of prompts`. Kiro cannot report credits in headless mode, so that line plus `_meta.prompt_bytes` in each manifest is how you reconcile a run against the Kiro dashboard. Check credit usage in Kiro, then read `atlas/docs/*.md` and `atlas/graph.json`. This pilot is also how you judge whether the bundle is enough for your repos: if an entry is thin or its consumes are missing, rerun that repo with `explore_repos` set and compare `_meta.dropped_without_evidence` and the `unresolved` count between the two.

`--limit N` caps how many repos a run touches, for a bigger pilot than `--only` without going all the way to a full run. `--no-build` skips rebuilding `graph.json` and the docs after mapping, useful when queuing several partial runs and only building once at the end.

- `uv run --script atlas.py unresolved --top 25` groups every unmatched consume by target. This is the fastest way to see what the join is missing. Entries with `candidates=` were deliberately not linked because the match was weak and ambiguous.
- Many `unresolved` consumes that are really internal: the provider repo is missing that name in `identifiers`. Rerun it with `--only NAME --full`.
- `dropped_without_evidence` in a manifest's `_meta`: items the model claimed without a real file. Some is normal; lots means the repo needs a stronger model (set `model` and use `--only`).

Then run everything: `uv run --script atlas.py generate`.

## 5. Connect Kiro

Copy `kiro/steering/atlas.md` to `~/.kiro/steering/atlas.md`, then pick one of the two registrations below. Do not do both: each one registers a server named `atlas`.

**An agent (recommended).** Copy `kiro/agents/atlas.json` to `~/.kiro/agents/atlas.json` and edit the absolute paths. It declares the atlas server, pre-approves its tools, and lists the steering file in `resources` (the relative path resolves from the agent file, so keep the file in `~/.kiro/agents/`). Use it with `kiro-cli chat --agent atlas`.

**The global MCP file.** Merge `kiro/mcp.json` into `~/.kiro/settings/mcp.json` using absolute paths, because Kiro does not inherit your shell PATH. Optionally merge `kiro/permissions.yaml` into `~/.kiro/settings/permissions.yaml` (edit paths) to stop the atlas tools prompting on each call.

Either way, start a new `kiro-cli` session, run `/mcp` to confirm `atlas` is connected, then ask something like "what depends on payments-service?"

The agent is the recommended path because a steering file in `~/.kiro/steering/` is not injected when the workspace has its own `.kiro/steering/`, which is the normal case in an org repo ([kirodotdev/Kiro#8121](https://github.com/kirodotdev/Kiro/issues/8121), [aws/amazon-q-developer-cli#3719](https://github.com/aws/amazon-q-developer-cli/issues/3719)). An agent that lists the file as a resource loads it either way.

## 6. Keep it current

`generate` only spends credits where needed:

| Situation | Mode | Cost |
| --- | --- | --- |
| HEAD unchanged | skip | none |
| Only tests, docs, images changed (`ignore_changes`) | restamp | none |
| HEAD unchanged but the deterministic extractors have moved on | restamp | none |
| Up to `max_changed_files_for_update` relevant files changed | update (old entry + changed file list) | small |
| New repo, big change, history rewritten, or last full run older than `full_regen_days` | full | normal |

Schedule it, for example every 6 hours with cron. Spell out the path to `uv` from `which uv`; cron has a minimal PATH, the same reason `kiro_bin` needs an absolute path:

```
0 */6 * * * cd $HOME/atlas-mcp && $HOME/.local/bin/uv run --script atlas.py generate --pull >> $HOME/atlas/cron.log 2>&1
```

**Pick an interval longer than a full run.** The worst case is `repos / parallel * timeout_minutes`, roughly 11 hours over 100 repos at the defaults. That bound belongs to `explore` mode, where a session can sit at the timeout; a `bundle` run is one call per repo and finishes far inside it. Size the interval for the worst case anyway, because a stuck session is exactly when it matters. A lock file (`atlas/.generate.lock`) makes a second run exit immediately rather than double-spend credits, so a short interval is safe but pointless. `--dry-run` does not take the lock, so you can always see what is queued while a real run is in flight. Where `flock` is unavailable, clear a lock left by a killed process with `--force-unlock`.

`atlas.py status` shows which entries are stale, plus any orphans. The MCP `freshness` tool gives Kiro the same view.

### Housekeeping

- `atlas.py prune` lists manifests with no matching clone (repo deleted, renamed, or newly excluded). They are excluded from the graph automatically; `--apply` moves the files to `atlas/repos/_orphans/`. Nothing is ever deleted.
- `build`, `prune`, and `status` refuse to run when repo discovery comes back empty, so an unmounted `repo_roots` cannot quietly empty the atlas.

## Troubleshooting

- **Timeouts:** in `explore` mode, almost always a tool call waiting for approval in headless mode. `bundle` mode has no tools to approve, so a timeout there is the model or the network. Check `atlas/logs/NAME.log`, which keeps one section per attempt. Reading inside the repo is allowed by default in v3; if the model insists on other tools, pass a narrow trust flag via `kiro_extra_args` (see `kiro-cli chat --help` for `--trust-tools`), never `--trust-all-tools`.
- **"no `<<<ATLAS_JSON` block":** the model did not finish or ignored the format. It retries once; both attempts are in the log. Runs pass `--wrap never` so a long JSON line is never hard-wrapped into invalid JSON; if the log shows an unrecognized `--wrap` argument, upgrade to Kiro CLI v3. Adding `--output-format stream-json` (plus the `--engine` it requires) to `kiro_extra_args` also works, because the parser falls back to reading the string leaves of a JSON Lines transcript, but that path has not been run against a live CLI.
- **Mapping runs go through a dedicated agent.** Without one, `kiro-cli` maps each repo under that repo's own workspace configuration, which means starting whatever MCP servers and hooks the repo declares, unattended, with your environment and `KIRO_API_KEY`. `atlas-mapper` (explore mode) sets `includeMcpJson: false`, no MCP servers, and read-only tools. `atlas-bundle` (bundle mode) declares no tools at all, and its session runs from `atlas_dir` rather than inside the mapped repo, so none of that repo's configuration is loaded in the first place. Kiro still inherits default resources such as workspace steering unless you set `chat.disableInheritingDefaultResources`; that is text in the prompt, not code that runs.
- **Matching is string-based.** Consumes are matched to providers by hostname, service name, topic, package name, or owned datastore name. Each edge carries a `match` tier: `exact` (literal identifier or host), `alias` (the first label of a hostname), `envvar` (derived from an env var name such as `ORDERS_SERVICE_URL` -> `orders-service`), or `ambiguous` (more than one candidate). Treat anything but `exact` as a lead and check the evidence path. Names read deterministically out of deploy and API manifests are indexed as `exact`, so a repo with a `catalog-info.yaml` or a Kubernetes `Service` lands there even when the model's entry is thin. An identifier containing a `/` but no scheme (a Go module path, a scoped npm name) is indexed as a package name rather than a hostname, so `github.com/org/a` never makes its repo answer for `github.com`. More candidates than `max_ambiguous_hits` leaves the consume unresolved with a shortlist, except for topics and queues, where several producers of one name are normal and each of those edges keeps the tier it matched at rather than being demoted to `ambiguous`.
- **A repo is huge and updates keep regenerating in full.** Update prompts are capped at 96 KB, separately from `bundle_budget_bytes`, because an update also carries the old entry and the changed-file list; past that the run falls back to a full regeneration rather than truncating the entry. Reduce `max_changed_files_for_update` if you would rather cap the changed-file list.

## Development

```bash
uv run --group dev pytest
```

`pyproject.toml` holds the test configuration and the dev dependency group. It deliberately sets `package = false`: this is two standalone scripts, not an installable package, so nothing is ever built or installed. Runtime dependencies live in each script's `# /// script` header instead, because `uv run --script` ignores `pyproject.toml` entirely.

Two lockfiles, with different jobs:

- `atlas_mcp.py.lock` pins the MCP server's runtime, `mcp` plus its 29 transitive packages, by hash. This is the one that matters operationally, because Kiro launches the server with `--locked`.
- `uv.lock` pins the dev toolchain so the test suite is reproducible.

The tests cover the deterministic core with no Kiro calls and no credits spent: discovery and naming, package extraction for npm, Go, Python, Maven, Gradle, Cargo and NuGet, the yaml subset parser and the deterministic extraction it feeds (CODEOWNERS, `catalog-info.yaml`, Kubernetes, Helm, compose, OpenAPI, proto, Terraform) along with how those facts merge into a model entry and refresh on a restamp, secret redaction, the remote URL and last commit date recorded for each repo, the context bundle over the files git tracks and its budget, evidence gating, manifest normalisation, the graph join and its match tiers, the lock, the prompt going to Kiro on stdin within its configured budget, the run summary, `generate` end to end in both mapper modes, every MCP tool including `impact`, the read-only annotations, and the server answering over stdio.

To move to a newer `mcp`, edit the pin in the `# /// script` block at the top of `atlas_mcp.py`, run `uv lock --script atlas_mcp.py`, and commit the regenerated lockfile. The server imports either `mcp` 1.x or 2.x, so the pin is for reproducibility in unattended runs rather than compatibility, though only the pinned 2.x is exercised by the tests.
