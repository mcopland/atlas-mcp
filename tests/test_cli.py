import argparse
import json
import os

import pytest

import atlas


def test_load_config_reports_a_missing_file(tmp_path):
    with pytest.raises(SystemExit) as e:
        atlas.load_config(str(tmp_path / "nope.json"))
    assert "config.example.json" in str(e.value)


def test_load_config_reports_invalid_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{nope")
    with pytest.raises(SystemExit) as e:
        atlas.load_config(str(path))
    assert "invalid JSON" in str(e.value)


def test_load_config_applies_defaults(make_cfg):
    cfg = make_cfg()
    assert cfg["parallel"] == 3
    assert cfg["max_ambiguous_hits"] == 3
    assert cfg["repo_names"] == {}
    assert "api" in cfg["generic_identifiers"]


def test_config_extends_the_generic_identifier_stoplist(make_cfg):
    cfg = make_cfg(generic_identifiers=["Orders"])
    assert "orders" in cfg["generic_identifiers"]
    assert "api" in cfg["generic_identifiers"]


def test_lock_blocks_a_second_run(tmp_path):
    ad = tmp_path / "atlas"
    with atlas.atlas_lock(ad):
        with pytest.raises(SystemExit) as e:
            with atlas.atlas_lock(ad):
                pass
    assert "another atlas run" in str(e.value)


def test_lock_is_released_after_a_clean_run(tmp_path):
    ad = tmp_path / "atlas"
    with atlas.atlas_lock(ad):
        pass
    with atlas.atlas_lock(ad):
        pass


def test_lock_is_released_when_the_run_raises(tmp_path):
    ad = tmp_path / "atlas"
    with pytest.raises(RuntimeError):
        with atlas.atlas_lock(ad):
            raise RuntimeError("boom")
    with atlas.atlas_lock(ad):
        pass


def test_lock_records_the_holding_pid(tmp_path):
    ad = tmp_path / "atlas"
    with atlas.atlas_lock(ad):
        assert str(os.getpid()) in (ad / ".generate.lock").read_text()


@pytest.mark.skipif(atlas.fcntl is not None, reason="flock releases on process death")
def test_force_unlock_clears_a_stale_lock_file(tmp_path):
    ad = tmp_path / "atlas"
    ad.mkdir(parents=True)
    (ad / ".generate.lock").write_text("pid 999999 started earlier\n")
    with pytest.raises(SystemExit):
        with atlas.atlas_lock(ad):
            pass
    with atlas.atlas_lock(ad, force=True):
        pass


def test_known_repo_names_refuses_when_discovery_is_empty(make_cfg):
    with pytest.raises(SystemExit) as e:
        atlas.known_repo_names(make_cfg(repo_roots=[]))
    assert "no repos found" in str(e.value)


def test_build_command_leaves_the_graph_intact_when_discovery_is_empty(
        make_cfg, make_manifest):
    cfg = make_cfg(repo_roots=[])
    make_manifest(cfg, "a")
    atlas.write_json(cfg["atlas_dir"] / "graph.json", {"repos": {"a": {}}})
    with pytest.raises(SystemExit):
        atlas.cmd_build(cfg, argparse.Namespace())
    kept = json.loads((cfg["atlas_dir"] / "graph.json").read_text())
    assert kept["repos"] == {"a": {}}


def test_prune_reports_orphans_without_moving_them(
        make_repo, make_cfg, make_manifest, capsys):
    make_repo("live")
    cfg = make_cfg()
    make_manifest(cfg, "live")
    make_manifest(cfg, "gone")
    atlas.cmd_prune(cfg, argparse.Namespace(apply=False))
    assert (cfg["atlas_dir"] / "repos" / "gone.json").exists()
    assert "gone" in capsys.readouterr().out


def test_prune_apply_moves_orphans_aside(make_repo, make_cfg, make_manifest):
    make_repo("live")
    cfg = make_cfg()
    make_manifest(cfg, "live")
    make_manifest(cfg, "gone")
    atlas.cmd_prune(cfg, argparse.Namespace(apply=True))
    repos = cfg["atlas_dir"] / "repos"
    assert not (repos / "gone.json").exists()
    assert (repos / "_orphans" / "gone.json").exists()
    assert (repos / "live.json").exists()


def test_prune_refuses_when_discovery_is_empty(make_cfg, make_manifest):
    cfg = make_cfg(repo_roots=[])
    make_manifest(cfg, "a")
    with pytest.raises(SystemExit):
        atlas.cmd_prune(cfg, argparse.Namespace(apply=True))
    assert (cfg["atlas_dir"] / "repos" / "a.json").exists()


def test_unresolved_groups_targets_by_frequency(make_cfg, capsys):
    cfg = make_cfg()
    atlas.write_json(cfg["atlas_dir"] / "graph.json", {"unresolved": [
        {"repo": "a", "kind": "http", "key": "stripe.com"},
        {"repo": "b", "kind": "http", "key": "stripe.com"},
        {"repo": "c", "kind": "topic", "key": "legacy.events", "candidates": ["x", "y"]}]})
    atlas.cmd_unresolved(cfg, argparse.Namespace(top=10))
    printed = capsys.readouterr().out
    assert "stripe.com" in printed
    assert "2x" in printed.replace(" ", "")
    assert "candidates=x,y" in printed


def test_unresolved_requires_a_graph(make_cfg):
    with pytest.raises(SystemExit) as e:
        atlas.cmd_unresolved(make_cfg(), argparse.Namespace(top=10))
    assert "no graph" in str(e.value)


def test_repo_map_warns_when_a_path_changes_name(make_cfg, capsys, tmp_path):
    cfg = make_cfg()
    atlas.save_repo_map(cfg, {"svc": tmp_path / "src" / "svc"})
    capsys.readouterr()
    atlas.save_repo_map(cfg, {"team-a-svc": tmp_path / "src" / "svc"})
    assert "renamed" in capsys.readouterr().err


def _gen_args(**overrides):
    args = {"only": None, "limit": None, "full": False, "pull": False, "dry_run": False,
            "no_build": False, "force_unlock": False}
    args.update(overrides)
    return argparse.Namespace(**args)


def _stub_manifest(*_a, **_k):
    return {"summary": "s", "overview": "o", "domain": "unassigned", "kind": "service",
            "identifiers": [], "exposes": [], "consumes": [], "datastores": []}


def test_generate_writes_the_map_the_graph_and_the_docs(make_repo, make_cfg, monkeypatch):
    make_repo("svc-a")
    make_repo("svc-b")
    cfg = make_cfg()
    monkeypatch.setattr(atlas, "run_kiro", _stub_manifest)
    with pytest.raises(SystemExit) as e:
        atlas.cmd_generate(cfg, _gen_args())
    assert e.value.code == 0
    ad = cfg["atlas_dir"]
    assert set(json.loads((ad / "repos.json").read_text())) == {"svc-a", "svc-b"}
    assert set(json.loads((ad / "graph.json").read_text())["repos"]) == {"svc-a", "svc-b"}
    assert (ad / "docs" / "svc-a.md").exists()
    assert (ad / "index.md").exists()


def test_generate_reports_a_failed_repo_and_keeps_the_others(make_repo, make_cfg,
                                                             monkeypatch, capsys):
    make_repo("good")
    make_repo("bad")
    cfg = make_cfg()

    def flaky(cfg_, repo, prompt, log):
        if repo.name == "bad":
            raise RuntimeError("kiro exploded")
        return _stub_manifest()

    monkeypatch.setattr(atlas, "run_kiro", flaky)
    with pytest.raises(SystemExit) as e:
        atlas.cmd_generate(cfg, _gen_args())
    assert e.value.code == 1
    repos = cfg["atlas_dir"] / "repos"
    assert (repos / "good.json").exists()
    assert not (repos / "bad.json").exists()
    assert "ERROR" in capsys.readouterr().err


def test_generate_only_selects_a_subset(make_repo, make_cfg, monkeypatch):
    make_repo("svc-a")
    make_repo("svc-b")
    cfg = make_cfg()
    monkeypatch.setattr(atlas, "run_kiro", _stub_manifest)
    with pytest.raises(SystemExit):
        atlas.cmd_generate(cfg, _gen_args(only=["svc-a"]))
    repos = cfg["atlas_dir"] / "repos"
    assert (repos / "svc-a.json").exists()
    assert not (repos / "svc-b.json").exists()


def test_load_config_normalises_the_domain_list(make_cfg):
    cfg = make_cfg(domains=["Payments", " Identity ", ""])
    assert cfg["domains"] == ["payments", "identity"]


@pytest.mark.skipif(atlas.fcntl is None, reason="the O_EXCL fallback removes its lock file")
def test_flock_keeps_the_lock_file_after_release(tmp_path):
    ad = tmp_path / "atlas"
    with atlas.atlas_lock(ad):
        pass
    assert (ad / ".generate.lock").exists()
    with atlas.atlas_lock(ad):
        pass


def test_prune_apply_keeps_an_earlier_orphan_snapshot(make_repo, make_cfg, make_manifest):
    make_repo("live")
    cfg = make_cfg()
    make_manifest(cfg, "live")
    make_manifest(cfg, "gone")
    atlas.cmd_prune(cfg, argparse.Namespace(apply=True))
    make_manifest(cfg, "gone")
    atlas.cmd_prune(cfg, argparse.Namespace(apply=True))
    kept = sorted(p.name for p in (cfg["atlas_dir"] / "repos" / "_orphans").glob("gone*.json"))
    assert len(kept) == 2


def test_status_refuses_when_discovery_is_empty(make_cfg, make_manifest):
    cfg = make_cfg(repo_roots=[])
    make_manifest(cfg, "a")
    with pytest.raises(SystemExit) as e:
        atlas.cmd_status(cfg, argparse.Namespace())
    assert "no repos found" in str(e.value)


def test_status_reports_fresh_entries_and_orphans(make_repo, make_cfg, make_manifest, capsys):
    repo = make_repo("live")
    cfg = make_cfg()
    m = make_manifest(cfg, "live")
    m["_meta"]["commit"] = atlas.git(repo, "rev-parse", "HEAD")
    atlas.write_json(cfg["atlas_dir"] / "repos" / "live.json", m)
    make_manifest(cfg, "gone")
    atlas.cmd_status(cfg, argparse.Namespace())
    out = capsys.readouterr().out
    assert "live: fresh" in out
    assert "gone: orphan" in out
