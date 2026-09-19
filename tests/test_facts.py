import json

import pytest

import atlas


def write(tmp_path, files):
    for rel, text in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return tmp_path


def idents(repo, stoplist=frozenset()):
    return set(atlas.extract_facts(repo, stoplist)["identifiers"])


def exposes(repo, stoplist=frozenset()):
    return {(e["kind"], e["key"]) for e in atlas.extract_facts(repo, stoplist)["exposes"]}


# ---------- the yaml subset parser ----------


def test_parses_nested_mappings():
    docs = atlas.parse_yaml_docs("metadata:\n  name: orders\n  labels:\n    app: orders\n")
    assert docs == [{"metadata": {"name": "orders", "labels": {"app": "orders"}}}]


def test_parses_a_sequence_of_scalars():
    assert atlas.parse_yaml_docs("apis:\n  - one\n  - two\n") == [{"apis": ["one", "two"]}]


def test_parses_a_sequence_of_mappings():
    docs = atlas.parse_yaml_docs(
        "rules:\n  - host: a.example.net\n    path: /\n  - host: b.example.net\n"
    )
    assert docs == [{"rules": [{"host": "a.example.net", "path": "/"}, {"host": "b.example.net"}]}]


@pytest.mark.parametrize(
    "text",
    [
        "rules:\n  - host: a.example.net\n    path: /\n  - host: b.example.net\n",
        "rules:\n- host: a.example.net\n  path: /\n- host: b.example.net\n",
    ],
    ids=["indented", "same-indent"],
)
def test_parses_a_sequence_of_mappings_in_either_indent_style(text):
    assert atlas.parse_yaml_docs(text) == [
        {"rules": [{"host": "a.example.net", "path": "/"}, {"host": "b.example.net"}]}
    ]


def test_a_same_indent_sequence_does_not_swallow_the_keys_after_it():
    """The style kubectl, PyYAML and kustomize emit: the dash sits at the key's own column."""
    docs = atlas.parse_yaml_docs(
        "spec:\n  ports:\n  - port: 80\n    targetPort: 8080\n  selector:\n    app: orders\n"
    )
    assert docs == [
        {"spec": {"ports": [{"port": "80", "targetPort": "8080"}], "selector": {"app": "orders"}}}
    ]


def test_a_same_indent_sequence_nested_inside_a_sequence_item():
    docs = atlas.parse_yaml_docs("- name: a\n  ports:\n  - port: 80\n- name: b\n")
    assert docs == [[{"name": "a", "ports": [{"port": "80"}]}, {"name": "b"}]]


def test_a_sequence_item_where_a_key_is_expected_is_rejected():
    assert atlas.parse_yaml_docs("name: orders\n- host: a.example.net\n") == []


def test_splits_documents_on_markers():
    docs = atlas.parse_yaml_docs("kind: Service\n---\nkind: Ingress\n...\n")
    assert docs == [{"kind": "Service"}, {"kind": "Ingress"}]


def test_strips_comments_but_keeps_hashes_inside_quotes():
    docs = atlas.parse_yaml_docs('# leading\nname: orders  # trailing\ntag: "a#b"\n')
    assert docs == [{"name": "orders", "tag": "a#b"}]


@pytest.mark.parametrize(
    "text,want",
    [
        ("apis: [one, two]", {"apis": ["one", "two"]}),
        ("apis: ['one', \"two\"]", {"apis": ["one", "two"]}),
        ("apis: []", {"apis": []}),
        ("labels: {app: orders, tier: web}", {"labels": {"app": "orders", "tier": "web"}}),
    ],
)
def test_parses_flow_collections(text, want):
    assert atlas.parse_yaml_docs(text) == [want]


def test_block_scalars_do_not_swallow_the_next_key():
    docs = atlas.parse_yaml_docs("script: |\n  line one\n  key: not-a-key\nname: orders\n")
    assert docs[0]["name"] == "orders"
    assert "line one" in docs[0]["script"]


def test_unparseable_input_yields_no_documents_rather_than_raising():
    assert atlas.parse_yaml_docs("  - stray\nname: orders\n\tkey: tabbed\n") == []


# ---------- codeowners ----------


def test_codeowners_prefers_the_catch_all_rule(tmp_path):
    write(
        tmp_path,
        {"CODEOWNERS": "# who\n/docs/ @org/docs-team\n*  @org/platform  alice@example.com\n"},
    )
    assert atlas.extract_facts(tmp_path)["owners"] == ["@org/platform", "alice@example.com"]


