#!/usr/bin/env python3
import datetime as dt
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11: pyproject parsing is skipped
    tomllib = None

KIT = Path(__file__).resolve().parent
START, END = "<<<ATLAS_JSON", "ATLAS_JSON>>>"
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "target", "venv", "__pycache__", "site-packages"}
FAMILY = {"http": "svc", "grpc": "svc", "graphql": "svc", "websocket": "svc",
          "topic": "msg", "queue": "msg", "event": "msg", "package": "pkg", "database": "db"}
GENERIC_STORES = {"db", "database", "postgres", "postgresql", "mysql", "redis", "cache", "s3", "dynamodb", "main", "default"}


# ---------- helpers ----------

def now():
    return dt.datetime.now(dt.timezone.utc)


def expand(p):
    return Path(os.path.expanduser(str(p))).resolve()


def load_config(path):
    cfg = json.loads(Path(path).read_text())
    cfg["atlas_dir"] = expand(cfg.get("atlas_dir", "~/atlas"))
    defaults = {"repo_roots": [], "repos": [], "exclude_repos": [], "kiro_bin": "kiro-cli",
                "model": "claude-haiku-4.5", "kiro_extra_args": [], "parallel": 3, "timeout_minutes": 20,
                "full_regen_days": 30, "max_changed_files_for_update": 150, "domains": [], "ignore_changes": []}
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    return cfg


def git(repo, *args, timeout=60):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or f"git {' '.join(args)} failed")
    return r.stdout.strip()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def discover_repos(cfg):
    found = {}

    def add(path):
        path = expand(path)
        if not (path / ".git").exists():
            return
        name = path.name
        if name in found and found[name] != path:
            name = f"{path.parent.name}-{path.name}"
        found[name] = path

    for root in cfg["repo_roots"]:
        root = expand(root)
        if not root.is_dir():
            print(f"warn: repo root not found: {root}", file=sys.stderr)
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir():
                if (child / ".git").exists():
                    add(child)
                else:
                    for grandchild in sorted(child.iterdir()):
                        if grandchild.is_dir():
                            add(grandchild)
    for p in cfg["repos"]:
        add(p)
    return {n: p for n, p in found.items() if n not in set(cfg["exclude_repos"])}


# ---------- deterministic package facts ----------

def walk(repo, filenames, max_depth=4):
    for dirpath, dirnames, files in os.walk(repo):
        depth = len(Path(dirpath).relative_to(repo).parts)
        dirnames[:] = [] if depth >= max_depth else [
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f in filenames or any(fnmatch.fnmatch(f, pat) for pat in filenames if "*" in pat):
                yield Path(dirpath) / f


def norm_pkg(eco, name):
    name = name.strip().lower()
    if eco == "pypi":
        name = re.sub(r"[-_.]+", "-", name)
    return name


def extract_packages(repo):
    publishes, depends = {}, {}

    def pub(eco, name, f):
        if name:
            publishes.setdefault((eco, norm_pkg(eco, name)), str(f.relative_to(repo)))

    def dep(eco, name, f):
        if name:
            depends.setdefault((eco, norm_pkg(eco, name)), str(f.relative_to(repo)))

    for f in walk(repo, {"package.json", "go.mod", "pyproject.toml", "requirements*.txt"}):
        try:
            text = f.read_text(errors="replace")
            if f.name == "package.json":
                data = json.loads(text)
                pub("npm", data.get("name"), f)
                for section in ("dependencies", "devDependencies", "peerDependencies"):
                    for n in (data.get(section) or {}):
                        dep("npm", n, f)
            elif f.name == "go.mod":
                m = re.search(r"^module\s+(\S+)", text, re.M)
                pub("go", m and m.group(1), f)
                block = re.findall(r"^require\s*\((.*?)^\)", text, re.M | re.S)
                lines = "\n".join(block).splitlines() + re.findall(r"^require\s+([^\s(]+\s+\S+)", text, re.M)
                for line in lines:
                    parts = line.split("//")[0].split()
                    if len(parts) >= 2:
                        dep("go", parts[0], f)
            elif f.name == "pyproject.toml" and tomllib:
                data = tomllib.loads(text)
                project = data.get("project") or {}
                poetry = (data.get("tool") or {}).get("poetry") or {}
                pub("pypi", project.get("name") or poetry.get("name"), f)
                for spec in project.get("dependencies") or []:
                    m = re.match(r"[A-Za-z0-9_.\-]+", spec)
                    dep("pypi", m and m.group(0), f)
                for n in (poetry.get("dependencies") or {}):
                    if n.lower() != "python":
                        dep("pypi", n, f)
            elif f.name.startswith("requirements"):
                for line in text.splitlines():
                    m = re.match(r"\s*([A-Za-z0-9_.\-]+)", line)
                    if m and not line.strip().startswith(("#", "-")):
                        dep("pypi", m.group(1), f)
        except Exception as e:  # malformed manifests should not stop the run
            print(f"warn: could not parse {f}: {e}", file=sys.stderr)

    for key in list(depends):
        if key in publishes:  # internal to a monorepo
            del depends[key]
    to_list = lambda d: [{"ecosystem": e, "name": n, "evidence": ev} for (e, n), ev in sorted(d.items())]
    return {"publishes": to_list(publishes), "depends_on": to_list(depends)}


# ---------- LLM extraction ----------

def build_prompt(cfg, template, **values):
    text = (KIT / "prompts" / template).read_text()
    values.setdefault("SCHEMA", (KIT / "prompts" / "schema.json").read_text())
    values.setdefault("DOMAINS", ", ".join(cfg["domains"] + ["unassigned"]) if cfg["domains"]
                      else "a short lowercase name of your choice")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def parse_output(text):
    s = text.rfind(START)
    if s < 0:
        raise ValueError("no <<<ATLAS_JSON block in Kiro output")
    e = text.find(END, s)
    body = text[s + len(START): e if e >= 0 else None].strip()
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body)
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("ATLAS_JSON block is not an object")
    return data


