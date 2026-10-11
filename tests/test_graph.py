import pytest

import atlas


def ev(name="a.go"):
    return name


def test_other_kind_exposes_can_be_matched(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "dropbox-svc",
        exposes=[{"kind": "sftp", "name": "drop zone", "key": "drop-zone", "evidence": ev()}],
    )
    make_manifest(
        cfg,
        "importer",
        consumes=[
            {"kind": "sftp", "name": "drop zone", "key": "drop-zone", "evidence": ev("b.go")}
        ],
    )
    g = graph_of(cfg)
    assert [(e["from"], e["to"], e["match"]) for e in g["edges"]] == [
        ("importer", "dropbox-svc", "exact")
    ]
    assert g["unresolved"] == []


def test_ambiguous_alias_above_cap_becomes_unresolved_with_candidates(
    make_cfg, make_manifest, graph_of
):
    cfg = make_cfg(max_ambiguous_hits=3)
    for i in range(4):
        make_manifest(cfg, f"orders-{i}", identifiers=[f"orders.cell{i}.internal"])
    make_manifest(
        cfg,
        "client",
        consumes=[{"kind": "http", "name": "orders", "key": "orders", "evidence": ev()}],
    )
    g = graph_of(cfg)
    assert g["edges"] == []
    u = [x for x in g["unresolved"] if x["repo"] == "client"]
    assert len(u) == 1
    assert u[0]["candidates"] == [f"orders-{i}" for i in range(4)]


def test_alias_within_cap_still_produces_edges(make_cfg, make_manifest, graph_of):
    cfg = make_cfg(max_ambiguous_hits=3)
    make_manifest(cfg, "orders-a", identifiers=["orders.a.internal"])
    make_manifest(
        cfg,
        "client",
        consumes=[{"kind": "http", "name": "orders", "key": "orders", "evidence": ev()}],
    )
    g = graph_of(cfg)
    assert [(e["from"], e["to"], e["match"]) for e in g["edges"]] == [
        ("client", "orders-a", "alias")
    ]


def test_generic_identifier_does_not_provide_alias_matches(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "some-gateway", identifiers=["api.internal"])
    make_manifest(
        cfg, "client", consumes=[{"kind": "http", "name": "api", "key": "api", "evidence": ev()}]
    )
    g = graph_of(cfg)
    assert g["edges"] == []
    assert [u["key"] for u in g["unresolved"]] == ["api"]
    assert "candidates" not in g["unresolved"][0]


def test_envvar_consume_resolves_to_the_named_service(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "orders-service")
    make_manifest(
        cfg,
        "client",
        consumes=[
            {"kind": "http", "name": "orders", "key": "ORDERS_SERVICE_URL", "evidence": ev()}
        ],
    )
    g = graph_of(cfg)
    assert [(e["from"], e["to"], e["match"]) for e in g["edges"]] == [
        ("client", "orders-service", "envvar")
    ]


def test_exact_host_match_beats_envvar_tier(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "orders-service", identifiers=["orders-service.internal"])
    make_manifest(
        cfg,
        "client",
        consumes=[
            {"kind": "http", "name": "orders", "key": "orders-service.internal", "evidence": ev()}
        ],
    )
    g = graph_of(cfg)
    assert [e["match"] for e in g["edges"]] == ["exact"]


def test_owned_datastore_links_database_consumers(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "orders-service",
        datastores=[{"kind": "postgres", "name": "orders_db", "access": "owner", "evidence": ev()}],
    )
    make_manifest(
        cfg,
        "reporting",
        consumes=[
            {"kind": "database", "name": "orders db", "key": "orders_db", "evidence": ev("b.go")}
        ],
    )
    g = graph_of(cfg)
    assert [(e["from"], e["to"]) for e in g["edges"]] == [("reporting", "orders-service")]


def test_generic_datastore_name_is_not_indexed(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "a",
        datastores=[{"kind": "postgres", "name": "postgres", "access": "owner", "evidence": ev()}],
    )
    make_manifest(
        cfg,
        "b",
        consumes=[{"kind": "database", "name": "pg", "key": "postgres", "evidence": ev("b.go")}],
    )
    assert graph_of(cfg)["edges"] == []


def test_non_owner_datastore_does_not_provide(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "a",
        datastores=[{"kind": "postgres", "name": "orders_db", "access": "read", "evidence": ev()}],
    )
    make_manifest(
        cfg,
        "b",
        consumes=[{"kind": "database", "name": "o", "key": "orders_db", "evidence": ev("b.go")}],
    )
    assert graph_of(cfg)["edges"] == []


def test_internal_package_dependency_creates_an_edge(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "shared-lib",
        packages={
            "publishes": [{"ecosystem": "npm", "name": "@org/shared", "evidence": "package.json"}],
            "depends_on": [],
        },
    )
    make_manifest(
        cfg,
        "web",
        packages={
            "publishes": [],
            "depends_on": [{"ecosystem": "npm", "name": "@org/shared", "evidence": "package.json"}],
        },
    )
    g = graph_of(cfg)
    assert [(e["from"], e["to"], e["kind"], e["match"]) for e in g["edges"]] == [
        ("web", "shared-lib", "package", "exact")
    ]


