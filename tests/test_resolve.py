import argparse
import json

import pytest

import atlas
from conftest import consume


class Session:
    """A scripted terminal: answers are consumed in order, then input() hits end of file."""

    def __init__(self, *answers):
        self.answers = iter(answers)
        self.prompts, self.said = [], []

    def ask(self, prompt):
        self.prompts.append(prompt)
        try:
            return next(self.answers)
        except StopIteration:
            raise EOFError from None

    def say(self, text=""):
        self.said.append(text)

    @property
    def out(self):
        return "\n".join(self.said)


def run(cfg, session, top=None, suggested=False):
    ns = argparse.Namespace(top=top, suggested=suggested)
    atlas.cmd_resolve(cfg, ns, ask=session.ask, say=session.say)
    return json.loads((cfg["atlas_dir"] / "graph.json").read_text())


def saved(cfg):
    return json.loads(cfg["resolve_overrides"].read_text())


def test_link_answer_is_persisted_and_applied(world):
    cfg = world({"orders-svc": [], "client": consume("ORDERS_BACKEND")})
    s = Session("l", "orders-svc")
    g = run(cfg, s)
    assert saved(cfg)["targets"] == [
        {"key": "ORDERS_BACKEND", "repo": "client", "action": "link", "to": "orders-svc"}
    ]
    assert [(e["from"], e["to"], e["match"]) for e in g["edges"]] == [
        ("client", "orders-svc", "override")
    ]
    assert g["unresolved"] == []
    assert "unknown 1 -> 0" in s.out


def test_link_reprompts_on_an_unknown_repo_or_a_consumer(world):
    cfg = world({"orders-svc": [], "client": consume("ORDERS_BACKEND")})
    s = Session("l", "nope", "client", "orders-svc")
    run(cfg, s)
    assert "no repo named 'nope'" in s.out
    assert saved(cfg)["targets"][0]["to"] == "orders-svc"


def test_link_accepts_a_candidate_number(world):
    repos = {f"orders-{i}": [] for i in range(4)}
    cfg = world({**repos, "client": consume("https://orders.vendor.com", "http")})
    for i in range(4):  # four weak alias matches: over the ambiguity cap
        m = json.loads((cfg["atlas_dir"] / "repos" / f"orders-{i}.json").read_text())
        m["identifiers"] = ["orders"]
        atlas.write_json(cfg["atlas_dir"] / "repos" / f"orders-{i}.json", m)
    run(cfg, Session("l", "2"))
    assert saved(cfg)["targets"][0]["to"] == "orders-1"


def test_external_answer_defines_the_node_once(world):
    cfg = world(
        {
            "a": consume("WORKLOG_API_URL"),
            "b": consume("TIMESHEET_URL"),
        }
    )
    g = run(cfg, Session("e", "worklog", "Worklog SaaS", "e", "worklog"))
    doc = saved(cfg)
    assert doc["externals"]["worklog"]["label"] == "Worklog SaaS"
    assert {t["action"] for t in doc["targets"]} == {"external"}
    assert g["external_nodes"]["worklog"]["label"] == "Worklog SaaS"
    assert sorted(e["from"] for e in g["external_edges"]) == ["a", "b"]
    assert g["resolution"]["unknown"] == 0


def test_generic_with_a_group_collapses_every_consumer_of_the_var(world):
    cfg = world({n: consume("MONGO_URI") for n in "abc"})
    g = run(cfg, Session("g", "mongo", "shared-mongo", "Shared Mongo"))
    assert {t.get("group") for t in saved(cfg)["targets"]} == {"shared-mongo"}
    shared = [s for s in g["shared_datastores"] if s.get("source") == "override"]
    assert [(s["label"], sorted(s["repos"])) for s in shared] == [("Shared Mongo", ["a", "b", "c"])]


def test_generic_without_a_group_keeps_each_consumer_separate(world):
    cfg = world({n: consume("MONGO_URI") for n in "abc"})
    g = run(cfg, Session("g", "mongo", ""))
    assert all("group" not in t for t in saved(cfg)["targets"])
    assert len(g["generic"]) == 3 and g["shared_datastores"] == []


