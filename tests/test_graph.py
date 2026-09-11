import atlas


def ev(name="a.go"):
    return name


def test_other_kind_exposes_can_be_matched(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "dropbox-svc", exposes=[
        {"kind": "sftp", "name": "drop zone", "key": "drop-zone", "evidence": ev()}])
    make_manifest(cfg, "importer", consumes=[
        {"kind": "sftp", "name": "drop zone", "key": "drop-zone", "evidence": ev("b.go")}])
    g = graph_of(cfg)
    assert [(e["from"], e["to"], e["match"]) for e in g["edges"]] == [
        ("importer", "dropbox-svc", "exact")]
    assert g["unresolved"] == []


def test_ambiguous_alias_above_cap_becomes_unresolved_with_candidates(
        make_cfg, make_manifest, graph_of):
    cfg = make_cfg(max_ambiguous_hits=3)
    for i in range(4):
        make_manifest(cfg, f"orders-{i}", identifiers=[f"orders.cell{i}.internal"])
    make_manifest(cfg, "client", consumes=[
        {"kind": "http", "name": "orders", "key": "orders", "evidence": ev()}])
    g = graph_of(cfg)
    assert g["edges"] == []
    u = [x for x in g["unresolved"] if x["repo"] == "client"]
    assert len(u) == 1
    assert u[0]["candidates"] == [f"orders-{i}" for i in range(4)]


def test_alias_within_cap_still_produces_edges(make_cfg, make_manifest, graph_of):
    cfg = make_cfg(max_ambiguous_hits=3)
    make_manifest(cfg, "orders-a", identifiers=["orders.a.internal"])
    make_manifest(cfg, "client", consumes=[
        {"kind": "http", "name": "orders", "key": "orders", "evidence": ev()}])
    g = graph_of(cfg)
    assert [(e["from"], e["to"], e["match"]) for e in g["edges"]] == [
        ("client", "orders-a", "alias")]


def test_generic_identifier_does_not_provide_alias_matches(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "some-gateway", identifiers=["api.internal"])
    make_manifest(cfg, "client", consumes=[
        {"kind": "http", "name": "api", "key": "api", "evidence": ev()}])
    g = graph_of(cfg)
    assert g["edges"] == []
    assert [u["key"] for u in g["unresolved"]] == ["api"]
    assert "candidates" not in g["unresolved"][0]


def test_envvar_consume_resolves_to_the_named_service(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "orders-service")
    make_manifest(cfg, "client", consumes=[
        {"kind": "http", "name": "orders", "key": "ORDERS_SERVICE_URL", "evidence": ev()}])
    g = graph_of(cfg)
    assert [(e["from"], e["to"], e["match"]) for e in g["edges"]] == [
        ("client", "orders-service", "envvar")]


def test_exact_host_match_beats_envvar_tier(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "orders-service", identifiers=["orders-service.internal"])
    make_manifest(cfg, "client", consumes=[
        {"kind": "http", "name": "orders", "key": "orders-service.internal", "evidence": ev()}])
    g = graph_of(cfg)
    assert [e["match"] for e in g["edges"]] == ["exact"]


def test_owned_datastore_links_database_consumers(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "orders-service", datastores=[
        {"kind": "postgres", "name": "orders_db", "access": "owner", "evidence": ev()}])
    make_manifest(cfg, "reporting", consumes=[
        {"kind": "database", "name": "orders db", "key": "orders_db", "evidence": ev("b.go")}])
    g = graph_of(cfg)
    assert [(e["from"], e["to"]) for e in g["edges"]] == [("reporting", "orders-service")]


def test_generic_datastore_name_is_not_indexed(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "a", datastores=[
        {"kind": "postgres", "name": "postgres", "access": "owner", "evidence": ev()}])
    make_manifest(cfg, "b", consumes=[
        {"kind": "database", "name": "pg", "key": "postgres", "evidence": ev("b.go")}])
    assert graph_of(cfg)["edges"] == []


def test_non_owner_datastore_does_not_provide(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "a", datastores=[
        {"kind": "postgres", "name": "orders_db", "access": "read", "evidence": ev()}])
    make_manifest(cfg, "b", consumes=[
        {"kind": "database", "name": "o", "key": "orders_db", "evidence": ev("b.go")}])
    assert graph_of(cfg)["edges"] == []


def test_internal_package_dependency_creates_an_edge(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "shared-lib", packages={
        "publishes": [{"ecosystem": "npm", "name": "@org/shared", "evidence": "package.json"}],
        "depends_on": []})
    make_manifest(cfg, "web", packages={
        "publishes": [],
        "depends_on": [{"ecosystem": "npm", "name": "@org/shared", "evidence": "package.json"}]})
    g = graph_of(cfg)
    assert [(e["from"], e["to"], e["kind"], e["match"]) for e in g["edges"]] == [
        ("web", "shared-lib", "package", "exact")]


