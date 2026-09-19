import pytest

import atlas


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://github.com/org/orders.git", "https://github.com/org/orders"),
        ("https://github.com/org/orders", "https://github.com/org/orders"),
        ("https://x-token:abc123@github.com/org/orders.git", "https://github.com/org/orders"),
        # the form GitHub documents for token clones: a userinfo with no password part
        ("https://ghp_abc123@github.com/org/orders.git", "https://github.com/org/orders"),
        ("https://user@ghe.corp/org/orders", "https://ghe.corp/org/orders"),
        ("git@github.com:org/orders.git", "https://github.com/org/orders"),
        ("ssh://git@ghe.corp:2222/org/orders.git", "https://ghe.corp:2222/org/orders"),
        ("git://ghe.corp/org/orders.git", "https://ghe.corp/org/orders"),
        ("http://ghe.corp/org/orders.git/", "http://ghe.corp/org/orders"),
        ("/srv/mirrors/orders.git", "/srv/mirrors/orders.git"),
        ("file:///srv/mirrors/orders.git", "file:///srv/mirrors/orders.git"),
        ("", ""),
    ],
)
def test_normalize_remote_yields_one_credential_free_form(raw, expected):
    assert atlas.normalize_remote(raw) == expected


def test_svc_forms_strips_scheme_port_path_and_credentials():
    assert atlas.svc_forms("https://payments-api.svc.cluster.local:8080/v1/x") == (
        "payments-api.svc.cluster.local",
        "payments-api",
    )
    assert atlas.svc_forms("postgres://u:p@orders-db.internal:5432/mydb") == (
        "orders-db.internal",
        "orders-db",
    )
    assert atlas.svc_forms("orders") == ("orders", "orders")


def test_envvar_forms_yields_service_and_bare_stem():
    assert atlas.envvar_forms("ORDERS_SERVICE_URL") == ["orders-service", "orders"]
    assert atlas.envvar_forms("BILLING_HOST") == ["billing"]
    assert atlas.envvar_forms("SEARCH_API_ENDPOINT") == ["search-api", "search"]


def test_envvar_forms_ignores_non_envvar_keys():
    assert atlas.envvar_forms("orders-service") == []
    assert atlas.envvar_forms("https://x.internal") == []
    assert atlas.envvar_forms("") == []


def test_alias_ok_rejects_short_and_generic_forms():
    stop = atlas.GENERIC_IDENTIFIERS
    assert atlas.alias_ok("orders-service", stop)
    assert not atlas.alias_ok("db", stop)
    assert not atlas.alias_ok("api", stop)
    assert not atlas.alias_ok("gateway", stop)
