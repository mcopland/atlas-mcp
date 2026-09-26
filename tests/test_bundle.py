import json
import shutil

import pytest

import atlas


@pytest.mark.parametrize(
    "line, expected",
    [
        ("DB_PASSWORD=hunter2", "DB_PASSWORD=<redacted>"),
        ("export API_KEY='abc123'", "export API_KEY=<redacted>"),
        ("  aws_secret_access_key: AKIAxyz", "  aws_secret_access_key: <redacted>"),
        ('"token": "abc",', '"token": <redacted>'),
        ("PRIVATE_KEY_PATH=/etc/key.pem", "PRIVATE_KEY_PATH=<redacted>"),
        ("SECRET=", "SECRET="),
        (
            "postgres://app:s3cret@db.internal:5432/app",
            "postgres://<redacted>@db.internal:5432/app",
        ),
        ('url = "https://user:pw@host/path"', 'url = "https://<redacted>@host/path"'),
        ('KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCD"', 'KEY = "<redacted>"'),
        (
            "stripe = 'sk_live_" + "51H8xk2eZvKYlo2C0abcdefghijklmnop'",
            "stripe = '<redacted>'",
        ),
        (
            "ORDERS_SERVICE_URL=http://orders.internal:8080",
            "ORDERS_SERVICE_URL=http://orders.internal:8080",
        ),
        ("PASSWORD_MIN_LENGTH_DOC=see docs", "PASSWORD_MIN_LENGTH_DOC=<redacted>"),
        (
            'name = "a-very-long-package-name-with-only-letters-in-it"',
            'name = "a-very-long-package-name-with-only-letters-in-it"',
        ),
        (
            'mod = "github.com/org/some-module-name-that-is-quite-long-v2"',
            'mod = "github.com/org/some-module-name-that-is-quite-long-v2"',
        ),
        ("plain line of code", "plain line of code"),
        ("aws_key_id = AKIAIOSFODNN7EXAMPLE", "aws_key_id = <redacted>"),
        ("  accessKeyId: AKIAIOSFODNN7EXAMPLE", "  accessKeyId: <redacted>"),
        ("# example: ghp_" + "a" * 36, "# example: <redacted>"),
        ("pat = github_pat_" + "a" * 22 + "_" + "b" * 59, "pat = <redacted>"),
        ("slack = xoxb-123456789012-abcdefghijkl", "slack = <redacted>"),
        ("google = AIza" + "b" * 35, "google = <redacted>"),
        (
            (
                "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
                ".dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
            ),
            "Bearer <redacted>",
        ),
        (
            (
                'hook = "https://hooks.slack.com/services/'
                + 'T00000000/B00000000/abcdefghijklmnopqrstuvwx"'
            ),
            'hook = "<redacted>"',
        ),
        (
            "commit 9e1f2a3b4c5d6e7f8091a2b3c4d5e6f708192a3b",
            "commit 9e1f2a3b4c5d6e7f8091a2b3c4d5e6f708192a3b",
        ),
        ("lowercase akiaiosfodnn7example", "lowercase akiaiosfodnn7example"),
        (
            'hook = "https://hooks.example.com/services/team/channel"',
            'hook = "https://hooks.example.com/services/team/channel"',
        ),
        ("DB_PWD=hunter2", "DB_PWD=<redacted>"),
        (
            "DB_CONNECTION_STRING=Server=db.internal;Password=x",
            "DB_CONNECTION_STRING=Server=db.internal;Password=<redacted>",
        ),
        (
            'dsn := "postgres://db.internal:5432/orders"',
            'dsn := "postgres://db.internal:5432/orders"',
        ),
        ("storage_account_key: abc", "storage_account_key: <redacted>"),
        (
            'conn = "Server=db.internal;User Id=app;Password=hunter2;"',
            'conn = "Server=db.internal;User Id=app;Password=<redacted>;"',
        ),
        (
            "conn = DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=abc+def/ghi==;",
            "conn = DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=<redacted>;",
        ),
        ("# token glpat-" + "a" * 20, "# token <redacted>"),
        ("//registry/:_authToken=npm_" + "a" * 36, "//registry/:_authToken=<redacted>"),
        ("stripe: sk_live_" + "a" * 24, "stripe: <redacted>"),
        ("restricted: rk_live_" + "a" * 24, "restricted: <redacted>"),
        ("anthropic: sk-ant-api03-" + "a" * 40, "anthropic: <redacted>"),
        ("openai: sk-proj-" + "a" * 40, "openai: <redacted>"),
        (
            "PASSPORT_SERVICE_URL=http://passport.internal",
            "PASSPORT_SERVICE_URL=http://passport.internal",
        ),
        ("AUTH_SERVICE_URL=http://auth.internal", "AUTH_SERVICE_URL=http://auth.internal"),
        (
            "npm_config_registry=https://registry.internal",
            "npm_config_registry=https://registry.internal",
        ),
    ],
)
def test_redact_masks_secret_values_and_leaves_other_lines_alone(line, expected):
    assert atlas.redact(line) == expected


