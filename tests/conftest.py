import json
import subprocess

import pytest

import atlas


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


@pytest.fixture
def make_repo(tmp_path):
    def build(name, files=None, root="src"):
        repo = tmp_path / root / name
        repo.mkdir(parents=True)
        for rel, text in (files or {"README.md": name}).items():
            f = repo / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "t")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")
        return repo

    return build


@pytest.fixture
def make_cfg(tmp_path):
    def build(**overrides):
        data = {
            "atlas_dir": str(tmp_path / "atlas"),
            "repo_roots": [str(tmp_path / "src")],
        }
        data.update(overrides)
        path = tmp_path / "config.json"
        path.write_text(json.dumps(data))
        return atlas.load_config(str(path))

    return build


@pytest.fixture
def make_manifest(tmp_path):
    def build(cfg, name, **fields):
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
                "repo_path": str(tmp_path / "src" / name),
                "commit": "0" * 40,
                "generated_at": "2026-01-01T00:00:00+00:00",
                "mode": "full",
                "model": "m",
                "last_full_at": "2026-01-01T00:00:00+00:00",
                "dropped_without_evidence": [],
            },
        }
        m.update(fields)
        atlas.write_json(cfg["atlas_dir"] / "repos" / f"{name}.json", m)
        return m

    return build


@pytest.fixture
def graph_of(make_cfg):
    def build(cfg, known=None):
        names = known if known is not None else {p.stem for p in (cfg["atlas_dir"] / "repos").glob("*.json")}
        atlas.build(cfg, set(names))
        return json.loads((cfg["atlas_dir"] / "graph.json").read_text())

    return build
