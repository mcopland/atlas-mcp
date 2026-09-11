# Org atlas for Kiro CLI

Maps every repo with headless Kiro (Haiku), joins the results into one dependency graph, and serves it to Kiro through a local MCP server. Everything lives in your home directory. Nothing is committed to any repo.

```
atlas-kit/   this kit (code, prompts, config)
atlas-src/   dedicated clones of every repo, default branch only (recommended)
atlas/       generated output: repos/*.json, graph.json, docs/, index.md, logs/
~/.kiro/     global steering, MCP registration, permissions
```

## 1. Prerequisites

- Python 3.11+ and git
- Kiro CLI v3, logged in. For unattended runs without a browser session, set `KIRO_API_KEY` (Pro, Pro+, or Power).
- Clones of every repo under one or two root folders. Use dedicated clones (`atlas-src`), not your working copies, because `--pull` fast-forwards them.

## 2. Install

```bash
cd ~/atlas-kit
python3 -m venv .venv
.venv/bin/pip install mcp
cp config.example.json config.json
kiro-cli chat --list-models          # confirm the Haiku model id, put it in config.json "model"
```

Edit `config.json`:

- `repo_roots`: folders containing clones (scanned 2 levels deep). `repos`: extra individual paths. `exclude_repos`: names to skip.
- `kiro_bin`: absolute path from `which kiro-cli` (cron has a minimal PATH).
- `domains`: fixed list like `["payments", "identity", "platform"]`. Strongly recommended; without it the model invents inconsistent domain names.
- `parallel`: concurrent Kiro sessions. Start at 3.

## 3. Pilot, then full run

```bash
.venv/bin/python atlas.py generate --dry-run          # spends nothing
.venv/bin/python atlas.py generate --limit 5          # pick a representative mix with --only a b c
```

Check credit usage in Kiro, then read `atlas/docs/*.md` and `atlas/graph.json`:

- Many `unresolved` consumes that are really internal: the provider repo is missing that name in `identifiers`. Rerun it with `--only NAME --full`.
- `dropped_without_evidence` in a manifest's `_meta`: items the model claimed without a real file. Some is normal; lots means the repo needs a stronger model (set `model` and use `--only`).

Then run everything: `.venv/bin/python atlas.py generate`.

## 4. Connect Kiro (global)

1. Merge `kiro/mcp.json` into `~/.kiro/settings/mcp.json`. Use absolute paths; Kiro does not inherit your shell PATH.
2. Merge `kiro/permissions.yaml` into `~/.kiro/settings/permissions.yaml` (edit paths).
3. Copy `kiro/steering/atlas.md` to `~/.kiro/steering/atlas.md`.
4. Start a new `kiro-cli` session, run `/mcp` to confirm `atlas` is connected, then ask something like "what depends on payments-service?"

## 5. Keep it current

`generate` only spends credits where needed:

| Situation | Mode | Cost |
|---|---|---|
| HEAD unchanged | skip | none |
| Only tests, docs, images changed (`ignore_changes`) | restamp | none |
| Up to `max_changed_files_for_update` relevant files changed | update (old entry + changed file list) | small |
| New repo, big change, history rewritten, or last full run older than `full_regen_days` | full | normal |

Schedule it, for example every 2 hours with cron:

```
0 */2 * * * cd $HOME/atlas-kit && .venv/bin/python atlas.py generate --pull >> $HOME/atlas/cron.log 2>&1
```

`atlas.py status` shows which entries are stale. The MCP `freshness` tool gives Kiro the same view.

## Troubleshooting

- **Timeouts:** almost always a tool call waiting for approval in headless mode. Check `atlas/logs/NAME.log`. Reading inside the repo is allowed by default in v3; if the model insists on other tools, pass a narrow trust flag via `kiro_extra_args` (see `kiro-cli chat --help` for `--trust-tools`), never `--trust-all-tools`.
- **"no <<<ATLAS_JSON block":** the model did not finish or ignored the format. It retries once; check the log.
- **Mapping sessions also see the atlas tools and steering** because they are global. That is harmless, but if you want clean runs, create a read-only mapper agent without MCP and set `"kiro_extra_args": ["--agent", "atlas-mapper"]`.
- **Matching is string-based.** Consumes are matched to providers by hostname, service name, topic, or package name. Env-var-only targets stay unresolved until something maps the variable to a service; improve by having config/deploy repos list those mappings, or add identifiers.