def test_redact_masks_a_private_key_block():
    text = (
        "key: |\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxLpuLzPtPPUQmVNQWsG\n"
        "9fZLpFNnaKcPQHHkiM0rqLNEEoZWFLbTfKA\n"
        "-----END RSA PRIVATE KEY-----\n"
        'url = "https://orders.internal"\n'
    )
    got = atlas.redact(text)
    assert "MIIEowIBAAKCAQEAxLpuLzPtPPUQmVNQWsG" not in got
    assert "9fZLpFNnaKcPQHHkiM0rqLNEEoZWFLbTfKA" not in got
    assert "-----BEGIN RSA PRIVATE KEY-----" in got
    assert "-----END RSA PRIVATE KEY-----" in got
    assert '"https://orders.internal"' in got


def test_redact_masks_a_pgp_private_key_block():
    text = (
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
        "lQOYBF0Ab0kBCAC7pgpbodymarker\n"
        "-----END PGP PRIVATE KEY BLOCK-----\n"
    )
    got = atlas.redact(text)
    assert "lQOYBF0Ab0kBCAC7pgpbodymarker" not in got
    assert "-----BEGIN PGP PRIVATE KEY BLOCK-----" in got


def test_redact_masks_a_private_key_block_the_file_cuts_short():
    text = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU\n"
    got = atlas.redact(text)
    assert "b3BlbnNzaC1rZXktdjEAAAAABG5vbmU" not in got
    assert "<redacted>" in got


def test_redact_stays_linear_on_a_long_single_line(monkeypatch):
    """A minified bundle or a one-line JSON blob is one enormous token. A pattern that can start
    at every character and backtrack turns that into minutes of CPU per repo."""
    import time

    text = json.dumps(
        {
            "x": "y" * 200_000,
            "jwt": "eyJnope " * 25_000,
            "aws": "AKIAnope " * 25_000,
            "gh": "ghp_nope " * 25_000,
            "pem": "-----BEGIN nope " * 12_500,
        }
    )
    start = time.monotonic()
    atlas.redact(text)
    assert time.monotonic() - start < 2.0


def test_redact_handles_multiline_text():
    text = "A=1\nTOKEN=abc\nB=2\n"
    assert atlas.redact(text) == "A=1\nTOKEN=<redacted>\nB=2\n"


def test_prompt_hash_is_short_hex_and_stable():
    a, b = atlas.prompt_hash("explore"), atlas.prompt_hash("explore")
    assert a == b
    assert len(a) == 12
    int(a, 16)


def test_prompt_hash_changes_when_a_template_changes(tmp_path, monkeypatch):
    kit = tmp_path / "kit"
    shutil.copytree(atlas.KIT / "prompts", kit / "prompts")
    monkeypatch.setattr(atlas, "KIT", kit)
    before = atlas.prompt_hash("explore")
    (kit / "prompts" / "update.md").write_text("changed\n", encoding="utf-8")
    assert atlas.prompt_hash("explore") != before


