import json

import pytest

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


def pypi(got, key):
    return {(p["ecosystem"], p["name"]) for p in got[key]}


def test_setup_py_publishes_its_name_and_reads_dependencies(tmp_path):
    write(
        tmp_path,
        {
            "setup.py": (
                "from setuptools import setup\n"
                "setup(name='sltc', install_requires=['requests>=2'],\n"
                "      extras_require={'aws': ['boto3'], 'dev': ['pytest']})\n"
            )
        },
    )
    got = atlas.extract_packages(tmp_path)
    assert pypi(got, "publishes") == {("pypi", "sltc")}
    assert pypi(got, "depends_on") == {
        ("pypi", "requests"),
        ("pypi", "boto3"),
        ("pypi", "pytest"),
    }
    assert got["publishes"][0]["evidence"] == "setup.py"


@pytest.mark.parametrize("call", ["setup", "setuptools.setup"])
def test_setup_py_resolves_module_level_constants(tmp_path, call):
    write(
        tmp_path,
        {
            "setup.py": (
                "import setuptools\nfrom setuptools import setup\n"
                f"NAME = 'Sltc_Core'\nREQUIRES = ['requests']\n"
                f"{call}(name=NAME, install_requires=REQUIRES)\n"
            )
        },
    )
    got = atlas.extract_packages(tmp_path)
    assert pypi(got, "publishes") == {("pypi", "sltc-core")}
    assert pypi(got, "depends_on") == {("pypi", "requests")}


def test_setup_py_skips_a_non_literal_kwarg_but_keeps_the_rest(tmp_path):
    write(tmp_path, {"setup.py": "setup(name=get_name(), install_requires=['requests'])\n"})
    got = atlas.extract_packages(tmp_path)
    assert got["publishes"] == []
    assert pypi(got, "depends_on") == {("pypi", "requests")}


def test_setup_py_is_parsed_and_never_executed(tmp_path):
    marker = tmp_path / "ran"
    write(tmp_path, {"setup.py": f"open({str(marker)!r}, 'w').close()\nsetup(name='sltc')\n"})
    assert pypi(atlas.extract_packages(tmp_path), "publishes") == {("pypi", "sltc")}
    assert not marker.exists()


def test_setup_cfg_publishes_and_reads_multiline_dependencies(tmp_path):
    write(
        tmp_path,
        {
            "setup.py": "from setuptools import setup\nsetup()\n",
            "setup.cfg": (
                "[metadata]\nname = sltc\n\n"
                "[options]\ninstall_requires =\n    requests>=2\n    # a comment\n\n    boto3\n\n"
                "[options.extras_require]\naws =\n    s3fs\n"
            ),
        },
    )
    got = atlas.extract_packages(tmp_path)
    assert pypi(got, "publishes") == {("pypi", "sltc")}
    assert pypi(got, "depends_on") == {("pypi", "requests"), ("pypi", "boto3"), ("pypi", "s3fs")}


@pytest.mark.parametrize(
    "rel,text",
    [("setup.py", "setup(name='broken'\n"), ("setup.cfg", "[metadata\nname = broken\n")],
)
def test_a_malformed_legacy_manifest_warns_and_the_rest_still_extract(tmp_path, capsys, rel, text):
    write(tmp_path, {rel: text, "go.mod": "module github.com/org/ok\n"})
    got = atlas.extract_packages(tmp_path)
    assert pypi(got, "publishes") == {("go", "github.com/org/ok")}
    assert f"could not parse {tmp_path / rel}" in capsys.readouterr().err


def test_a_package_the_repo_publishes_is_not_listed_as_its_dependency(tmp_path):
    write(
        tmp_path,
        {"setup.py": "setup(name='sltc')\n", "requirements.txt": "sltc==1.0\nrequests\n"},
    )
    got = atlas.extract_packages(tmp_path)
    assert pypi(got, "depends_on") == {("pypi", "requests")}


def requirements(tmp_path, *lines):
    write(tmp_path, {"requirements.txt": "\n".join(lines) + "\n"})
    return atlas.extract_packages(tmp_path)["depends_on"]


