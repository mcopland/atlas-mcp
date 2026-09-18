import json

import atlas


def write(tmp_path, files):
    for rel, text in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return tmp_path


def test_extract_packages_reads_every_ecosystem(tmp_path):
    write(
        tmp_path,
        {
            "package.json": json.dumps(
                {
                    "name": "@org/web",
                    "dependencies": {"react": "^18"},
                    "devDependencies": {"jest": "^29"},
                }
            ),
            "svc/go.mod": "module github.com/org/svc\n\nrequire (\n\tgithub.com/org/lib v1.2.3 // c\n)\n",
            "lib/pyproject.toml": '[project]\nname = "Org_Lib"\ndependencies = ["requests>=2", "boto3"]\n',
            "requirements.txt": "# comment\n-e .\nDjango==5.0\n",
        },
    )
    got = atlas.extract_packages(tmp_path)
    pub = {(p["ecosystem"], p["name"]) for p in got["publishes"]}
    dep = {(d["ecosystem"], d["name"]) for d in got["depends_on"]}
    assert pub == {("npm", "@org/web"), ("go", "github.com/org/svc"), ("pypi", "org-lib")}
    assert ("npm", "react") in dep and ("npm", "jest") in dep
    assert ("go", "github.com/org/lib") in dep
    assert ("pypi", "requests") in dep and ("pypi", "boto3") in dep
    assert ("pypi", "django") in dep


def test_extract_packages_drops_monorepo_internal_dependencies(tmp_path):
    write(
        tmp_path,
        {
            "a/package.json": json.dumps({"name": "@org/a", "dependencies": {"@org/b": "*"}}),
            "b/package.json": json.dumps({"name": "@org/b"}),
        },
    )
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


POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <groupId>com.org</groupId>
  <artifactId>orders</artifactId>
  <dependencies>
    <dependency><groupId>com.org</groupId><artifactId>shared</artifactId></dependency>
    <dependency><groupId>org.slf4j</groupId><artifactId>slf4j-api</artifactId></dependency>
  </dependencies>
</project>
"""

GRADLE = """group = 'com.org'
dependencies {
  implementation 'com.org:shared:1.0'
  testImplementation("org.junit:junit:5")
}
"""

CSPROJ = """<Project>
  <PropertyGroup><PackageId>Org.Orders</PackageId></PropertyGroup>
  <ItemGroup><PackageReference Include="Newtonsoft.Json" Version="13" /></ItemGroup>
</Project>
"""


def test_extract_packages_reads_maven_gradle_cargo_and_nuget(tmp_path):
    write(
        tmp_path,
        {
            "svc/pom.xml": POM,
            "app/settings.gradle": "rootProject.name = 'web'\n",
            "app/build.gradle": GRADLE,
            "rs/Cargo.toml": '[package]\nname = "orders-rs"\n\n[dependencies]\nserde = "1"\n',
            "dotnet/Orders.csproj": CSPROJ,
        },
    )
    got = atlas.extract_packages(tmp_path)
    pub = {(p["ecosystem"], p["name"]) for p in got["publishes"]}
    dep = {(d["ecosystem"], d["name"]) for d in got["depends_on"]}
    assert ("maven", "com.org:orders") in pub
    assert ("maven", "com.org:web") in pub
    assert ("cargo", "orders-rs") in pub
    assert ("nuget", "org.orders") in pub
    assert ("maven", "com.org:shared") in dep
    assert ("maven", "org.slf4j:slf4j-api") in dep
    assert ("maven", "org.junit:junit") in dep
    assert ("cargo", "serde") in dep
    assert ("nuget", "newtonsoft.json") in dep


def test_csproj_without_a_package_id_falls_back_to_the_project_name(tmp_path):
    write(tmp_path, {"svc/Orders.Api.csproj": "<Project><ItemGroup /></Project>"})
    got = atlas.extract_packages(tmp_path)
    assert {(p["ecosystem"], p["name"]) for p in got["publishes"]} == {("nuget", "orders.api")}


def test_maven_property_placeholders_are_not_treated_as_packages(tmp_path):
    write(
        tmp_path,
        {
            "svc/pom.xml": """<project>
  <groupId>com.org</groupId><artifactId>a</artifactId>
  <dependencies>
    <dependency><groupId>${project.groupId}</groupId><artifactId>b</artifactId></dependency>
  </dependencies>
</project>
"""
        },
    )
    assert atlas.extract_packages(tmp_path)["depends_on"] == []


def test_maven_child_module_inherits_the_parent_group(tmp_path):
    write(
        tmp_path,
        {
            "mod/pom.xml": """<project>
  <parent><groupId>com.org</groupId><artifactId>root</artifactId></parent>
  <artifactId>orders-api</artifactId>
</project>
"""
        },
    )
    got = atlas.extract_packages(tmp_path)
    assert {(p["ecosystem"], p["name"]) for p in got["publishes"]} == {
        ("maven", "com.org:orders-api")
    }