def test_prompt_hash_changes_when_the_schema_changes(tmp_path, monkeypatch):
    kit = tmp_path / "kit"
    shutil.copytree(atlas.KIT / "prompts", kit / "prompts")
    monkeypatch.setattr(atlas, "KIT", kit)
    before = atlas.prompt_hash("explore")
    (kit / "prompts" / "schema.json").write_text("{}\n", encoding="utf-8")
    assert atlas.prompt_hash("explore") != before


def test_prompt_hash_rejects_an_unknown_mode():
    with pytest.raises(KeyError):
        atlas.prompt_hash("telepathy")


def test_build_prompt_does_not_expand_a_placeholder_hiding_in_a_value(make_cfg):
    """Substitution runs in one pass over the template text: a repo file that happens to
    contain a literal {{SCHEMA}} or {{MANIFEST}} must not get expanded just because it was
    dropped into the BUNDLE or MANIFEST value."""
    cfg = make_cfg()
    text = atlas.build_prompt(cfg, "bundle_full.md", BUNDLE="a repo file says {{SCHEMA}} here")
    assert "a repo file says {{SCHEMA}} here" in text


def test_build_prompt_substitutes_every_known_placeholder(make_cfg):
    cfg = make_cfg()
    text = atlas.build_prompt(
        cfg,
        "bundle_update.md",
        BUNDLE="B",
        OLD_COMMIT="a",
        NEW_COMMIT="b",
        CHANGED_FILES="f.go",
        MANIFEST="{}",
    )
    assert "{{" not in text


BUDGET = 64 * 1024


def bundle(repo, budget=BUDGET, only=None):
    return atlas.gather_bundle(repo, budget, only=only)


def test_the_bundle_lists_the_tree_and_skips_vendored_and_dot_directories(make_repo):
    repo = make_repo(
        "svc",
        files={
            "main.go": "package main",
            "internal/store/db.go": "package store",
            "node_modules/left-pad/index.js": "module.exports = 1",
            ".venv/lib/thing.py": "x = 1",
        },
    )
    text = bundle(repo)
    assert "main.go" in text
    assert "internal/store/db.go" in text
    assert "left-pad" not in text
    assert ".venv" not in text


def test_the_bundle_lists_tracked_files_and_ignores_untracked_build_output(make_repo):
    repo = make_repo(
        "svc",
        files={"main.go": "package main", ".gitignore": "generated.go\n"},
    )
    (repo / "out").mkdir()
    (repo / "out" / "app.js").write_text('u := "https://untracked.internal"')
    (repo / "generated.go").write_text('u := "https://ignored.internal"')
    text = bundle(repo)
    assert "main.go" in text
    assert "out/app.js" not in text
    assert "untracked.internal" not in text
    assert "generated.go" not in text
    assert "ignored.internal" not in text


def test_the_bundle_falls_back_to_walking_a_path_that_is_not_a_git_repo(tmp_path):
    repo = tmp_path / "plain"
    (repo / "internal").mkdir(parents=True)
    (repo / "main.go").write_text("package main")
    (repo / "internal" / "db.go").write_text('u := "https://orders.internal"')
    text = bundle(repo)
    assert "main.go" in text
    assert "internal/db.go" in text
    assert "orders.internal" in text


def test_a_tracked_file_below_the_walk_depth_is_left_out(make_repo):
    repo = make_repo(
        "svc",
        files={
            "a/b/c/d/shallow.go": 'u := "https://shallow.internal"',
            "a/b/c/d/e/deep.go": 'u := "https://deep.internal"',
        },
    )
    text = bundle(repo)
    assert "shallow.internal" in text
    assert "deep.go" not in text
    assert "deep.internal" not in text


