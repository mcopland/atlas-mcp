#!/usr/bin/env python3
"""Atlas MCP server (stdio). Reads ATLAS_DIR/graph.json and repos/*.json; reloads when graph.json changes."""
import json
import os
import re
import subprocess
from collections import deque
from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer as Server  # mcp >= 2
except ImportError:
    from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x

ATLAS = Path(os.path.expanduser(os.environ.get("ATLAS_DIR", "~/atlas")))
server = Server("atlas")


class Store:
    def __init__(self):
        self.mtime, self.graph, self.manifests, self.blobs = None, {}, {}, None

    def load(self):
        path = ATLAS / "graph.json"
        if not path.exists():
            raise RuntimeError(f"no atlas at {path}; run atlas.py generate")
        mtime = path.stat().st_mtime
        if mtime != self.mtime:
            self.graph = json.loads(path.read_text())
            self.manifests, self.blobs, self.mtime = {}, None, mtime
        return self.graph

    def manifest(self, name):
        if name not in self.manifests:
            path = ATLAS / "repos" / f"{name}.json"
            self.manifests[name] = json.loads(path.read_text()) if path.exists() else {}
        return self.manifests[name]


store = Store()


def resolve(name):
    """Map a repo name, identifier, or unique substring to a repo name."""
    repos = store.load()["repos"]
    q = name.strip().lower()
    if name in repos:
        return name, None
    exact = [n for n in repos if n.lower() == q]
    if exact:
        return exact[0], None
    by_ident = [n for n, r in repos.items() if q in (i.lower() for i in r.get("identifiers", []))]
    if len(by_ident) == 1:
        return by_ident[0], None
    partial = [n for n in repos if q in n.lower()]
    if len(partial) == 1:
        return partial[0], None
    return None, {"error": f"no unique repo matches '{name}'", "candidates": sorted(set(by_ident + partial))[:15]}


def edge_view(e):
    return {k: e[k] for k in ("from", "to", "kind", "key", "name", "detail", "evidence", "match") if e.get(k)}


def out(obj):
    """Compact JSON keeps tool results small in the agent's context."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


@server.tool()
def list_repos(domain: str = "", kind: str = "") -> str:
    """List every repo in the org atlas with a one-line summary. Optional filters: domain, kind
    (service, library, frontend, infra, job, tool)."""
    g = store.load()
    rows = [{"name": n, "summary": r["summary"], "domain": r["domain"], "kind": r["kind"]}
            for n, r in sorted(g["repos"].items())
            if (not domain or r["domain"] == domain.lower()) and (not kind or r["kind"] == kind.lower())]
    domains = sorted({r["domain"] for r in g["repos"].values()})
    return out({"count": len(rows), "domains": domains, "repos": rows})


@server.tool()
def get_repo(name: str) -> str:
    """Full atlas entry for one repo: purpose, overview, what it exposes and consumes (with evidence
    file paths), datastores, components, resolved dependencies and dependents, source commit, local
    path, and markdown doc path. Accepts a repo name or any identifier (service name, hostname, package)."""
    repo, err = resolve(name)
    if err:
        return out(err)
    g = store.load()
    m = dict(store.manifest(repo))
    meta = m.pop("_meta", {})
    m.pop("packages", None)  # deterministic package lists are summarized by depends_on/used_by edges
    return out({
        **m,
        "commit": meta.get("commit"), "generated_at": meta.get("generated_at"),
        "repo_path": meta.get("repo_path"), "doc": g["repos"][repo]["doc"],
        "depends_on": [edge_view(e) for e in g["edges"] if e["from"] == repo],
        "used_by": [edge_view(e) for e in g["edges"] if e["to"] == repo],
        "unresolved_consumes": [u for u in g["unresolved"] if u["repo"] == repo],
        "shared_datastores": [s for s in g["shared_datastores"] if repo in s["repos"]],
    })


@server.tool()
def dependents(name: str, kind: str = "") -> str:
    """Repos that depend on this repo: call its APIs, consume its events, or import its packages.
    Call this before changing an interface to find affected consumers. Optional kind filter."""
    repo, err = resolve(name)
    if err:
        return out(err)
    edges = [edge_view(e) for e in store.load()["edges"] if e["to"] == repo and (not kind or e["kind"] == kind)]
    return out({"repo": repo, "count": len(edges), "dependents": edges})


@server.tool()
def dependencies(name: str, kind: str = "") -> str:
    """What this repo depends on, plus consumes that could not be matched to any known repo (unresolved,
    often external services or env vars). Optional kind filter."""
    repo, err = resolve(name)
    if err:
        return out(err)
    g = store.load()
    edges = [edge_view(e) for e in g["edges"] if e["from"] == repo and (not kind or e["kind"] == kind)]
    unresolved = [u for u in g["unresolved"] if u["repo"] == repo and (not kind or u["kind"] == kind)]
    return out({"repo": repo, "dependencies": edges, "unresolved": unresolved})


@server.tool()
def find_path(source: str, target: str, max_hops: int = 6) -> str:
    """How two repos are connected: the shortest chain of dependency edges from source to target.
    Falls back to ignoring edge direction if no directed path exists."""
    a, err = resolve(source)
    if err:
        return out(err)
    b, err = resolve(target)
    if err:
        return out(err)
    edges = store.load()["edges"]
    for directed in (True, False):
        adj = {}
        for e in edges:
            adj.setdefault(e["from"], []).append((e["to"], e))
            if not directed:
                adj.setdefault(e["to"], []).append((e["from"], e))
        prev, queue = {a: None}, deque([(a, 0)])
        while queue:
            node, depth = queue.popleft()
            if node == b:
                hops = []
                while prev[node]:
                    node, e = prev[node]
                    hops.append(edge_view(e))
                return out({"source": a, "target": b, "directed": directed, "hops": list(reversed(hops))})
            if depth < max_hops:
                for nxt, e in adj.get(node, []):
                    if nxt not in prev:
                        prev[nxt] = (node, e)
                        queue.append((nxt, depth + 1))
    return out({"source": a, "target": b, "hops": [], "message": f"no connection within {max_hops} hops"})


def _blobs():
    if store.blobs is None:
        store.blobs = {}
        for n in store.load()["repos"]:
            m = store.manifest(n)
            text = lambda items: " ".join(f"{i.get('name', '')} {i.get('key', '')}" for i in items).lower()
            store.blobs[n] = [
                (5, n.lower()),
                (4, " ".join(m.get("identifiers", [])).lower()),
                (3, m.get("summary", "").lower()),
                (3, text(m.get("exposes", []))),  # owners outrank callers
                (2, text(m.get("datastores", []))),
                (1, text(m.get("consumes", []))),
                (2, " ".join(p["name"] for p in m.get("packages", {}).get("publishes", [])).lower()),
                (1, (m.get("overview", "") + " " + " ".join(c.get("role", "") for c in m.get("components", []))).lower()),
            ]
    return store.blobs


@server.tool()
def search(query: str, limit: int = 10) -> str:
    """Keyword search across repo names, identifiers, summaries, overviews, endpoints, topics, packages,
    and datastores. Use when you don't know which repo owns something (an endpoint, topic, table, or concept)."""
    store.load()
    terms = [t for t in re.split(r"[^a-z0-9_./-]+", query.lower()) if len(t) >= 2]
    if not terms:
        return out({"results": []})
    results = []
    for n, fields in _blobs().items():
        score = sum(w * text.count(t) for w, text in fields for t in terms)
        if score:
            m = store.manifest(n)
            matches = [{"field": f, "kind": i.get("kind"), "name": i.get("name"), "key": i.get("key")}
                       for f in ("exposes", "consumes", "datastores") for i in m.get(f, [])
                       if any(t in f"{i.get('name', '')} {i.get('key', '')}".lower() for t in terms)][:5]
            results.append({"repo": n, "score": score, "summary": m.get("summary", ""), "matches": matches})
    results.sort(key=lambda r: -r["score"])
    return out({"results": results[:limit]})