def test_per_consumer_answers_can_mix_shared_and_separate(world):
    cfg = world({n: consume("MONGO_URI") for n in "abc"})
    answers = ["p", "g", "mongo", "billing", "Billing", "g", "mongo", "billing", "g", "mongo", ""]
    g = run(cfg, Session(*answers))
    by_repo = {t["repo"]: t.get("group") for t in saved(cfg)["targets"]}
    assert by_repo == {"a": "billing", "b": "billing", "c": None}
    shared = [s for s in g["shared_datastores"] if s.get("source") == "override"]
    assert [sorted(s["repos"]) for s in shared] == [["a", "b"]]


def test_skip_persists_nothing(world):
    cfg = world({"client": consume("MYSTERY_URL")})
    s = Session("s")
    g = run(cfg, s)
    assert not cfg["resolve_overrides"].exists()
    assert [u["key"] for u in g["unresolved"]] == ["MYSTERY_URL"]
    assert "no answers saved" in s.out


def test_quitting_or_end_of_input_keeps_earlier_answers(world):
    cfg = world({"a": consume("ONE_URL"), "b": consume("TWO_URL")})
    g = run(cfg, Session("e", "vendor", "Vendor"))  # then EOF on the second target
    assert len(saved(cfg)["targets"]) == 1
    assert [u["key"] for u in g["unresolved"]] == ["TWO_URL"]


def test_q_stops_the_session(world):
    cfg = world({"a": consume("ONE_URL"), "b": consume("TWO_URL")})
    s = Session("q")
    g = run(cfg, s)
    assert len(g["unresolved"]) == 2 and not cfg["resolve_overrides"].exists()


def test_most_shared_targets_come_first_and_top_limits_the_session(world):
    cfg = world({"a": consume("RARE_URL"), "b": consume("COMMON_URL"), "c": consume("COMMON_URL")})
    s = Session("q")
    run(cfg, s, top=1)
    assert "COMMON_URL" in s.out and "RARE_URL" not in s.out


def test_an_invalid_menu_choice_asks_again(world):
    cfg = world({"client": consume("X_URL")})
    s = Session("z", "e", "vendor", "")
    run(cfg, s)
    assert saved(cfg)["externals"]["vendor"]["label"] == "vendor"
    assert saved(cfg)["targets"][0]["external"] == "vendor"


def test_other_keys_in_the_overrides_file_survive_a_session(world):
    cfg = world({"client": consume("X_URL")})
    cfg["resolve_overrides"].write_text(json.dumps({"version": 1, "note": "keep me"}))
    run(cfg, Session("e", "vendor", "Vendor"))
    assert saved(cfg)["note"] == "keep me"


def test_a_repeat_answer_replaces_the_earlier_one(world):
    cfg = world({"orders-svc": [], "client": consume("ORDERS_BACKEND")})
    atlas.write_json(
        cfg["resolve_overrides"],
        {
            "version": 1,
            "targets": [
                {"key": "ORDERS_BACKEND", "repo": "client", "action": "link", "to": "ghost"}
            ],
        },
    )
    run(cfg, Session("l", "orders-svc"))  # the stale link left the target unknown; answer again
    assert [t["to"] for t in saved(cfg)["targets"]] == ["orders-svc"]


def test_nothing_to_resolve(world):
    cfg = world({"client": []})
    s = Session()
    run(cfg, s)
    assert "nothing to resolve" in s.out


def test_unresolved_targets_group_by_key_and_put_the_most_shared_first(world, graph_of):
    cfg = world(
        {
            "a": consume("RARE_URL"),
            "b": consume("Common_URL"),
            "c": consume("COMMON_URL", "http"),
        }
    )
    targets = atlas.unresolved_targets(graph_of(cfg))
    assert [(t["key"], sorted(t["consumers"]), sorted(t["kinds"])) for t in targets] == [
        ("Common_URL", ["b", "c"], ["http", "other"]),
        ("RARE_URL", ["a"], ["other"]),
    ]
    assert [t["key"] for t in atlas.unresolved_targets(graph_of(cfg), top=1)] == ["Common_URL"]


def test_main_dispatches_resolve(make_cfg, tmp_path, monkeypatch):
    make_cfg()
    seen = {}
    monkeypatch.setattr(
        atlas, "cmd_resolve", lambda cfg, args: seen.update(top=args.top, sug=args.suggested)
    )
    argv = ["atlas.py", "--config", str(tmp_path / "config.json"), "resolve", "--top", "3"]
    monkeypatch.setattr("sys.argv", [*argv, "--suggested"])
    atlas.main()
    assert seen == {"top": 3, "sug": True}


