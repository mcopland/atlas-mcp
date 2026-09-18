import json
import os
import subprocess

import pytest

pytest.importorskip("mcp", reason="install the mcp package to run the MCP server tests")

import atlas_mcp


def manifest(name, **fields):
    m = {
        "name": name,
        "summary": f"{name} summary",
        "overview": "",
        "domain": "unassigned",
        "kind": "service",
        "languages": [],
        "owners": [],
        "identifiers": [],
        "exposes": [],
        "consumes": [],
        "datastores": [],
        "components": [],
        "component_edges": [],
        "entrypoints": [],
        "notes": [],
        "packages": {"publishes": [], "depends_on": []},
        "_meta": {
            "commit": "a" * 40,
            "generated_at": "2026-01-01T00:00:00+00:00",
            "repo_path": "/src/" + name,
            "mode": "full",
        },
    }
    m.update(fields)
    return m


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    ).stdout.strip()


def _commit(path, text):
    (path / "f.txt").write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", text)
    return _git(path, "rev-parse", "HEAD")


def _git_repo(path, commits=1):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    return [_commit(path, f"c{i}") for i in range(commits)]


@pytest.fixture
def atlas_env(tmp_path, monkeypatch):
    monkeypatch.setattr(atlas_mcp, "ATLAS", tmp_path)
    monkeypatch.setattr(atlas_mcp, "store", atlas_mcp.Store())

    def write(graph, manifests=None, docs=None):
        (tmp_path / "repos").mkdir(parents=True, exist_ok=True)
        (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
        for name, m in (manifests or {}).items():
            (tmp_path / "repos" / f"{name}.json").write_text(json.dumps(m))
        for name, text in (docs or {}).items():
            (tmp_path / "docs" / f"{name}.md").write_text(text)
        (tmp_path / "graph.json").write_text(json.dumps(graph))
        return tmp_path

    return write


def repo_row(tmp_path, name, domain="payments", kind="service", identifiers=()):
    return {
        "summary": f"{name} summary",
        "domain": domain,
        "kind": kind,
        "identifiers": list(identifiers),
        "commit": "a" * 40,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "repo_path": str(tmp_path / name),
        "doc": str(tmp_path / "docs" / f"{name}.md"),
    }


@pytest.fixture
def loaded(atlas_env, tmp_path):
    graph = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "repos": {
            "orders": repo_row(tmp_path, "orders", identifiers=["orders.internal"]),
            "web": repo_row(tmp_path, "web", kind="frontend"),
            "billing": repo_row(tmp_path, "billing", domain="identity"),
        },
        "edges": [
            {
                "from": "web",
                "to": "orders",
                "kind": "http",
                "key": "orders.internal",
                "name": "orders api",
                "detail": "",
                "evidence": "a.go",
                "match": "exact",
            },
            {
                "from": "orders",
                "to": "billing",
                "kind": "topic",
                "key": "invoice.raised",
                "name": "invoice",
                "detail": "",
                "evidence": "b.go",
                "match": "alias",
            },
        ],
        "unresolved": [
            {
                "repo": "web",
                "kind": "http",
                "key": "stripe.com",
                "name": "stripe",
                "evidence": "c.go",
            }
        ],
        "shared_datastores": [{"name": "orders_db", "repos": {"orders": "owner", "web": "read"}}],
    }
    manifests = {
        "orders": manifest(
            "orders",
            identifiers=["orders.internal"],
            exposes=[
                {
                    "kind": "topic",
                    "name": "OrderCreated",
                    "key": "order.created",
                    "evidence": "a.go",
                }
            ],
        ),
        "web": manifest(
            "web",
            consumes=[
                {
                    "kind": "topic",
                    "name": "OrderCreated",
                    "key": "order.created",
                    "evidence": "b.go",
                }
            ],
        ),
        "billing": manifest("billing"),
    }
    atlas_env(graph, manifests, {"orders": "# orders\n\nthe orders doc\n"})
    return graph


