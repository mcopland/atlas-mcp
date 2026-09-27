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


def test_extract_packages_skips_a_gitignored_manifest(make_repo):
    """A file .gitignore hides from the model must not become a deterministic fact either:
    the model never saw it, so a name it declares should not silently reach the graph."""
    repo = make_repo(
        "svc",
        files={
            ".gitignore": "ignored/package.json\n",
            "ignored/package.json": json.dumps({"name": "should-not-appear"}),
            "package.json": json.dumps({"name": "svc"}),
        },
    )
    got = atlas.extract_packages(repo)
    assert {p["name"] for p in got["publishes"]} == {"svc"}


def test_extract_packages_reads_a_tracked_manifest_in_a_git_repo(make_repo):
    repo = make_repo("svc", files={"package.json": json.dumps({"name": "svc"})})
    got = atlas.extract_packages(repo)
    assert {p["name"] for p in got["publishes"]} == {"svc"}


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


def test_extract_packages_reads_npm_optional_dependencies(tmp_path):
    write(
        tmp_path,
        {"package.json": json.dumps({"name": "svc", "optionalDependencies": {"fsevents": "^2"}})},
    )
    got = atlas.extract_packages(tmp_path)
    assert ("npm", "fsevents") in {(d["ecosystem"], d["name"]) for d in got["depends_on"]}


def test_extract_packages_reads_pyproject_optional_dependencies(tmp_path):
    write(
        tmp_path,
        {
            "pyproject.toml": (
                '[project]\nname = "svc"\n\n[project.optional-dependencies]\ntest = ["pytest>=8"]\n'
            )
        },
    )
    got = atlas.extract_packages(tmp_path)
    assert ("pypi", "pytest") in {(d["ecosystem"], d["name"]) for d in got["depends_on"]}


def test_extract_packages_reads_pep735_dependency_groups(tmp_path):
    write(
        tmp_path,
        {
            "pyproject.toml": (
                '[project]\nname = "svc"\n\n'
                "[dependency-groups]\n"
                'dev = ["ruff>=0.16", { include-group = "test" }]\n'
                'test = ["pytest>=8"]\n'
            )
        },
    )
    got = atlas.extract_packages(tmp_path)
    dep = {(d["ecosystem"], d["name"]) for d in got["depends_on"]}
    assert ("pypi", "ruff") in dep
    assert ("pypi", "pytest") in dep


def test_extract_packages_reads_poetry_group_dependencies(tmp_path):
    write(
        tmp_path,
        {
            "pyproject.toml": (
                '[tool.poetry]\nname = "svc"\n\n'
                '[tool.poetry.group.dev.dependencies]\npytest = "^8"\n'
            )
        },
    )
    got = atlas.extract_packages(tmp_path)
    assert ("pypi", "pytest") in {(d["ecosystem"], d["name"]) for d in got["depends_on"]}


def test_extract_packages_reads_a_gradle_version_catalog(tmp_path):
    write(
        tmp_path,
        {
            "gradle/libs.versions.toml": (
                "[versions]\n"
                'guava = "32.1"\n\n'
                "[libraries]\n"
                'guava = { module = "com.google.guava:guava", version.ref = "guava" }\n'
                'junit-jupiter = { group = "org.junit.jupiter", name = "junit-jupiter", version = "5.10" }\n'
            )
        },
    )
    got = atlas.extract_packages(tmp_path)
    dep = {(d["ecosystem"], d["name"]) for d in got["depends_on"]}
    assert ("maven", "com.google.guava:guava") in dep
    assert ("maven", "org.junit.jupiter:junit-jupiter") in dep


DIRECTORY_PACKAGES_PROPS = """<Project>
  <ItemGroup>
    <PackageVersion Include="Newtonsoft.Json" Version="13.0.3" />
    <PackageVersion Include="Serilog" Version="3.1.1" />
  </ItemGroup>
</Project>
"""


def test_extract_packages_reads_dotnet_central_package_versions(tmp_path):
    write(tmp_path, {"Directory.Packages.props": DIRECTORY_PACKAGES_PROPS})
    got = atlas.extract_packages(tmp_path)
    dep = {(d["ecosystem"], d["name"]) for d in got["depends_on"]}
    assert ("nuget", "newtonsoft.json") in dep
    assert ("nuget", "serilog") in dep


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
