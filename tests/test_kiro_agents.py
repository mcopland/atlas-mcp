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
    ["**/.ssh/**", "**/.aws/**", "**/.kube/**", "**/.gnupg/**", "**/.netrc", "**/.kiro/**", "**/.env", "**/.env.*"],
)
def test_the_explore_mapper_denies_reading_credential_locations(pattern):
    assert pattern in denied_reads(load("atlas-mapper"))


def test_the_explore_mapper_can_still_read_env_examples():
    rule = next(r for r in load("atlas-mapper")["permissions"]["rules"] if "**/.env.*" in r["match"])
    assert {"**/.env.example", "**/.env.sample", "**/.env.template"} <= set(rule.get("exclude", []))


def test_the_explore_mapper_has_only_the_read_tool_and_no_mcp():
    agent = load("atlas-mapper")
    assert agent["tools"] == ["read"]
    assert agent["allowedTools"] == ["read"]
    assert agent["includeMcpJson"] is False
    assert agent["mcpServers"] == {}


def test_the_bundle_mapper_has_no_tools_and_denies_everything():
    agent = load("atlas-bundle")
    assert agent["tools"] == []
    assert agent["allowedTools"] == []
    assert agent["includeMcpJson"] is False
    assert {"capability": "all", "match": ["*"], "effect": "deny"} in agent["permissions"]["rules"]