def run_kiro(cfg, repo, prompt, log_path):
    cmd = [cfg["kiro_bin"], "chat", "--no-interactive", "--model", cfg["model"], *cfg["kiro_extra_args"], prompt]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=cfg["timeout_minutes"] * 60, env={**os.environ, "NO_COLOR": "1"})
    except subprocess.TimeoutExpired as e:
        partial = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        log_path.write_text(ANSI.sub("", partial))
        raise RuntimeError("timed out; usually a tool call waiting for approval (see README)")
    log_path.write_text(f"$ {' '.join(cmd[:-1])} <prompt>\nexit={r.returncode}\n\n"
                        + ANSI.sub("", r.stdout or "") + "\n--- stderr ---\n" + ANSI.sub("", r.stderr or ""))
    if r.returncode != 0:
        raise RuntimeError(f"kiro-cli exited {r.returncode}; see {log_path}")
    return parse_output(ANSI.sub("", r.stdout or ""))


def evidence_ok(repo, evidence):
    m = re.match(r"^\s*([^\s:#]+)", str(evidence or ""))
    if not m:
        return False
    rel = m.group(1)
    rel = rel[2:] if rel.startswith("./") else rel
    target = (repo / rel).resolve()
    return target.exists() and (target == repo or repo in target.parents)


def clean_manifest(m, repo):
    dropped = []
    for field in ("exposes", "consumes", "datastores"):
        kept = []
        for item in m.get(field) or []:
            if isinstance(item, dict) and evidence_ok(repo, item.get("evidence")):
                kept.append(item)
            else:
                dropped.append({"field": field, "item": item})
        m[field] = kept
    for field in ("languages", "owners", "identifiers", "components", "component_edges", "entrypoints", "notes"):
        if not isinstance(m.get(field), list):
            m[field] = []
    for field in ("summary", "overview", "domain", "kind"):
        m[field] = str(m.get(field) or "")
    m["domain"] = m["domain"].strip().lower() or "unassigned"
    return dropped


def process_repo(cfg, name, repo, args):
    out = cfg["atlas_dir"] / "repos" / f"{name}.json"
    if args.pull:
        try:
            git(repo, "pull", "--ff-only", timeout=300)
        except Exception as e:
            print(f"warn: {name}: pull failed: {e}", file=sys.stderr)
    head = git(repo, "rev-parse", "HEAD")
    old = json.loads(out.read_text()) if out.exists() else None
    meta = (old or {}).get("_meta", {})
    mode, changed = "full", []

    if old and not args.full:
        if meta.get("commit") == head:
            return name, "skip", "up to date"
        last_full = meta.get("last_full_at")
        full_due = not last_full or now() - dt.datetime.fromisoformat(last_full) > dt.timedelta(days=cfg["full_regen_days"])
        if not full_due:
            try:
                changed = git(repo, "diff", "--name-only", meta["commit"], head).splitlines()
                relevant = [f for f in changed if not any(fnmatch.fnmatch(f, p) for p in cfg["ignore_changes"])]
                if not relevant:
                    mode = "restamp"
                elif len(relevant) <= cfg["max_changed_files_for_update"]:
                    mode, changed = "update", relevant
            except Exception:
                mode = "full"  # old commit missing (rewritten history, shallow clone)

    if args.dry_run:
        return name, "dry-run", mode

    if mode == "restamp":
        manifest = {k: v for k, v in old.items() if k != "_meta"}
    else:
        if mode == "update":
            prompt = build_prompt(cfg, "update.md", OLD_COMMIT=meta["commit"], NEW_COMMIT=head,
                                  CHANGED_FILES="\n".join(f"- {f}" for f in changed),
                                  MANIFEST=json.dumps({k: v for k, v in old.items() if k not in ("_meta", "packages")}, indent=2))
        else:
            prompt = build_prompt(cfg, "full.md")
        log = cfg["atlas_dir"] / "logs" / f"{name}.log"
        try:
            manifest = run_kiro(cfg, repo, prompt, log)
        except ValueError:
            manifest = run_kiro(cfg, repo, prompt, log)  # one retry on unparseable output

    dropped = clean_manifest(manifest, repo)
    manifest["name"] = name
    manifest["packages"] = extract_packages(repo)
    manifest["_meta"] = {
        "repo_path": str(repo), "commit": head, "generated_at": now().isoformat(timespec="seconds"),
        "mode": mode, "model": cfg["model"] if mode != "restamp" else meta.get("model"),
        "last_full_at": now().isoformat(timespec="seconds") if mode == "full" else meta.get("last_full_at"),
        "dropped_without_evidence": dropped,
    }
    write_json(out, manifest)
    return name, mode, (f"{len(manifest['exposes'])} exposes, {len(manifest['consumes'])} consumes, "
                        f"{len(dropped)} dropped")