def test_external_package_dependency_is_ignored(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "web",
        packages={
            "publishes": [],
            "depends_on": [{"ecosystem": "npm", "name": "react", "evidence": "package.json"}],
        },
    )
    g = graph_of(cfg)
    assert g["edges"] == []
    assert g["unresolved"] == []


def test_same_bare_package_name_in_two_ecosystems_stays_alias_tier(
    make_cfg, make_manifest, graph_of
):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "py-lib",
        packages={
            "publishes": [{"ecosystem": "pypi", "name": "widgets", "evidence": "pyproject.toml"}],
            "depends_on": [],
        },
    )
    make_manifest(
        cfg,
        "js-app",
        packages={
            "publishes": [],
            "depends_on": [{"ecosystem": "npm", "name": "widgets", "evidence": "package.json"}],
        },
    )
    g = graph_of(cfg)
    assert [e["match"] for e in g["edges"]] == ["alias"]


def test_orphan_manifests_are_excluded_from_the_graph(make_cfg, make_manifest, graph_of, capsys):
    cfg = make_cfg()
    make_manifest(cfg, "live", identifiers=["live.internal"])
    make_manifest(cfg, "deleted", identifiers=["deleted.internal"])
    make_manifest(
        cfg,
        "client",
        consumes=[{"kind": "http", "name": "d", "key": "deleted.internal", "evidence": ev()}],
    )
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


def test_a_manifest_that_is_not_an_object_is_skipped_with_a_warning(
    make_cfg, make_manifest, graph_of, capsys
):
    cfg = make_cfg()
    make_manifest(cfg, "good")
    (cfg["atlas_dir"] / "repos" / "list.json").write_text("[1, 2]")
    g = graph_of(cfg, known={"good", "list"})
    assert set(g["repos"]) == {"good"}
    assert "list" in capsys.readouterr().err


def test_load_manifests_lets_unexpected_errors_propagate(make_cfg, make_manifest, monkeypatch):
    cfg = make_cfg()
    make_manifest(cfg, "good")

    def boom(_m):
        raise RuntimeError("not a manifest problem")

    monkeypatch.setattr(atlas, "check_meta", boom)
    with pytest.raises(RuntimeError):
        atlas.load_manifests(cfg["atlas_dir"], {"good"})


def test_manifest_without_commit_metadata_is_skipped(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "good")
    make_manifest(cfg, "nometa", _meta={})
    assert set(graph_of(cfg, known={"good", "nometa"})["repos"]) == {"good"}


def test_a_repo_does_not_depend_on_itself(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "svc",
        identifiers=["svc.internal"],
        consumes=[{"kind": "http", "name": "self", "key": "svc.internal", "evidence": ev()}],
    )
    g = graph_of(cfg)
    assert g["edges"] == []
    assert len(g["unresolved"]) == 1


def test_duplicate_consumes_produce_one_edge(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "orders", identifiers=["orders.internal"])
    make_manifest(
        cfg,
        "client",
        consumes=[
            {"kind": "http", "name": "a", "key": "orders.internal", "evidence": ev()},
            {"kind": "http", "name": "b", "key": "orders.internal", "evidence": ev("b.go")},
        ],
    )
    assert len(graph_of(cfg)["edges"]) == 1


def test_shared_datastores_lists_stores_touched_by_more_than_one_repo(
    make_cfg, make_manifest, graph_of
):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "a",
        datastores=[{"kind": "postgres", "name": "orders_db", "access": "owner", "evidence": ev()}],
    )
    make_manifest(
        cfg,
        "b",
        datastores=[
            {"kind": "postgres", "name": "orders_db", "access": "read", "evidence": ev("b.go")}
        ],
    )
    make_manifest(
        cfg,
        "c",
        datastores=[
            {"kind": "postgres", "name": "solo_db", "access": "owner", "evidence": ev("c.go")}
        ],
    )
    g = graph_of(cfg)
    assert [s["name"] for s in g["shared_datastores"]] == ["orders_db"]
    assert g["shared_datastores"][0]["repos"] == {"a": "owner", "b": "read"}


def test_build_renders_docs_and_leaves_no_staging_dirs(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "svc-a",
        domain="payments",
        components=[{"name": "api", "path": "api", "role": "http edge"}],
        component_edges=[{"from": "api", "to": "api", "label": "self"}],
    )
    g = graph_of(cfg)
    ad = cfg["atlas_dir"]
    assert (ad / "docs" / "svc-a.md").exists()
    assert (ad / "docs" / "domains" / "payments.md").exists()
    assert (ad / "index.md").exists()
    assert not (ad / "docs.new").exists()
    assert not (ad / "docs.old").exists()
    assert g["repos"]["svc-a"]["doc"] == str(ad / "docs" / "svc-a.md")