def test_list_repos_returns_every_repo_with_domains(loaded):
    got = json.loads(atlas_mcp.list_repos())
    assert got["count"] == 3
    assert got["domains"] == ["identity", "payments"]
    assert [r["name"] for r in got["repos"]] == ["billing", "orders", "web"]


def test_list_repos_filters_by_domain_and_kind(loaded):
    assert json.loads(atlas_mcp.list_repos(domain="identity"))["count"] == 1
    assert json.loads(atlas_mcp.list_repos(kind="frontend"))["count"] == 1
    assert json.loads(atlas_mcp.list_repos(domain="payments", kind="service"))["count"] == 1


def test_resolve_accepts_name_identifier_and_unique_substring(loaded):
    assert atlas_mcp.resolve("orders")[0] == "orders"
    assert atlas_mcp.resolve("orders.internal")[0] == "orders"
    assert atlas_mcp.resolve("bill")[0] == "billing"


def test_resolve_reports_candidates_when_ambiguous(loaded):
    name, err = atlas_mcp.resolve("nonexistent-thing")
    assert name is None
    assert "no unique repo matches" in err["error"]


def test_get_repo_joins_edges_and_unresolved(loaded):
    got = json.loads(atlas_mcp.get_repo("web"))
    assert [e["to"] for e in got["depends_on"]] == ["orders"]
    assert got["used_by"] == []
    assert [u["key"] for u in got["unresolved_consumes"]] == ["stripe.com"]
    assert got["shared_datastores"][0]["name"] == "orders_db"
    assert "packages" not in got
    assert "_meta" not in got


def test_get_repo_resolves_by_identifier(loaded):
    assert json.loads(atlas_mcp.get_repo("orders.internal"))["name"] == "orders"


def test_dependents_and_dependencies_filter_by_kind(loaded):
    assert json.loads(atlas_mcp.dependents("orders"))["count"] == 1
    assert json.loads(atlas_mcp.dependents("orders", kind="topic"))["count"] == 0
    deps = json.loads(atlas_mcp.dependencies("orders"))
    assert [e["to"] for e in deps["dependencies"]] == ["billing"]
    assert json.loads(atlas_mcp.dependencies("web"))["unresolved"][0]["key"] == "stripe.com"


def test_find_path_walks_directed_edges(loaded):
    got = json.loads(atlas_mcp.find_path("web", "billing"))
    assert got["directed"] is True
    assert [h["to"] for h in got["hops"]] == ["orders", "billing"]


def test_find_path_falls_back_to_undirected(loaded):
    got = json.loads(atlas_mcp.find_path("billing", "web"))
    assert got["directed"] is False
    assert len(got["hops"]) == 2


def test_find_path_reports_no_connection(loaded, atlas_env, tmp_path):
    atlas_env(
        {
            "generated_at": "x",
            "repos": {"a": repo_row(tmp_path, "a"), "b": repo_row(tmp_path, "b")},
            "edges": [],
            "unresolved": [],
            "shared_datastores": [],
        },
        {"a": manifest("a"), "b": manifest("b")},
    )
    got = json.loads(atlas_mcp.find_path("a", "b"))
    assert got["hops"] == []
    assert "no connection" in got["message"]


def test_search_ranks_the_exposer_above_the_consumer(loaded):
    results = json.loads(atlas_mcp.search("order.created"))["results"]
    assert [r["repo"] for r in results][:2] == ["orders", "web"]
    assert results[0]["matches"][0]["key"] == "order.created"


def test_search_ignores_short_terms(loaded):
    assert json.loads(atlas_mcp.search("a"))["results"] == []


def test_get_doc_returns_markdown(loaded):
    assert "the orders doc" in atlas_mcp.get_doc("orders")


def test_get_doc_reports_a_missing_doc(loaded):
    got = json.loads(atlas_mcp.get_doc("web"))
    assert "no doc" in got["error"]


