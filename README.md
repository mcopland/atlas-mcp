# Org atlas for Kiro CLI

Maps every repo with headless Kiro (Haiku), joins the results into one dependency graph, and serves it to Kiro through a local MCP server. Everything lives in your home directory. Nothing is committed to any repo.

```
atlas-mcp/   this kit (code, prompts, config)
atlas-src/   dedicated clones of every repo, default branch only (recommended)
atlas/       generated output: repos/*.json, graph.json, docs/, index.md, logs/
~/.kiro/     global steering, MCP registration, permissions
```

## 1. Prerequisites

- git, and [uv](https://docs.astral.sh/uv/). Linux or macOS; the paths below are POSIX.
- No Python setup needed. Both scripts carry PEP 723 inline metadata, so `uv` provisions a
  3.11+ interpreter (required for `tomllib`) and, for the MCP server, the `mcp` package.
  `atlas.py` itself is stdlib-only, so `python3 atlas.py` also works on any 3.11+ system.
- Kiro CLI v3, logged in. For unattended runs without a browser session, set `KIRO_API_KEY` (Pro, Pro+, or Power).
- Clones of every repo under one or two root folders. Use dedicated clones (`atlas-src`), not your working copies, because `--pull` fast-forwards them.

## 2. Install

```bash
cd ~/atlas-mcp
cp config.example.json config.json
uv run --locked --script atlas_mcp.py < /dev/null   # warm the dependency cache once
kiro-cli chat --list-models          # confirm the Haiku model id, put it in config.json "model"
```

That warm-up matters: `atlas_mcp.py.lock` pins `mcp` and its 29 transitive dependencies by
hash, but the first run still downloads them. Doing it by hand keeps Kiro from having to
resolve during MCP startup, where a slow or offline resolve looks like a broken server.

Edit `config.json`:

- `repo_roots`: folders containing clones (scanned 2 levels deep). `repos`: extra individual paths. `exclude_repos`: names to skip (matches either the resolved atlas name or the directory basename).
- `kiro_bin`: absolute path from `which kiro-cli` (cron has a minimal PATH).
- `domains`: fixed list like `["payments", "identity", "platform"]`. Strongly recommended; without it the model invents inconsistent domain names. Anything off the list is forced to `unassigned` and recorded in the manifest's `_meta.domain_rejected`.
- `parallel`: concurrent Kiro sessions. Start at 3.
- `repo_names`: pin a name for a clone, `{"/abs/path/to/clone": "orders-service"}`. Needed only if you want a name that differs from the directory, or want it stable regardless of what else gets cloned.
- `generic_identifiers`: extra names that must never act as a weak alias (added to a built-in list of `api`, `db`, `gateway`, and similar). Use this when one repo claims a name so generic it starts matching half the org.
- `max_ambiguous_hits`: if a weak (alias or env-var) match lands on more than this many repos, no edge is created; the consume becomes `unresolved` with a `candidates` shortlist instead. Default 3.

## 3. Pilot, then full run

```bash
uv run --script atlas.py generate --dry-run          # spends nothing
uv run --script atlas.py generate --only a b c       # a representative mix
```

Check credit usage in Kiro, then read `atlas/docs/*.md` and `atlas/graph.json`:

- `uv run --script atlas.py unresolved --top 25` groups every unmatched consume by target. This is the fastest way to see what the join is missing. Entries with `candidates=` were deliberately not linked because the match was weak and ambiguous.
- Many `unresolved` consumes that are really internal: the provider repo is missing that name in `identifiers`. Rerun it with `--only NAME --full`.
- `dropped_without_evidence` in a manifest's `_meta`: items the model claimed without a real file. Some is normal; lots means the repo needs a stronger model (set `model` and use `--only`).

Then run everything: `uv run --script atlas.py generate`.

## 4. Connect Kiro (global)

1. Merge `kiro/mcp.json` into `~/.kiro/settings/mcp.json`. Use absolute paths; Kiro does not inherit your shell PATH.
2. Copy `kiro/steering/atlas.md` to `~/.kiro/steering/atlas.md`.
3. Optionally merge `kiro/permissions.yaml` into `~/.kiro/settings/permissions.yaml` (edit paths) to stop the atlas tools prompting on each call.
4. Start a new `kiro-cli` session, run `/mcp` to confirm `atlas` is connected, then ask something like "what depends on payments-service?"

## 5. Keep it current

`generate` only spends credits where needed:

| Situation | Mode | Cost |
|---|---|---|
| HEAD unchanged | skip | none |
| Only tests, docs, images changed (`ignore_changes`) | restamp | none |
| Up to `max_changed_files_for_update` relevant files changed | update (old entry + changed file list) | small |
| New repo, big change, history rewritten, or last full run older than `full_regen_days` | full | normal |

Schedule it, for example every 6 hours with cron. Spell out the path to `uv` from `which uv`; cron has a minimal PATH, the same reason `kiro_bin` needs an absolute path:

```
0 */6 * * * cd $HOME/atlas-mcp && $HOME/.local/bin/uv run --script atlas.py generate --pull >> $HOME/atlas/cron.log 2>&1
```

**Pick an interval longer than a full run.** A first run over 100 repos can take `repos / parallel * timeout_minutes` in the worst case (roughly 11 hours at the defaults), so a 2-hour cron would start stacking runs. A lock file (`atlas/.generate.lock`) makes a second run exit immediately rather than double-spend credits, so a short interval is safe but pointless. Where `flock` is unavailable, clear a lock left by a killed process with `--force-unlock`.

`atlas.py status` shows which entries are stale, plus any orphans. The MCP `freshness` tool gives Kiro the same view.

### Housekeeping

- `atlas.py prune` lists manifests with no matching clone (repo deleted, renamed, or newly excluded). They are excluded from the graph automatically; `--apply` moves the files to `atlas/repos/_orphans/`. Nothing is ever deleted.
- `build`, `prune`, and `status` refuse to run when repo discovery comes back empty, so an unmounted `repo_roots` cannot quietly empty the atlas.

## Troubleshooting

- **Timeouts:** almost always a tool call waiting for approval in headless mode. Check `atlas/logs/NAME.log`, which keeps one section per attempt. Reading inside the repo is allowed by default in v3; if the model insists on other tools, pass a narrow trust flag via `kiro_extra_args` (see `kiro-cli chat --help` for `--trust-tools`), never `--trust-all-tools`.
- **"no `<<<ATLAS_JSON` block":** the model did not finish or ignored the format. It retries once; both attempts are in the log.
- **Mapping sessions also see the atlas tools and steering** because they are global. That is harmless, but if you want clean runs, create a read-only mapper agent without MCP and set `"kiro_extra_args": ["--agent", "atlas-mapper"]`.
- **Matching is string-based.** Consumes are matched to providers by hostname, service name, topic, package name, or owned datastore name. Each edge carries a `match` tier: `exact` (literal identifier or host), `alias` (the first label of a hostname), `envvar` (derived from an env var name such as `ORDERS_SERVICE_URL` -> `orders-service`), or `ambiguous` (more than one candidate). Treat anything but `exact` as a lead and check the evidence path. An identifier containing a `/` but no scheme (a Go module path, a scoped npm name) is indexed as a package name rather than a hostname, so `github.com/org/a` never makes its repo answer for `github.com`. More candidates than `max_ambiguous_hits` leaves the consume unresolved with a shortlist, except for topics and queues, where several producers of one name are normal.
- **A repo is huge and updates keep regenerating in full.** Update prompts are capped at 96 KB; past that the run falls back to a full regeneration rather than truncating the entry. Reduce `max_changed_files_for_update` if you would rather cap the changed-file list.

## Development

```bash
uv run --group dev pytest
```

`pyproject.toml` holds the test configuration and the dev dependency group. It deliberately
sets `package = false`: this is two standalone scripts, not an installable package, so nothing
is ever built or installed. Runtime dependencies live in each script's `# /// script` header
instead, because `uv run --script` ignores `pyproject.toml` entirely.

Two lockfiles, with different jobs:

- `atlas_mcp.py.lock` pins the MCP server's runtime, `mcp` plus its 29 transitive packages, by
  hash. This is the one that matters operationally, because Kiro launches the server with
  `--locked`.
- `uv.lock` pins the dev toolchain so the test suite is reproducible.

The tests cover the deterministic core (discovery and naming, package extraction, evidence gating, the graph join and its match tiers, the lock, and the MCP tools) with no Kiro calls and no credits spent.

To move to a newer `mcp`, edit the pin in the `# /// script` block at the top of `atlas_mcp.py`, run `uv lock --script atlas_mcp.py`, and commit the regenerated lockfile. The server supports both `mcp` 1.x and 2.x, so the pin is for reproducibility in unattended runs rather than compatibility.