def test_a_tracked_submodule_is_not_read_as_a_file(make_repo, capsys):
    repo = make_repo("svc", files={"main.go": "package main"})
    head = atlas.git(repo, "rev-parse", "HEAD")
    atlas.git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},vendored-sub")
    text = bundle(repo)
    assert "main.go" in text
    assert "vendored-sub" not in text
    assert "warn:" not in capsys.readouterr().err


def test_a_tracked_path_that_is_not_utf8_does_not_fail_the_repo(make_repo):
    """git prints paths as the bytes they are. One undecodable name must not take the whole
    listing, and with it the repo, down: it is skipped like any path that cannot be read. The
    surrogate in the name reaches git as the raw byte 0xe9, with no file on disk to match."""
    repo = make_repo("svc", files={"main.go": "package main"})
    blob = atlas.git(repo, "rev-parse", "HEAD:main.go")
    atlas.git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},caf\udce9.go")
    text = bundle(repo)
    assert "main.go" in text
    assert "caf" not in text


def test_the_tree_is_capped(make_repo, monkeypatch):
    files = {f"pkg/f{i}.go": "package pkg" for i in range(30)}
    repo = make_repo("svc", files=files)
    monkeypatch.setattr(atlas, "BUNDLE_TREE_MAX", 5)
    tree = bundle(repo).split("## Files")[0]
    assert tree.count("\n- ") <= 5
    assert "more" in tree


def test_key_files_appear_in_priority_order(make_repo):
    repo = make_repo(
        "svc",
        files={
            "README.md": "# svc\nthe orders service",
            "package.json": '{"name": "@org/svc"}',
            "Dockerfile": "FROM golang:1.22",
            ".env.example": "ORDERS_URL=http://orders.internal",
            "openapi.yaml": "openapi: 3.0.0",
        },
    )
    text = bundle(repo)
    names = ("README.md", "package.json", "Dockerfile", ".env.example", "openapi.yaml")
    order = [text.index(f"### {n}") for n in names]
    assert order == sorted(order)


@pytest.mark.parametrize(
    "rel",
    [
        "README.md",
        "CODEOWNERS",
        ".github/CODEOWNERS",
        "catalog-info.yaml",
        "go.mod",
        "pyproject.toml",
        "pom.xml",
        "Dockerfile",
        "docker-compose.yml",
        "k8s/deployment.yaml",
        "helm/values.yaml",
        "main.tf",
        ".env.example",
        "openapi.yaml",
        "api/orders.proto",
        "schema.graphql",
        "main.go",
        "cmd/server/main.go",
        "src/index.ts",
    ],
)
def test_every_key_file_kind_is_excerpted(make_repo, rel):
    repo = make_repo("svc", files={rel: "the file body marker"})
    text = bundle(repo)
    assert f"### {rel}" in text
    assert "the file body marker" in text


def test_a_source_file_that_is_not_a_key_file_is_not_excerpted(make_repo):
    repo = make_repo("svc", files={"internal/store/db.go": "package store // body marker"})
    assert "### internal/store/db.go" not in bundle(repo)


def test_tracked_files_is_not_truncated_at_bundle_max_files(make_repo, monkeypatch):
    """The cap used to cut the candidate list itself, alphabetically, so a monorepo past it
    could never have its later files excerpted or scanned at all. Bounding what gets read is
    excerpt_blocks' and signal_sections' job now, not tracked_files'."""
    monkeypatch.setattr(atlas, "BUNDLE_MAX_FILES", 2)
    repo = make_repo("svc", files={f"filler{i}.txt": "x" for i in range(5)})
    assert len(atlas.tracked_files(repo)) == 5


def test_a_key_file_sorting_after_the_file_cap_still_reaches_the_excerpts(make_repo, monkeypatch):
    monkeypatch.setattr(atlas, "BUNDLE_MAX_FILES", 2)
    files = {f"aaa_filler{i}.txt": "x" for i in range(4)}
    files["zzz/package.json"] = json.dumps({"name": "late-package-marker"})
    repo = make_repo("svc", files=files)
    text = bundle(repo)
    assert "### zzz/package.json" in text
    assert "late-package-marker" in text


