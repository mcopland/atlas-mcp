#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Org atlas: map every repo with Kiro CLI, join the results into one graph, render docs.

  atlas.py generate [--only NAME ...] [--limit N] [--full] [--pull] [--dry-run] [--no-build]
                    [--force-unlock]
  atlas.py build
  atlas.py prune [--apply]
  atlas.py unresolved [--top N]
  atlas.py status

Nothing is written to the repos. Output goes to atlas_dir (default ~/atlas):
  repos/<name>.json   per-repo manifest (LLM facts + deterministic package facts + _meta)
  repos.json          resolved repo name -> path map from the last run
  graph.json          joined cross-repo graph (read by atlas_mcp.py)
  docs/<name>.md      per-repo doc with component diagram
  docs/domains/*.md   per-domain HLD diagram
  index.md            one line per repo
  logs/<name>.log     Kiro output, one section per attempt
"""

import argparse
import concurrent.futures as cf
import contextlib
import datetime as dt
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import fcntl
except ImportError:  # non-POSIX: fall back to an O_EXCL lock file
    fcntl = None

KIT = Path(__file__).resolve().parent
START, END = "<<<ATLAS_JSON", "ATLAS_JSON>>>"
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "target", "venv", "__pycache__", "site-packages"}
FAMILY = {
    "http": "svc",
    "grpc": "svc",
    "graphql": "svc",
    "websocket": "svc",
    "topic": "msg",
    "queue": "msg",
    "event": "msg",
    "package": "pkg",
    "database": "db",
}
GENERIC_STORES = {
    "db",
    "database",
    "postgres",
    "postgresql",
    "mysql",
    "redis",
    "cache",
    "s3",
    "dynamodb",
    "main",
    "default",
}
GENERIC_IDENTIFIERS = {
    "api",
    "app",
    "web",
    "db",
    "service",
    "services",
    "server",
    "backend",
    "frontend",
    "core",
    "common",
    "shared",
    "utils",
    "util",
    "lib",
    "libs",
    "client",
    "gateway",
    "internal",
    "main",
    "default",
    "www",
    "localhost",
}
ENV_SUFFIX = re.compile(r"_(?:BASE_URL|URL|URI|HOSTNAME|HOST|ENDPOINT|ADDRESS|ADDR|PORT)$")
RANK = {"exact": 0, "alias": 1, "envvar": 2}
GRADLE_DEP = re.compile(
    r"""\b(?:implementation|api|compile|compileOnly|runtimeOnly
                        |testImplementation|testCompileOnly|annotationProcessor|kapt)
                        \b[\s(]+['"]([^'":\s]+):([^'":\s]+)""",
    re.X,
)
MAX_PROMPT_BYTES = 96 * 1024
TEXT_KEYS = {"text", "content", "delta", "message", "output", "value", "chunk"}


# ---------- helpers ----------


def now():
    return dt.datetime.now(dt.UTC)


def expand(p):
    return Path(os.path.expanduser(str(p))).resolve()


def load_config(path):
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"no config at {path}; copy config.example.json to config.json and edit it")
    except json.JSONDecodeError as e:
        sys.exit(f"invalid JSON in {path}: {e}")
    cfg["atlas_dir"] = expand(cfg.get("atlas_dir", "~/atlas"))
    defaults = {
        "repo_roots": [],
        "repos": [],
        "exclude_repos": [],
        "repo_names": {},
        "kiro_bin": "kiro-cli",
        "model": "claude-haiku-4.5",
        "kiro_extra_args": [],
        "kiro_agent": "atlas-mapper",
        "parallel": 3,
        "timeout_minutes": 20,
        "full_regen_days": 30,
        "max_changed_files_for_update": 150,
        "max_ambiguous_hits": 3,
        "domains": [],
        "ignore_changes": [],
        "generic_identifiers": [],
    }
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    cfg["generic_identifiers"] = {str(s).strip().lower() for s in cfg["generic_identifiers"]} | GENERIC_IDENTIFIERS
    cfg["domains"] = [str(d).strip().lower() for d in cfg["domains"] if str(d).strip()]
    return cfg


def git(repo, *args, timeout=60):
    r = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout
    )
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or f"git {' '.join(args)} failed")
    return r.stdout.strip()


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_json(path, data):
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


@contextlib.contextmanager
def atlas_lock(atlas_dir, force=False):
    """One generate at a time: a full org run can outlast its own cron interval."""
    atlas_dir.mkdir(parents=True, exist_ok=True)
    path = atlas_dir / ".generate.lock"
    stamp = f"pid {os.getpid()} started {now().isoformat(timespec='seconds')}\n"
    if fcntl is not None:
        fh = open(path, "a+", encoding="utf-8")  # noqa: SIM115 - released in the finally below, not at end of scope
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            holder = fh.read().strip() or "unknown holder"
            fh.close()
            sys.exit(f"another atlas run holds {path} ({holder}); waiting for it to finish")
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(stamp)
            fh.flush()
            yield
        finally:
            with contextlib.suppress(Exception):
                fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()  # the file stays; unlinking it hands the next run a dead inode
        return
    if force:
        with contextlib.suppress(OSError):
            path.unlink()
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = ""
        with contextlib.suppress(OSError):
            holder = path.read_text(encoding="utf-8").strip()
        sys.exit(
            f"another atlas run holds {path} ({holder or 'unknown holder'}); "
            f"if that process is dead, rerun with --force-unlock"
        )
    try:
        os.write(fd, stamp.encode())
    finally:
        os.close(fd)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def discover_repos(cfg):
    """Resolve clones to stable names. Colliding basenames disambiguate every member, so
    adding a clone never renames an existing entry out from under its manifest."""
    aliases = {str(expand(k)): str(v) for k, v in (cfg["repo_names"] or {}).items()}
    paths = []

    def add(path):
        path = expand(path)
        if (path / ".git").exists() and path not in paths:
            paths.append(path)

    for root in cfg["repo_roots"]:
        root = expand(root)
        if not root.is_dir():
            print(f"warn: repo root not found: {root}", file=sys.stderr)
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if (child / ".git").exists():
                add(child)
            else:
                with contextlib.suppress(OSError):
                    for grandchild in sorted(child.iterdir()):
                        if grandchild.is_dir():
                            add(grandchild)
    for p in cfg["repos"]:
        add(p)

    groups = {}
    for path in paths:
        groups.setdefault(path.name, []).append(path)
    excluded = set(cfg["exclude_repos"])
    found = {}
    for base, members in sorted(groups.items()):
        for path in members:
            name = aliases.get(str(path)) or (base if len(members) == 1 else f"{path.parent.name}-{base}")
            if name in found:
                name = re.sub(r"\W+", "-", str(path)).strip("-").lower()
            found[name] = path
    return {n: p for n, p in found.items() if n not in excluded and p.name not in excluded}


def known_repo_names(cfg):
    repos = discover_repos(cfg)
    if not repos:
        sys.exit(
            "no repos found; check repo_roots / repos in config. Refusing to rebuild or "
            "prune the atlas from an empty discovery (an unmounted root would wipe it)."
        )
    return set(repos)


def save_repo_map(cfg, repos):
    path = cfg["atlas_dir"] / "repos.json"
    new = {n: str(p) for n, p in sorted(repos.items())}
    old = {}
    if path.exists():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            old = json.loads(path.read_text(encoding="utf-8"))
    was = {v: k for k, v in old.items()}
    for name, p in new.items():
        prev = was.get(p)
        if prev and prev != name:
            print(
                f"warn: {p} renamed {prev} -> {name}; repos/{prev}.json is now an orphan (run atlas.py prune)",
                file=sys.stderr,
            )
    write_json(path, new)


# ---------- deterministic package facts ----------


def walk(repo, filenames, max_depth=4):
    for dirpath, dirnames, files in os.walk(repo):
        depth = len(Path(dirpath).relative_to(repo).parts)
        dirnames[:] = (
            [] if depth >= max_depth else [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        )
        for f in files:
            if f in filenames or any(fnmatch.fnmatch(f, pat) for pat in filenames if "*" in pat):
                yield Path(dirpath) / f


def norm_pkg(eco, name):
    name = name.strip().lower()
    if eco == "pypi":
        name = re.sub(r"[-_.]+", "-", name)
    return name


def local(el):
    return el.tag.split("}")[-1] if isinstance(el.tag, str) else ""


def child_text(el, name):
    return next(((c.text or "").strip() for c in el if local(c) == name), "")


def coord(group, artifact):
    """Maven coordinate; a placeholder such as ${project.groupId} is not a real package."""
    if not artifact or "${" in f"{group}{artifact}":
        return ""
    return f"{group}:{artifact}".strip(":")


def extract_packages(repo):
    publishes, depends = {}, {}

    def pub(eco, name, f):
        if name:
            publishes.setdefault((eco, norm_pkg(eco, name)), str(f.relative_to(repo)))

    def dep(eco, name, f):
        if name:
            depends.setdefault((eco, norm_pkg(eco, name)), str(f.relative_to(repo)))

    wanted = {
        "package.json",
        "go.mod",
        "pyproject.toml",
        "requirements*.txt",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "Cargo.toml",
        "*.csproj",
    }
    for f in walk(repo, wanted):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            if f.name == "package.json":
                data = json.loads(text)
                pub("npm", data.get("name"), f)
                for section in ("dependencies", "devDependencies", "peerDependencies"):
                    for n in data.get(section) or {}:
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
            elif f.name == "pyproject.toml":
                data = tomllib.loads(text)
                project = data.get("project") or {}
                poetry = (data.get("tool") or {}).get("poetry") or {}
                pub("pypi", project.get("name") or poetry.get("name"), f)
                for spec in project.get("dependencies") or []:
                    m = re.match(r"[A-Za-z0-9_.\-]+", spec)
                    dep("pypi", m and m.group(0), f)
                for n in poetry.get("dependencies") or {}:
                    if n.lower() != "python":
                        dep("pypi", n, f)
            elif f.name.startswith("requirements"):
                for line in text.splitlines():
                    m = re.match(r"\s*([A-Za-z0-9_.\-]+)", line)
                    if m and not line.strip().startswith(("#", "-")):
                        dep("pypi", m.group(1), f)
            elif f.name == "pom.xml":
                root = ET.fromstring(text)
                parent = next((c for c in root if local(c) == "parent"), None)
                group = child_text(root, "groupId") or (child_text(parent, "groupId") if parent is not None else "")
                pub("maven", coord(group, child_text(root, "artifactId")), f)
                for node in root.iter():
                    if local(node) == "dependency":
                        dep("maven", coord(child_text(node, "groupId"), child_text(node, "artifactId")), f)
            elif f.name.startswith("build.gradle"):
                for m in re.finditer(GRADLE_DEP, text):
                    dep("maven", coord(m.group(1), m.group(2)), f)
                group = re.search(r"""^\s*group\s*=?\s*['"]([^'"]+)['"]""", text, re.M)
                root_name = ""
                for settings in ("settings.gradle", "settings.gradle.kts"):
                    path = f.parent / settings
                    if path.exists():
                        m = re.search(
                            r"""rootProject\.name\s*=\s*['"]([^'"]+)['"]""",
                            path.read_text(encoding="utf-8", errors="replace"),
                        )
                        root_name = m.group(1) if m else ""
                        break
                if group and root_name:
                    pub("maven", coord(group.group(1), root_name), f)
            elif f.name == "Cargo.toml":
                data = tomllib.loads(text)
                name = (data.get("package") or {}).get("name")
                pub("cargo", name if isinstance(name, str) else None, f)
                sections = ("dependencies", "dev-dependencies", "build-dependencies")
                for section in sections:
                    for n in data.get(section) or {}:
                        dep("cargo", n, f)
                for n in (data.get("workspace") or {}).get("dependencies") or {}:
                    dep("cargo", n, f)
            elif f.name.endswith(".csproj"):
                root = ET.fromstring(text)
                ident = ""
                for node in root.iter():
                    if local(node) in ("PackageId", "AssemblyName") and not ident:
                        ident = (node.text or "").strip()
                pub("nuget", ident or f.stem, f)
                for node in root.iter():
                    if local(node) == "PackageReference":
                        name = node.get("Include") or node.get("Update") or ""
                        if "$(" not in name:
                            dep("nuget", name, f)
        # ValueError covers json.JSONDecodeError and tomllib.TOMLDecodeError; the parsers
        # also raise AttributeError/TypeError on structurally surprising but valid documents.
        except (OSError, ValueError, ET.ParseError, AttributeError, TypeError) as e:
            print(f"warn: could not parse {f}: {e}", file=sys.stderr)

    for key in list(depends):
        if key in publishes:  # internal to a monorepo
            del depends[key]

    def to_list(d):
        return [{"ecosystem": e, "name": n, "evidence": ev} for (e, n), ev in sorted(d.items())]

    return {"publishes": to_list(publishes), "depends_on": to_list(depends)}


# ---------- LLM extraction ----------


def build_prompt(cfg, template, **values):
    text = (KIT / "prompts" / template).read_text(encoding="utf-8")
    values.setdefault("SCHEMA", (KIT / "prompts" / "schema.json").read_text(encoding="utf-8"))
    values.setdefault(
        "DOMAINS",
        ", ".join(cfg["domains"] + ["unassigned"]) if cfg["domains"] else "a short lowercase name of your choice",
    )
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def jsonl_text(text):
    """Rebuild the assistant text from a stream-json transcript: the text-bearing fields of
    every event, concatenated in order. Other fields, such as an echo of the prompt in a tool
    call, are skipped so they cannot be mistaken for the answer."""
    chunks = []

    def collect(value, key=None):
        if isinstance(value, str):
            if key is None or key in TEXT_KEYS:
                chunks.append(value)
        elif isinstance(value, dict):
            for k, item in value.items():
                collect(item, k)
        elif isinstance(value, list):
            for item in value:
                collect(item, key)

    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            collect(json.loads(line))
        except json.JSONDecodeError:
            return ""
    return "".join(chunks)


def parse_output(text):
    """Rendered output first, then the same text read as a stream-json transcript: a raw
    transcript contains the markers inside JSON strings, so it matches but does not parse."""
    for candidate in (text, jsonl_text(text)):
        s = candidate.rfind(START)
        if s < 0:
            continue
        e = candidate.find(END, s)
        body = candidate[s + len(START) : e if e >= 0 else None].strip()
        body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            raise ValueError("ATLAS_JSON block is not an object")
        return data
    raise ValueError("no parseable <<<ATLAS_JSON block in Kiro output")


def log_append(log_path, text):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(text)


def run_kiro(cfg, repo, prompt, log_path):
    agent = ["--agent", cfg["kiro_agent"]] if cfg["kiro_agent"] else []
    cmd = [
        cfg["kiro_bin"],
        "chat",
        "--no-interactive",
        "--wrap",
        "never",
        "--model",
        cfg["model"],
        *agent,
        *cfg["kiro_extra_args"],
        prompt,
    ]
    header = (
        f"=== attempt {now().isoformat(timespec='seconds')} ===\n$ {' '.join(cmd[:-1])} <prompt {len(prompt)} chars>\n"
    )
    try:
        r = subprocess.run(
            cmd,
            cwd=repo,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=cfg["timeout_minutes"] * 60,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired as e:
        partial = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        log_append(log_path, header + "TIMEOUT\n" + ANSI.sub("", partial) + "\n")
        raise RuntimeError("timed out; usually a tool call waiting for approval (see README)") from e
    log_append(
        log_path,
        header
        + f"exit={r.returncode}\n\n"
        + ANSI.sub("", r.stdout or "")
        + "\n--- stderr ---\n"
        + ANSI.sub("", r.stderr or "")
        + "\n",
    )
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


def clean_manifest(m, repo, cfg):
    dropped = []
    for field in ("exposes", "consumes", "datastores"):
        kept = []
        for item in m.get(field) or []:
            if isinstance(item, dict) and evidence_ok(repo, item.get("evidence")):
                item["kind"] = str(item.get("kind") or "").strip().lower()
                kept.append(item)
            else:
                dropped.append({"field": field, "item": item})
        m[field] = kept
    for field in ("languages", "owners", "identifiers", "entrypoints", "notes"):
        items = m.get(field)
        m[field] = [str(i) for i in items if not isinstance(i, (dict, list))] if isinstance(items, list) else []
    for field in ("components", "component_edges"):
        items = m.get(field)
        m[field] = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    for field in ("summary", "overview", "domain", "kind"):
        m[field] = str(m.get(field) or "")
    m["kind"] = m["kind"].strip().lower()
    domain = m["domain"].strip().lower() or "unassigned"
    rejected, allowed = "", cfg["domains"]
    if allowed and domain not in set(allowed) | {"unassigned"}:
        rejected, domain = domain, "unassigned"
    m["domain"] = domain
    return dropped, rejected


def is_full_due(last_full, full_regen_days):
    """An unparseable or naive stamp means a full run is due: anything else would raise on
    every subsequent run and leave the repo permanently erroring."""
    if not last_full:
        return True
    try:
        stamp = dt.datetime.fromisoformat(last_full)
    except (TypeError, ValueError):
        return True
    if stamp.tzinfo is None:
        return True
    return now() - stamp > dt.timedelta(days=full_regen_days)


def process_repo(cfg, name, repo, args):
    out = cfg["atlas_dir"] / "repos" / f"{name}.json"
    if args.pull:
        try:
            git(repo, "pull", "--ff-only", timeout=300)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            print(f"warn: {name}: pull failed: {e}", file=sys.stderr)
    head = git(repo, "rev-parse", "HEAD")
    old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    meta = (old or {}).get("_meta", {})
    mode, changed = "full", []

    if old and not args.full:
        if meta.get("commit") == head:
            return name, "skip", "up to date"
        full_due = is_full_due(meta.get("last_full_at"), cfg["full_regen_days"])
        if not full_due:
            try:
                changed = git(repo, "diff", "--name-only", meta["commit"], head).splitlines()
                relevant = [f for f in changed if not any(fnmatch.fnmatch(f, p) for p in cfg["ignore_changes"])]
                if not relevant:
                    mode = "restamp"
                elif len(relevant) <= cfg["max_changed_files_for_update"]:
                    mode, changed = "update", relevant
            except (RuntimeError, OSError, subprocess.SubprocessError):
                mode = "full"  # old commit missing (rewritten history, shallow clone)

    if args.dry_run:
        return name, "dry-run", mode

    if mode == "restamp":
        manifest = {k: v for k, v in old.items() if k != "_meta"}
    else:
        prompt = None
        if mode == "update":
            prompt = build_prompt(
                cfg,
                "update.md",
                OLD_COMMIT=meta["commit"],
                NEW_COMMIT=head,
                CHANGED_FILES="\n".join(f"- {f}" for f in changed),
                MANIFEST=json.dumps({k: v for k, v in old.items() if k not in ("_meta", "packages")}, indent=2),
            )
            if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
                print(f"warn: {name}: update prompt is {len(prompt)} chars, regenerating in full", file=sys.stderr)
                mode, changed, prompt = "full", [], None
        if prompt is None:
            prompt = build_prompt(cfg, "full.md")
        log = cfg["atlas_dir"] / "logs" / f"{name}.log"
        try:
            manifest = run_kiro(cfg, repo, prompt, log)
        except ValueError:
            manifest = run_kiro(cfg, repo, prompt, log)  # one retry on unparseable output

    dropped, rejected = clean_manifest(manifest, repo, cfg)
    manifest["name"] = name
    manifest["packages"] = extract_packages(repo)
    manifest["_meta"] = {
        "repo_path": str(repo),
        "commit": head,
        "generated_at": now().isoformat(timespec="seconds"),
        "mode": mode,
        "model": cfg["model"] if mode != "restamp" else meta.get("model"),
        "last_full_at": now().isoformat(timespec="seconds") if mode == "full" else meta.get("last_full_at"),
        "dropped_without_evidence": dropped,
        "domain_rejected": rejected,
    }
    write_json(out, manifest)
    return (
        name,
        mode,
        (f"{len(manifest['exposes'])} exposes, {len(manifest['consumes'])} consumes, {len(dropped)} dropped"),
    )


# ---------- graph ----------


def svc_forms(raw):
    s = str(raw).strip().lower()
    host = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", s)
    host = re.split(r"[/?#]", host, maxsplit=1)[0].split("@")[-1]
    host = re.sub(r":\d+$", "", host) or s
    return host, host.split(".")[0]


def envvar_forms(raw):
    """ORDERS_SERVICE_URL -> ['orders-service', 'orders']: env-var-only targets are the single
    biggest source of unresolved consumes, and the stem usually is the service name."""
    s = str(raw).strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", s):
        return []
    stem = ENV_SUFFIX.sub("", s).lower().replace("_", "-")
    forms = []
    for form in (stem, re.sub(r"-(?:service|svc|api)$", "", stem)):
        if len(form) >= 3 and form not in forms:
            forms.append(form)
    return forms


def alias_ok(form, stoplist):
    return len(form) >= 3 and form not in stoplist


def weaker(a, b):
    return a if RANK[a] >= RANK[b] else b


META_FIELDS = ("commit", "generated_at", "repo_path", "mode")


def check_meta(m):
    """Manifests are documented as editable JSON and orphan snapshots get restored by hand,
    so a hand-edited file must be skipped with a warning rather than taking the whole build
    down with a KeyError from render_docs or the graph."""
    meta = m.get("_meta")
    if not isinstance(meta, dict):
        raise ValueError("missing _meta")
    missing = [k for k in META_FIELDS if not meta.get(k)]
    if missing:
        raise ValueError(f"incomplete metadata: _meta is missing {', '.join(missing)}")


def load_manifests(ad, known):
    manifests, orphans = {}, []
    for p in sorted((ad / "repos").glob("*.json")):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
            check_meta(m)
        except Exception as e:
            print(f"warn: skipping malformed manifest {p.name}: {e}", file=sys.stderr)
            continue
        if p.stem in known:
            manifests[p.stem] = m
        else:
            orphans.append(p.stem)
    if orphans:
        shown = ", ".join(sorted(orphans)[:10]) + (" ..." if len(orphans) > 10 else "")
        print(
            f"warn: {len(orphans)} orphan manifests excluded from the graph ({shown}); run atlas.py prune",
            file=sys.stderr,
        )
    return manifests


def build(cfg, known):
    ad = cfg["atlas_dir"]
    manifests = load_manifests(ad, known)
    stop = cfg["generic_identifiers"]
    index = {}  # (family, form) -> {repo: strength}

    def provide(family, form, repo, strength):
        form = str(form or "").strip().lower()
        if not form:
            return
        if strength == "exact":
            if len(form) < 2:
                return
        elif not alias_ok(form, stop):
            return
        slot = index.setdefault((family, form), {})
        if slot.get(repo) != "exact":
            slot[repo] = strength

    for name, m in manifests.items():
        for ident in list(m.get("identifiers") or []) + [name]:
            text = str(ident or "").strip()
            if "/" in text and "://" not in text:  # a package path, not a host
                provide("pkg", text, name, "exact")
                continue
            full, first = svc_forms(text)
            provide("svc", full, name, "exact")
            provide("svc", first, name, "alias")
        for item in m.get("exposes") or []:
            fam = FAMILY.get(item.get("kind"), "other")
            key = str(item.get("key") or "")
            if fam == "svc":
                full, first = svc_forms(key)
                provide("svc", full, name, "exact")
                provide("svc", first, name, "alias")
            elif key:
                provide(fam, key, name, "exact")
        for ds in m.get("datastores") or []:
            store = str(ds.get("name") or "").strip().lower()
            if str(ds.get("access") or "").strip().lower() == "owner" and store not in GENERIC_STORES:
                provide("db", store, name, "exact")
        for p in (m.get("packages") or {}).get("publishes", []):
            provide("pkg", f"{p['ecosystem']}:{p['name']}", name, "exact")
            provide("pkg", p["name"], name, "alias")

    edges, unresolved, seen = [], [], set()

    def link(src, item, kind, key, lookups):
        hits = {}
        for fam, form, strength in lookups:
            form = str(form).strip().lower()
            if strength != "exact" and not alias_ok(form, stop):
                continue
            for repo, s in index.get((fam, form), {}).items():
                if repo != src and repo not in hits:
                    hits[repo] = weaker(s, strength)
            if hits:
                break
        weakest = max((RANK[v] for v in hits.values()), default=0)
        fan_in = FAMILY.get(kind) == "msg" and weakest == 0  # one topic, many producers
        if not hits or (not fan_in and len(hits) > cfg["max_ambiguous_hits"]):
            row = {
                "repo": src,
                "kind": kind,
                "key": key,
                "name": item.get("name", key),
                "evidence": item.get("evidence"),
            }
            if hits:  # too many weak matches to pick from; hand the agent the shortlist
                row["candidates"] = sorted(hits)
            unresolved.append(row)
            return
        for dst, strength in hits.items():
            sig = (src, dst, kind, key)
            if sig in seen:
                continue
            seen.add(sig)
            edges.append(
                {
                    "from": src,
                    "to": dst,
                    "kind": kind,
                    "key": key,
                    "name": item.get("name", key),
                    "detail": item.get("detail", ""),
                    "evidence": item.get("evidence"),
                    "match": "ambiguous" if len(hits) > 1 and not fan_in else strength,
                }
            )

    for name, m in manifests.items():
        for item in m.get("consumes") or []:
            kind, key = item.get("kind", "other"), str(item.get("key") or "")
            if not key:
                continue
            fam = FAMILY.get(kind, "other")
            if fam in ("svc", "other"):
                full, first = svc_forms(key)
                lookups = [("svc", full, "exact"), ("svc", first, "alias")]
                if fam == "other":
                    lookups.insert(0, ("other", key, "exact"))
            else:
                lookups = [(fam, key, "exact")]
            lookups += [("svc", f, "envvar") for f in envvar_forms(key)]
            link(name, item, kind, key, lookups)
        for p in (m.get("packages") or {}).get("depends_on", []):
            eco_key, bare = f"{p['ecosystem']}:{p['name']}".lower(), p["name"].lower()
            if ("pkg", eco_key) in index or ("pkg", bare) in index:  # only internal packages
                link(
                    name,
                    {"name": p["name"], "evidence": p["evidence"], "detail": "declared dependency"},
                    "package",
                    p["name"],
                    [("pkg", eco_key, "exact"), ("pkg", bare, "alias")],
                )

    stores = {}
    for name, m in manifests.items():
        for ds in m.get("datastores") or []:
            key = str(ds.get("name") or "").strip().lower()
            if key and key not in GENERIC_STORES:
                stores.setdefault(key, {})[name] = ds.get("access", "")
    shared = [{"name": k, "repos": v} for k, v in sorted(stores.items()) if len(v) > 1]

    graph = {
        "generated_at": now().isoformat(timespec="seconds"),
        "repos": {
            n: {
                "summary": m.get("summary", ""),
                "domain": m.get("domain", "unassigned"),
                "kind": m.get("kind", ""),
                "identifiers": m.get("identifiers", []),
                "commit": m["_meta"]["commit"],
                "generated_at": m["_meta"]["generated_at"],
                "repo_path": m["_meta"]["repo_path"],
                "doc": str(ad / "docs" / f"{n}.md"),
            }
            for n, m in manifests.items()
        },
        "edges": edges,
        "unresolved": unresolved,
        "shared_datastores": shared,
    }
    render_docs(ad, manifests, graph)
    write_json(ad / "graph.json", graph)  # published last: docs it points at already exist
    print(
        f"graph: {len(manifests)} repos, {len(edges)} edges, {len(unresolved)} unresolved, "
        f"{len(shared)} shared datastores -> {ad / 'graph.json'}"
    )


# ---------- docs ----------


def mid(s):
    return "n_" + re.sub(r"\W", "_", str(s))


def mtext(s):
    return re.sub(r"[\"'`|<>\[\]{}()#;]", " ", str(s)).strip()[:60] or "-"


def render_docs(ad, manifests, graph):
    staging = ad / "docs.new"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "domains").mkdir(parents=True, exist_ok=True)

    for name, m in manifests.items():
        meta = m["_meta"]
        out_edges = [e for e in graph["edges"] if e["from"] == name]
        in_edges = [e for e in graph["edges"] if e["to"] == name]
        lines = [
            f"# {name}",
            "",
            m.get("summary", ""),
            "",
            f"- Domain: {m.get('domain')} | Kind: {m.get('kind')} | Languages: {', '.join(m.get('languages', []))}",
            f"- Owners: {', '.join(m.get('owners', [])) or 'unknown'}",
            f"- Commit: `{meta['commit'][:12]}` generated {meta['generated_at']} ({meta['mode']})",
            f"- Path: {meta['repo_path']}",
            "",
            "## Overview",
            "",
            m.get("overview", ""),
            "",
            "## Exposes",
            "",
        ]
        lines += [
            f"- {e.get('kind')} `{e.get('name')}` key `{e.get('key')}`: {e.get('detail', '')} ({e.get('evidence')})"
            for e in m.get("exposes", [])
        ] or ["- none found"]
        lines += ["", "## Consumes", ""]
        for c in m.get("consumes", []):
            targets = [e["to"] for e in out_edges if e["key"] == c.get("key")]
            lines.append(
                f"- {c.get('kind')} `{c.get('name')}` key `{c.get('key')}` -> "
                f"{', '.join(targets) or 'unresolved'}: {c.get('detail', '')} ({c.get('evidence')})"
            )
        lines += [] if m.get("consumes") else ["- none found"]
        lines += ["", "## Used by", ""]
        lines += [f"- {e['from']} via {e['kind']} `{e['key']}` ({e['match']})" for e in in_edges] or [
            "- no known dependents"
        ]
        lines += ["", "## Datastores", ""]
        lines += [
            f"- {d.get('kind')} `{d.get('name')}` ({d.get('access')}) ({d.get('evidence')})"
            for d in m.get("datastores", [])
        ] or ["- none found"]
        if m.get("components"):
            lines += ["", "## Components", ""]
            lines += [f"- {c.get('name')} `{c.get('path')}`: {c.get('role')}" for c in m["components"]]
            lines += ["", "```mermaid", "flowchart LR"]
            lines += [f'  {mid(c.get("name"))}["{mtext(c.get("name"))}"]' for c in m["components"]]
            lines += [
                f"  {mid(e.get('from'))} -->|{mtext(e.get('label', ''))}| {mid(e.get('to'))}"
                for e in m.get("component_edges", [])
            ]
            lines += ["```"]
        if m.get("notes"):
            lines += ["", "## Notes", ""] + [f"- {n}" for n in m["notes"]]
        (staging / f"{name}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    by_domain = {}
    for name, r in graph["repos"].items():
        by_domain.setdefault(r["domain"], []).append(name)
    for domain, members in sorted(by_domain.items()):
        members_set = set(members)
        pairs = {}
        for e in graph["edges"]:
            if e["from"] in members_set or e["to"] in members_set:
                pairs.setdefault((e["from"], e["to"]), set()).add(e["kind"])
        outsiders = {n for pair in pairs for n in pair} - members_set
        lines = [
            f"# Domain: {domain}",
            "",
            "```mermaid",
            "flowchart LR",
            f'  subgraph {mid("d_" + domain)}["{mtext(domain)}"]',
        ]
        lines += [f'    {mid(n)}["{mtext(n)}"]' for n in sorted(members)]
        lines += ["  end"] + [
            f'  {mid(n)}["{mtext(n)} ({mtext(graph["repos"][n]["domain"])})"]' for n in sorted(outsiders)
        ]
        lines += [f"  {mid(a)} -->|{mtext(', '.join(sorted(k)))}| {mid(b)}" for (a, b), k in sorted(pairs.items())]
        lines += ["```", ""] + [f"- {n}: {graph['repos'][n]['summary']}" for n in sorted(members)]
        (staging / "domains" / f"{re.sub(r'[^a-z0-9_-]', '_', domain)}.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    live, retired = ad / "docs", ad / "docs.old"
    if retired.exists():
        shutil.rmtree(retired)
    if live.exists():
        live.rename(retired)
    staging.rename(live)
    if retired.exists():
        shutil.rmtree(retired)

    index = ["# Org atlas", "", f"Generated {graph['generated_at']}. {len(manifests)} repos.", ""]
    for domain, members in sorted(by_domain.items()):
        index += (
            [f"## {domain}", ""]
            + [f"- {n} ({graph['repos'][n]['kind']}): {graph['repos'][n]['summary']}" for n in sorted(members)]
            + [""]
        )
    write_text(ad / "index.md", "\n".join(index) + "\n")


# ---------- commands ----------


def cmd_generate(cfg, args):
    repos = discover_repos(cfg)
    if not repos:
        sys.exit("no repos found; check repo_roots / repos in config")
    known, selected = set(repos), repos
    if args.only:
        selected = {n: p for n, p in repos.items() if n in set(args.only)}
        missing = set(args.only) - set(selected)
        if missing:
            print(f"warn: not found in discovery: {', '.join(sorted(missing))}", file=sys.stderr)
    items = sorted(selected.items())
    if args.limit:
        items = items[: args.limit]
    if not items:
        sys.exit("no repos selected")

    errors = 0
    with atlas_lock(cfg["atlas_dir"], args.force_unlock):
        if not args.dry_run:
            save_repo_map(cfg, repos)
        print(f"{len(items)} repos, model {cfg['model']}, parallel {cfg['parallel']}")
        with cf.ThreadPoolExecutor(max_workers=cfg["parallel"]) as pool:
            futures = {pool.submit(process_repo, cfg, n, p, args): n for n, p in items}
            for fut in cf.as_completed(futures):
                try:
                    name, mode, msg = fut.result()
                    print(f"  {name}: {mode} ({msg})")
                except Exception as e:
                    errors += 1
                    print(f"  {futures[fut]}: ERROR {e}", file=sys.stderr)
        if not args.dry_run and not args.no_build:
            build(cfg, known)
    sys.exit(1 if errors else 0)


def cmd_build(cfg, _args):
    build(cfg, known_repo_names(cfg))


def cmd_prune(cfg, args):
    known = known_repo_names(cfg)
    repos_dir = cfg["atlas_dir"] / "repos"
    orphans = [p for p in sorted(repos_dir.glob("*.json")) if p.stem not in known]
    if not orphans:
        print("no orphan manifests")
        return
    dest = repos_dir / "_orphans"
    for p in orphans:
        if args.apply:
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / p.name
            if target.exists():  # keep the earlier snapshot; nothing is ever deleted
                target = dest / f"{p.stem}.{now().strftime('%Y%m%dT%H%M%S')}.json"
            p.replace(target)
        print(f"  {p.stem}: {'moved aside' if args.apply else 'orphan'}")
    print(
        f"{len(orphans)} orphans "
        + (f"moved to {dest}" if args.apply else f"found; rerun with --apply to move them to {dest}")
    )


def cmd_unresolved(cfg, args):
    path = cfg["atlas_dir"] / "graph.json"
    if not path.exists():
        sys.exit(f"no graph at {path}; run atlas.py generate")
    groups = {}
    for u in json.loads(path.read_text(encoding="utf-8")).get("unresolved", []):
        g = groups.setdefault((u.get("kind", ""), u.get("key", "")), {"repos": set(), "candidates": set()})
        g["repos"].add(u.get("repo", ""))
        g["candidates"].update(u.get("candidates", []))
    rows = sorted(groups.items(), key=lambda kv: (-len(kv[1]["repos"]), kv[0]))
    print(f"{len(rows)} distinct unresolved targets")
    for (kind, key), g in rows[: args.top]:
        cand = f"  candidates={','.join(sorted(g['candidates']))}" if g["candidates"] else ""
        print(f"  {len(g['repos'])}x {kind} {key}{cand}")
        print(f"      from: {', '.join(sorted(g['repos'])[:6])}")


def cmd_status(cfg, _args):
    repos = discover_repos(cfg)
    if not repos:
        sys.exit(
            "no repos found; check repo_roots / repos in config. Refusing to report every "
            "manifest as an orphan from an empty discovery."
        )
    for name, path in sorted(repos.items()):
        f = cfg["atlas_dir"] / "repos" / f"{name}.json"
        if not f.exists():
            print(f"  {name}: not generated")
            continue
        try:
            manifest = json.loads(f.read_text(encoding="utf-8"))
            check_meta(manifest)
        except (OSError, ValueError) as e:
            print(f"  {name}: unreadable ({e})")
            continue
        meta = manifest["_meta"]
        try:
            state = "fresh" if git(path, "rev-parse", "HEAD") == meta["commit"] else "stale"
        except (RuntimeError, subprocess.SubprocessError) as e:
            state = f"error: {e}"
        print(f"  {name}: {state} ({meta['commit'][:8]}, {meta['generated_at']}, {meta['mode']})")
    orphans = [p.stem for p in sorted((cfg["atlas_dir"] / "repos").glob("*.json")) if p.stem not in repos]
    for name in orphans:
        print(f"  {name}: orphan (no matching clone; run atlas.py prune)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(KIT / "config.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--only", nargs="+")
    g.add_argument("--limit", type=int)
    g.add_argument("--full", action="store_true", help="ignore existing manifests and regenerate")
    g.add_argument("--pull", action="store_true", help="git pull --ff-only each repo first")
    g.add_argument("--dry-run", action="store_true", help="show what would run, spend nothing")
    g.add_argument("--no-build", action="store_true")
    g.add_argument(
        "--force-unlock", action="store_true", help="clear a stale lock file (not needed where flock is available)"
    )
    sub.add_parser("build")
    p = sub.add_parser("prune")
    p.add_argument("--apply", action="store_true", help="move orphan manifests to repos/_orphans")
    u = sub.add_parser("unresolved")
    u.add_argument("--top", type=int, default=25)
    sub.add_parser("status")
    args = ap.parse_args()
    cfg = load_config(args.config)
    {
        "generate": cmd_generate,
        "build": cmd_build,
        "prune": cmd_prune,
        "unresolved": cmd_unresolved,
        "status": cmd_status,
    }[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