def test_graph_entries_carry_the_remote_url_and_last_commit_date(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    m = make_manifest(cfg, "svc-a")
    m["_meta"].update(
        remote_url="https://github.com/org/svc-a", last_commit_at="2025-12-25T09:00:00+00:00"
    )
    atlas.write_json(cfg["atlas_dir"] / "repos" / "svc-a.json", m)
    entry = graph_of(cfg)["repos"]["svc-a"]
    assert entry["remote_url"] == "https://github.com/org/svc-a"
    assert entry["last_commit_at"] == "2025-12-25T09:00:00+00:00"


def test_a_manifest_without_the_git_metadata_still_builds(make_cfg, make_manifest, graph_of):
    """Every entry written before those fields existed, until its repo next changes."""
    cfg = make_cfg()
    make_manifest(cfg, "svc-a")
    entry = graph_of(cfg)["repos"]["svc-a"]
    assert entry["remote_url"] is None
    assert entry["last_commit_at"] is None


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
    make_manifest(
        cfg,
        "client",
        consumes=[{"kind": "package", "name": "a", "key": "github.com/org/a", "evidence": ev()}],
    )
    g = graph_of(cfg)
    assert [(e["from"], e["to"], e["match"]) for e in g["edges"]] == [("client", "go-a", "exact")]


def test_code_host_url_does_not_link_every_go_repo(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "go-a", identifiers=["github.com/org/a"])
    make_manifest(cfg, "go-b", identifiers=["github.com/org/b"])
    make_manifest(
        cfg,
        "client",
        consumes=[
            {"kind": "http", "name": "repo", "key": "https://github.com/org/a", "evidence": ev()}
        ],
    )
    g = graph_of(cfg)
    assert g["edges"] == []
    assert [u["key"] for u in g["unresolved"]] == ["https://github.com/org/a"]


def test_scoped_package_identifier_does_not_provide_its_scope(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "shared", identifiers=["@org/shared"])
    make_manifest(
        cfg, "client", consumes=[{"kind": "http", "name": "org", "key": "org", "evidence": ev()}]
    )
    assert graph_of(cfg)["edges"] == []


def test_scoped_package_identifier_still_matches_a_package_consume(
    make_cfg, make_manifest, graph_of
):
    cfg = make_cfg()
    make_manifest(cfg, "shared", identifiers=["@org/shared"])
    make_manifest(
        cfg,
        "client",
        consumes=[{"kind": "package", "name": "shared", "key": "@org/shared", "evidence": ev()}],
    )
    assert [(e["from"], e["to"], e["match"]) for e in graph_of(cfg)["edges"]] == [
        ("client", "shared", "exact")
    ]


def test_too_many_exact_hits_are_capped_with_candidates(make_cfg, make_manifest, graph_of):
    cfg = make_cfg(max_ambiguous_hits=3)
    for i in range(4):
        make_manifest(cfg, f"svc-{i}", identifiers=["orders.internal"])
    make_manifest(
        cfg,
        "client",
        consumes=[{"kind": "http", "name": "o", "key": "orders.internal", "evidence": ev()}],
    )
    g = graph_of(cfg)
    assert g["edges"] == []
    assert g["unresolved"][0]["candidates"] == [f"svc-{i}" for i in range(4)]


def test_many_producers_of_one_topic_still_link(make_cfg, make_manifest, graph_of):
    cfg = make_cfg(max_ambiguous_hits=3)
    for i in range(4):
        make_manifest(
            cfg,
            f"producer-{i}",
            exposes=[
                {"kind": "topic", "name": "OrderCreated", "key": "order.created", "evidence": ev()}
            ],
        )
    make_manifest(
        cfg,
        "consumer",
        consumes=[
            {"kind": "topic", "name": "OrderCreated", "key": "order.created", "evidence": ev()}
        ],
    )
    g = graph_of(cfg)
    assert sorted(e["to"] for e in g["edges"]) == [f"producer-{i}" for i in range(4)]
    assert g["unresolved"] == []
    # Several producers of one topic is the normal pub/sub shape, not a weak guess, so the
    # edges must keep the tier they matched at rather than being demoted to "ambiguous".
    assert {e["match"] for e in g["edges"]} == {"exact"}


def test_a_generically_named_repo_does_not_attract_alias_matches(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "api")
    make_manifest(
        cfg,
        "client",
        consumes=[
            {
                "kind": "http",
                "name": "orders",
                "key": "https://api.orders.internal/v1",
                "evidence": ev(),
            }
        ],
    )
    g = graph_of(cfg)
    assert g["edges"] == []
    assert [u["key"] for u in g["unresolved"]] == ["https://api.orders.internal/v1"]


def test_a_generic_envvar_stem_does_not_attract_matches(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "api")
    make_manifest(
        cfg,
        "client",
        consumes=[{"kind": "http", "name": "api", "key": "API_URL", "evidence": ev()}],
    )
    assert graph_of(cfg)["edges"] == []


def test_an_exact_identifier_still_matches_a_generic_name(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "api")
    make_manifest(
        cfg, "client", consumes=[{"kind": "http", "name": "api", "key": "api", "evidence": ev()}]
    )
    assert [(e["from"], e["to"], e["match"]) for e in graph_of(cfg)["edges"]] == [
        ("client", "api", "exact")
    ]


def db_consumer(key, kind="database"):
    return [{"kind": kind, "name": "db", "key": key, "evidence": ev("b.cs")}]


@pytest.mark.parametrize(
    "key", ["Billing", "ConnectionStrings:Billing", "ConnectionStrings__billing"]
)
def test_a_named_connection_string_links_to_a_dotted_suffix_provider(
    make_cfg, make_manifest, graph_of, key
):
    cfg = make_cfg()
    make_manifest(cfg, "orm-billing", identifiers=["Acme.Orm.Billing"])
    for i in range(5):
        make_manifest(cfg, f"app-{i}", consumes=db_consumer(key))
    g = graph_of(cfg)
    assert sorted((e["from"], e["to"], e["match"]) for e in g["edges"]) == [
        (f"app-{i}", "orm-billing", "alias") for i in range(5)
    ]
    assert g["unresolved"] == []


def test_a_dotted_suffix_matches_an_exposed_database_key(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "orm-ledger",
        exposes=[
            {"kind": "database", "name": "ledger", "key": "Acme.Orm.Ledger", "evidence": ev()}
        ],
    )
    make_manifest(cfg, "app", consumes=db_consumer("Ledger"))
    assert [(e["to"], e["match"]) for e in graph_of(cfg)["edges"]] == [("orm-ledger", "alias")]


def test_a_dotted_suffix_does_not_apply_to_service_consumers(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "orm-billing", identifiers=["Acme.Orm.Billing"])
    make_manifest(cfg, "app", consumes=db_consumer("Billing", kind="http"))
    assert graph_of(cfg)["edges"] == []


def test_too_many_dotted_suffix_providers_stay_unresolved_with_candidates(
    make_cfg, make_manifest, graph_of
):
    cfg = make_cfg()
    for i in range(4):
        make_manifest(cfg, f"orm-{i}", identifiers=[f"Org{i}.Orm.Billing"])
    make_manifest(cfg, "app", consumes=db_consumer("Billing"))
    g = graph_of(cfg)
    assert g["edges"] == []
    assert g["unresolved"][0]["candidates"] == [f"orm-{i}" for i in range(4)]


def test_a_configured_shared_name_reported_as_an_identifier_makes_no_edge(
    make_cfg, make_manifest, graph_of
):
    cfg = make_cfg(generic_identifiers=["AcmeFeed"])
    make_manifest(cfg, "one", identifiers=["AcmeFeed"])
    make_manifest(cfg, "two", identifiers=["AcmeFeed"])
    make_manifest(
        cfg,
        "client",
        consumes=[{"kind": "http", "name": "feed", "key": "AcmeFeed", "evidence": ev()}],
    )
    assert graph_of(cfg)["edges"] == []


def test_a_placeholder_identifier_never_forms_an_exact_edge(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "holder", identifiers=["Default"])
    make_manifest(
        cfg,
        "client",
        consumes=[{"kind": "http", "name": "d", "key": "Default", "evidence": ev()}],
    )
    assert graph_of(cfg)["edges"] == []


def env_publisher(cfg, make_manifest, name, var="DATAW_URI", consumes=()):
    make_manifest(
        cfg,
        name,
        exposes=[
            {"kind": "envvar", "name": var, "key": var, "evidence": ev(), "source": "deterministic"}
        ],
        consumes=list(consumes),
    )


def env_consumer(var="DATAW_URI", kind="other"):
    return [{"kind": kind, "name": var, "key": var, "evidence": ev("b.py")}]


def test_a_published_env_var_name_links_its_consumers_at_alias(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    env_publisher(cfg, make_manifest, "dw-loader")
    make_manifest(cfg, "report", consumes=env_consumer())
    make_manifest(cfg, "etl", consumes=env_consumer(kind="database"))
    g = graph_of(cfg)
    assert sorted((e["from"], e["to"], e["match"]) for e in g["edges"]) == [
        ("etl", "dw-loader", "alias"),
        ("report", "dw-loader", "alias"),
    ]
    assert g["unresolved"] == []


def test_env_var_names_are_matched_whole_not_by_prefix(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    env_publisher(cfg, make_manifest, "dw-loader")
    make_manifest(cfg, "report", consumes=env_consumer("DATAW_URL"))
    assert graph_of(cfg)["edges"] == []


def test_too_many_publishers_of_one_env_var_stay_unresolved(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    for i in range(4):
        env_publisher(cfg, make_manifest, f"pub-{i}")
    make_manifest(cfg, "report", consumes=env_consumer())
    g = graph_of(cfg)
    assert g["edges"] == []
    assert g["unresolved"][0]["candidates"] == [f"pub-{i}" for i in range(4)]


def test_a_repo_that_consumes_its_own_published_env_var_does_not_provide_it(
    make_cfg, make_manifest, graph_of
):
    cfg = make_cfg()
    env_publisher(cfg, make_manifest, "reader", consumes=env_consumer())
    make_manifest(cfg, "report", consumes=env_consumer())
    g = graph_of(cfg)
    assert g["edges"] == []
    assert [u["repo"] for u in g["unresolved"]] == ["reader", "report"]


def test_a_generic_env_var_is_never_linked_by_name(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    env_publisher(cfg, make_manifest, "infra", var="MONGO_URI")
    make_manifest(cfg, "report", consumes=env_consumer("MONGO_URI"))
    g = graph_of(cfg)
    assert g["edges"] == []
    assert [u["key"] for u in g["unresolved"]] == ["MONGO_URI"]


def http_consumer(key, name="call"):
    return [{"kind": "http", "name": name, "key": key, "evidence": ev("b.py")}]


def test_an_outside_fqdn_becomes_an_external_node_and_edge(make_cfg, make_manifest, graph_of):
    cfg = make_cfg(internal_domains=["corp.example"])
    make_manifest(cfg, "client", consumes=http_consumer("https://api.vendor.com/v1/x"))
    g = graph_of(cfg)
    assert g["external_nodes"] == {
        "api.vendor.com": {
            "label": "api.vendor.com",
            "kind": "external",
            "hosts": ["api.vendor.com"],
            "source": "derived",
        }
    }
    assert [(e["from"], e["to"], e["kind"], e["match"]) for e in g["external_edges"]] == [
        ("client", "api.vendor.com", "http", "derived")
    ]
    assert g["edges"] == [] and g["unresolved"] == []


@pytest.mark.parametrize(
    "key",
    [
        "https://orders.corp.example/v1",
        "https://corp.example",
        "http://localhost:8080",
        "http://10.1.2.3/x",
        "http://orders.default.svc.cluster.local",
        "http://thing.internal",
        "https://example.com",
        "https://{host}.vendor.com",
        "VENDOR_API_URL",
        "vendor",
    ],
)
def test_only_an_evidence_backed_outside_fqdn_is_derived_as_external(
    make_cfg, make_manifest, graph_of, key
):
    cfg = make_cfg(internal_domains=["corp.example"])
    make_manifest(cfg, "client", consumes=http_consumer(key))
    g = graph_of(cfg)
    assert g["external_nodes"] == {} and g["external_edges"] == []
    assert [u["key"] for u in g["unresolved"]] == [key]


def test_nothing_is_derived_as_external_without_internal_domains(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "client", consumes=http_consumer("https://api.vendor.com/v1"))
    g = graph_of(cfg)
    assert g["external_nodes"] == {}
    assert [u["key"] for u in g["unresolved"]] == ["https://api.vendor.com/v1"]


def test_an_ambiguous_target_is_never_derived_as_external(make_cfg, make_manifest, graph_of):
    cfg = make_cfg(internal_domains=["corp.example"])
    for i in range(4):
        make_manifest(cfg, f"orders-{i}", identifiers=["orders"])
    make_manifest(cfg, "client", consumes=http_consumer("https://orders.vendor.com"))
    g = graph_of(cfg)
    assert g["external_nodes"] == {}
    assert g["unresolved"][0]["candidates"]


def test_an_internal_match_wins_over_an_external_guess(make_cfg, make_manifest, graph_of):
    cfg = make_cfg(internal_domains=["corp.example"])
    make_manifest(cfg, "billing", identifiers=["billing.vendor.com"])
    make_manifest(cfg, "client", consumes=http_consumer("https://billing.vendor.com"))
    g = graph_of(cfg)
    assert [e["to"] for e in g["edges"]] == ["billing"]
    assert g["external_nodes"] == {}


def test_every_target_lands_in_exactly_one_bucket(make_cfg, make_manifest, graph_of):
    cfg = make_cfg(internal_domains=["corp.example"])
    make_manifest(cfg, "orders-service")
    make_manifest(
        cfg,
        "client",
        consumes=[
            *http_consumer("ORDERS_SERVICE_URL"),
            *http_consumer("https://api.vendor.com"),
            *http_consumer("https://api.other.com"),
            *http_consumer("MYSTERY_URL"),
        ],
    )
    g = graph_of(cfg)
    assert g["resolution"] == {
        "internal": 1,
        "external": 2,
        "generic": 0,
        "unknown": 1,
        "stale_overrides": [],
    }
    assert sum(g["resolution"][b] for b in ("internal", "external", "generic", "unknown")) == 4


def write_overrides(cfg, targets=(), externals=None, groups=None):
    atlas.write_json(
        cfg["resolve_overrides"],
        {
            "version": 1,
            "externals": externals or {},
            "groups": groups or {},
            "targets": list(targets),
        },
    )


def target(key, repo, action, **fields):
    return {"key": key, "repo": repo, "action": action, **fields}


def test_a_link_override_edges_an_otherwise_unresolved_target(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "orders-svc")
    make_manifest(cfg, "client", consumes=http_consumer("ORDERS_BACKEND"))
    write_overrides(cfg, [target("ORDERS_BACKEND", "client", "link", to="orders-svc")])
    g = graph_of(cfg)
    assert [(e["from"], e["to"], e["match"]) for e in g["edges"]] == [
        ("client", "orders-svc", "override")
    ]
    assert g["unresolved"] == []
    assert g["resolution"]["internal"] == 1


def test_an_override_beats_a_rule_and_matches_case_and_kind_loosely(
    make_cfg, make_manifest, graph_of
):
    cfg = make_cfg()
    make_manifest(cfg, "orders-service")
    make_manifest(cfg, "billing")
    make_manifest(
        cfg,
        "client",
        consumes=[{"kind": "other", "name": "o", "key": "ORDERS_SERVICE_URL", "evidence": ev()}],
    )
    write_overrides(cfg, [target("orders_service_url", "client", "link", to="billing")])
    assert [(e["to"], e["match"]) for e in graph_of(cfg)["edges"]] == [("billing", "override")]


def test_a_link_to_an_unknown_repo_stays_unknown_and_warns(
    make_cfg, make_manifest, graph_of, capsys
):
    cfg = make_cfg()
    make_manifest(cfg, "client", consumes=http_consumer("ORDERS_BACKEND"))
    write_overrides(cfg, [target("ORDERS_BACKEND", "client", "link", to="gone")])
    g = graph_of(cfg)
    assert g["edges"] == [] and [u["key"] for u in g["unresolved"]] == ["ORDERS_BACKEND"]
    assert "gone" in capsys.readouterr().err


def test_an_external_override_creates_a_labelled_node(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "client", consumes=http_consumer("WORKLOG_API_URL"))
    write_overrides(
        cfg,
        [target("WORKLOG_API_URL", "client", "external", external="worklog")],
        externals={"worklog": {"label": "Worklog SaaS API", "hosts": ["api.worklog.io"]}},
    )
    g = graph_of(cfg)
    assert g["external_nodes"] == {
        "worklog": {
            "label": "Worklog SaaS API",
            "kind": "external",
            "hosts": ["api.worklog.io"],
            "source": "override",
        }
    }
    assert [(e["to"], e["match"]) for e in g["external_edges"]] == [("worklog", "override")]
    assert g["unresolved"] == [] and g["resolution"]["external"] == 1


def test_an_undefined_external_id_is_created_labelled_by_its_id(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(cfg, "client", consumes=http_consumer("X_URL"))
    write_overrides(cfg, [target("X_URL", "client", "external", external="state-portal")])
    assert graph_of(cfg)["external_nodes"]["state-portal"]["label"] == "state-portal"


def test_override_hosts_merge_and_relabel_derived_externals(make_cfg, make_manifest, graph_of):
    cfg = make_cfg(internal_domains=["corp.example"])
    make_manifest(cfg, "a", consumes=http_consumer("https://api.worklog.io/v1"))
    make_manifest(cfg, "b", consumes=http_consumer("https://eu.worklog.io/v1"))
    write_overrides(
        cfg,
        externals={"worklog": {"label": "Worklog", "hosts": ["api.worklog.io", "eu.worklog.io"]}},
    )
    g = graph_of(cfg)
    assert list(g["external_nodes"]) == ["worklog"]
    assert g["external_nodes"]["worklog"]["label"] == "Worklog"
    assert sorted(e["from"] for e in g["external_edges"]) == ["a", "b"]
    assert {e["to"] for e in g["external_edges"]} == {"worklog"}


def mongo_consumers(cfg, make_manifest, repos):
    for repo in repos:
        make_manifest(cfg, repo, consumes=http_consumer("MONGO_URI"))


def test_a_generic_var_is_separate_per_consumer_unless_explicitly_grouped(
    make_cfg, make_manifest, graph_of
):
    cfg = make_cfg()
    mongo_consumers(cfg, make_manifest, ["a", "b", "c"])
    write_overrides(
        cfg,
        [
            target("MONGO_URI", "a", "generic", tag="mongo", group="mongo-billing"),
            target("MONGO_URI", "b", "generic", tag="mongo", group="mongo-billing"),
            target("MONGO_URI", "c", "generic", tag="mongo"),
        ],
        groups={"mongo-billing": {"label": "Billing Mongo"}},
    )
    g = graph_of(cfg)
    assert [(r["repo"], r["tag"], r.get("group")) for r in g["generic"]] == [
        ("a", "mongo", "mongo-billing"),
        ("b", "mongo", "mongo-billing"),
        ("c", "mongo", None),
    ]
    assert [s for s in g["shared_datastores"] if s.get("source") == "override"] == [
        {
            "name": "mongo-billing",
            "label": "Billing Mongo",
            "tag": "mongo",
            "kind": "generic",
            "repos": {"a": "", "b": ""},
            "source": "override",
        }
    ]
    assert g["unresolved"] == [] and g["resolution"]["generic"] == 3


def test_no_rule_ever_groups_a_generic_var(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    mongo_consumers(cfg, make_manifest, ["a", "b", "c"])
    g = graph_of(cfg)
    assert g["shared_datastores"] == [] and g["generic"] == []
    assert [u["repo"] for u in g["unresolved"]] == ["a", "b", "c"]


def test_a_group_of_one_is_not_reported_as_shared(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    mongo_consumers(cfg, make_manifest, ["a"])
    write_overrides(cfg, [target("MONGO_URI", "a", "generic", tag="mongo", group="solo")])
    g = graph_of(cfg)
    assert g["shared_datastores"] == [] and g["generic"][0]["group"] == "solo"


def test_an_override_matching_nothing_is_reported_stale_not_dropped(
    make_cfg, make_manifest, graph_of, capsys
):
    cfg = make_cfg()
    make_manifest(cfg, "client")
    write_overrides(cfg, [target("OLD_URL", "client", "external", external="x")])
    g = graph_of(cfg)
    assert g["resolution"]["stale_overrides"] == [
        {"key": "OLD_URL", "repo": "client", "action": "external"}
    ]
    assert "OLD_URL" in capsys.readouterr().err
    assert atlas.load_overrides(cfg["resolve_overrides"])["targets"]


def test_every_bucket_still_sums_to_the_targets_with_overrides(make_cfg, make_manifest, graph_of):
    cfg = make_cfg(internal_domains=["corp.example"])
    make_manifest(cfg, "orders-service")
    make_manifest(
        cfg,
        "client",
        consumes=[
            *http_consumer("ORDERS_SERVICE_URL"),
            *http_consumer("https://api.vendor.com"),
            *http_consumer("MONGO_URI"),
            *http_consumer("MYSTERY_URL"),
        ],
    )
    write_overrides(cfg, [target("MONGO_URI", "client", "generic", tag="mongo")])
    r = graph_of(cfg)["resolution"]
    assert (r["internal"], r["external"], r["generic"], r["unknown"]) == (1, 1, 1, 1)


def test_a_missing_overrides_file_is_empty(make_cfg):
    cfg = make_cfg()
    assert atlas.load_overrides(cfg["resolve_overrides"]) == {
        "externals": {},
        "groups": {},
        "targets": {},
    }


@pytest.mark.parametrize(
    "body,fragment",
    [
        ("{not json", "invalid JSON"),
        ("[]", "JSON object"),
        ('{"version": 2}', "version"),
        ('{"targets": [{"key": "K", "repo": "r", "action": "nuke"}]}', "action"),
        ('{"targets": [{"key": "K", "repo": "r", "action": "link"}]}', "'to'"),
        ('{"targets": [{"key": "K", "repo": "r", "action": "external"}]}', "'external'"),
        ('{"targets": [{"key": "K", "repo": "r", "action": "generic"}]}', "'tag'"),
        ('{"targets": [{"key": "", "repo": "r", "action": "generic", "tag": "t"}]}', "'key'"),
        (
            (
                '{"targets": [{"key": "K", "repo": "r", "action": "generic", "tag": "t"},'
                ' {"key": "k", "repo": "r", "action": "generic", "tag": "t"}]}'
            ),
            "duplicate",
        ),
        ('{"externals": {"x": {"hosts": "h"}}}', "hosts"),
    ],
)
def test_a_bad_overrides_file_fails_naming_the_file_and_the_problem(make_cfg, body, fragment):
    cfg = make_cfg()
    cfg["resolve_overrides"].parent.mkdir(parents=True, exist_ok=True)
    cfg["resolve_overrides"].write_text(body)
    with pytest.raises(SystemExit) as e:
        atlas.load_overrides(cfg["resolve_overrides"])
    message = str(e.value)
    assert str(cfg["resolve_overrides"]) in message and fragment in message


def outbound_fixture(cfg, make_manifest):
    make_manifest(
        cfg,
        "client",
        domain="payments",
        consumes=[
            *http_consumer("https://api.vendor.com/v1"),
            *http_consumer("MONGO_URI"),
            *http_consumer("MYSTERY_URL"),
        ],
    )
    make_manifest(cfg, "client2", consumes=http_consumer("MONGO_URI"))
    write_overrides(
        cfg,
        [
            target("MONGO_URI", "client", "generic", tag="mongo", group="shared-mongo"),
            target("MONGO_URI", "client2", "generic", tag="mongo", group="shared-mongo"),
        ],
        groups={"shared-mongo": {"label": "Shared Mongo"}},
    )


def test_outbound_doc_lists_each_third_party_with_its_consumers(make_cfg, make_manifest, graph_of):
    cfg = make_cfg(internal_domains=["corp.example"])
    outbound_fixture(cfg, make_manifest)
    graph_of(cfg)
    text = (cfg["atlas_dir"] / "docs" / "outbound.md").read_text()
    assert "## api.vendor.com" in text
    assert "client" in text and "https://api.vendor.com/v1" in text
    assert "```mermaid" in text and "n_x_api_vendor_com" in text


def test_outbound_doc_exists_and_says_so_when_there_is_nothing_outbound(
    make_cfg, make_manifest, graph_of
):
    cfg = make_cfg()
    make_manifest(cfg, "client")
    graph_of(cfg)
    assert "No outbound" in (cfg["atlas_dir"] / "docs" / "outbound.md").read_text()


def test_consumes_lines_say_where_each_target_went(make_cfg, make_manifest, graph_of):
    cfg = make_cfg(internal_domains=["corp.example"])
    outbound_fixture(cfg, make_manifest)
    graph_of(cfg)
    doc = (cfg["atlas_dir"] / "docs" / "client.md").read_text()
    assert "-> external: api.vendor.com:" in doc
    assert "-> generic (mongo, shared: Shared Mongo):" in doc
    assert "-> unresolved:" in doc


def test_domain_diagram_and_index_surface_the_externals(make_cfg, make_manifest, graph_of):
    cfg = make_cfg(internal_domains=["corp.example"])
    outbound_fixture(cfg, make_manifest)
    graph_of(cfg)
    domain = (cfg["atlas_dir"] / "docs" / "domains" / "payments.md").read_text()
    assert "n_x_api_vendor_com" in domain and "n_client --" in domain
    assert "docs/outbound.md" in (cfg["atlas_dir"] / "index.md").read_text()


def test_manifest_missing_other_meta_fields_is_skipped(make_cfg, make_manifest, graph_of, capsys):
    cfg = make_cfg()
    make_manifest(cfg, "good")
    make_manifest(cfg, "partial", _meta={"commit": "a" * 40})
    g = graph_of(cfg, known={"good", "partial"})
    assert set(g["repos"]) == {"good"}
    assert "partial" in capsys.readouterr().err


def test_a_deterministic_identifier_resolves_a_consume_the_model_left_hanging(
    make_cfg, make_manifest, graph_of
):
    """The provider side of the join is what deterministic extraction is for: without the
    Kubernetes service name, "orders-api" matches nothing and the consume goes unresolved."""
    cfg = make_cfg()
    make_manifest(cfg, "orders")
    make_manifest(
        cfg,
        "web",
        consumes=[{"kind": "http", "name": "orders", "key": "orders-api", "evidence": ev()}],
    )
    assert graph_of(cfg)["unresolved"][0]["key"] == "orders-api"

    make_manifest(cfg, "orders", identifiers=["orders-api"])
    g = graph_of(cfg)
    assert [(e["from"], e["to"], e["match"]) for e in g["edges"]] == [("web", "orders", "exact")]
    assert g["unresolved"] == []


def test_a_terraform_queue_expose_matches_a_consumer_of_that_queue(
    make_cfg, make_manifest, graph_of
):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "orders",
        exposes=[
            {
                "kind": "queue",
                "name": "orders-events",
                "key": "orders-events",
                "evidence": "main.tf:2",
                "source": "deterministic",
            }
        ],
    )
    make_manifest(
        cfg,
        "billing",
        consumes=[
            {"kind": "queue", "name": "orders", "key": "orders-events", "evidence": ev("b.go")}
        ],
    )
    assert [(e["from"], e["to"], e["match"]) for e in graph_of(cfg)["edges"]] == [
        ("billing", "orders", "exact")
    ]


def test_package_consume_keyed_on_the_repo_name_resolves_at_alias_tier(
    make_cfg, make_manifest, graph_of
):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "py-sltc",
        packages={
            "publishes": [{"ecosystem": "pypi", "name": "sltc", "evidence": "setup.py"}],
            "depends_on": [],
        },
    )
    make_manifest(
        cfg,
        "client",
        consumes=[{"kind": "package", "name": "sltc", "key": "py-sltc", "evidence": ev()}],
    )
    assert [(e["from"], e["to"], e["match"]) for e in graph_of(cfg)["edges"]] == [
        ("client", "py-sltc", "alias")
    ]


def test_a_generically_named_repo_does_not_answer_a_package_consume(
    make_cfg, make_manifest, graph_of
):
    cfg = make_cfg()
    make_manifest(cfg, "api")
    make_manifest(
        cfg,
        "client",
        consumes=[{"kind": "package", "name": "api", "key": "api", "evidence": ev()}],
    )
    g = graph_of(cfg)
    assert g["edges"] == []
    assert [u["key"] for u in g["unresolved"]] == ["api"]


def test_a_package_consume_on_the_repos_own_name_makes_no_self_edge(
    make_cfg, make_manifest, graph_of
):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "py-sltc",
        consumes=[{"kind": "package", "name": "sltc", "key": "py-sltc", "evidence": ev()}],
    )
    assert graph_of(cfg)["edges"] == []


def test_an_inferred_package_dependency_never_links_above_alias(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "lib",
        packages={
            "publishes": [{"ecosystem": "pypi", "name": "py-sltc", "evidence": "setup.py"}],
            "depends_on": [],
        },
    )
    make_manifest(
        cfg,
        "app",
        packages={
            "publishes": [],
            "depends_on": [
                {
                    "ecosystem": "pypi",
                    "name": "py-sltc",
                    "evidence": "requirements.txt",
                    "inferred": True,
                }
            ],
        },
    )
    assert [(e["from"], e["to"], e["match"]) for e in graph_of(cfg)["edges"]] == [
        ("app", "lib", "alias")
    ]


def test_a_declared_package_dependency_still_links_exact(make_cfg, make_manifest, graph_of):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "lib",
        packages={
            "publishes": [{"ecosystem": "pypi", "name": "sltc", "evidence": "setup.py"}],
            "depends_on": [],
        },
    )
    make_manifest(
        cfg,
        "app",
        packages={
            "publishes": [],
            "depends_on": [{"ecosystem": "pypi", "name": "sltc", "evidence": "requirements.txt"}],
        },
    )
    assert [e["match"] for e in graph_of(cfg)["edges"]] == ["exact"]


def test_a_repo_pinning_itself_by_url_makes_no_edge_and_no_unresolved_row(
    make_cfg, make_manifest, graph_of
):
    cfg = make_cfg()
    make_manifest(
        cfg,
        "py-sltc",
        packages={
            "publishes": [{"ecosystem": "pypi", "name": "sltc", "evidence": "setup.py"}],
            "depends_on": [
                {
                    "ecosystem": "pypi",
                    "name": "py-sltc",
                    "evidence": "requirements.txt",
                    "inferred": True,
                }
            ],
        },
    )
    g = graph_of(cfg)
    assert g["edges"] == []
    assert g["unresolved"] == []


@pytest.mark.parametrize(
    "repo,dep", [("my_lib", "my-lib"), ("my.lib", "my-lib"), ("my-lib", "my_lib")]
)
def test_repo_name_alias_matches_across_pypi_name_normalisation(
    make_cfg, make_manifest, graph_of, repo, dep
):
    cfg = make_cfg()
    make_manifest(cfg, repo)
    make_manifest(
        cfg,
        "app",
        packages={
            "publishes": [],
            "depends_on": [
                {"ecosystem": "pypi", "name": atlas.norm_pkg("pypi", dep), "evidence": "r.txt"}
            ],
        },
    )
    assert [(e["from"], e["to"], e["match"]) for e in graph_of(cfg)["edges"]] == [
        ("app", repo, "alias")
    ]