def test_external_package_dependency_is_ignored(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "web", packages={
        "publishes": [],
        "depends_on": [{"ecosystem": "npm", "name": "react", "evidence": "package.json"}]})
    g = graph_of(cfg)
    assert g["edges"] == []
    assert g["unresolved"] == []


def test_same_bare_package_name_in_two_ecosystems_stays_alias_tier(
        make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "py-lib", packages={
        "publishes": [{"ecosystem": "pypi", "name": "widgets", "evidence": "pyproject.toml"}],
        "depends_on": []})
    make_manifest(cfg, "js-app", packages={
        "publishes": [],
        "depends_on": [{"ecosystem": "npm", "name": "widgets", "evidence": "package.json"}]})
    g = graph_of(cfg)
    assert [e["match"] for e in g["edges"]] == ["alias"]


def test_orphan_manifests_are_excluded_from_the_graph(
        make_cfg, make_manifest, graph_of, capsys):
    cfg = make_cfg()
    make_manifest(cfg, "live", identifiers=["live.internal"])
    make_manifest(cfg, "deleted", identifiers=["deleted.internal"])
    make_manifest(cfg, "client", consumes=[
        {"kind": "http", "name": "d", "key": "deleted.internal", "evidence": ev()}])
    g = graph_of(cfg, known={"live", "client"})
    assert set(g["repos"]) == {"live", "client"}
    assert g["edges"] == []
    assert "orphan" in capsys.readouterr().err


def test_malformed_manifest_is_skipped_with_a_warning(make_cfg, make_manifest, graph_of, capsys):
    cfg = make_cfg()
    make_manifest(cfg, "good")
    (cfg["atlas_dir"] / "repos" / "broken.json").write_text("{not json")
    g = graph_of(cfg, known={"good", "broken"})
    assert set(g["repos"]) == {"good"}
    assert "broken" in capsys.readouterr().err


def test_manifest_without_commit_metadata_is_skipped(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "good")
    make_manifest(cfg, "nometa", _meta={})
    assert set(graph_of(cfg, known={"good", "nometa"})["repos"]) == {"good"}


def test_a_repo_does_not_depend_on_itself(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "svc", identifiers=["svc.internal"], consumes=[
        {"kind": "http", "name": "self", "key": "svc.internal", "evidence": ev()}])
    g = graph_of(cfg)
    assert g["edges"] == []
    assert len(g["unresolved"]) == 1


def test_duplicate_consumes_produce_one_edge(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "orders", identifiers=["orders.internal"])
    make_manifest(cfg, "client", consumes=[
        {"kind": "http", "name": "a", "key": "orders.internal", "evidence": ev()},
        {"kind": "http", "name": "b", "key": "orders.internal", "evidence": ev("b.go")}])
    assert len(graph_of(cfg)["edges"]) == 1


def test_shared_datastores_lists_stores_touched_by_more_than_one_repo(
        make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "a", datastores=[
        {"kind": "postgres", "name": "orders_db", "access": "owner", "evidence": ev()}])
    make_manifest(cfg, "b", datastores=[
        {"kind": "postgres", "name": "orders_db", "access": "read", "evidence": ev("b.go")}])
    make_manifest(cfg, "c", datastores=[
        {"kind": "postgres", "name": "solo_db", "access": "owner", "evidence": ev("c.go")}])
    g = graph_of(cfg)
    assert [s["name"] for s in g["shared_datastores"]] == ["orders_db"]
    assert g["shared_datastores"][0]["repos"] == {"a": "owner", "b": "read"}


def test_build_renders_docs_and_leaves_no_staging_dirs(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "svc-a", domain="payments",
                  components=[{"name": "api", "path": "api", "role": "http edge"}],
                  component_edges=[{"from": "api", "to": "api", "label": "self"}])
    g = graph_of(cfg)
    ad = cfg["atlas_dir"]
    assert (ad / "docs" / "svc-a.md").exists()
    assert (ad / "docs" / "domains" / "payments.md").exists()
    assert (ad / "index.md").exists()
    assert not (ad / "docs.new").exists()
    assert not (ad / "docs.old").exists()
    assert g["repos"]["svc-a"]["doc"] == str(ad / "docs" / "svc-a.md")


def test_rebuild_removes_docs_for_repos_that_disappear(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "keeper")
    make_manifest(cfg, "goner")
    graph_of(cfg)
    assert (cfg["atlas_dir"] / "docs" / "goner.md").exists()
    graph_of(cfg, known={"keeper"})
    assert (cfg["atlas_dir"] / "docs" / "keeper.md").exists()
    assert not (cfg["atlas_dir"] / "docs" / "goner.md").exists()


