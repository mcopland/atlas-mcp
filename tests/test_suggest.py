import argparse
import json

import pytest
from conftest import consume

import atlas


class FakeKiro:
    """Stands in for run_kiro: each call takes the next scripted answer, which is either a
    parsed ATLAS_JSON object, an exception to raise, or a function of the prompt."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, cfg, repo, prompt, log_path, mapper="explore", model=None):
        self.calls.append({"repo": repo, "prompt": prompt, "mapper": mapper, "model": model})
        if not self.answers:
            raise AssertionError("run_kiro called more often than the test expected")
        nxt = self.answers.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt(prompt) if callable(nxt) else nxt


def answer(*items):
    return {"suggestions": list(items)}


def link(key="ORDERS_BACKEND", to="orders-svc", **extra):
    return {
        "key": key,
        "action": "link",
        "to": to,
        "confidence": "high",
        "reason": "orders-svc owns it",
        "basis": "orders.internal",
        **extra,
    }


def run(cfg, kiro, monkeypatch, **flags):
    monkeypatch.setattr(atlas, "run_kiro", kiro)
    said = []
    ns = {"top": None, "dry_run": False, "all": False, "verbose": False, **flags}
    atlas.cmd_suggest(cfg, argparse.Namespace(**ns), say=said.append)
    return "\n".join(said)


def stored(cfg):
    return json.loads(atlas.suggestions_path(cfg).read_text())["suggestions"]


def edit_manifest(cfg, name, **fields):
    path = cfg["atlas_dir"] / "repos" / f"{name}.json"
    m = json.loads(path.read_text())
    m.update(fields)
    atlas.write_json(path, m)


@pytest.fixture
def orders(world):
    def build(**cfg_overrides):
        return world({"orders-svc": [], "client": consume("ORDERS_BACKEND")}, **cfg_overrides)

    return build


def test_one_call_asks_the_resolver_agent_with_the_resolver_model(orders, monkeypatch):
    cfg = orders(resolver_model="big")
    kiro = FakeKiro(answer(link()))
    out = run(cfg, kiro, monkeypatch)
    [call] = kiro.calls
    assert (call["mapper"], call["model"], call["repo"]) == ("resolver", "big", cfg["atlas_dir"])
    s = stored(cfg)["orders_backend"]
    assert (s["action"], s["to"], s["confidence"], s["model"]) == (
        "link",
        "orders-svc",
        "high",
        "big",
    )
    assert s["fingerprint"]
    assert "link 1" in out


def test_the_resolver_model_defaults_to_the_mapping_model(orders, monkeypatch):
    cfg = orders(model="m1")
    kiro = FakeKiro(answer(link()))
    run(cfg, kiro, monkeypatch)
    assert kiro.calls[0]["model"] == "m1"


def test_the_prompt_carries_consumer_evidence_and_the_repo_directory(orders, monkeypatch):
    cfg = orders()
    edit_manifest(
        cfg,
        "orders-svc",
        identifiers=["orders-internal"],
        exposes=[{"kind": "http", "name": "api", "key": "/v1/orders", "evidence": "a.py"}],
    )
    kiro = FakeKiro(answer())
    run(cfg, kiro, monkeypatch)
    prompt = kiro.calls[0]["prompt"]
    for text in ("ORDERS_BACKEND", "client", "a.py:1", "orders-internal", "http:/v1/orders"):
        assert text in prompt
    assert "orders-svc summary" in prompt
    assert "{{" not in prompt


def test_a_rerun_with_nothing_changed_makes_no_call(orders, monkeypatch):
    cfg = orders()
    run(cfg, FakeKiro(answer(link())), monkeypatch)
    kiro = FakeKiro()
    out = run(cfg, kiro, monkeypatch)
    assert kiro.calls == []
    assert "1 already current" in out
    assert stored(cfg)["orders_backend"]["to"] == "orders-svc"


def test_all_asks_again_even_when_the_suggestion_is_current(orders, monkeypatch):
    cfg = orders()
    run(cfg, FakeKiro(answer(link())), monkeypatch)
    kiro = FakeKiro(answer(link(confidence="low")))
    run(cfg, kiro, monkeypatch, all=True)
    assert len(kiro.calls) == 1
    assert stored(cfg)["orders_backend"]["confidence"] == "low"


def test_a_new_consumer_asks_again(orders, make_repo, make_manifest, monkeypatch):
    cfg = orders()
    run(cfg, FakeKiro(answer(link())), monkeypatch)
    before = stored(cfg)["orders_backend"]["fingerprint"]
    make_repo("billing")
    make_manifest(cfg, "billing", consumes=consume("ORDERS_BACKEND"))
    kiro = FakeKiro(answer(link()))
    run(cfg, kiro, monkeypatch)
    assert len(kiro.calls) == 1
    assert stored(cfg)["orders_backend"]["fingerprint"] != before


@pytest.mark.parametrize(
    "item",
    [
        link(to="ghost"),
        link(to="client"),
        link(key="SOMETHING_ELSE"),
        {"key": "ORDERS_BACKEND", "action": "delete"},
        {"key": "ORDERS_BACKEND", "action": "external", "external": "not a name!"},
        {"key": "ORDERS_BACKEND", "action": "generic", "tag": ""},
        {"key": "ORDERS_BACKEND", "action": "external", "external": None},
        {"key": "ORDERS_BACKEND", "action": "generic", "tag": None},
        link(to=None),
        "not an object",
    ],
    ids=[
        "unknown-repo",
        "sole-consumer",
        "not-asked",
        "bad-action",
        "bad-id",
        "no-tag",
        "null-id",
        "null-tag",
        "null-repo",
        "shape",
    ],
)
def test_an_invalid_answer_is_dropped_and_counted(world, monkeypatch, item):
    cfg = world({"orders-svc": [], "client": consume("ORDERS_BACKEND")})
    out = run(cfg, FakeKiro(answer(item)), monkeypatch)
    assert stored(cfg) == {}
    assert "1 dropped" in out


def test_a_second_answer_for_the_same_key_is_dropped(orders, monkeypatch):
    cfg = orders()
    out = run(cfg, FakeKiro(answer(link(), link(confidence="low"))), monkeypatch)
    assert stored(cfg)["orders_backend"]["confidence"] == "high"
    assert "1 dropped" in out


def test_a_generic_suggestion_never_carries_a_group(world, monkeypatch):
    cfg = world({n: consume("MONGO_URI") for n in "ab"})
    item = {"key": "MONGO_URI", "action": "generic", "tag": "Mongo", "group": "shared"}
    run(cfg, FakeKiro(answer(item)), monkeypatch)
    s = stored(cfg)["mongo_uri"]
    assert (s["action"], s["tag"]) == ("generic", "mongo")
    assert "group" not in s


def test_an_external_suggestion_keeps_its_label(orders, monkeypatch):
    cfg = orders()
    item = {"key": "ORDERS_BACKEND", "action": "external", "external": "acme", "label": "Acme"}
    run(cfg, FakeKiro(answer(item)), monkeypatch)
    s = stored(cfg)["orders_backend"]
    assert (s["action"], s["external"], s["label"]) == ("external", "acme", "Acme")


def test_none_is_kept_as_an_answer(orders, monkeypatch):
    cfg = orders()
    item = {"key": "ORDERS_BACKEND", "action": "none", "reason": "nothing fits"}
    out = run(cfg, FakeKiro(answer(item)), monkeypatch)
    assert stored(cfg)["orders_backend"]["action"] == "none"
    assert "none 1" in out


def test_an_unknown_confidence_becomes_low(orders, monkeypatch):
    cfg = orders()
    run(cfg, FakeKiro(answer(link(confidence="certain"))), monkeypatch)
    assert stored(cfg)["orders_backend"]["confidence"] == "low"


def test_strings_the_model_wrote_are_redacted(orders, monkeypatch):
    cfg = orders()
    token = "ghp_" + "A" * 36
    run(cfg, FakeKiro(answer(link(reason=f"saw {token}"))), monkeypatch)
    assert token not in atlas.suggestions_path(cfg).read_text()


def test_control_characters_the_model_wrote_are_removed(orders, monkeypatch):
    cfg = orders()
    run(cfg, FakeKiro(answer(link(reason="ok\x1b[2K\x1b]0;title\x07 fine"))), monkeypatch)
    reason = stored(cfg)["orders_backend"]["reason"]
    assert "\x1b" not in reason
    assert "\x07" not in reason
    assert reason.startswith("ok")
    assert reason.endswith("fine")


def test_dry_run_makes_no_call_and_writes_nothing(orders, monkeypatch):
    cfg = orders()
    kiro = FakeKiro()
    out = run(cfg, kiro, monkeypatch, dry_run=True)
    assert kiro.calls == []
    assert not atlas.suggestions_path(cfg).exists()
    assert "batch 1: 1 targets" in out


def test_top_limits_the_targets_asked(world, monkeypatch):
    cfg = world({"a": consume("RARE_URL"), "b": consume("COMMON_URL"), "c": consume("COMMON_URL")})
    kiro = FakeKiro(answer())
    run(cfg, kiro, monkeypatch, top=1)
    assert "COMMON_URL" in kiro.calls[0]["prompt"]
    assert "RARE_URL" not in kiro.calls[0]["prompt"]


def test_batches_stay_inside_the_budget_and_ask_every_target_once(world, monkeypatch):
    budget = 12 * 1024
    repos = {f"client-{i:02}": consume(f"SERVICE_{i:02}_" + "X" * 200) for i in range(40)}
    cfg = world(repos, resolver_budget_bytes=budget)
    kiro = FakeKiro(*[lambda _p: answer()] * 40)
    run(cfg, kiro, monkeypatch)
    assert len(kiro.calls) > 1
    assert all(len(c["prompt"].encode()) <= budget for c in kiro.calls)
    for i in range(40):
        key = f"SERVICE_{i:02}_" + "X" * 200
        assert sum(f"### {key}\n" in c["prompt"] for c in kiro.calls) == 1


def test_unparseable_output_is_retried_once(orders, monkeypatch):
    cfg = orders()
    kiro = FakeKiro(ValueError("no block"), answer(link()))
    run(cfg, kiro, monkeypatch)
    assert len(kiro.calls) == 2
    assert stored(cfg)["orders_backend"]["to"] == "orders-svc"


def test_a_batch_that_fails_twice_is_reported_and_the_run_finishes(orders, monkeypatch):
    cfg = orders()
    kiro = FakeKiro(ValueError("no block"), ValueError("no block"))
    out = run(cfg, kiro, monkeypatch)
    assert "batch 1 failed" in out
    assert "1 failed" in out
    assert stored(cfg) == {}


def test_a_kiro_error_is_reported_without_a_retry(orders, monkeypatch):
    cfg = orders()
    kiro = FakeKiro(RuntimeError("kiro-cli exited 1"))
    out = run(cfg, kiro, monkeypatch)
    assert len(kiro.calls) == 1
    assert "kiro-cli exited 1" in out


def test_suggestions_for_targets_that_resolved_are_pruned(orders, monkeypatch):
    cfg = orders()
    run(cfg, FakeKiro(answer(link())), monkeypatch)
    edit_manifest(cfg, "orders-svc", identifiers=["ORDERS_BACKEND"])
    out = run(cfg, FakeKiro(), monkeypatch)
    assert stored(cfg) == {}
    assert "nothing to suggest" in out


def test_a_malformed_suggestions_file_stops_with_its_path(orders, monkeypatch):
    cfg = orders()
    atlas.suggestions_path(cfg).write_text("{bad")
    with pytest.raises(SystemExit) as e:
        run(cfg, FakeKiro(), monkeypatch)
    assert str(atlas.suggestions_path(cfg)) in str(e.value)


def test_main_dispatches_suggest(make_cfg, tmp_path, monkeypatch):
    make_cfg()
    seen = {}
    monkeypatch.setattr(
        atlas, "cmd_suggest", lambda cfg, args: seen.update(top=args.top, dry=args.dry_run)
    )
    argv = ["atlas.py", "--config", str(tmp_path / "config.json"), "suggest", "--top", "5"]
    monkeypatch.setattr("sys.argv", [*argv, "--dry-run"])
    atlas.main()
    assert seen == {"top": 5, "dry": True}
