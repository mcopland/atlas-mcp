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
  repos/<name>.json   per-repo manifest (LLM facts + deterministic repo facts + _meta)
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
import hashlib
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
SKIP_DIRS = {
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
    "venv",
    "__pycache__",
    "site-packages",
}
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
    re.VERBOSE,
)
# Update prompts only: an update carries the old entry and the changed-file list as well as a
# bundle, and past this it is cheaper to regenerate in full. Full prompts are sized by the
# bundle_budget_bytes config key, which the model's context window bounds, not argv.
MAX_UPDATE_PROMPT_BYTES = 96 * 1024
DEFAULT_BUNDLE_BUDGET_BYTES = 400 * 1024
TEXT_KEYS = {"text", "content", "delta", "message", "output", "value", "chunk"}
PROMPTS = {"explore": ("full.md", "update.md"), "bundle": ("bundle_full.md", "bundle_update.md")}
DEFAULT_AGENTS = {"explore": "atlas-mapper", "bundle": "atlas-bundle"}
BUNDLE_MARGIN_BYTES = 2 * 1024
SECRET_ASSIGNMENT = re.compile(
    r"""^(\s*(?:export\s+)?["']?[\w.\-]*(?:SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|API_KEY|ACCESS_KEY|CREDENTIAL)
        [\w.\-]*["']?\s*[=:]\s*)(\S.*)$""",
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)
# Anchored on the literal "://" so the scan can skip ahead: a leading `\w[\w+.\-]*` would
# start at every character of a long token and backtrack, which is quadratic on minified files.
URL_CREDENTIALS = re.compile(r"://[^/\s@:]+:[^/\s@]*@")
OPAQUE_LITERAL = re.compile(r"""(?<=["'])[A-Za-z0-9_\-]{32,}(?=["'])""")
# The markers are kept so the bundle still says a key lives here; only the body goes. Ending on
# \Z as well covers a block the file cuts short, which is what a truncated read leaves behind.
PEM_BLOCK = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----).*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
# These carry their own prefix, so they are recognisable with neither an assignment nor a quoted
# literal around them, which is what the three rules above need: an unquoted YAML value, a token
# pasted into a comment. Every alternative starts with a literal, for the reason noted above.
PROVIDER_SECRET = re.compile(
    r"""(?:
        (?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AIPA|ANPA|ANVA|AROA)[0-9A-Z]{16}   # AWS access key id
      | gh[pousr]_[A-Za-z0-9]{36}                                           # GitHub token
      | github_pat_[A-Za-z0-9_]{60,}                                        # GitHub fine-grained PAT
      | xox[abposr]-[A-Za-z0-9-]{10,}                                       # Slack token
      | AIza[0-9A-Za-z_\-]{35}                                              # Google API key
      | https://hooks\.slack\.com/services/[A-Za-z0-9/+]{20,}               # Slack webhook
      | eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,} # JWT
    )""",
    re.VERBOSE,
)


# ---------- helpers ----------


def now():
    return dt.datetime.now(dt.UTC)


def expand(p):
    return Path(os.path.expanduser(str(p))).resolve()


DEFAULT_IGNORE_CHANGES = [
    "test/*",
    "tests/*",
    "*/test/*",
    "*/tests/*",
    "*__tests__*",
    "*__mocks__*",
    "*_test.go",
    "*_test.py",
    "test_*.py",
    "*/test_*.py",
    "*.test.*",
    "*.spec.*",
    "docs/*",
    "*/docs/*",
    "*.md",
    "*.txt",
    "*.png",
    "*.jpg",
    "*.gif",
    "*.svg",
    # CODEOWNERS lives here, but owners are deterministic now: an owners-only commit lands in
    # restamp, which re-runs extract_facts for free. Ignoring it buys the refresh without a call.
    ".github/*",
    ".vscode/*",
    ".idea/*",
    "LICENSE",
    "CHANGELOG*",
]


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
        "mapper_mode": "bundle",
        "bundle_budget_bytes": DEFAULT_BUNDLE_BUDGET_BYTES,
        "explore_repos": [],
        "repo_domains": {},
        "parallel": 3,
        "timeout_minutes": 20,
        "full_regen_days": 30,
        "max_changed_files_for_update": 150,
        "max_ambiguous_hits": 3,
        "domains": [],
        "ignore_changes": list(DEFAULT_IGNORE_CHANGES),
        "generic_identifiers": [],
    }
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    if cfg["mapper_mode"] not in PROMPTS:
        sys.exit(
            f"unknown mapper_mode {cfg['mapper_mode']!r}; expected one of {', '.join(sorted(PROMPTS))}"
        )
    budget = cfg["bundle_budget_bytes"]
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        sys.exit(f"bundle_budget_bytes must be a positive integer, got {budget!r}")
    # None means "pick the agent that matches each repo's mapper mode"; "" means "pass no --agent".
    cfg.setdefault("kiro_agent", None)
    cfg["generic_identifiers"] = {
        str(s).strip().lower() for s in cfg["generic_identifiers"]
    } | GENERIC_IDENTIFIERS
    cfg["domains"] = [str(d).strip().lower() for d in cfg["domains"] if str(d).strip()]
    return cfg


def git(repo, *args, timeout=60, errors=None):
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        errors=errors,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or f"git {' '.join(args)} failed")
    return r.stdout.strip()


def normalize_remote(url):
    """One clickable https form whatever the clone used, with any embedded credential dropped:
    this URL is written to graph.json and read back by a model. A path or a scheme that has no
    web form is left alone rather than guessed at."""
    # Any userinfo goes, not only user:pass. A token clone is `https://TOKEN@host/...`, and
    # this value is written to graph.json and read back by a model.
    url = re.sub(r"://[^/@\s]+@", "://", url.strip())
    if m := re.fullmatch(r"(?:ssh|git)://(?:[^@/]+@)?(.+)", url):
        url = "https://" + m.group(1)
    elif m := re.fullmatch(r"(?:[\w.\-]+@)?([\w.\-]+):(?!/)(\S+)", url):  # scp form
        url = f"https://{m.group(1)}/{m.group(2)}"
    return re.sub(r"\.git/?$", "", url) if url.startswith("http") else url