@server.tool()
def freshness(name: str = "") -> str:
    """Compare the commit each atlas entry was generated from with the repo's current local HEAD.
    Omit name to check every repo, which reports the total checked but lists only the entries that
    are not fresh. Stale entries may not reflect recent changes; verify in source."""
    g = store.load()
    if name:
        repo, err = resolve(name)
        if err:
            return out(err)
        names = [repo]
    else:
        names = sorted(g["repos"])
    rows = []
    for n in names:
        r = g["repos"][n]
        row = {"repo": n, "atlas_commit": r["commit"][:12], "generated_at": r["generated_at"]}
        try:
            head = subprocess.run(["git", "-C", r["repo_path"], "rev-parse", "HEAD"],
                                  capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=5).stdout.strip()
            if not head:
                row["status"] = "missing"
            elif head == r["commit"]:
                row["status"] = "fresh"
            else:
                behind = subprocess.run(
                    ["git", "-C", r["repo_path"], "rev-list", "--count", f"{r['commit']}..HEAD"],
                    capture_output=True, text=True, stdin=subprocess.DEVNULL,
                    timeout=5).stdout.strip()
                row.update(status="stale", head=head[:12], commits_behind=int(behind) if behind.isdigit() else None)
        except Exception as e:
            row["status"] = f"error: {e}"
        rows.append(row)
    stale = sum(1 for r in rows if r["status"] != "fresh")
    return out({"checked": len(rows), "not_fresh": stale, "repos": rows if name else [r for r in rows if r["status"] != "fresh"], "graph_generated_at": g["generated_at"]})


@server.tool()
def get_doc(name: str) -> str:
    """The rendered markdown doc for one repo: summary, overview, exposes, consumes with their
    resolved targets, dependents, datastores, and a component diagram. Use after get_repo when
    you want the prose and the diagram rather than structured fields."""
    repo, err = resolve(name)
    if err:
        return out(err)
    path = Path(store.load()["repos"][repo]["doc"])
    if not path.exists():
        return out({"error": f"no doc at {path}; run atlas.py build"})
    return path.read_text(encoding="utf-8")


if __name__ == "__main__":
    server.run()
