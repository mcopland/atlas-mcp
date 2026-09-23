import os

import pytest

import atlas
from tests.conftest import _git

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="symlink creation needs privileges off POSIX"
)

SENTINEL = "SENTINEL-outside-the-repo"


def link_and_commit(repo, rel, target):
    link = repo / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "link")


@pytest.fixture
def outside(tmp_path):
    def write(name, text=SENTINEL):
        path = tmp_path / "home" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    return write


def test_a_tracked_symlink_out_of_the_repo_is_not_excerpted(make_repo, outside):
    repo = make_repo("svc", files={"main.go": "package main"})
    link_and_commit(repo, "README.md", outside("credentials"))
    bundle = atlas.gather_bundle(repo, 100_000)
    assert SENTINEL not in bundle
    assert "README.md" not in bundle


def test_a_tracked_symlink_out_of_the_repo_is_not_scanned_for_signals(make_repo, outside):
    repo = make_repo("svc", files={"main.go": "package main"})
    link_and_commit(
        repo, "config/app.yaml", outside("app.yaml", f"url: https://{SENTINEL.lower()}.internal")
    )
    assert SENTINEL.lower() not in atlas.gather_bundle(repo, 100_000).lower()


def test_the_walk_fallback_skips_a_symlink_out_of_the_repo(tmp_path, outside):
    repo = tmp_path / "plain"
    repo.mkdir()
    (repo / "main.go").write_text("package main")
    (repo / "README.md").symlink_to(outside("credentials"))
    bundle = atlas.gather_bundle(repo, 100_000)
    assert SENTINEL not in bundle
    assert "README.md" not in bundle


def test_a_symlink_inside_the_repo_is_still_read(make_repo):
    repo = make_repo("svc", files={"docs/README.md": "the inner readme marker"})
    link_and_commit(repo, "README.md", repo / "docs" / "README.md")
    assert "the inner readme marker" in atlas.gather_bundle(repo, 100_000)


def test_deterministic_facts_ignore_a_symlink_out_of_the_repo(make_repo, outside):
    repo = make_repo("svc", files={"main.go": "package main"})
    catalog = "metadata:\n  name: stolen-name\nspec:\n  owner: team-stolen\n"
    link_and_commit(repo, "catalog-info.yaml", outside("catalog-info.yaml", catalog))
    link_and_commit(repo, "CODEOWNERS", outside("CODEOWNERS", "* @stolen\n"))
    facts = atlas.extract_facts(repo)
    assert "stolen-name" not in facts["identifiers"]
    assert facts["owners"] == []


def test_packages_ignore_a_symlink_out_of_the_repo(make_repo, outside):
    repo = make_repo("svc", files={"main.go": "package main"})
    link_and_commit(repo, "package.json", outside("package.json", '{"name": "stolen-pkg"}'))
    assert atlas.extract_packages(repo)["publishes"] == []


def test_gradle_settings_symlinked_out_of_the_repo_are_not_read(make_repo, outside):
    repo = make_repo("svc", files={"build.gradle": "group = 'com.acme'\n"})
    link_and_commit(
        repo, "settings.gradle", outside("settings.gradle", "rootProject.name = 'stolen'\n")
    )
    assert atlas.extract_packages(repo)["publishes"] == []


def test_evidence_ok_accepts_a_repo_reached_through_a_symlinked_parent(tmp_path):
    real = tmp_path / "real"
    (real / "svc").mkdir(parents=True)
    (real / "svc" / "main.go").write_text("x")
    (tmp_path / "via").symlink_to(real)
    assert atlas.evidence_ok(tmp_path / "via" / "svc", "main.go:1")
