import json
from pathlib import Path

import pytest

AGENTS = Path(__file__).resolve().parent.parent / "kiro" / "agents"


def load(name):
    return json.loads((AGENTS / f"{name}.json").read_text(encoding="utf-8"))


def denied_reads(agent):
    return {
        pattern
        for rule in agent.get("permissions", {}).get("rules", [])
        if rule.get("effect") == "deny" and rule.get("capability") in ("fs_read", "all")
        for pattern in rule.get("match", [])
    }


@pytest.mark.parametrize(
    "pattern",
    [
        "**/.ssh/**",
        "**/.aws/**",
        "**/.kube/**",
        "**/.gnupg/**",
        "**/.netrc",
        "**/.kiro/**",
        "**/.env",
        "**/.env.*",
    ],
)
def test_the_explore_mapper_denies_reading_credential_locations(pattern):
    assert pattern in denied_reads(load("atlas-mapper"))


def test_the_explore_mapper_can_still_read_env_examples():
    rule = next(
        r for r in load("atlas-mapper")["permissions"]["rules"] if "**/.env.*" in r["match"]
    )
    assert {"**/.env.example", "**/.env.sample", "**/.env.template"} <= set(rule.get("exclude", []))


def test_the_explore_mapper_has_only_the_read_tool_and_no_mcp():
    agent = load("atlas-mapper")
    assert agent["tools"] == ["read"]
    assert agent["allowedTools"] == ["read"]
    assert agent["includeMcpJson"] is False
    assert agent["mcpServers"] == {}


def skill_frontmatter(path):
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} has no frontmatter"
    block = text.split("---\n", 2)[1]
    return dict(line.split(":", 1) for line in block.splitlines() if ":" in line)


def test_the_atlas_agent_loads_the_rules_as_a_skill_not_a_file():
    assert load("atlas")["resources"] == ["skill://../skills/atlas/SKILL.md"]


def test_the_atlas_skill_resource_resolves_to_a_skill_with_name_and_description():
    uri = load("atlas")["resources"][0]
    skill = (AGENTS / uri.removeprefix("skill://")).resolve()
    assert skill.is_file()
    meta = skill_frontmatter(skill)
    assert meta["name"].strip() == "atlas"
    assert meta["description"].strip()
    assert "inclusion" not in meta


def test_the_repo_no_longer_ships_the_atlas_steering_file():
    assert not (AGENTS.parent / "steering" / "atlas.md").exists()


def test_the_bundle_mapper_has_no_tools_and_denies_everything():
    agent = load("atlas-bundle")
    assert agent["tools"] == []
    assert agent["allowedTools"] == []
    assert agent["includeMcpJson"] is False
    assert {"capability": "all", "match": ["*"], "effect": "deny"} in agent["permissions"]["rules"]