def test_an_excerpt_is_capped_per_file(make_repo, monkeypatch):
    repo = make_repo("svc", files={"main.go": "package main\n" + ("// filler\n" * 4000)})
    monkeypatch.setattr(atlas, "BUNDLE_FILE_BYTES", 500)
    body = bundle(repo).split("### main.go\n", 1)[1]
    assert len(body.split("###")[0].encode()) < 700
    assert "truncated" in body


def test_a_readme_is_capped_more_tightly_than_other_files(make_repo):
    assert atlas.BUNDLE_README_BYTES < atlas.BUNDLE_FILE_BYTES


def test_a_huge_file_is_skipped_entirely(make_repo, monkeypatch):
    repo = make_repo("svc", files={"main.go": "package main\n" + ("// x\n" * 200_000)})
    assert (repo / "main.go").stat().st_size > atlas.BUNDLE_MAX_FILE_BYTES
    assert "### main.go" not in bundle(repo)


@pytest.mark.parametrize(
    "category, line, expected",
    [
        ("url", 'u := "https://orders.internal:8080/v1"', "orders.internal"),
        ("env", 'os.Getenv("ORDERS_SERVICE_URL")', "ORDERS_SERVICE_URL"),
        ("env", "process.env.PAYMENTS_TOPIC", "PAYMENTS_TOPIC"),
        ("env", 'os.environ["EVENTS_QUEUE"]', "EVENTS_QUEUE"),
        ("messaging", 'producer.publish("order.created", payload)', "order.created"),
        ("messaging", 'kafka.NewReader(Topic: "billing-events")', "billing-events"),
        ("datastore", 'dsn := "postgres://db.internal:5432/orders"', "db.internal"),
        ("datastore", 'redis.NewClient(Addr: "cache.internal:6379")', "cache.internal"),
        ("client", 'grpc.Dial("inventory.internal:443")', "inventory.internal"),
        ("client", 'baseURL = "https://shipping.internal"', "shipping.internal"),
    ],
)
def test_each_signal_category_finds_its_target(make_repo, category, line, expected):
    repo = make_repo("svc", files={"internal/client.go": line})
    section = bundle(repo).split(f"### {category}\n", 1)
    assert len(section) == 2, f"no {category} section"
    assert expected in section[1].split("###")[0]


def test_signal_lines_carry_the_path_and_line_number(make_repo):
    repo = make_repo(
        "svc", files={"internal/client.go": 'package main\n\nu := "https://orders.internal/v1"'}
    )
    assert "internal/client.go:3:" in bundle(repo)


def test_noisy_hosts_are_not_reported_as_signals(make_repo):
    repo = make_repo(
        "svc",
        files={
            "internal/client.go": 'a := "https://github.com/org/x"\nb := "http://localhost:3000"\nc := "https://orders.internal"'
        },
    )
    urls = bundle(repo).split("### url\n", 1)[1].split("###")[0]
    assert "orders.internal" in urls
    assert "github.com" not in urls
    assert "localhost" not in urls


def test_a_repeated_signal_is_reported_once(make_repo):
    body = "\n".join(f'call{i} := "https://orders.internal/v1"' for i in range(5))
    repo = make_repo("svc", files={"internal/client.go": body})
    urls = bundle(repo).split("### url\n", 1)[1].split("###")[0]
    assert urls.count("orders.internal") == 1


def test_signals_per_category_are_capped(make_repo, monkeypatch):
    body = "\n".join(f'u{i} := "https://host{i}.internal/v1"' for i in range(40))
    repo = make_repo("svc", files={"internal/client.go": body})
    monkeypatch.setattr(atlas, "BUNDLE_SIGNALS_PER_CATEGORY", 5)
    urls = bundle(repo).split("### url\n", 1)[1].split("###")[0]
    assert len([ln for ln in urls.splitlines() if ln.strip()]) == 5


