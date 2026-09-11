import json

import atlas


def write(tmp_path, files):
    for rel, text in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return tmp_path


def test_extract_packages_reads_every_ecosystem(tmp_path):
    write(tmp_path, {
        "package.json": json.dumps({"name": "@org/web", "dependencies": {"react": "^18"},
                                    "devDependencies": {"jest": "^29"}}),
        "svc/go.mod": "module github.com/org/svc\n\nrequire (\n\tgithub.com/org/lib v1.2.3 // c\n)\n",
        "lib/pyproject.toml": '[project]\nname = "Org_Lib"\ndependencies = ["requests>=2", "boto3"]\n',
        "requirements.txt": "# comment\n-e .\nDjango==5.0\n",
    })
    got = atlas.extract_packages(tmp_path)
    pub = {(p["ecosystem"], p["name"]) for p in got["publishes"]}
    dep = {(d["ecosystem"], d["name"]) for d in got["depends_on"]}
    assert pub == {("npm", "@org/web"), ("go", "github.com/org/svc"), ("pypi", "org-lib")}
    assert ("npm", "react") in dep and ("npm", "jest") in dep
    assert ("go", "github.com/org/lib") in dep
    assert ("pypi", "requests") in dep and ("pypi", "boto3") in dep
    assert ("pypi", "django") in dep


def test_extract_packages_drops_monorepo_internal_dependencies(tmp_path):
    write(tmp_path, {
        "a/package.json": json.dumps({"name": "@org/a", "dependencies": {"@org/b": "*"}}),
        "b/package.json": json.dumps({"name": "@org/b"}),
    })
    got = atlas.extract_packages(tmp_path)
    assert {(d["ecosystem"], d["name"]) for d in got["depends_on"]} == set()


def test_extract_packages_survives_malformed_manifests(tmp_path, capsys):
    write(tmp_path, {"package.json": "{not json", "go.mod": "module github.com/org/ok\n"})
    got = atlas.extract_packages(tmp_path)
    assert {(p["ecosystem"], p["name"]) for p in got["publishes"]} == {("go", "github.com/org/ok")}
    assert "could not parse" in capsys.readouterr().err


def test_extract_packages_skips_vendored_trees(tmp_path):
    write(tmp_path, {"node_modules/dep/package.json": json.dumps({"name": "vendored"})})
    assert atlas.extract_packages(tmp_path)["publishes"] == []


def test_extract_packages_records_relative_evidence(tmp_path):
    write(tmp_path, {"svc/go.mod": "module github.com/org/svc\n"})
    ev = atlas.extract_packages(tmp_path)["publishes"][0]["evidence"]
    assert ev.replace("\\", "/") == "svc/go.mod"