@pytest.mark.parametrize(
    "line,name,inferred",
    [
        ("git+https://h/org/py-sltc.git@v1#egg=sltc", "sltc", False),
        ("-e git+ssh://git@h/org/py-sltc.git#egg=sltc", "sltc", False),
        ("git+https://h/org/py-sltc.git#subdirectory=x&egg=sltc[aws]", "sltc", False),
        ("sltc @ git+https://h/org/py-sltc.git", "sltc", False),
        ("git+https://h/org/py-sltc.git@v1.2", "py-sltc", True),
        ("git+https://h/org/py-sltc.git@feature/x", "py-sltc", True),
        ("-e git+ssh://git@h/org/py-sltc.git", "py-sltc", True),
        ("git+https://h/org/py-sltc", "py-sltc", True),
    ],
)
def test_vcs_requirement_names_the_package_not_git(tmp_path, line, name, inferred):
    got = requirements(tmp_path, line)
    assert [(d["name"], d.get("inferred", False)) for d in got] == [(name, inferred)]


def test_a_declared_dependency_replaces_an_inferred_one_in_either_order(tmp_path):
    for lines in (
        ["git+https://h/org/sltc.git", "sltc==1.0"],
        ["sltc==1.0", "git+https://h/org/sltc.git"],
    ):
        got = requirements(tmp_path, *lines)
        assert [(d["name"], d.get("inferred", False)) for d in got] == [("sltc", False)]


def test_an_inferred_dependency_on_a_published_package_is_dropped(tmp_path):
    write(
        tmp_path,
        {
            "setup.py": "setup(name='sltc')\n",
            "requirements.txt": "git+https://h/org/sltc.git\nrequests\n",
        },
    )
    got = atlas.extract_packages(tmp_path)
    assert pypi(got, "depends_on") == {("pypi", "requests")}


@pytest.mark.parametrize(
    "cfg",
    [
        "[metadata]\nname = real\n[options]\ninstall_requires = file: requirements.txt\n",
        "[metadata]\nname = real\n[options.extras_require]\naws = file: extras.txt\n",
    ],
)
def test_setup_cfg_file_directives_are_not_dependency_names(tmp_path, cfg):
    write(tmp_path, {"setup.cfg": cfg})
    assert pypi(atlas.extract_packages(tmp_path), "depends_on") == set()


def test_setup_cfg_attr_and_file_names_are_not_published(tmp_path):
    write(tmp_path, {"setup.cfg": "[metadata]\nname = attr: pkg.__name__\n"})
    assert atlas.extract_packages(tmp_path)["publishes"] == []


def test_setup_cfg_default_section_is_not_read_as_extras(tmp_path):
    write(
        tmp_path,
        {"setup.cfg": "[DEFAULT]\nbogus = evilpkg\n[options.extras_require]\naws = s3fs\n"},
    )
    assert pypi(atlas.extract_packages(tmp_path), "depends_on") == {("pypi", "s3fs")}


@pytest.mark.parametrize("other", ["helper.setup()", "logging_config.setup(level=1)", "setup()"])
def test_setup_py_ignores_unrelated_setup_calls(tmp_path, other):
    write(
        tmp_path,
        {"setup.py": f"import setuptools\n{other}\nsetuptools.setup(name='real')\n"},
    )
    assert pypi(atlas.extract_packages(tmp_path), "publishes") == {("pypi", "real")}


def test_setup_py_warns_when_a_declared_field_is_not_a_literal(tmp_path, capsys):
    write(tmp_path, {"setup.py": "setup(name=get_name(), install_requires=base + extra)\n"})
    atlas.extract_packages(tmp_path)
    err = capsys.readouterr().err
    assert f"{tmp_path / 'setup.py'}" in err
    assert "name" in err and "install_requires" in err


def test_a_pathologically_nested_setup_py_warns_instead_of_aborting(tmp_path, capsys):
    write(
        tmp_path,
        {
            "setup.py": "x = " + "+".join(["1"] * 200000) + "\n",
            "go.mod": "module github.com/org/ok\n",
        },
    )
    got = atlas.extract_packages(tmp_path)
    assert pypi(got, "publishes") == {("go", "github.com/org/ok")}
    assert "could not parse" in capsys.readouterr().err