def repo_remote_url(repo):
    """None rather than a raise: a clone with no origin is unusual, not a reason to fail a repo."""
    try:
        return normalize_remote(git(repo, "remote", "get-url", "origin")) or None
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None


def repo_last_commit_at(repo):
    """Committer date in strict ISO 8601. A year-old date is the dead-repo signal an agent needs
    before it trusts what an entry says."""
    try:
        return git(repo, "log", "-1", "--format=%cI") or None
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None


def human_bytes(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def human_secs(seconds):
    return f"{seconds:.0f}s" if seconds < 60 else f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


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
            name = aliases.get(str(path)) or (
                base if len(members) == 1 else f"{path.parent.name}-{base}"
            )
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
    root = repo.resolve()
    for dirpath, dirnames, files in os.walk(repo):
        depth = len(Path(dirpath).relative_to(repo).parts)
        dirnames[:] = (
            []
            if depth >= max_depth
            else [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        )
        for f in files:
            if f in filenames or any(fnmatch.fnmatch(f, pat) for pat in filenames if "*" in pat):
                if contained(root, Path(dirpath) / f):
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
                m = re.search(r"^module\s+(\S+)", text, re.MULTILINE)
                pub("go", m and m.group(1), f)
                block = re.findall(r"^require\s*\((.*?)^\)", text, re.MULTILINE | re.DOTALL)
                lines = "\n".join(block).splitlines() + re.findall(
                    r"^require\s+([^\s(]+\s+\S+)", text, re.MULTILINE
                )
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
                group = child_text(root, "groupId") or (
                    child_text(parent, "groupId") if parent is not None else ""
                )
                pub("maven", coord(group, child_text(root, "artifactId")), f)
                for node in root.iter():
                    if local(node) == "dependency":
                        dep(
                            "maven",
                            coord(child_text(node, "groupId"), child_text(node, "artifactId")),
                            f,
                        )
            elif f.name.startswith("build.gradle"):
                for m in re.finditer(GRADLE_DEP, text):
                    dep("maven", coord(m.group(1), m.group(2)), f)
                group = re.search(r"""^\s*group\s*=?\s*['"]([^'"]+)['"]""", text, re.MULTILINE)
                root_name = ""
                for settings in ("settings.gradle", "settings.gradle.kts"):
                    path = inside_repo(repo, f.parent.relative_to(repo) / settings)
                    if path is not None:
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


# ---------- a yaml subset ----------

# Deploy and API manifests are yaml, and reading them by hand is the price of the script staying
# stdlib-only. This covers block mappings and sequences, quoted scalars, flow collections, and
# block scalars kept as opaque text. Anchors and aliases are dropped rather than resolved, and
# anything else yields no documents at all, so a file we cannot read is simply not a source.

BLOCK_SCALAR = re.compile(r"^(.*?):\s*[|>][0-9+-]*$")
# A colon only opens a value when a space or the line end follows it, which is what stops
# `https://host` and `image: redis:7` from being read as keys.
MAP_ENTRY = re.compile(r"^([^:\s][^:]*):(?:\s+(.*))?$")


def strip_comment(line):
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def yaml_scalar(text):
    text = text.strip()
    if text[:1] == "*":  # an alias; resolving these is more machinery than these formats need
        return ""
    if text[:1] in "&!":  # an anchor or a tag: keep the value it decorates, drop the decoration
        text = text.split(" ", 1)[1].strip() if " " in text else ""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def split_flow(text):
    parts, depth, quote, start = [], 0, "", 0
    for i, ch in enumerate(text):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [p for p in (p.strip() for p in parts) if p]


def yaml_value(text):
    if text.startswith("[") and text.endswith("]"):
        return [yaml_value(p) for p in split_flow(text[1:-1])]
    if text.startswith("{") and text.endswith("}"):
        out = {}
        for part in split_flow(text[1:-1]):
            key, sep, value = part.partition(":")
            if not sep:
                raise ValueError(f"not a flow mapping entry: {part!r}")
            out[yaml_scalar(key)] = yaml_value(value.strip())
        return out
    return yaml_scalar(text)


def yaml_scan(text):
    """One list of (indent, content, block_body) per document. Block bodies are taken from the
    raw lines so that a `#` or a `key:` inside a script cannot be mistaken for structure."""
    raw, docs, items, i = text.splitlines(), [], [], 0
    while i < len(raw):
        line, i = raw[i], i + 1
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise ValueError("tab indentation")
        content = strip_comment(line).rstrip()
        if not content.strip():
            continue
        indent = len(content) - len(content.lstrip(" "))
        content = content.strip()
        if indent == 0 and content in ("---", "..."):
            docs.append(items)
            items = []
            continue
        if m := BLOCK_SCALAR.match(content):
            body = []
            while i < len(raw) and (
                not raw[i].strip() or len(raw[i]) - len(raw[i].lstrip()) > indent
            ):
                body.append(raw[i].strip())
                i += 1
            items.append((indent, m.group(1).strip() + ":", "\n".join(body).strip()))
            continue
        items.append((indent, content, None))
    docs.append(items)
    return docs


def seq_item(text):
    return text == "-" or text.startswith("- ")


def yaml_node(items, pos, indent):
    if seq_item(items[pos][1]):
        return yaml_seq(items, pos, indent)
    return yaml_map(items, pos, indent)


def yaml_map(items, pos, indent):
    out = {}
    while pos < len(items) and items[pos][0] == indent:
        _, text, block = items[pos]
        # MAP_ENTRY would happily read `- host: a` as a key named `- host`.
        m = None if seq_item(text) else MAP_ENTRY.match(text)
        if not m:
            raise ValueError(f"not a mapping entry: {text!r}")
        key, value = yaml_scalar(m.group(1)), (m.group(2) or "").strip()
        pos += 1
        if block is not None:
            out[key] = block
        elif value:
            out[key] = yaml_value(value)
        elif pos < len(items) and items[pos][0] > indent:
            out[key], pos = yaml_node(items, pos, items[pos][0])
        elif pos < len(items) and items[pos][0] == indent and seq_item(items[pos][1]):
            # kubectl, PyYAML and kustomize all put the dash at the key's own column. yaml_seq
            # stops at the first line that is not an item, which is this mapping's next key.
            out[key], pos = yaml_seq(items, pos, indent)
        else:
            out[key] = None
    if pos < len(items) and items[pos][0] > indent:
        raise ValueError(f"unexpected indent at {items[pos][1]!r}")
    return out, pos


def yaml_seq(items, pos, indent):
    out = []
    while pos < len(items) and items[pos][0] == indent:
        col, text, block = items[pos]
        if not seq_item(text):
            break
        rest = text[1:].strip()
        pos += 1
        if not rest:
            if pos < len(items) and items[pos][0] > indent:
                value, pos = yaml_node(items, pos, items[pos][0])
            else:
                value = None
        elif MAP_ENTRY.match(rest):
            # `- host: a` opens a mapping whose column is where `host` starts, and its sibling
            # keys are indented to that same column rather than to the dash.
            inner = col + len(text) - len(rest)
            sub, end = [(inner, rest, block)], pos
            while end < len(items) and items[end][0] >= inner:
                sub.append(items[end])
                end += 1
            value, used = yaml_node(sub, 0, inner)
            if used != len(sub):
                raise ValueError(f"unread lines under {rest!r}")
            pos = end
        else:
            value = yaml_value(rest)
        out.append(value)
    return out, pos


def parse_yaml_docs(text):
    try:
        out = []
        for items in yaml_scan(text):
            if not items:
                continue
            value, pos = yaml_node(items, 0, items[0][0])
            if pos != len(items):
                raise ValueError(f"trailing content at {items[pos][1]!r}")
            out.append(value)
        return out
    except (ValueError, IndexError):
        return []


# ---------- deterministic repo facts ----------

FACTS_VERSION = 3
FACTS_MAX_YAML_FILES = 400
CODEOWNERS_PATHS = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS")
OPENAPI_JSON = ("openapi*.json", "swagger*.json")
PROTO_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;")
PROTO_SERVICE = re.compile(r"^\s*service\s+(\w+)\s*\{")
TF_RESOURCE = re.compile(r'^\s*resource\s+"(\w+)"\s+"[^"]*"\s*\{')
TF_LITERAL = re.compile(r'^\s*(\w+)\s*=\s*"([^"]*)"\s*$')
TF_RESOURCES = {  # resource type -> (attribute holding the real name, our kind, is a datastore)
    "aws_sqs_queue": ("name", "queue", False),
    "aws_s3_bucket": ("bucket", "s3", True),
    "aws_dynamodb_table": ("name", "dynamodb", True),
}


def facts_text(path):
    """Unredacted, unlike read_capped: nothing here is shown to a model, and redaction would eat
    the very names we are after. None when the file is missing or too big to be worth parsing."""
    try:
        if path.stat().st_size > BUNDLE_MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def codeowners_rule(text):
    """The rule that covers the whole repo: `*` if it is there, else the first one written."""
    best = None
    for line in text.splitlines():
        line = strip_comment(line).strip()
        if not line or line.startswith("["):  # a GitLab section header, not a rule
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        if best is None or parts[0] == "*":
            best = parts[1:]
        if parts[0] == "*":
            break
    return best or []


def extract_facts(repo, stoplist=frozenset()):
    """Names, endpoints, owned stores and owners read straight out of the repo's own deploy and
    API manifests. The model would otherwise have to infer all of this from a bundle excerpt, so
    doing it here costs no credits and puts the resulting edges on the `exact` tier."""
    idents, owners, exposes, datastores = {}, [], [], []

    def usable(raw):
        # provide(..., "exact") in build() skips the alias stoplist, so a machine-generated "api"
        # or "db" would quietly make this repo the org-wide owner of that name.
        text = str(raw or "").strip()
        return "" if len(text) < 2 or text.lower() in stoplist else text

    def ident(raw):
        if text := usable(raw):
            idents.setdefault(text.lower(), text)
        return text

    def expose(kind, key, detail, evidence, as_identifier=True):
        # A service name or an ingress host is also a name this repo answers to; a queue name is
        # only ever an expose key, and indexing it as a hostname would invent edges.
        key = ident(key) if as_identifier else usable(key)
        if key:
            exposes.append(
                {
                    "kind": kind,
                    "name": key,
                    "key": key,
                    "detail": detail,
                    "evidence": evidence,
                    "source": "deterministic",
                }
            )

    def store(kind, name, evidence):
        if name := str(name or "").strip():
            datastores.append(
                {
                    "kind": kind,
                    "name": name,
                    "access": "owner",
                    "evidence": evidence,
                    "source": "deterministic",
                }
            )

    def own(raw):
        if (text := str(raw or "").strip()) and text not in owners:
            owners.append(text)

    def server_hosts(doc, rel):
        info = doc.get("info") if isinstance(doc.get("info"), dict) else {}
        title = str(info.get("title") or "").strip()
        if title and not re.search(r"\s", title):  # a prose title is not a join key
            ident(title)
        servers = doc.get("servers")
        for server in servers if isinstance(servers, list) else []:
            url = str((server.get("url") if isinstance(server, dict) else server) or "")
            # A relative url (`/api/v1`) names no host, and svc_forms would hand it back whole.
            if not re.match(r"[a-z][a-z0-9+.\-]*://", url.strip().lower()):
                continue
            host = svc_forms(url)[0]
            # `{scheme}://{host}` is a template, not a host a consumer could match on.
            if host and "{" not in host and not noisy_host(host):
                expose("http", host, "openapi server", rel)

    for rel in CODEOWNERS_PATHS:
        if (path := inside_repo(repo, rel)) and (text := facts_text(path)) is not None:
            for owner in codeowners_rule(text):
                own(owner)
            break

    parsed = 0
    # Sorted, not os.walk order: which manifests fit under the cap is a property of the
    # repo, not of how the filesystem happens to hand back a directory.
    for f in sorted(walk(repo, {"*.yaml", "*.yml"})):
        rel = f.relative_to(repo).as_posix()
        if "templates/" in rel:  # a Helm template is a Go template, not yaml
            continue
        if parsed >= FACTS_MAX_YAML_FILES:
            break
        text = facts_text(f)
        if text is None:
            continue
        parsed += 1
        base = f.name.lower()
        for doc in parse_yaml_docs(text):
            if not isinstance(doc, dict):
                continue
            meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
            spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
            if base.startswith("catalog-info"):
                ident(meta.get("name"))
                own(spec.get("owner"))
                apis = spec.get("providesApis")
                for api in apis if isinstance(apis, list) else []:
                    ident(api)
            elif base == "chart.yaml":
                ident(doc.get("name"))
            elif base.startswith(("docker-compose", "compose")):
                services = doc.get("services")
                for name, body in (services if isinstance(services, dict) else {}).items():
                    # Without a build section the entry is an off-the-shelf image, a local dev
                    # alias such as `postgres`, not a name this repo answers to.
                    if isinstance(body, dict) and body.get("build") is not None:
                        ident(name)
            elif base.startswith(("openapi", "swagger")):
                server_hosts(doc, rel)
            if not (doc.get("apiVersion") and doc.get("kind")):
                continue
            if doc["kind"] == "Service":
                name = ident(meta.get("name"))
                expose("http", name, "kubernetes service", rel)
                if name and (ns := str(meta.get("namespace") or "").strip()):
                    ident(f"{name}.{ns}.svc.cluster.local")
            elif doc["kind"] == "Ingress":
                rules = spec.get("rules")
                for rule in rules if isinstance(rules, list) else []:
                    if isinstance(rule, dict):
                        expose("http", rule.get("host"), "ingress host", rel)

    for f in sorted(walk(repo, set(OPENAPI_JSON))):
        text = facts_text(f)
        if text is None:
            continue
        try:
            doc = json.loads(text)
        except ValueError as e:
            print(f"warn: could not parse {f}: {e}", file=sys.stderr)
            continue
        if isinstance(doc, dict):
            server_hosts(doc, f.relative_to(repo).as_posix())

    for f in sorted(walk(repo, {"*.proto"})):
        text = facts_text(f)
        if text is None:
            continue
        rel, package = f.relative_to(repo).as_posix(), ""
        for n, line in enumerate(text.splitlines(), 1):
            if m := PROTO_PACKAGE.match(line):
                package = m.group(1)
            elif package and (m := PROTO_SERVICE.match(line)):
                expose("grpc", f"{package}.{m.group(1)}", "grpc service", f"{rel}:{n}")

    for f in sorted(walk(repo, {"*.tf"})):
        text = facts_text(f)
        if text is None:
            continue
        rel, resource = f.relative_to(repo).as_posix(), None
        for n, line in enumerate(text.splitlines(), 1):
            if m := TF_RESOURCE.match(line):
                resource = TF_RESOURCES.get(m.group(1))
            elif line.startswith("}"):
                resource = None
            elif resource and (m := TF_LITERAL.match(line)):
                attr, kind, is_store = resource
                # A partly interpolated name is not a string any consumer can match on.
                if m.group(1) != attr or "${" in m.group(2):
                    continue
                if is_store:
                    store(kind, m.group(2), f"{rel}:{n}")
                else:
                    expose(kind, m.group(2), f"terraform {kind}", f"{rel}:{n}", as_identifier=False)

    def first_per_key(items, keyer, order):
        """One entry per name. An ingress host is usually also the openapi server url, and the
        second copy only doubles up in the docs; sorting first keeps which one wins stable."""
        out = {}
        for item in sorted(items, key=order):
            out.setdefault(keyer(item), item)
        return list(out.values())

    return {
        "identifiers": [text for _, text in sorted(idents.items())],
        "exposes": first_per_key(
            exposes, expose_key, lambda e: (e["kind"], e["key"], e["evidence"])
        ),
        "datastores": first_per_key(
            datastores, store_key, lambda d: (d["kind"], d["name"], d["evidence"])
        ),
        "owners": owners,
    }


def expose_key(item):
    return str(item.get("kind") or "").lower(), str(item.get("key") or "").lower()


def store_key(item):
    return str(item.get("kind") or "").lower(), str(item.get("name") or "").lower()


def merge_facts(manifest, facts, prev_meta):
    """Deterministic items beat the model's on a collision, and the previous run's are cleared
    out first: a restamp reuses the old entry wholesale and an update prompt hands the model the
    old entry to edit, so without the strip every run would append another copy of everything,
    and a deleted catalog-info.yaml would leave its name behind for good."""
    stale = (prev_meta or {}).get("deterministic") or {}
    for field, keyer in (("exposes", expose_key), ("datastores", store_key)):
        model = [
            i
            for i in manifest.get(field) or []
            if isinstance(i, dict) and i.get("source") != "deterministic"
        ]
        fresh = [dict(i) for i in facts[field]]
        taken = {keyer(i) for i in fresh}
        for item in fresh:
            beaten = next((m for m in model if keyer(m) == keyer(item)), {})
            # A deterministic name is only ever the key repeated, so a label the model wrote
            # ("Orders API", "POST /orders") reads better in the docs. Its detail is all we get.
            if beaten.get("name") and item.get("name") == item.get("key"):
                item["name"] = beaten["name"]
            if beaten.get("detail") and not item.get("detail"):
                item["detail"] = beaten["detail"]
        manifest[field] = fresh + [i for i in model if keyer(i) not in taken]

    dead = {str(s).strip().lower() for s in stale.get("identifiers") or []}
    live = {str(t).strip().lower() for t in facts["identifiers"]}
    seen, kept = set(), []
    for text in list(manifest.get("identifiers") or []) + list(facts["identifiers"]):
        form = str(text).strip().lower()
        if form in seen or (form in dead and form not in live):
            continue
        seen.add(form)
        kept.append(text)
    manifest["identifiers"] = kept

    if facts["owners"]:
        manifest["owners"] = list(facts["owners"])
    else:
        dead = {str(s).strip() for s in stale.get("owners") or []}
        manifest["owners"] = [o for o in manifest.get("owners") or [] if str(o).strip() not in dead]


# ---------- context bundle ----------

BUNDLE_WALK_DEPTH = 4
BUNDLE_TREE_DEPTH = 3
BUNDLE_TREE_MAX = 400
BUNDLE_FILE_BYTES = 12 * 1024
BUNDLE_README_BYTES = 8 * 1024
BUNDLE_MAX_FILE_BYTES = 512 * 1024
BUNDLE_MAX_FILES = 4000
BUNDLE_SIGNALS_PER_CATEGORY = 60
BUNDLE_LINE_CHARS = 200
BUNDLE_DOT_DIRS = {".github"}
KEY_FILES = (
    ("README*", "CODEOWNERS", "OWNERS", "catalog-info.y*ml"),
    (
        "package.json",
        "go.mod",
        "pyproject.toml",
        "requirements*.txt",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle*",
        "Cargo.toml",
        "*.csproj",
    ),
    (
        "Dockerfile*",
        "docker-compose*.y*ml",
        "compose*.y*ml",
        "Chart.yaml",
        "values*.y*ml",
        "serverless.y*ml",
        "*.tf",
        "Procfile",
        "app.yaml",
        "fly.toml",
        "k8s/*.y*ml",
        "deploy/*.y*ml",
        "deployment/*.y*ml",
        "manifests/*.y*ml",
        "charts/*.y*ml",
        "helm/*.y*ml",
    ),
    (".env.example", ".env.sample", ".env.template", "env.example"),
    (
        "openapi*.yaml",
        "openapi*.yml",
        "openapi*.json",
        "swagger*.yaml",
        "swagger*.yml",
        "swagger*.json",
        "*.proto",
        "*.graphql",
        "*.graphqls",
        "asyncapi*.y*ml",
    ),
    (
        "main.go",
        "cmd/*/main.go",
        "main.py",
        "app.py",
        "manage.py",
        "src/main.*",
        "index.js",
        "index.ts",
        "src/index.js",
        "src/index.ts",
        "server.js",
        "server.ts",
        "Program.cs",
        "src/main.rs",
    ),
)
SIGNAL_EXTS = {
    ".go", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".rb", ".cs", ".rs", ".php", ".scala",
    ".sh", ".yaml", ".yml", ".toml", ".json", ".tf", ".hcl", ".properties", ".conf", ".cfg", ".ini", ".env",
}  # fmt: skip
SIGNAL_PATTERNS = {
    "url": re.compile(r"\bhttps?://(?P<key>[A-Za-z0-9_.\-]+(?::\d+)?)"),
    "env": re.compile(
        r"(?i:getenv|environ|process\.env|env::var|getenvironmentvariable|\bENV\b)\W{0,4}(?P<key>[A-Z][A-Z0-9_]{2,})"
    ),
    "messaging": re.compile(
        r"""(?:topic|queue|subscri|publish|kafka|sqs|sns|pubsub|nats)[^"']{0,60}["'](?P<key>[\w.\-/:]{3,})["']""",
        re.IGNORECASE,
    ),
    "datastore": re.compile(
        r"""(?P<key>(?:postgres|postgresql|mysql|mongodb|redis|dynamodb|elasticsearch|s3)://[^\s"'`,)]+
            |(?:postgres|postgresql|mysql|mongodb|redis|dynamodb|elasticsearch)[^"']{0,40}["'][^"']{3,}["'])""",
        re.IGNORECASE | re.VERBOSE,
    ),
    "client": re.compile(
        r"""(?:grpc\.Dial|NewClient|\.Dial\(|baseURL|base_url|BaseUrl)[^"']{0,40}["'](?P<key>[^"']{3,})["']""",
        re.IGNORECASE,
    ),
}
NOISE_HOSTS = (
    "localhost", "127.0.0.1", "0.0.0.0", "example.com", "example.org", "w3.org", "json-schema.org",
    "schema.org", "github.com", "gitlab.com", "npmjs.org", "npmjs.com", "pypi.org", "golang.org",
    "apache.org", "maven.org", "googleapis.com", "amazonaws.com",
)  # fmt: skip
ENV_INTERESTING = (
    "TOPIC",
    "QUEUE",
    "BUCKET",
    "TABLE",
    "DB",
    "DATABASE",
    "KAFKA",
    "SQS",
    "SNS",
    "REDIS",
)


def bundle_path_ok(rel, repo):
    """The limits the walk enforces by not descending, applied to a path it did not produce, so
    both collectors agree on what a bundle may contain. The is_file check also drops submodules,
    which git lists as paths but which nothing here can read."""
    parts = rel.split("/")
    if len(parts) - 1 > BUNDLE_WALK_DEPTH:
        return False
    if any(d in SKIP_DIRS or (d.startswith(".") and d not in BUNDLE_DOT_DIRS) for d in parts[:-1]):
        return False
    return inside_repo(repo, rel) is not None


def tracked_files(repo):
    """git knows what .gitignore says and os.walk does not, so build output and local scratch
    never reach the tree, the excerpts or the signal scan. None when there is nothing to list,
    which is the only case the walk still has to cover."""
    try:
        # A tracked path need not be utf-8, and git prints the bytes as they are. surrogateescape
        # decodes such a name to the same string the filesystem calls it, so it is read if it is
        # there and skipped if it is not, rather than failing the listing for the whole repo.
        listing = git(
            repo, "ls-files", "-z", "--cached", "--exclude-standard", errors="surrogateescape"
        )
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
        return None  # not a work tree: falling back to the walk is the documented behaviour
    # git() strips whitespace and NUL is not whitespace, so the trailing separator survives.
    rels = sorted(r for r in listing.split("\0") if r and bundle_path_ok(r, repo))
    return rels[:BUNDLE_MAX_FILES] or None


def bundle_files(repo):
    """Every candidate path, repo-relative and sorted."""
    return tracked_files(repo) or walked_files(repo)


def walked_files(repo):
    """The fallback for a path git will not list. `.github` is the one dot-directory worth
    descending into, because CODEOWNERS often lives there."""
    out = []
    for dirpath, dirnames, files in os.walk(repo):
        rel_dir = Path(dirpath).relative_to(repo)
        depth = len(rel_dir.parts)
        dirnames[:] = (
            []
            if depth >= BUNDLE_WALK_DEPTH
            else sorted(
                d
                for d in dirnames
                if d not in SKIP_DIRS and (not d.startswith(".") or d in BUNDLE_DOT_DIRS)
            )
        )
        for f in sorted(files):
            if inside_repo(repo, rel_dir / f) is None:
                continue
            out.append((rel_dir / f).as_posix())
            if len(out) >= BUNDLE_MAX_FILES:
                return sorted(out)
    return sorted(out)


def path_matches(rel, pattern):
    """A pattern containing a slash is matched against the whole repo-relative path, where
    fnmatch's `*` also spans separators, so `k8s/*.yaml` reaches nested manifests."""
    return fnmatch.fnmatch(rel if "/" in pattern else rel.rsplit("/", 1)[-1], pattern)


def key_file_group(rel):
    for i, patterns in enumerate(KEY_FILES):
        if any(path_matches(rel, p) for p in patterns):
            return i
    return None


def read_capped(path, cap):
    """None when the file is too big to be worth reading at all."""
    try:
        if path.stat().st_size > BUNDLE_MAX_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"warn: could not read {path}: {e}", file=sys.stderr)
        return None
    text = redact(text)
    return text[:cap] + "\n... truncated\n" if len(text) > cap else text


def excerpt_blocks(repo, paths):
    ranked = sorted((g, rel) for rel in paths if (g := key_file_group(rel)) is not None)
    blocks = []
    for _, rel in ranked:
        cap = (
            BUNDLE_README_BYTES
            if rel.rsplit("/", 1)[-1].startswith("README")
            else BUNDLE_FILE_BYTES
        )
        body = read_capped(repo / rel, cap)
        if body is not None:
            blocks.append(f"### {rel}\n{body.rstrip()}\n\n")
    return blocks


def noisy_host(host):
    return any(host == n or host.endswith("." + n) for n in NOISE_HOSTS)


def signal_key(category, match):
    if category != "url":
        return match.group("key").lower()
    host = match.group("key").split(":")[0].lower()
    return None if noisy_host(host) else host


def signal_sections(repo, paths):
    """One line per distinct target, so a URL repeated in fifty call sites costs one slot."""
    found = {name: {} for name in SIGNAL_PATTERNS}
    for rel in paths:
        if all(len(hits) >= BUNDLE_SIGNALS_PER_CATEGORY for hits in found.values()):
            break  # every category full: the rest can only be read, scanned and thrown away
        # Not Path.suffix: pathlib reports no suffix for a dotfile, which would skip `.env`.
        if "." + rel.rsplit(".", 1)[-1] not in SIGNAL_EXTS:
            continue
        text = read_capped(repo / rel, BUNDLE_MAX_FILE_BYTES)
        if text is None:
            continue
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line:
                continue
            for category, pattern in SIGNAL_PATTERNS.items():
                hits = found[category]
                if len(hits) >= BUNDLE_SIGNALS_PER_CATEGORY:
                    continue
                for m in pattern.finditer(line):
                    key = signal_key(category, m)
                    if category == "env" and not (
                        ENV_SUFFIX.search(m.group("key"))
                        or any(w in m.group("key") for w in ENV_INTERESTING)
                    ):
                        continue
                    if key and key not in hits:
                        hits[key] = f"{rel}:{lineno}: {line[:BUNDLE_LINE_CHARS]}\n"
    return [(name, list(hits.values())) for name, hits in found.items() if hits]


def fit(blocks, budget):
    """Priority order is kept: the first block that does not fit ends the section, rather than
    letting a later small block jump the queue."""
    out, used = [], 0
    for b in blocks:
        size = len(b.encode("utf-8"))
        if used + size > budget:
            break
        out.append(b)
        used += size
    return "".join(out)


def gather_bundle(repo, budget, only=None):
    """A deterministic, redacted, budgeted picture of one repo: tree, key files, and the lines
    that name other systems. Replaces letting the model explore the repo itself."""
    files = bundle_files(repo)
    chosen = files if only is None else [f for f in files if f in {str(p) for p in only}]

    shallow = [f for f in files if len(f.split("/")) <= BUNDLE_TREE_DEPTH]
    tree = [f"- {f}\n" for f in shallow[:BUNDLE_TREE_MAX]]
    hidden = len(files) - len(tree)
    if hidden > 0:
        tree.append(f"... and {hidden} more files\n")
    text = "## Tree\n" + fit(tree, budget * 10 // 100)

    excerpts = fit(excerpt_blocks(repo, chosen), budget * 70 // 100 - len(text.encode("utf-8")))
    if excerpts:
        text += "\n## Files\n" + excerpts

    sections = []
    for name, lines in signal_sections(repo, chosen):
        sections.append(f"### {name}\n" + "".join(lines) + "\n")
    signals = fit(sections, budget - len(text.encode("utf-8")) - len("\n## Signals\n"))
    if signals:
        text += "\n## Signals\n" + signals
    return text


# ---------- LLM extraction ----------


def redact(text):
    """Mask secret values before repo content reaches the model or the atlas: private key blocks,
    assignments to secret-looking keys, credentials embedded in URLs, tokens whose provider prefix
    gives them away, and long opaque quoted literals."""

    def opaque(m):
        s = m.group(0)
        return "<redacted>" if re.search(r"[A-Za-z]", s) and re.search(r"[0-9]", s) else s

    # The multi-line rule runs first, so the line-scoped ones never see a key body.
    text = PEM_BLOCK.sub(r"\1\n<redacted>\n\2", text)
    text = SECRET_ASSIGNMENT.sub(r"\g<1><redacted>", text)
    text = URL_CREDENTIALS.sub("://<redacted>@", text)
    text = PROVIDER_SECRET.sub("<redacted>", text)
    return OPAQUE_LITERAL.sub(opaque, text)


def domain_for(cfg, name):
    """First matching glob wins, so the config can pin a domain the model keeps getting wrong."""
    for pattern, domain in (cfg["repo_domains"] or {}).items():
        if fnmatch.fnmatch(name, str(pattern)):
            return str(domain).strip().lower()
    return ""


def mapper_prompt(cfg, repo, mapper, template, values, only=None, cap=None):
    """In bundle mode the bundle gets whatever the rendered template leaves of the prompt cap."""
    if mapper != "bundle":
        return build_prompt(cfg, template, **values)
    shell = build_prompt(cfg, template, BUNDLE="", **values)
    cap = cfg["bundle_budget_bytes"] if cap is None else cap
    budget = cap - len(shell.encode("utf-8")) - BUNDLE_MARGIN_BYTES
    return build_prompt(cfg, template, BUNDLE=gather_bundle(repo, budget, only=only), **values)


def prompt_hash(mode):
    """Changing a prompt or the schema invalidates every manifest made with the old one."""
    h = hashlib.sha256(mode.encode("utf-8"))
    for name in (*PROMPTS[mode], "schema.json"):
        h.update((KIT / "prompts" / name).read_bytes())
    return h.hexdigest()[:12]


def build_prompt(cfg, template, **values):
    text = (KIT / "prompts" / template).read_text(encoding="utf-8")
    values.setdefault("SCHEMA", (KIT / "prompts" / "schema.json").read_text(encoding="utf-8"))
    values.setdefault(
        "DOMAINS",
        ", ".join(cfg["domains"] + ["unassigned"])
        if cfg["domains"]
        else "a short lowercase name of your choice",
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


def run_kiro(cfg, repo, prompt, log_path, mapper="explore"):
    name = cfg["kiro_agent"] if cfg["kiro_agent"] is not None else DEFAULT_AGENTS[mapper]
    agent = ["--agent", name] if name else []
    # Bundle mode has already read the repo, so it runs outside it: a session started inside a
    # repo loads that repo's steering, hooks and MCP servers unattended.
    cwd = cfg["atlas_dir"] if mapper == "bundle" else repo
    Path(cwd).mkdir(parents=True, exist_ok=True)
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
    ]
    stamp = now().isoformat(timespec="seconds")
    # The prompt goes in on stdin, which has no size limit of its own, and is logged by length
    # rather than text so repo content never lands in the log.
    header = f"=== attempt {stamp} ===\n$ {' '.join(cmd)} <prompt {len(prompt)} chars>\n"
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=cfg["timeout_minutes"] * 60,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired as e:
        partial = (
            e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        )
        log_append(log_path, header + "TIMEOUT\n" + ANSI.sub("", partial) + "\n")
        raise RuntimeError(
            "timed out; usually a tool call waiting for approval (see README)"
        ) from e
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


def contained(root, path):
    """`path` with every symlink resolved, or None when that lands outside the resolved `root`.
    Repo content is untrusted: a committed `README.md -> ~/.aws/credentials` must not be read
    into a prompt, and a repo reached through a symlinked parent must still contain its files."""
    root, target = root.resolve(), path.resolve()
    return target if target == root or root in target.parents else None


def inside_repo(repo, rel):
    """The regular file `rel` names, if it is one and it stays inside the repo."""
    target = contained(repo, repo / rel)
    return target if target is not None and target.is_file() else None


def evidence_ok(repo, evidence):
    m = re.match(r"^\s*([^\s:#]+)", str(evidence or ""))
    if not m:
        return False
    target = contained(repo, repo / m.group(1).removeprefix("./"))
    return target is not None and target.exists()


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
        m[field] = (
            [str(i) for i in items if not isinstance(i, (dict, list))]
            if isinstance(items, list)
            else []
        )
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
    mapper = "explore" if name in set(cfg["explore_repos"]) else cfg["mapper_mode"]
    stamp = prompt_hash(mapper)

    if old and not args.full and meta.get("prompt_hash") == stamp:
        if meta.get("commit") == head:
            # Nothing atlas.py reads out of a repo itself moves the prompt hash, so without the
            # facts stamp a repo whose commit has not changed would never pick up a new extractor
            # or a new field in _meta. A stale stamp restamps, which re-reads it all without a
            # credit, even when a full regen is due: the model would see nothing new, and paying
            # for every dormant repo after a version bump is not what the 30-day refresh is for.
            if meta.get("facts_version") == FACTS_VERSION:
                return name, "skip", "up to date", 0
            mode = "restamp"
        elif not is_full_due(meta.get("last_full_at"), cfg["full_regen_days"]):
            try:
                changed = git(repo, "diff", "--name-only", meta["commit"], head).splitlines()
                relevant = [
                    f
                    for f in changed
                    if not any(fnmatch.fnmatch(f, p) for p in cfg["ignore_changes"])
                ]
                if not relevant:
                    mode = "restamp"
                elif len(relevant) <= cfg["max_changed_files_for_update"]:
                    mode, changed = "update", relevant
            except (RuntimeError, OSError, subprocess.SubprocessError):
                mode = "full"  # old commit missing (rewritten history, shallow clone)

    if args.dry_run:
        return name, "dry-run", mode, 0

    # Read after the skip and dry-run branches, so neither pays for a subprocess it cannot use.
    remote_url = repo_remote_url(repo)
    last_commit_at = repo_last_commit_at(repo)
    prompt_bytes = 0
    if mode == "restamp":
        manifest = {k: v for k, v in old.items() if k != "_meta"}
    else:
        full_template, update_template = PROMPTS[mapper]
        prompt = None
        if mode == "update":
            prompt = mapper_prompt(
                cfg,
                repo,
                mapper,
                update_template,
                {
                    "OLD_COMMIT": meta["commit"],
                    "NEW_COMMIT": head,
                    "CHANGED_FILES": "\n".join(f"- {f}" for f in changed),
                    "MANIFEST": json.dumps(
                        {k: v for k, v in old.items() if k not in ("_meta", "packages")}, indent=2
                    ),
                },
                only=set(changed),
                cap=MAX_UPDATE_PROMPT_BYTES,
            )
            if len(prompt.encode("utf-8")) > MAX_UPDATE_PROMPT_BYTES:
                print(
                    f"warn: {name}: update prompt is {len(prompt)} chars, regenerating in full",
                    file=sys.stderr,
                )
                mode, changed, prompt = "full", [], None
        if prompt is None:
            prompt = mapper_prompt(cfg, repo, mapper, full_template, {})
        prompt_bytes = len(prompt.encode("utf-8"))
        log = cfg["atlas_dir"] / "logs" / f"{name}.log"
        try:
            manifest = run_kiro(cfg, repo, prompt, log, mapper)
        except ValueError:
            manifest = run_kiro(cfg, repo, prompt, log, mapper)  # one retry on unparseable output

    # Merged before the gate, so a parser that emits a path the repo does not have shows up in
    # dropped_without_evidence instead of reaching the graph unchallenged.
    facts = extract_facts(repo, cfg["generic_identifiers"])
    merge_facts(manifest, facts, meta)
    dropped, rejected = clean_manifest(manifest, repo, cfg)
    manifest["name"] = name
    forced = domain_for(cfg, name)
    if forced:
        manifest["domain"] = forced
    manifest["packages"] = extract_packages(repo)
    manifest["_meta"] = {
        "repo_path": str(repo),
        "remote_url": remote_url,
        "commit": head,
        "last_commit_at": last_commit_at,
        "generated_at": now().isoformat(timespec="seconds"),
        "mode": mode,
        "mapper_mode": mapper,
        "domain_source": "config" if forced else "model",
        "model": cfg["model"] if mode != "restamp" else meta.get("model"),
        "last_full_at": now().isoformat(timespec="seconds")
        if mode == "full"
        else meta.get("last_full_at"),
        "dropped_without_evidence": dropped,
        "domain_rejected": rejected,
        "prompt_hash": stamp,
        "prompt_bytes": prompt_bytes,
        "facts_version": FACTS_VERSION,
        # identifiers and owners are plain strings and cannot carry a per-item source key, so
        # the next run needs this record to tell last run's deterministic names from the model's.
        "deterministic": {"identifiers": facts["identifiers"], "owners": facts["owners"]},
    }
    write_json(out, manifest)
    sent = f", {human_bytes(prompt_bytes)} prompt" if prompt_bytes else ""
    return (
        name,
        mode,
        (
            f"{len(manifest['exposes'])} exposes, {len(manifest['consumes'])} consumes, "
            f"{len(dropped)} dropped{sent}"
        ),
        prompt_bytes,
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
    if not isinstance(m, dict):
        raise ValueError("manifest is not a JSON object")
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
        except (OSError, ValueError) as e:  # JSONDecodeError is a ValueError
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
            if (
                str(ds.get("access") or "").strip().lower() == "owner"
                and store not in GENERIC_STORES
            ):
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
                # .get: an entry written before these existed still builds, and stays that way
                # until its repo next changes or the facts stamp moves.
                "remote_url": m["_meta"].get("remote_url"),
                "last_commit_at": m["_meta"].get("last_commit_at"),
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
        lines += [
            f"- {e['from']} via {e['kind']} `{e['key']}` ({e['match']})" for e in in_edges
        ] or ["- no known dependents"]
        lines += ["", "## Datastores", ""]
        lines += [
            f"- {d.get('kind')} `{d.get('name')}` ({d.get('access')}) ({d.get('evidence')})"
            for d in m.get("datastores", [])
        ] or ["- none found"]
        if m.get("components"):
            lines += ["", "## Components", ""]
            lines += [
                f"- {c.get('name')} `{c.get('path')}`: {c.get('role')}" for c in m["components"]
            ]
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
            f'  {mid(n)}["{mtext(n)} ({mtext(graph["repos"][n]["domain"])})"]'
            for n in sorted(outsiders)
        ]
        lines += [
            f"  {mid(a)} -->|{mtext(', '.join(sorted(k)))}| {mid(b)}"
            for (a, b), k in sorted(pairs.items())
        ]
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
            + [
                f"- {n} ({graph['repos'][n]['kind']}): {graph['repos'][n]['summary']}"
                for n in sorted(members)
            ]
            + [""]
        )
    write_text(ad / "index.md", "\n".join(index) + "\n")


# ---------- commands ----------


MODE_ORDER = ("full", "update", "restamp", "skip", "dry-run")


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

    errors, counts, prompt_bytes, started = 0, {}, 0, now()
    # A dry run writes nothing, so make it readable during a real run rather than having it
    # exit with "another atlas run holds ..." exactly when someone wants to see what is queued.
    lock = (
        contextlib.nullcontext()
        if args.dry_run
        else atlas_lock(cfg["atlas_dir"], args.force_unlock)
    )
    with lock:
        if not args.dry_run:
            save_repo_map(cfg, repos)
        print(f"{len(items)} repos, model {cfg['model']}, parallel {cfg['parallel']}")
        with cf.ThreadPoolExecutor(max_workers=cfg["parallel"]) as pool:
            futures = {pool.submit(process_repo, cfg, n, p, args): n for n, p in items}
            for fut in cf.as_completed(futures):
                try:
                    # Kiro cannot report credits in headless mode, so the summary reports
                    # what each repo was sent instead.
                    name, mode, msg, sent = fut.result()
                    counts[mode] = counts.get(mode, 0) + 1
                    prompt_bytes += sent
                    print(f"  {name}: {mode} ({msg})")
                except Exception as e:
                    errors += 1
                    print(f"  {futures[fut]}: ERROR {e}", file=sys.stderr)
        modes = (
            ", ".join(f"{mode} {counts[mode]}" for mode in MODE_ORDER if counts.get(mode))
            or "nothing mapped"
        )
        sent = "" if args.dry_run else f" | {human_bytes(prompt_bytes)} of prompts"
        print(
            f"done: {len(items)} repos in {human_secs((now() - started).total_seconds())} | "
            f"{modes} | {errors} errors{sent}"
        )
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
        + (
            f"moved to {dest}"
            if args.apply
            else f"found; rerun with --apply to move them to {dest}"
        )
    )


def cmd_unresolved(cfg, args):
    path = cfg["atlas_dir"] / "graph.json"
    if not path.exists():
        sys.exit(f"no graph at {path}; run atlas.py generate")
    groups = {}
    for u in json.loads(path.read_text(encoding="utf-8")).get("unresolved", []):
        g = groups.setdefault(
            (u.get("kind", ""), u.get("key", "")), {"repos": set(), "candidates": set()}
        )
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
    orphans = [
        p.stem for p in sorted((cfg["atlas_dir"] / "repos").glob("*.json")) if p.stem not in repos
    ]
    for name in orphans:
        print(f"  {name}: orphan (no matching clone; run atlas.py prune)")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
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
        "--force-unlock",
        action="store_true",
        help="clear a stale lock file (not needed where flock is available)",
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