def test_linking_a_shared_target_to_one_consumer_links_the_others(world):
    cfg = world({"orders": consume("ORDERS_DB_URL"), "reports": consume("ORDERS_DB_URL")})
    s = Session("l", "orders")
    g = run(cfg, s)
    assert [(t["repo"], t["to"]) for t in saved(cfg)["targets"]] == [("reports", "orders")]
    assert [(u["repo"], u["key"]) for u in g["unresolved"]] == [("orders", "ORDERS_DB_URL")]
    assert "1 answers saved" in s.out


def test_a_group_cannot_be_reused_under_a_different_tag(world):
    cfg = world({"a": consume("MONGO_URI"), "b": consume("PG_URI")})
    atlas.write_json(
        cfg["resolve_overrides"],
        {"version": 1, "groups": {"shared": {"label": "Shared", "tag": "mongo"}}},
    )
    s = Session("g", "mongo", "shared", "q")
    run(cfg, s)  # first target (most consumers, then name): MONGO_URI joins the group
    s = Session("g", "postgres", "shared", "pg", "PG")
    run(cfg, s)
    assert "group 'shared' is tagged 'mongo'" in s.out
    groups = {t["key"]: t.get("group") for t in saved(cfg)["targets"]}
    assert groups["PG_URI"] == "pg"


def test_a_file_corrupted_mid_session_stops_with_its_path(world):
    cfg = world({"client": consume("X_URL")})
    session = Session("e", "vendor", "Vendor")

    def ask(prompt):
        if "external id" in prompt:
            cfg["resolve_overrides"].write_text("{bad")
        return session.ask(prompt)

    with pytest.raises(SystemExit) as e:
        atlas.cmd_resolve(
            cfg, argparse.Namespace(top=None, suggested=False), ask=ask, say=session.say
        )
    assert str(cfg["resolve_overrides"]) in str(e.value)


def suggest(cfg, graph_of, key, fingerprint=None, **answer):
    """Store a drafted answer for `key` the way `suggest` would, current unless told otherwise."""
    t = next(t for t in atlas.unresolved_targets(graph_of(cfg)) if t["key"] == key)
    path = atlas.suggestions_path(cfg)
    found = atlas.load_suggestions(path)
    found[key.lower()] = {
        "key": key,
        "confidence": "high",
        "reason": "it fits",
        "basis": "orders.internal",
        **answer,
        "fingerprint": fingerprint or atlas.target_fingerprint(t, atlas.resolver_prompt_hash()),
    }
    atlas.write_suggestions(path, found)


def test_enter_accepts_a_suggested_link(world, graph_of):
    cfg = world({"orders-svc": [], "client": consume("ORDERS_BACKEND")})
    suggest(cfg, graph_of, "ORDERS_BACKEND", action="link", to="orders-svc")
    s = Session("")
    g = run(cfg, s)
    assert "suggested: link -> orders-svc (high) - it fits [basis: orders.internal]" in s.out
    assert "[enter] accept" in s.prompts[0]
    assert saved(cfg)["targets"] == [
        {"key": "ORDERS_BACKEND", "repo": "client", "action": "link", "to": "orders-svc"}
    ]
    assert g["unresolved"] == []


def test_enter_accepts_a_suggested_external_and_defines_it(world, graph_of):
    cfg = world({"a": consume("WORKLOG_API_URL")})
    suggest(
        cfg, graph_of, "WORKLOG_API_URL", action="external", external="worklog", label="Worklog"
    )
    g = run(cfg, Session(""))
    doc = saved(cfg)
    assert doc["externals"]["worklog"]["label"] == "Worklog"
    assert doc["targets"] == [
        {"key": "WORKLOG_API_URL", "repo": "a", "action": "external", "external": "worklog"}
    ]
    assert g["external_nodes"]["worklog"]["label"] == "Worklog"


def test_accepting_an_external_that_exists_keeps_its_definition(world, graph_of):
    cfg = world({"a": consume("WORKLOG_API_URL")})
    atlas.write_json(
        cfg["resolve_overrides"],
        {"version": 1, "externals": {"worklog": {"label": "Mine", "hosts": []}}},
    )
    suggest(cfg, graph_of, "WORKLOG_API_URL", action="external", external="worklog", label="Theirs")
    run(cfg, Session(""))
    assert saved(cfg)["externals"]["worklog"]["label"] == "Mine"