def test_codeowners_falls_back_to_the_first_rule(tmp_path):
    write(tmp_path, {"CODEOWNERS": "/src/ @org/platform\n/docs/ @org/docs-team\n"})
    assert atlas.extract_facts(tmp_path)["owners"] == ["@org/platform"]


@pytest.mark.parametrize("rel", ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"])
def test_codeowners_is_found_in_every_supported_location(tmp_path, rel):
    write(tmp_path, {rel: "* @org/platform\n"})
    assert atlas.extract_facts(tmp_path)["owners"] == ["@org/platform"]


def test_gitlab_section_headers_are_not_mistaken_for_rules(tmp_path):
    write(tmp_path, {"CODEOWNERS": "[Platform]\n* @org/platform\n"})
    assert atlas.extract_facts(tmp_path)["owners"] == ["@org/platform"]


# ---------- catalog-info ----------

CATALOG = """apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: orders-service
spec:
  type: service
  owner: group:default/team-orders
  providesApis: [orders-api, orders-events]
"""


def test_catalog_info_yields_the_entity_name_its_apis_and_its_owner(tmp_path):
    write(tmp_path, {"catalog-info.yaml": CATALOG})
    got = atlas.extract_facts(tmp_path)
    assert set(got["identifiers"]) == {"orders-service", "orders-api", "orders-events"}
    assert got["owners"] == ["group:default/team-orders"]


def test_catalog_info_reads_every_document_in_the_file(tmp_path):
    write(
        tmp_path,
        {
            "catalog-info.yml": CATALOG
            + "---\napiVersion: backstage.io/v1alpha1\nkind: Component\nmetadata:\n  name: orders-worker\n"
        },
    )
    assert "orders-worker" in idents(tmp_path)


# ---------- kubernetes ----------


def test_kubernetes_service_yields_its_name_the_cluster_form_and_an_expose(tmp_path):
    write(
        tmp_path,
        {
            "k8s/svc.yaml": "apiVersion: v1\nkind: Service\nmetadata:\n"
            "  name: orders-api\n  namespace: shop\nspec:\n  ports:\n    - port: 80\n"
        },
    )
    got = atlas.extract_facts(tmp_path)
    assert set(got["identifiers"]) == {"orders-api", "orders-api.shop.svc.cluster.local"}
    assert [(e["kind"], e["key"], e["evidence"]) for e in got["exposes"]] == [
        ("http", "orders-api", "k8s/svc.yaml")
    ]


def test_kubernetes_ingress_yields_every_rule_host(tmp_path):
    write(
        tmp_path,
        {
            "deploy/ing.yaml": "apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n"
            "  name: orders\nspec:\n  rules:\n    - host: orders.example.net\n"
            "    - host: orders-admin.example.net\n"
        },
    )
    assert idents(tmp_path) >= {"orders.example.net", "orders-admin.example.net"}
    assert exposes(tmp_path) == {
        ("http", "orders.example.net"),
        ("http", "orders-admin.example.net"),
    }


K8S_INDENTED = (
    "apiVersion: v1\nkind: Service\nmetadata:\n  name: orders-api\nspec:\n  ports:\n"
    "    - port: 80\n      targetPort: 8080\n"
    "---\napiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n  name: orders\nspec:\n"
    "  rules:\n    - host: orders.example.net\n      http:\n        paths:\n          - path: /\n"
)
K8S_SAME_INDENT = (
    "apiVersion: v1\nkind: Service\nmetadata:\n  name: orders-api\nspec:\n  ports:\n"
    "  - port: 80\n    targetPort: 8080\n"
    "---\napiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n  name: orders\nspec:\n"
    "  rules:\n  - host: orders.example.net\n    http:\n      paths:\n      - path: /\n"
)


@pytest.mark.parametrize("text", [K8S_INDENTED, K8S_SAME_INDENT], ids=["indented", "same-indent"])
def test_kubernetes_manifests_are_read_in_either_sequence_style(tmp_path, text):
    """`kubectl get -o yaml`, PyYAML and kustomize put the dash at the parent key's column."""
    write(tmp_path, {"k8s/all.yaml": text})
    assert idents(tmp_path) == {"orders-api", "orders.example.net"}
    assert exposes(tmp_path) == {("http", "orders-api"), ("http", "orders.example.net")}


@pytest.mark.parametrize(
    "apis",
    [
        "  providesApis:\n    - orders-api\n    - orders-events\n",
        "  providesApis:\n  - orders-api\n  - orders-events\n",
    ],
    ids=["indented", "same-indent"],
)
def test_catalog_info_apis_are_read_in_either_sequence_style(tmp_path, apis):
    write(
        tmp_path,
        {"catalog-info.yaml": "kind: Component\nmetadata:\n  name: orders\nspec:\n" + apis},
    )
    assert idents(tmp_path) == {"orders", "orders-api", "orders-events"}


def test_a_yaml_document_without_a_kind_is_not_read_as_kubernetes(tmp_path):
    write(tmp_path, {"k8s/kustomization.yaml": "resources:\n  - svc.yaml\n"})
    assert atlas.extract_facts(tmp_path)["identifiers"] == []


def test_helm_templates_are_skipped(tmp_path):
    write(
        tmp_path,
        {
            "charts/orders/templates/svc.yaml": "apiVersion: v1\nkind: Service\nmetadata:\n"
            '  name: {{ include "orders.fullname" . }}\n'
        },
    )
    assert atlas.extract_facts(tmp_path)["identifiers"] == []


def test_chart_yaml_yields_the_chart_name(tmp_path):
    write(
        tmp_path,
        {"charts/orders/Chart.yaml": "apiVersion: v2\nname: orders-chart\nversion: 1.0.0\n"},
    )
    assert idents(tmp_path) == {"orders-chart"}


# ---------- docker compose ----------


def test_compose_yields_only_services_built_from_this_repo(tmp_path):
    write(
        tmp_path,
        {
            "docker-compose.yml": "services:\n  orders-web:\n    build: ./web\n"
            "  cache:\n    image: redis:7\n"
        },
    )
    assert idents(tmp_path) == {"orders-web"}


# ---------- openapi ----------


def test_openapi_yields_server_hosts_and_an_identifier_shaped_title(tmp_path):
    write(
        tmp_path,
        {
            "openapi.yaml": "openapi: 3.0.0\ninfo:\n  title: orders-api\nservers:\n"
            "  - url: https://orders.internal.example.net/v1\n  - url: http://localhost:8080\n"
        },
    )
    got = atlas.extract_facts(tmp_path)
    assert set(got["identifiers"]) == {"orders-api", "orders.internal.example.net"}
    assert exposes(tmp_path) == {("http", "orders.internal.example.net")}


def test_openapi_ignores_a_prose_title(tmp_path):
    write(tmp_path, {"openapi.yaml": "openapi: 3.0.0\ninfo:\n  title: The Orders API\n"})
    assert atlas.extract_facts(tmp_path)["identifiers"] == []


def test_openapi_json_is_read_too(tmp_path):
    write(
        tmp_path,
        {
            "swagger.json": json.dumps(
                {
                    "info": {"title": "orders-api"},
                    "servers": [{"url": "https://orders.example.net"}],
                }
            )
        },
    )
    assert idents(tmp_path) == {"orders-api", "orders.example.net"}


# ---------- proto ----------


def test_proto_yields_the_fully_qualified_service_name_with_its_line(tmp_path):
    write(
        tmp_path,
        {
            "api/orders.proto": 'syntax = "proto3";\n\npackage shop.orders.v1;\n\n'
            "service OrderService {\n  rpc Get(GetReq) returns (GetResp);\n}\n"
        },
    )
    got = atlas.extract_facts(tmp_path)
    assert got["identifiers"] == ["shop.orders.v1.OrderService"]
    assert [(e["kind"], e["key"], e["evidence"]) for e in got["exposes"]] == [
        ("grpc", "shop.orders.v1.OrderService", "api/orders.proto:5")
    ]


def test_proto_without_a_package_is_skipped(tmp_path):
    write(tmp_path, {"api/orders.proto": "service OrderService {\n}\n"})
    assert atlas.extract_facts(tmp_path)["identifiers"] == []


# ---------- terraform ----------

TF = """resource "aws_sqs_queue" "orders" {
  name = "orders-events"
}

resource "aws_s3_bucket" "docs" {
  bucket = "org-order-documents"
}

resource "aws_dynamodb_table" "sessions" {
  name = "order-sessions"
}

resource "aws_sqs_queue" "interpolated" {
  name = "${var.env}-orders"
}
"""


def test_terraform_yields_owned_stores_and_queues_but_skips_interpolated_names(tmp_path):
    write(tmp_path, {"main.tf": TF})
    got = atlas.extract_facts(tmp_path)
    assert [(e["kind"], e["key"]) for e in got["exposes"]] == [("queue", "orders-events")]
    assert {(d["kind"], d["name"], d["access"]) for d in got["datastores"]} == {
        ("s3", "org-order-documents", "owner"),
        ("dynamodb", "order-sessions", "owner"),
    }


# ---------- shared rules ----------


def test_every_deterministic_item_is_marked_as_such(tmp_path):
    write(tmp_path, {"main.tf": TF})
    got = atlas.extract_facts(tmp_path)
    assert got["exposes"] and got["datastores"]
    for item in got["exposes"] + got["datastores"]:
        assert item["source"] == "deterministic"


@pytest.mark.parametrize(
    "rel,text,want",
    [
        (
            "catalog-info.yaml",
            "apiVersion: b/v1\nkind: Component\nmetadata:\n  name: orders\n"
            "spec:\n  providesApis: orders-api\n",
            ["orders"],
        ),
        ("openapi.yaml", "openapi: 3.0.0\nservers: https://orders.example.net\n", []),
        (
            "k8s/ing.yaml",
            "apiVersion: networking.k8s.io/v1\nkind: Ingress\nspec:\n  rules: orders.example.net\n",
            [],
        ),
    ],
)
def test_a_list_field_written_as_a_scalar_is_ignored_not_iterated(tmp_path, rel, text, want):
    """A hand-edited manifest writes these as a plain scalar often enough to matter. Iterating one
    yields its characters, and nothing in the graph should come of that."""
    write(tmp_path, {rel: text})
    got = atlas.extract_facts(tmp_path)
    assert got["identifiers"] == want
    assert got["exposes"] == []


def test_one_name_reached_by_two_sources_is_listed_once(tmp_path):
    """An ingress host is usually also the openapi server url; two entries for it would only
    double up in the docs, since the graph dedupes edges by key anyway."""
    write(
        tmp_path,
        {
            "k8s/ing.yaml": "apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n"
            "  name: orders\nspec:\n  rules:\n    - host: orders.example.net\n",
            "openapi.yaml": "openapi: 3.0.0\nservers:\n  - url: https://orders.example.net/v1\n",
        },
    )
    got = atlas.extract_facts(tmp_path)
    assert [(e["kind"], e["key"]) for e in got["exposes"]] == [("http", "orders.example.net")]


def test_generic_identifiers_are_filtered_out(tmp_path):
    write(
        tmp_path,
        {
            "docker-compose.yml": "services:\n  api:\n    build: .\n  orders-web:\n    build: ./web\n",
            "k8s/svc.yaml": "apiVersion: v1\nkind: Service\nmetadata:\n  name: gateway\n",
        },
    )
    assert idents(tmp_path, atlas.GENERIC_IDENTIFIERS) == {"orders-web"}


def test_a_generic_identifier_is_also_dropped_from_the_exposes_it_would_key(tmp_path):
    write(tmp_path, {"k8s/svc.yaml": "apiVersion: v1\nkind: Service\nmetadata:\n  name: gateway\n"})
    assert atlas.extract_facts(tmp_path, atlas.GENERIC_IDENTIFIERS)["exposes"] == []


def test_evidence_is_repo_relative_so_the_evidence_gate_accepts_it(tmp_path):
    write(tmp_path, {"k8s/svc.yaml": "apiVersion: v1\nkind: Service\nmetadata:\n  name: orders\n"})
    items = atlas.extract_facts(tmp_path)["exposes"]
    assert items
    for item in items:
        assert atlas.evidence_ok(tmp_path, item["evidence"])


def test_a_file_that_is_not_yaml_does_not_stop_the_scan(tmp_path):
    write(tmp_path, {"a.tf": 'resource "aws_sqs_queue" "q" {\n  name = "orders-events"\n}\n'})
    (tmp_path / "k8s").mkdir()
    (tmp_path / "k8s" / "svc.yaml").write_bytes(b"\xff\xfe not yaml at all\n\tindent\n")
    got = atlas.extract_facts(tmp_path)
    assert [e["key"] for e in got["exposes"]] == ["orders-events"]


# ---------- merge_facts ----------


def base_manifest(**fields):
    m = {
        "identifiers": [],
        "owners": [],
        "exposes": [],
        "consumes": [],
        "datastores": [],
    }
    m.update(fields)
    return m


def facts(**fields):
    f = {"identifiers": [], "exposes": [], "datastores": [], "owners": []}
    f.update(fields)
    return f


def test_merge_appends_deterministic_identifiers_without_duplicating(tmp_path):
    m = base_manifest(identifiers=["orders-service", "Orders-API"])
    atlas.merge_facts(m, facts(identifiers=["orders-api", "orders-worker"]), {})
    assert m["identifiers"] == ["orders-service", "Orders-API", "orders-worker"]


def test_deterministic_owners_replace_the_models_answer():
    m = base_manifest(owners=["@guessed/team"])
    atlas.merge_facts(m, facts(owners=["@org/platform"]), {})
    assert m["owners"] == ["@org/platform"]


def test_the_models_owners_stand_when_nothing_deterministic_was_found():
    m = base_manifest(owners=["@guessed/team"])
    atlas.merge_facts(m, facts(), {})
    assert m["owners"] == ["@guessed/team"]


def test_deterministic_exposes_win_on_a_key_collision_but_keep_the_readable_label():
    m = base_manifest(
        exposes=[
            {
                "kind": "http",
                "name": "Orders API",
                "key": "orders-api",
                "detail": "REST, bearer auth",
                "evidence": "README.md:3",
            }
        ]
    )
    det = {
        "kind": "http",
        "name": "orders-api",
        "key": "orders-api",
        "detail": "",
        "evidence": "k8s/svc.yaml",
        "source": "deterministic",
    }
    atlas.merge_facts(m, facts(exposes=[det]), {})
    assert len(m["exposes"]) == 1
    kept = m["exposes"][0]
    assert kept["source"] == "deterministic"
    assert kept["evidence"] == "k8s/svc.yaml"
    assert (kept["name"], kept["detail"]) == ("Orders API", "REST, bearer auth")


def test_a_model_expose_on_a_different_key_survives_the_merge():
    m = base_manifest(
        exposes=[{"kind": "http", "name": "health", "key": "orders-health", "evidence": "a.go"}]
    )
    det = {
        "kind": "http",
        "name": "o",
        "key": "orders-api",
        "evidence": "b.yaml",
        "source": "deterministic",
    }
    atlas.merge_facts(m, facts(exposes=[det]), {})
    assert {e["key"] for e in m["exposes"]} == {"orders-health", "orders-api"}


def test_last_runs_deterministic_items_are_replaced_rather_than_duplicated():
    stale = {
        "kind": "http",
        "name": "o",
        "key": "orders-api",
        "evidence": "old.yaml",
        "source": "deterministic",
    }
    m = base_manifest(identifiers=["orders-api"], owners=["@org/platform"], exposes=[stale])
    prev = {"deterministic": {"identifiers": ["orders-api"], "owners": ["@org/platform"]}}
    fresh = {
        "kind": "http",
        "name": "o",
        "key": "orders-api",
        "evidence": "new.yaml",
        "source": "deterministic",
    }
    atlas.merge_facts(
        m,
        facts(identifiers=["orders-api"], owners=["@org/platform"], exposes=[fresh]),
        prev,
    )
    assert m["identifiers"] == ["orders-api"]
    assert m["owners"] == ["@org/platform"]
    assert [e["evidence"] for e in m["exposes"]] == ["new.yaml"]


def test_a_deleted_source_file_takes_its_identifier_with_it():
    m = base_manifest(identifiers=["orders-api", "orders-service"], owners=["@org/platform"])
    prev = {"deterministic": {"identifiers": ["orders-api"], "owners": ["@org/platform"]}}
    atlas.merge_facts(m, facts(), prev)
    assert m["identifiers"] == ["orders-service"]
    assert m["owners"] == []


def test_deterministic_datastores_replace_a_colliding_model_entry():
    m = base_manifest(
        datastores=[{"kind": "s3", "name": "org-docs", "access": "read", "evidence": "a.go"}]
    )
    det = {
        "kind": "s3",
        "name": "org-docs",
        "access": "owner",
        "evidence": "main.tf:2",
        "source": "deterministic",
    }
    atlas.merge_facts(m, facts(datastores=[det]), {})
    assert [(d["access"], d["evidence"]) for d in m["datastores"]] == [("owner", "main.tf:2")]