def test_graph_is_published_only_after_docs_exist(make_cfg, make_manifest, monkeypatch):
    cfg = make_cfg()
    make_manifest(cfg, "svc-a")
    seen = {}
    real = atlas.write_json

    def spy(path, data):
        if path.name == "graph.json":
            seen["docs_present"] = (cfg["atlas_dir"] / "docs" / "svc-a.md").exists()
        return real(path, data)

    monkeypatch.setattr(atlas, "write_json", spy)
    atlas.build(cfg, {"svc-a"})
    assert seen["docs_present"] is True


def test_mermaid_labels_are_sanitised():
    assert atlas.mid("api gateway") == "n_api_gateway"
    assert "|" not in atlas.mtext('a|b"c[d]')
    assert atlas.mtext("") == "-"


def test_package_path_identifier_matches_only_its_own_repo(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "go-a", identifiers=["github.com/org/a"])
    make_manifest(cfg, "go-b", identifiers=["github.com/org/b"])
    make_manifest(cfg, "client", consumes=[
        {"kind": "package", "name": "a", "key": "github.com/org/a", "evidence": ev()}])
    g = graph_of(cfg)
    assert [(e["from"], e["to"], e["match"]) for e in g["edges"]] == [
        ("client", "go-a", "exact")]


def test_code_host_url_does_not_link_every_go_repo(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "go-a", identifiers=["github.com/org/a"])
    make_manifest(cfg, "go-b", identifiers=["github.com/org/b"])
    make_manifest(cfg, "client", consumes=[
        {"kind": "http", "name": "repo", "key": "https://github.com/org/a", "evidence": ev()}])
    g = graph_of(cfg)
    assert g["edges"] == []
    assert [u["key"] for u in g["unresolved"]] == ["https://github.com/org/a"]


def test_scoped_package_identifier_does_not_provide_its_scope(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "shared", identifiers=["@org/shared"])
    make_manifest(cfg, "client", consumes=[
        {"kind": "http", "name": "org", "key": "org", "evidence": ev()}])
    assert graph_of(cfg)["edges"] == []


def test_scoped_package_identifier_still_matches_a_package_consume(
        make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "shared", identifiers=["@org/shared"])
    make_manifest(cfg, "client", consumes=[
        {"kind": "package", "name": "shared", "key": "@org/shared", "evidence": ev()}])
    assert [(e["from"], e["to"], e["match"]) for e in graph_of(cfg)["edges"]] == [
        ("client", "shared", "exact")]


def test_too_many_exact_hits_are_capped_with_candidates(make_cfg, make_manifest, graph_of):
    cfg = make_cfg(max_ambiguous_hits=3)
    for i in range(4):
        make_manifest(cfg, f"svc-{i}", identifiers=["orders.internal"])
    make_manifest(cfg, "client", consumes=[
        {"kind": "http", "name": "o", "key": "orders.internal", "evidence": ev()}])
    g = graph_of(cfg)
    assert g["edges"] == []
    assert g["unresolved"][0]["candidates"] == [f"svc-{i}" for i in range(4)]


def test_many_producers_of_one_topic_still_link(make_cfg, make_manifest, graph_of):
    cfg = make_cfg(max_ambiguous_hits=3)
    for i in range(4):
        make_manifest(cfg, f"producer-{i}", exposes=[
            {"kind": "topic", "name": "OrderCreated", "key": "order.created", "evidence": ev()}])
    make_manifest(cfg, "consumer", consumes=[
        {"kind": "topic", "name": "OrderCreated", "key": "order.created", "evidence": ev()}])
    g = graph_of(cfg)
    assert sorted(e["to"] for e in g["edges"]) == [f"producer-{i}" for i in range(4)]
    assert g["unresolved"] == []


def test_a_generically_named_repo_does_not_attract_alias_matches(
        make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "api")
    make_manifest(cfg, "client", consumes=[
        {"kind": "http", "name": "orders", "key": "https://api.orders.internal/v1",
         "evidence": ev()}])
    g = graph_of(cfg)
    assert g["edges"] == []
    assert [u["key"] for u in g["unresolved"]] == ["https://api.orders.internal/v1"]


def test_a_generic_envvar_stem_does_not_attract_matches(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "api")
    make_manifest(cfg, "client", consumes=[
        {"kind": "http", "name": "api", "key": "API_URL", "evidence": ev()}])
    assert graph_of(cfg)["edges"] == []


def test_an_exact_identifier_still_matches_a_generic_name(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "api")
    make_manifest(cfg, "client", consumes=[
        {"kind": "http", "name": "api", "key": "api", "evidence": ev()}])
    assert [(e["from"], e["to"], e["match"]) for e in graph_of(cfg)["edges"]] == [
        ("client", "api", "exact")]