def test_freshness_flags_a_repo_path_that_is_gone(loaded):
    status = json.loads(atlas_mcp.freshness("orders"))["repos"][0]["status"]
    assert status == "missing" or status.startswith("error")


def test_freshness_without_a_name_lists_only_entries_that_are_not_fresh(loaded):
    got = json.loads(atlas_mcp.freshness())
    assert got["checked"] == 3
    assert got["not_fresh"] == 3
    assert all(r["status"] != "fresh" for r in got["repos"])


def test_freshness_reports_fresh_then_stale_after_a_commit(atlas_env, tmp_path):
    repo = tmp_path / "orders"
    head = _git_repo(repo)[0]
    row = repo_row(tmp_path, "orders")
    row["repo_path"] = str(repo)
    row["commit"] = head
    atlas_env(
        {
            "generated_at": "x",
            "repos": {"orders": row},
            "edges": [],
            "unresolved": [],
            "shared_datastores": [],
        },
        {"orders": manifest("orders")},
    )

    assert json.loads(atlas_mcp.freshness("orders"))["repos"][0]["status"] == "fresh"

    _commit(repo, "second")
    got = json.loads(atlas_mcp.freshness("orders"))["repos"][0]
    assert got["status"] == "stale"
    assert got["commits_behind"] == 1


def test_freshness_over_every_repo_skips_the_commits_behind_call(atlas_env, tmp_path, monkeypatch):
    repo = tmp_path / "orders"
    heads = _git_repo(repo, commits=2)
    row = repo_row(tmp_path, "orders")
    row["repo_path"] = str(repo)
    row["commit"] = heads[0]
    atlas_env(
        {
            "generated_at": "x",
            "repos": {"orders": row},
            "edges": [],
            "unresolved": [],
            "shared_datastores": [],
        },
        {"orders": manifest("orders")},
    )
    calls = []
    real = atlas_mcp.subprocess.run

    def spy(cmd, **kwargs):
        calls.append(list(cmd))
        return real(cmd, **kwargs)

    monkeypatch.setattr(atlas_mcp.subprocess, "run", spy)
    got = json.loads(atlas_mcp.freshness())
    assert got["not_fresh"] == 1
    assert got["repos"][0]["status"] == "stale"
    assert not any("rev-list" in c for c in calls)


def test_resolve_refuses_to_guess_when_an_identifier_is_claimed_twice(atlas_env, tmp_path):
    repos = {
        "svc-a": repo_row(tmp_path, "svc-a", identifiers=["orders"]),
        "svc-b": repo_row(tmp_path, "svc-b", identifiers=["orders"]),
        "orders-legacy": repo_row(tmp_path, "orders-legacy"),
    }
    atlas_env(
        {
            "generated_at": "x",
            "repos": repos,
            "edges": [],
            "unresolved": [],
            "shared_datastores": [],
        },
        {n: manifest(n) for n in repos},
    )
    name, err = atlas_mcp.resolve("orders")
    assert name is None
    assert err["candidates"] == ["svc-a", "svc-b"]


def test_store_reloads_when_the_graph_changes(loaded, atlas_env, tmp_path):
    assert json.loads(atlas_mcp.list_repos())["count"] == 3
    atlas_env(
        {
            "generated_at": "y",
            "repos": {"solo": repo_row(tmp_path, "solo")},
            "edges": [],
            "unresolved": [],
            "shared_datastores": [],
        },
        {"solo": manifest("solo")},
    )
    stamp = os.path.getmtime(tmp_path / "graph.json") + 10
    os.utime(tmp_path / "graph.json", (stamp, stamp))
    assert json.loads(atlas_mcp.list_repos())["count"] == 1


def test_missing_graph_is_reported(atlas_env, tmp_path, monkeypatch):
    monkeypatch.setattr(atlas_mcp, "ATLAS", tmp_path / "empty")
    monkeypatch.setattr(atlas_mcp, "store", atlas_mcp.Store())
    with pytest.raises(RuntimeError, match="run atlas.py generate"):
        atlas_mcp.list_repos()
