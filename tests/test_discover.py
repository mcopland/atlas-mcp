import atlas


def test_discover_finds_repos_one_and_two_levels_deep(make_repo, make_cfg):
    make_repo("flat")
    make_repo("nested/deep")
    found = atlas.discover_repos(make_cfg())
    assert set(found) == {"flat", "deep"}


def test_colliding_basenames_disambiguate_every_member(make_repo, make_cfg):
    make_repo("team-a/svc")
    make_repo("team-b/svc")
    found = atlas.discover_repos(make_cfg())
    assert set(found) == {"team-a-svc", "team-b-svc"}


def test_adding_a_colliding_clone_does_not_rename_the_survivor(make_repo, make_cfg):
    make_repo("team-a/svc")
    before = atlas.discover_repos(make_cfg())
    assert set(before) == {"svc"}
    make_repo("team-b/svc")
    after = atlas.discover_repos(make_cfg())
    assert "svc" not in after
    assert set(after) == {"team-a-svc", "team-b-svc"}


def test_discovery_is_deterministic_across_calls(make_repo, make_cfg):
    make_repo("team-a/svc")
    make_repo("team-b/svc")
    make_repo("solo")
    cfg = make_cfg()
    assert atlas.discover_repos(cfg) == atlas.discover_repos(cfg)


def test_repo_names_alias_pins_a_name(make_repo, make_cfg):
    path = make_repo("team-a/svc")
    make_repo("team-b/svc")
    found = atlas.discover_repos(make_cfg(repo_names={str(path): "orders-service"}))
    assert set(found) == {"orders-service", "team-b-svc"}


def test_exclude_repos_matches_resolved_name_or_basename(make_repo, make_cfg):
    make_repo("team-a/svc")
    make_repo("team-b/svc")
    make_repo("solo")
    assert set(atlas.discover_repos(make_cfg(exclude_repos=["svc"]))) == {"solo"}
    assert set(atlas.discover_repos(make_cfg(exclude_repos=["team-a-svc"]))) == {"team-b-svc", "solo"}


def test_directories_without_git_are_ignored(make_repo, make_cfg, tmp_path):
    make_repo("real")
    (tmp_path / "src" / "not-a-repo").mkdir(parents=True)
    assert set(atlas.discover_repos(make_cfg())) == {"real"}
