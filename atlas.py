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

SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "target", "venv", "__pycache__", "site-packages"}


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
