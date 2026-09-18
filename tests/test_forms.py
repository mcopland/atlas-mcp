import atlas


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