def test_enter_accepts_a_suggested_generic_and_keeps_consumers_separate(world, graph_of):
    cfg = world({n: consume("MONGO_URI") for n in "ab"})
    suggest(cfg, graph_of, "MONGO_URI", action="generic", tag="mongo")
    g = run(cfg, Session(""))
    assert {(t["repo"], t["tag"]) for t in saved(cfg)["targets"]} == {
        ("a", "mongo"),
        ("b", "mongo"),
    }
    assert all("group" not in t for t in saved(cfg)["targets"])
    assert len(g["generic"]) == 2 and g["shared_datastores"] == []


def test_a_stale_suggestion_is_not_offered(world, graph_of):
    cfg = world({"orders-svc": [], "client": consume("ORDERS_BACKEND")})
    suggest(cfg, graph_of, "ORDERS_BACKEND", fingerprint="stale", action="link", to="orders-svc")
    s = Session("")
    run(cfg, s)
    assert "suggested" not in s.out
    assert "'' is not a choice" in s.out
    assert not cfg["resolve_overrides"].exists()


def test_a_manual_choice_overrides_the_suggestion(world, graph_of):
    cfg = world({"orders-svc": [], "client": consume("ORDERS_BACKEND")})
    suggest(cfg, graph_of, "ORDERS_BACKEND", action="link", to="orders-svc")
    run(cfg, Session("e", "vendor", "Vendor"))
    assert [t["action"] for t in saved(cfg)["targets"]] == ["external"]


def test_a_none_suggestion_is_shown_but_enter_does_not_accept_it(world, graph_of):
    cfg = world({"client": consume("MYSTERY_URL")})
    suggest(cfg, graph_of, "MYSTERY_URL", action="none", reason="nothing fits")
    s = Session("")
    run(cfg, s)
    assert "suggested: none (high) - nothing fits" in s.out
    assert "[enter] accept" not in s.prompts[0]
    assert not cfg["resolve_overrides"].exists()


def test_a_suggested_link_whose_repo_is_gone_is_not_offered(world, graph_of):
    cfg = world({"orders-svc": [], "client": consume("ORDERS_BACKEND")})
    suggest(cfg, graph_of, "ORDERS_BACKEND", action="link", to="retired-svc")
    s = Session("")
    run(cfg, s)
    assert "[enter] accept" not in s.prompts[0]
    assert not cfg["resolve_overrides"].exists()


def test_per_consumer_answers_do_not_offer_the_suggestion(world, graph_of):
    cfg = world({n: consume("MONGO_URI") for n in "ab"})
    suggest(cfg, graph_of, "MONGO_URI", action="generic", tag="mongo")
    s = Session("p", "s", "s")
    run(cfg, s)
    assert "[enter] accept" in s.prompts[0]
    assert all("[enter] accept" not in p for p in s.prompts[1:])


def test_suggested_walks_only_targets_with_an_answer_to_accept(world, graph_of):
    cfg = world(
        {
            "orders-svc": [],
            "client": consume("ORDERS_BACKEND") + consume("OTHER_URL"),
            "other": consume("OTHER_URL") + consume("MYSTERY_URL"),
        }
    )
    suggest(cfg, graph_of, "ORDERS_BACKEND", action="link", to="orders-svc")
    suggest(cfg, graph_of, "MYSTERY_URL", action="none")
    s = Session("")
    run(cfg, s, suggested=True)
    assert "ORDERS_BACKEND" in s.out
    assert "OTHER_URL" not in s.out and "MYSTERY_URL" not in s.out
    assert len(saved(cfg)["targets"]) == 1


def test_suggested_with_nothing_to_accept_says_so(world):
    cfg = world({"client": consume("MYSTERY_URL")})
    s = Session()
    run(cfg, s, suggested=True)
    assert "nothing to resolve: no current suggestions" in s.out


def test_a_malformed_suggestions_file_stops_resolve_with_its_path(world):
    cfg = world({"client": consume("MYSTERY_URL")})
    atlas.suggestions_path(cfg).write_text("[]")
    with pytest.raises(SystemExit) as e:
        run(cfg, Session())
    assert str(atlas.suggestions_path(cfg)) in str(e.value)