def test_the_signal_scan_stops_once_every_category_is_full(make_repo, monkeypatch):
    """The per-category cap alone still reads and scans every remaining file."""
    saturating = (
        'u := "https://orders.internal/v1"\n'
        'os.Getenv("EVENTS_QUEUE")\n'
        'producer.publish("order.created", payload)\n'
        'dsn := "postgres://db.internal:5432/orders"\n'
        'grpc.Dial("inventory.internal:443")\n'
    )
    repo = make_repo(
        "svc",
        files={"a_first.go": saturating, "z_last.go": 'u := "https://late.internal"'},
    )
    monkeypatch.setattr(atlas, "BUNDLE_SIGNALS_PER_CATEGORY", 1)
    read = []
    original = atlas.read_capped
    monkeypatch.setattr(
        atlas, "read_capped", lambda p, cap: read.append(p.name) or original(p, cap)
    )
    bundle(repo)
    assert "a_first.go" in read
    assert "z_last.go" not in read


def test_a_long_signal_line_is_trimmed(make_repo):
    repo = make_repo(
        "svc", files={"internal/client.go": 'u := "https://orders.internal" // ' + "x" * 500}
    )
    longest = max(len(ln) for ln in bundle(repo).splitlines())
    assert longest < 400


def test_signals_are_not_scanned_in_binary_or_unlisted_extensions(make_repo):
    repo = make_repo("svc", files={"fixture.bin": 'u := "https://secret-host.internal"'})
    assert "secret-host.internal" not in bundle(repo)


def test_secrets_are_redacted_in_excerpts_and_signals(make_repo):
    repo = make_repo(
        "svc",
        files={
            ".env.example": "DB_PASSWORD=hunter2\nORDERS_URL=http://orders.internal",
            "internal/client.go": 'dsn := "postgres://app:s3cret@db.internal:5432/orders"',
        },
    )
    text = bundle(repo)
    assert "hunter2" not in text
    assert "s3cret" not in text
    assert "<redacted>" in text
    assert "orders.internal" in text
    assert "db.internal" in text


def test_only_restricts_excerpts_and_signals_to_the_named_files(make_repo):
    repo = make_repo(
        "svc",
        files={
            "main.go": 'package main // u := "https://old.internal"',
            "internal/client.go": 'u := "https://changed.internal"',
        },
    )
    text = bundle(repo, only={"internal/client.go"})
    assert "changed.internal" in text
    assert "old.internal" not in text
    assert "### main.go" not in text


def test_an_empty_only_set_yields_no_excerpts_or_signals(make_repo):
    repo = make_repo("svc", files={"main.go": 'package main // u := "https://old.internal"'})
    text = bundle(repo, only=set())
    assert "old.internal" not in text
    assert "## Tree" in text


def test_the_bundle_never_exceeds_its_budget(make_repo):
    files = {
        f"pkg{i}/package.json": json.dumps({"name": f"p{i}", "x": "y" * 3000}) for i in range(40)
    }
    files["internal/client.go"] = "\n".join(
        f'u{i} := "https://host{i}.internal"' for i in range(200)
    )
    repo = make_repo("svc", files=files)
    for budget in (600, 4000, 20_000):
        assert len(bundle(repo, budget=budget).encode()) <= budget


def test_signals_still_get_budget_when_key_files_are_huge(make_repo):
    files = {
        f"pkg{i}/package.json": json.dumps({"name": f"p{i}", "x": "y" * 8000}) for i in range(30)
    }
    files["internal/client.go"] = 'u := "https://orders.internal"'
    repo = make_repo("svc", files=files)
    assert "orders.internal" in bundle(repo, budget=40_000)


def test_the_bundle_is_deterministic(make_repo):
    repo = make_repo(
        "svc",
        files={
            "README.md": "# svc",
            "package.json": '{"name": "svc"}',
            "internal/a.go": 'u := "https://a.internal"',
            "internal/b.go": 'u := "https://b.internal"',
        },
    )
    assert bundle(repo) == bundle(repo)
