import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="install the mcp package to run the MCP server tests")

import anyio  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

KIT = Path(__file__).resolve().parent.parent
TOOLS = ["dependencies", "dependents", "find_path", "freshness", "get_doc", "get_repo",
         "list_repos", "search"]


def _text(result):
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            return text
    raise AssertionError(f"no text content in {result!r}")


@pytest.fixture
def served_atlas(tmp_path):
    (tmp_path / "repos").mkdir()
    (tmp_path / "docs").mkdir()
    meta = {"commit": "a" * 40, "generated_at": "2026-01-01T00:00:00+00:00",
            "repo_path": str(tmp_path / "orders"), "mode": "full"}
    graph = {"generated_at": "2026-01-01T00:00:00+00:00",
             "repos": {"orders": {"summary": "orders summary", "domain": "payments",
                                  "kind": "service", "identifiers": ["orders.internal"],
                                  "commit": meta["commit"],
                                  "generated_at": meta["generated_at"],
                                  "repo_path": meta["repo_path"],
                                  "doc": str(tmp_path / "docs" / "orders.md")}},
             "edges": [], "unresolved": [], "shared_datastores": []}
    manifest = {"name": "orders", "summary": "orders summary",
                "identifiers": ["orders.internal"], "exposes": [], "consumes": [],
                "datastores": [], "packages": {"publishes": [], "depends_on": []},
                "_meta": meta}
    (tmp_path / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    (tmp_path / "repos" / "orders.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "docs" / "orders.md").write_text("# orders\n", encoding="utf-8")
    return tmp_path


def test_server_serves_its_tools_over_stdio(served_atlas):
    async def talk():
        params = StdioServerParameters(command=sys.executable,
                                       args=[str(KIT / "atlas_mcp.py")],
                                       env={"ATLAS_DIR": str(served_atlas)})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert sorted(t.name for t in listed.tools) == TOOLS
                assert all(t.description for t in listed.tools)
                repos = json.loads(_text(await session.call_tool("list_repos", {})))
                assert [r["name"] for r in repos["repos"]] == ["orders"]
                doc = _text(await session.call_tool("get_doc", {"name": "orders.internal"}))
                assert doc.startswith("# orders")

    async def main():
        with anyio.fail_after(90):
            await talk()

    anyio.run(main)
