#!/usr/bin/env python3
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path


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
