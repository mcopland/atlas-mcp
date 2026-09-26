import os

import pytest

import atlas


def test_evidence_ok_accepts_real_repo_relative_path(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.go").write_text("x")
    assert atlas.evidence_ok(tmp_path, "src/main.go:42")
    assert atlas.evidence_ok(tmp_path, "./src/main.go")


def test_evidence_ok_rejects_missing_and_escaping_paths(tmp_path):
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("x")
    assert not atlas.evidence_ok(tmp_path, "src/nope.go")
    assert not atlas.evidence_ok(tmp_path, "../secret.txt")
    assert not atlas.evidence_ok(tmp_path, "")
    assert not atlas.evidence_ok(tmp_path, None)


def test_evidence_ok_rejects_a_directory(tmp_path):
    """A directory always exists once the repo does, so citing one would let any claim
    through the gate; only a real file counts as evidence."""
    (tmp_path / "src").mkdir()
    assert not atlas.evidence_ok(tmp_path, "src")
    assert not atlas.evidence_ok(tmp_path, ".")
    assert not atlas.evidence_ok(tmp_path, "./")


@pytest.mark.skipif(os.name != "posix", reason="symlink creation needs privileges off POSIX")
def test_evidence_ok_rejects_symlink_escaping_the_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("x")
    (repo / "link.txt").symlink_to(target)
    assert not atlas.evidence_ok(repo, "link.txt")


def test_clean_manifest_drops_items_without_valid_evidence(tmp_path, make_cfg):
    (tmp_path / "ok.go").write_text("x")
    m = {
        "exposes": [
            {"kind": "http", "key": "a", "evidence": "ok.go"},
            {"kind": "http", "key": "b", "evidence": "ghost.go"},
        ],
        "consumes": ["not-a-dict"],
        "datastores": [],
    }
    dropped, rejected = atlas.clean_manifest(m, tmp_path, make_cfg())
    assert [e["key"] for e in m["exposes"]] == ["a"]
    assert m["consumes"] == []
    assert len(dropped) == 2
    assert rejected == ""


def test_clean_manifest_coerces_off_list_domain(tmp_path, make_cfg):
    cfg = make_cfg(domains=["payments", "identity"])
    m = {"domain": "  Payments  "}
    _, rejected = atlas.clean_manifest(m, tmp_path, cfg)
    assert (m["domain"], rejected) == ("payments", "")

    m = {"domain": "invented-domain"}
    _, rejected = atlas.clean_manifest(m, tmp_path, cfg)
    assert (m["domain"], rejected) == ("unassigned", "invented-domain")


def test_clean_manifest_allows_any_domain_when_list_is_empty(tmp_path, make_cfg):
    m = {"domain": "whatever"}
    _, rejected = atlas.clean_manifest(m, tmp_path, make_cfg())
    assert (m["domain"], rejected) == ("whatever", "")


def test_clean_manifest_normalises_missing_fields(tmp_path, make_cfg):
    m = {}
    atlas.clean_manifest(m, tmp_path, make_cfg())
    assert m["languages"] == [] and m["notes"] == []
    assert m["summary"] == "" and m["domain"] == "unassigned"


def test_clean_manifest_lowercases_repo_and_item_kinds(tmp_path, make_cfg):
    (tmp_path / "ok.go").write_text("x")
    m = {"kind": "Service", "exposes": [{"kind": "HTTP", "key": "a", "evidence": "ok.go"}]}
    atlas.clean_manifest(m, tmp_path, make_cfg())
    assert m["kind"] == "service"
    assert m["exposes"][0]["kind"] == "http"


def test_clean_manifest_drops_structured_entries_from_scalar_lists(tmp_path, make_cfg):
    m = {
        "identifiers": ["orders", {"name": "orders"}, 7],
        "components": [{"name": "api"}, "not-a-component"],
    }
    atlas.clean_manifest(m, tmp_path, make_cfg())
    assert m["identifiers"] == ["orders", "7"]
    assert m["components"] == [{"name": "api"}]


def test_clean_manifest_keeps_the_deterministic_source_marker(tmp_path, make_cfg):
    (tmp_path / "ok.go").write_text("x")
    m = {"exposes": [{"kind": "http", "key": "a", "evidence": "ok.go", "source": "deterministic"}]}
    atlas.clean_manifest(m, tmp_path, make_cfg())
    assert m["exposes"][0]["source"] == "deterministic"


def test_clean_manifest_gates_deterministic_items_on_evidence_too(tmp_path, make_cfg):
    """A parser that emits a path the repo does not have is a bug in the parser, and it has to
    surface in dropped_without_evidence rather than reach the graph unchallenged."""
    m = {
        "datastores": [
            {"kind": "s3", "name": "b", "evidence": "ghost.tf:2", "source": "deterministic"}
        ]
    }
    dropped, _ = atlas.clean_manifest(m, tmp_path, make_cfg())
    assert m["datastores"] == []
    assert [d["field"] for d in dropped] == ["datastores"]


def test_clean_manifest_accepts_a_mixed_case_domain_list(tmp_path, make_cfg):
    cfg = make_cfg(domains=["Payments", " Identity "])
    m = {"domain": "Payments"}
    _, rejected = atlas.clean_manifest(m, tmp_path, cfg)
    assert (m["domain"], rejected) == ("payments", "")