# ---------- graph ----------

def svc_forms(raw):
    s = str(raw).strip().lower()
    host = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", s)
    host = re.split(r"[/?#]", host, maxsplit=1)[0].split("@")[-1]
    host = re.sub(r":\d+$", "", host) or s
    return host, host.split(".")[0]


def build(cfg):
    ad = cfg["atlas_dir"]
    manifests = {p.stem: json.loads(p.read_text()) for p in sorted((ad / "repos").glob("*.json"))}
    index = {}  # (family, form) -> {repo: strength}

    def provide(family, form, repo, strength):
        if form and len(form) >= 2:
            slot = index.setdefault((family, form), {})
            if slot.get(repo) != "exact":
                slot[repo] = strength

    for name, m in manifests.items():
        for ident in m.get("identifiers", []) + [name]:
            full, first = svc_forms(ident)
            provide("svc", full, name, "exact")
            provide("svc", first, name, "alias")
        for item in m.get("exposes", []):
            fam, key = FAMILY.get(item.get("kind"), "other"), str(item.get("key") or "")
            if fam == "svc":
                full, first = svc_forms(key)
                provide("svc", full, name, "exact")
                provide("svc", first, name, "alias")
            elif key:
                provide(fam, key.strip().lower(), name, "exact")
        for p in m.get("packages", {}).get("publishes", []):
            provide("pkg", f"{p['ecosystem']}:{p['name']}", name, "exact")
            provide("pkg", p["name"], name, "exact")

    edges, unresolved, seen = [], [], set()

    def link(src, item, kind, key, lookups):
        hits = {}
        for fam, form, strength in lookups:
            for repo, s in index.get((fam, form), {}).items():
                if repo != src and repo not in hits:
                    hits[repo] = "exact" if s == "exact" and strength == "exact" else "alias"
            if hits:
                break
        if not hits:
            unresolved.append({"repo": src, "kind": kind, "key": key, "name": item.get("name", key),
                               "evidence": item.get("evidence")})
        for dst, strength in hits.items():
            sig = (src, dst, kind, key)
            if sig in seen:
                continue
            seen.add(sig)
            edges.append({"from": src, "to": dst, "kind": kind, "key": key, "name": item.get("name", key),
                          "detail": item.get("detail", ""), "evidence": item.get("evidence"),
                          "match": "ambiguous" if len(hits) > 1 else strength})

    for name, m in manifests.items():
        for item in m.get("consumes", []):
            kind, key = item.get("kind", "other"), str(item.get("key") or "")
            if not key:
                continue
            fam = FAMILY.get(kind, "other")
            if fam in ("svc", "other"):
                full, first = svc_forms(key)
                lookups = [("svc", full, "exact"), ("svc", first, "alias")]
            else:
                lookups = [(fam, key.strip().lower(), "exact")]
            link(name, item, kind, key, lookups)
        for p in m.get("packages", {}).get("depends_on", []):
            key = f"{p['ecosystem']}:{p['name']}"
            if ("pkg", key) in index or ("pkg", p["name"]) in index:  # only internal packages
                link(name, {"name": p["name"], "evidence": p["evidence"], "detail": "declared dependency"},
                     "package", p["name"], [("pkg", key, "exact"), ("pkg", p["name"], "exact")])

    stores = {}
    for name, m in manifests.items():
        for ds in m.get("datastores", []):
            key = str(ds.get("name") or "").strip().lower()
            if key and key not in GENERIC_STORES:
                stores.setdefault(key, {})[name] = ds.get("access", "")
    shared = [{"name": k, "repos": v} for k, v in sorted(stores.items()) if len(v) > 1]

    graph = {
        "generated_at": now().isoformat(timespec="seconds"),
        "repos": {n: {"summary": m.get("summary", ""), "domain": m.get("domain", "unassigned"),
                      "kind": m.get("kind", ""), "identifiers": m.get("identifiers", []),
                      "commit": m["_meta"]["commit"], "generated_at": m["_meta"]["generated_at"],
                      "repo_path": m["_meta"]["repo_path"], "doc": str(ad / "docs" / f"{n}.md")}
                  for n, m in manifests.items()},
        "edges": edges, "unresolved": unresolved, "shared_datastores": shared,
    }
    write_json(ad / "graph.json", graph)
    print(f"graph: {len(manifests)} repos, {len(edges)} edges, {len(unresolved)} unresolved, "
          f"{len(shared)} shared datastores -> {ad / 'graph.json'}")
