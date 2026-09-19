import argparse
import json

import pytest

import atlas


@pytest.fixture
def gen_args():
    return argparse.Namespace(pull=False, full=False, dry_run=False)


@pytest.fixture
def stub_kiro(monkeypatch):
    calls = []

    def fake(cfg, repo, prompt, log_path, mapper="explore"):
        calls.append(prompt)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return {
            "summary": "s",
            "overview": "o",
            "domain": "unassigned",
            "kind": "service",
            "identifiers": [],
            "exposes": [],
            "consumes": [],
            "datastores": [],
        }

    monkeypatch.setattr(atlas, "run_kiro", fake)
    return calls


def commit(repo, rel, text):
    (repo / rel).write_text(text)
    atlas.git(repo, "add", "-A")
    atlas.git(repo, "-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "-qm", "change")
    return atlas.git(repo, "rev-parse", "HEAD")


def seed(cfg, make_manifest, repo, name, head, **meta):
    base = {
        "repo_path": str(repo),
        "commit": head,
        "generated_at": atlas.now().isoformat(),
        "mode": "full",
        "model": "m",
        "last_full_at": atlas.now().isoformat(),
        "dropped_without_evidence": [],
        "prompt_hash": atlas.prompt_hash(cfg["mapper_mode"]),
        "facts_version": atlas.FACTS_VERSION,
    }
    base.update(meta)
    return make_manifest(cfg, name, _meta=base, summary="old summary")


def test_skips_when_head_is_unchanged(make_repo, make_cfg, make_manifest, gen_args, stub_kiro):
    repo = make_repo("svc")
    cfg = make_cfg()
    seed(cfg, make_manifest, repo, "svc", atlas.git(repo, "rev-parse", "HEAD"))
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "skip"
    assert stub_kiro == []


def test_restamps_when_only_ignored_files_changed(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    repo = make_repo("svc", files={"main.go": "package main", "README.md": "x"})
    cfg = make_cfg(ignore_changes=["*.md"])
    seed(cfg, make_manifest, repo, "svc", atlas.git(repo, "rev-parse", "HEAD"))
    head = commit(repo, "README.md", "changed")
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "restamp"
    assert stub_kiro == []
    written = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())
    assert written["_meta"]["commit"] == head
    assert written["summary"] == "old summary"


def test_updates_and_passes_the_changed_files_to_the_prompt(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    repo = make_repo("svc", files={"main.go": "package main"})
    cfg = make_cfg()
    seed(cfg, make_manifest, repo, "svc", atlas.git(repo, "rev-parse", "HEAD"))
    commit(repo, "main.go", "package main // v2")
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "update"
    assert len(stub_kiro) == 1
    assert "main.go" in stub_kiro[0]
    assert "old summary" in stub_kiro[0]


def test_full_regeneration_when_forced(make_repo, make_cfg, make_manifest, gen_args, stub_kiro):
    repo = make_repo("svc")
    cfg = make_cfg()
    seed(cfg, make_manifest, repo, "svc", atlas.git(repo, "rev-parse", "HEAD"))
    gen_args.full = True
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "full"
    assert "Current entry" not in stub_kiro[0]


def test_full_regeneration_when_the_old_commit_is_gone(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    repo = make_repo("svc")
    cfg = make_cfg()
    seed(cfg, make_manifest, repo, "svc", "f" * 40)
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "full"


def test_oversized_update_prompt_falls_back_to_full(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro, monkeypatch
):
    repo = make_repo("svc", files={"main.go": "package main"})
    cfg = make_cfg()
    seed(cfg, make_manifest, repo, "svc", atlas.git(repo, "rev-parse", "HEAD"))
    commit(repo, "main.go", "package main // v2")
    monkeypatch.setattr(atlas, "MAX_UPDATE_PROMPT_BYTES", 200)
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "full"
    assert "Current entry" not in stub_kiro[0]


def test_dry_run_never_calls_kiro(make_repo, make_cfg, gen_args, stub_kiro):
    repo = make_repo("svc")
    cfg = make_cfg()
    gen_args.dry_run = True
    _, mode, detail = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "dry-run"
    assert detail == "full"
    assert stub_kiro == []
    assert not (cfg["atlas_dir"] / "repos" / "svc.json").exists()


def test_off_list_domain_is_recorded_in_meta(make_repo, make_cfg, gen_args, monkeypatch):
    repo = make_repo("svc")
    cfg = make_cfg(domains=["payments"])
    monkeypatch.setattr(
        atlas, "run_kiro", lambda *a, **k: {"summary": "s", "domain": "invented", "kind": "service"}
    )
    atlas.process_repo(cfg, "svc", repo, gen_args)
    written = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())
    assert written["domain"] == "unassigned"
    assert written["_meta"]["domain_rejected"] == "invented"


def test_unparseable_output_is_retried_once_then_reported(
    make_repo, make_cfg, gen_args, monkeypatch
):
    repo = make_repo("svc")
    cfg = make_cfg()
    attempts = []

    def always_bad(cfg_, repo_, prompt, log_path, mapper="explore"):
        attempts.append(prompt)
        raise ValueError("no ATLAS_JSON block")

    monkeypatch.setattr(atlas, "run_kiro", always_bad)
    with pytest.raises(ValueError):
        atlas.process_repo(cfg, "svc", repo, gen_args)
    assert len(attempts) == 2


def test_parse_output_reads_the_last_block():
    text = 'noise\n<<<ATLAS_JSON\n{"a": 1}\nATLAS_JSON>>>\n'
    assert atlas.parse_output(text) == {"a": 1}


def test_parse_output_tolerates_code_fences():
    text = '<<<ATLAS_JSON\n```json\n{"a": 1}\n```\nATLAS_JSON>>>'
    assert atlas.parse_output(text) == {"a": 1}


def test_parse_output_rejects_missing_block():
    with pytest.raises(ValueError):
        atlas.parse_output("the model rambled and stopped")


def test_parse_output_rejects_a_non_object():
    with pytest.raises(ValueError):
        atlas.parse_output("<<<ATLAS_JSON\n[1,2]\nATLAS_JSON>>>")


def test_run_kiro_appends_each_attempt_to_the_log(make_repo, make_cfg, monkeypatch, tmp_path):
    repo = make_repo("svc")
    cfg = make_cfg()
    log = tmp_path / "logs" / "svc.log"

    class Result:
        returncode = 0
        stdout = '<<<ATLAS_JSON\n{"summary": "s"}\nATLAS_JSON>>>'
        stderr = ""

    monkeypatch.setattr(atlas.subprocess, "run", lambda *a, **k: Result())
    atlas.run_kiro(cfg, repo, "prompt one", log)
    atlas.run_kiro(cfg, repo, "prompt two", log)
    text = log.read_text()
    assert text.count("=== attempt") == 2


class _Result:
    returncode = 0
    stderr = ""

    def __init__(self, stdout):
        self.stdout = stdout


def _spy_run(monkeypatch, stdout='<<<ATLAS_JSON\n{"summary": "s"}\nATLAS_JSON>>>'):
    seen = {}

    def spy(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["kwargs"] = kwargs
        return _Result(stdout)

    monkeypatch.setattr(atlas.subprocess, "run", spy)
    return seen


def test_explore_mode_uses_the_read_only_mapper_agent(make_cfg, monkeypatch, tmp_path):
    cfg = make_cfg()
    seen = _spy_run(monkeypatch)
    atlas.run_kiro(cfg, tmp_path, "p", tmp_path / "logs" / "svc.log")
    cmd = seen["cmd"]
    assert cmd[cmd.index("--agent") + 1] == "atlas-mapper"


def test_empty_kiro_agent_drops_the_agent_flag(make_cfg, monkeypatch, tmp_path):
    cfg = make_cfg(kiro_agent="")
    seen = _spy_run(monkeypatch)
    atlas.run_kiro(cfg, tmp_path, "p", tmp_path / "logs" / "svc.log")
    assert "--agent" not in seen["cmd"]


def test_the_prompt_is_sent_on_stdin_and_not_on_the_command_line(make_cfg, monkeypatch, tmp_path):
    cfg = make_cfg()
    seen = _spy_run(monkeypatch)
    atlas.run_kiro(cfg, tmp_path, "map this repo", tmp_path / "logs" / "svc.log")
    assert seen["kwargs"]["input"] == "map this repo"
    assert "map this repo" not in seen["cmd"]
    assert "stdin" not in seen["kwargs"]


def test_the_log_records_the_prompt_length_not_its_text(make_cfg, monkeypatch, tmp_path):
    cfg = make_cfg()
    log = tmp_path / "logs" / "svc.log"
    _spy_run(monkeypatch)
    atlas.run_kiro(cfg, tmp_path, "secret-looking prompt text", log)
    text = log.read_text(encoding="utf-8")
    assert "<prompt 26 chars>" in text
    assert "secret-looking prompt text" not in text


def test_command_line_disables_line_wrapping(make_cfg, monkeypatch, tmp_path):
    cfg = make_cfg()
    seen = _spy_run(monkeypatch)
    atlas.run_kiro(cfg, tmp_path, "p", tmp_path / "logs" / "svc.log")
    cmd = seen["cmd"]
    assert cmd[cmd.index("--wrap") + 1] == "never"


def test_parse_output_recovers_the_block_from_a_stream_json_transcript():
    lines = [
        json.dumps({"type": "assistant", "text": "<<<ATLAS_JSON\n"}),
        json.dumps({"type": "assistant", "text": '{"summary": "s"}'}),
        json.dumps({"type": "assistant", "text": "\nATLAS_JSON>>>\n"}),
        json.dumps({"type": "result", "exit_code": 0}),
    ]
    assert atlas.parse_output("\n".join(lines)) == {"summary": "s"}


def test_parse_output_prefers_the_last_block_over_a_prompt_echo():
    lines = [
        json.dumps({"type": "tool_use", "input": {"prompt": "print <<<ATLAS_JSON here"}}),
        json.dumps(
            {"type": "assistant", "text": '<<<ATLAS_JSON\n{"summary": "real"}\nATLAS_JSON>>>'}
        ),
    ]
    assert atlas.parse_output("\n".join(lines))["summary"] == "real"


def test_parse_output_rejects_a_transcript_without_a_block():
    with pytest.raises(ValueError):
        atlas.parse_output(json.dumps({"type": "assistant", "text": "I could not finish"}))


def test_parse_output_rejects_a_block_broken_by_hard_wrapping():
    with pytest.raises(ValueError):
        atlas.parse_output('<<<ATLAS_JSON\n{"summary": "a very long\nvalue"}\nATLAS_JSON>>>')


def test_timeout_is_logged_and_blamed_on_approval(make_cfg, monkeypatch, tmp_path):
    cfg = make_cfg()
    log = tmp_path / "logs" / "svc.log"

    def boom(*_a, **_k):
        raise atlas.subprocess.TimeoutExpired(cmd="kiro-cli", timeout=1, output=b"partial output")

    monkeypatch.setattr(atlas.subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="approval"):
        atlas.run_kiro(cfg, tmp_path, "p", log)
    text = log.read_text(encoding="utf-8")
    assert "TIMEOUT" in text
    assert "partial output" in text


def test_a_naive_last_full_at_forces_a_full_run(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    repo = make_repo("svc", files={"main.go": "package main"})
    cfg = make_cfg()
    seed(
        cfg,
        make_manifest,
        repo,
        "svc",
        atlas.git(repo, "rev-parse", "HEAD"),
        last_full_at="2026-01-01T00:00:00",
    )
    commit(repo, "main.go", "package main // v2")
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "full"


def test_a_manifest_without_a_prompt_hash_is_regenerated_in_full(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    repo = make_repo("svc")
    cfg = make_cfg()
    seed(cfg, make_manifest, repo, "svc", atlas.git(repo, "rev-parse", "HEAD"), prompt_hash=None)
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "full"
    assert len(stub_kiro) == 1


def test_a_changed_prompt_hash_forces_a_full_run_even_when_head_is_unchanged(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    repo = make_repo("svc")
    cfg = make_cfg()
    seed(
        cfg,
        make_manifest,
        repo,
        "svc",
        atlas.git(repo, "rev-parse", "HEAD"),
        prompt_hash="000000000000",
    )
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "full"


def test_a_changed_prompt_hash_beats_an_update(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    repo = make_repo("svc", files={"main.go": "package main"})
    cfg = make_cfg()
    seed(
        cfg,
        make_manifest,
        repo,
        "svc",
        atlas.git(repo, "rev-parse", "HEAD"),
        prompt_hash="000000000000",
    )
    commit(repo, "main.go", "package main // v2")
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "full"
    assert "Current entry" not in stub_kiro[0]


def test_a_matching_prompt_hash_and_unchanged_head_still_skip(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    repo = make_repo("svc")
    cfg = make_cfg()
    seed(cfg, make_manifest, repo, "svc", atlas.git(repo, "rev-parse", "HEAD"))
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "skip"


def test_dry_run_reports_a_prompt_hash_change_as_full(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    repo = make_repo("svc")
    cfg = make_cfg()
    seed(
        cfg,
        make_manifest,
        repo,
        "svc",
        atlas.git(repo, "rev-parse", "HEAD"),
        prompt_hash="000000000000",
    )
    gen_args.dry_run = True
    _, mode, detail = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert (mode, detail) == ("dry-run", "full")
    assert stub_kiro == []


def test_the_prompt_hash_is_recorded_in_meta(make_repo, make_cfg, gen_args, stub_kiro):
    repo = make_repo("svc")
    cfg = make_cfg(mapper_mode="explore")
    atlas.process_repo(cfg, "svc", repo, gen_args)
    written = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())
    assert written["_meta"]["prompt_hash"] == atlas.prompt_hash(cfg["mapper_mode"])


def test_a_restamp_keeps_the_prompt_hash(make_repo, make_cfg, make_manifest, gen_args, stub_kiro):
    repo = make_repo("svc", files={"main.go": "package main", "README.md": "x"})
    cfg = make_cfg(ignore_changes=["*.md"])
    seed(cfg, make_manifest, repo, "svc", atlas.git(repo, "rev-parse", "HEAD"))
    commit(repo, "README.md", "changed")
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "restamp"
    written = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())
    assert written["_meta"]["prompt_hash"] == atlas.prompt_hash(cfg["mapper_mode"])


# ---------- deterministic facts ----------

SERVICE = "apiVersion: v1\nkind: Service\nmetadata:\n  name: orders-api\n"


def test_deterministic_facts_are_merged_into_the_model_entry(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    repo = make_repo("svc", files={"k8s/svc.yaml": SERVICE, "CODEOWNERS": "* @org/platform\n"})
    cfg = make_cfg()
    atlas.process_repo(cfg, "svc", repo, gen_args)
    written = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())
    assert "orders-api" in written["identifiers"]
    assert written["owners"] == ["@org/platform"]
    assert [e["key"] for e in written["exposes"]] == ["orders-api"]
    assert written["_meta"]["facts_version"] == atlas.FACTS_VERSION
    assert written["_meta"]["deterministic"]["owners"] == ["@org/platform"]


def test_a_stale_facts_version_restamps_instead_of_skipping(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    """Adding an extractor does not change the prompt hash, so without this a repo whose commit
    has not moved would never pick the new facts up."""
    repo = make_repo("svc", files={"k8s/svc.yaml": SERVICE})
    cfg = make_cfg()
    head = atlas.git(repo, "rev-parse", "HEAD")
    seed(cfg, make_manifest, repo, "svc", head, facts_version=atlas.FACTS_VERSION - 1)
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "restamp"
    assert stub_kiro == []
    written = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())
    assert written["identifiers"] == ["orders-api"]
    assert written["_meta"]["facts_version"] == atlas.FACTS_VERSION


def test_a_stale_facts_version_restamps_even_when_a_full_regen_is_due(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    """The commit has not moved, so the model would see nothing new. The full regen is for
    entries that drifted through updates, not a reason to pay for every dormant repo in the org
    on the first run after a FACTS_VERSION bump."""
    repo = make_repo("svc", files={"k8s/svc.yaml": SERVICE})
    cfg = make_cfg()
    head = atlas.git(repo, "rev-parse", "HEAD")
    seed(
        cfg,
        make_manifest,
        repo,
        "svc",
        head,
        facts_version=atlas.FACTS_VERSION - 1,
        last_full_at="2020-01-01T00:00:00+00:00",
    )
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "restamp"
    assert stub_kiro == []
    written = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())
    assert written["identifiers"] == ["orders-api"]


def test_meta_records_the_remote_url_and_the_last_commit_date(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    repo = make_repo("svc")
    atlas.git(repo, "remote", "add", "origin", "git@github.com:org/orders.git")
    cfg = make_cfg()
    atlas.process_repo(cfg, "svc", repo, gen_args)
    meta = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())["_meta"]
    assert meta["remote_url"] == "https://github.com/org/orders"
    assert meta["last_commit_at"].startswith(str(atlas.now().year))


def test_a_clone_without_an_origin_records_no_remote_url(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro, capsys
):
    repo = make_repo("svc")
    cfg = make_cfg()
    atlas.process_repo(cfg, "svc", repo, gen_args)
    meta = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())["_meta"]
    assert meta["remote_url"] is None
    assert meta["last_commit_at"] is not None


def test_a_restamp_backfills_the_git_metadata(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    """The whole org gains the two fields on the free path after a facts_version bump."""
    repo = make_repo("svc")
    atlas.git(repo, "remote", "add", "origin", "https://github.com/org/orders.git")
    cfg = make_cfg()
    head = atlas.git(repo, "rev-parse", "HEAD")
    seed(cfg, make_manifest, repo, "svc", head, facts_version=atlas.FACTS_VERSION - 1)
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "restamp"
    meta = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())["_meta"]
    assert meta["remote_url"] == "https://github.com/org/orders"
    assert meta["last_commit_at"] is not None


def test_a_skipped_repo_reads_git_only_to_check_its_head(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro, monkeypatch
):
    repo = make_repo("svc")
    cfg = make_cfg()
    seed(cfg, make_manifest, repo, "svc", atlas.git(repo, "rev-parse", "HEAD"))
    calls = []
    original = atlas.git
    monkeypatch.setattr(
        atlas, "git", lambda r, *a, **kw: calls.append(a[0]) or original(r, *a, **kw)
    )
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "skip"
    assert calls == ["rev-parse"]


def test_a_manifest_predating_the_extractors_restamps(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    repo = make_repo("svc", files={"k8s/svc.yaml": SERVICE})
    cfg = make_cfg()
    head = atlas.git(repo, "rev-parse", "HEAD")
    seed(cfg, make_manifest, repo, "svc", head, facts_version=None)
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "restamp"
    assert stub_kiro == []


def test_repeated_restamps_do_not_duplicate_deterministic_items(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    repo = make_repo("svc", files={"k8s/svc.yaml": SERVICE, "README.md": "x"})
    cfg = make_cfg(ignore_changes=["*.md"])
    seed(cfg, make_manifest, repo, "svc", atlas.git(repo, "rev-parse", "HEAD"))
    for i in range(3):
        commit(repo, "README.md", f"changed {i}")
        _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
        assert mode == "restamp"
    written = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())
    assert written["identifiers"] == ["orders-api"]
    assert len(written["exposes"]) == 1


def test_a_restamp_picks_up_a_codeowners_change_without_a_model_call(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    """CODEOWNERS lives under .github/*, which stays in ignore_changes: owners are deterministic
    now, so the free restamp path refreshes them and the update prompt is not worth its credit."""
    repo = make_repo("svc", files={".github/CODEOWNERS": "* @org/old-team\n"})
    cfg = make_cfg()
    seed(cfg, make_manifest, repo, "svc", atlas.git(repo, "rev-parse", "HEAD"))
    commit(repo, ".github/CODEOWNERS", "* @org/new-team\n")
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "restamp"
    assert stub_kiro == []
    written = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())
    assert written["owners"] == ["@org/new-team"]


def test_a_deterministic_identifier_that_is_generic_never_reaches_the_manifest(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    repo = make_repo(
        "svc", files={"k8s/svc.yaml": "apiVersion: v1\nkind: Service\nmetadata:\n  name: gateway\n"}
    )
    cfg = make_cfg()
    atlas.process_repo(cfg, "svc", repo, gen_args)
    written = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())
    assert written["identifiers"] == []


# ---------- bundle mode ----------


def test_bundle_mode_uses_the_tool_free_bundle_agent(make_cfg, monkeypatch, tmp_path):
    cfg = make_cfg()
    seen = _spy_run(monkeypatch)
    atlas.run_kiro(cfg, tmp_path, "p", tmp_path / "logs" / "svc.log", mapper="bundle")
    cmd = seen["cmd"]
    assert cmd[cmd.index("--agent") + 1] == "atlas-bundle"


def test_an_explicit_agent_overrides_both_modes(make_cfg, monkeypatch, tmp_path):
    cfg = make_cfg(kiro_agent="mine")
    seen = _spy_run(monkeypatch)
    atlas.run_kiro(cfg, tmp_path, "p", tmp_path / "logs" / "svc.log", mapper="bundle")
    cmd = seen["cmd"]
    assert cmd[cmd.index("--agent") + 1] == "mine"


def test_bundle_mode_never_runs_inside_the_mapped_repo(make_cfg, monkeypatch, tmp_path):
    """Running in the repo loads that repo's own steering, hooks and MCP servers unattended."""
    cfg = make_cfg()
    seen = _spy_run(monkeypatch)
    atlas.run_kiro(cfg, tmp_path, "p", tmp_path / "logs" / "svc.log", mapper="bundle")
    assert seen["kwargs"]["cwd"] == cfg["atlas_dir"]


def test_explore_mode_still_runs_inside_the_repo(make_cfg, monkeypatch, tmp_path):
    cfg = make_cfg()
    seen = _spy_run(monkeypatch)
    atlas.run_kiro(cfg, tmp_path, "p", tmp_path / "logs" / "svc.log", mapper="explore")
    assert seen["kwargs"]["cwd"] == tmp_path


def test_bundle_mode_sends_the_bundle_and_no_exploration_instructions(
    make_repo, make_cfg, gen_args, stub_kiro
):
    repo = make_repo(
        "svc", files={"README.md": "# svc", "internal/c.go": 'u := "https://orders.internal"'}
    )
    cfg = make_cfg()
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    prompt = stub_kiro[0]
    assert mode == "full"
    assert "## Tree" in prompt
    assert "orders.internal" in prompt
    assert "Explore efficiently" not in prompt


def test_explore_mode_sends_the_exploration_prompt(make_repo, make_cfg, gen_args, stub_kiro):
    repo = make_repo("svc", files={"README.md": "# svc"})
    cfg = make_cfg(mapper_mode="explore")
    atlas.process_repo(cfg, "svc", repo, gen_args)
    prompt = stub_kiro[0]
    assert "Explore efficiently" in prompt
    assert "## Tree" not in prompt


def test_explore_repos_forces_one_repo_back_to_exploring(make_repo, make_cfg, gen_args, stub_kiro):
    repo = make_repo("svc", files={"README.md": "# svc"})
    cfg = make_cfg(explore_repos=["svc"])
    atlas.process_repo(cfg, "svc", repo, gen_args)
    assert "Explore efficiently" in stub_kiro[0]


def test_a_bundle_update_carries_only_the_changed_files(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    repo = make_repo(
        "svc",
        files={
            "a.go": 'u := "https://untouched.internal"',
            "b.go": 'u := "https://before.internal"',
        },
    )
    cfg = make_cfg()
    seed(cfg, make_manifest, repo, "svc", atlas.git(repo, "rev-parse", "HEAD"))
    commit(repo, "b.go", 'u := "https://after.internal"')
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "update"
    assert "after.internal" in stub_kiro[0]
    assert "untouched.internal" not in stub_kiro[0]


def big_repo_files(count=60):
    files = {
        f"pkg{i}/package.json": json.dumps({"name": f"p{i}", "x": "y" * 9000}) for i in range(count)
    }
    files["internal/c.go"] = "\n".join(f'u{i} := "https://h{i}.internal"' for i in range(500))
    return files


def test_the_bundle_prompt_stays_within_the_configured_budget(
    make_repo, make_cfg, gen_args, stub_kiro
):
    repo = make_repo("svc", files=big_repo_files())
    cfg = make_cfg(bundle_budget_bytes=120 * 1024)
    atlas.process_repo(cfg, "svc", repo, gen_args)
    assert len(stub_kiro[0].encode()) <= cfg["bundle_budget_bytes"]


def test_a_bigger_budget_sends_more_of_the_repo(make_repo, make_cfg, gen_args, stub_kiro):
    repo = make_repo("svc", files=big_repo_files())
    small = make_cfg(bundle_budget_bytes=40 * 1024)
    atlas.process_repo(small, "svc", repo, gen_args)
    large = make_cfg(bundle_budget_bytes=400 * 1024)
    gen_args.full = True  # the first call already mapped this commit
    atlas.process_repo(large, "svc", repo, gen_args)
    assert len(stub_kiro[1].encode()) > len(stub_kiro[0].encode())
    assert len(stub_kiro[1].encode()) <= large["bundle_budget_bytes"]


def test_an_update_prompt_keeps_the_smaller_update_cap(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    """A full bundle may be 400 KB; an update carries the old entry too, so it keeps its own cap."""
    repo = make_repo("svc", files=big_repo_files())
    cfg = make_cfg(bundle_budget_bytes=400 * 1024, max_changed_files_for_update=150)
    seed(cfg, make_manifest, repo, "svc", atlas.git(repo, "rev-parse", "HEAD"))
    for i in range(60):
        (repo / f"pkg{i}" / "package.json").write_text(
            json.dumps({"name": f"p{i}", "x": "z" * 9000})
        )
    commit(repo, "internal/c.go", "\n".join(f'u{i} := "https://j{i}.internal"' for i in range(500)))
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "update"
    assert len(stub_kiro[0].encode()) <= atlas.MAX_UPDATE_PROMPT_BYTES


def test_meta_records_the_prompt_bytes_that_were_sent(make_repo, make_cfg, gen_args, stub_kiro):
    repo = make_repo("svc")
    cfg = make_cfg()
    atlas.process_repo(cfg, "svc", repo, gen_args)
    meta = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())["_meta"]
    assert meta["prompt_bytes"] == len(stub_kiro[0].encode("utf-8"))


def test_a_restamp_records_no_prompt_bytes(make_repo, make_cfg, make_manifest, gen_args, stub_kiro):
    repo = make_repo("svc", files={"main.go": "package main", "README.md": "x"})
    cfg = make_cfg(ignore_changes=["*.md"])
    seed(cfg, make_manifest, repo, "svc", atlas.git(repo, "rev-parse", "HEAD"))
    commit(repo, "README.md", "changed")
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "restamp"
    meta = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())["_meta"]
    assert meta["prompt_bytes"] == 0


def test_meta_records_the_mapper_mode_and_its_prompt_hash(make_repo, make_cfg, gen_args, stub_kiro):
    repo = make_repo("svc")
    cfg = make_cfg()
    atlas.process_repo(cfg, "svc", repo, gen_args)
    meta = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())["_meta"]
    assert meta["mapper_mode"] == "bundle"
    assert meta["prompt_hash"] == atlas.prompt_hash("bundle")


def test_switching_mapper_mode_regenerates_the_entry(
    make_repo, make_cfg, make_manifest, gen_args, stub_kiro
):
    repo = make_repo("svc")
    cfg = make_cfg()
    head = atlas.git(repo, "rev-parse", "HEAD")
    seed(cfg, make_manifest, repo, "svc", head, prompt_hash=atlas.prompt_hash("explore"))
    _, mode, _ = atlas.process_repo(cfg, "svc", repo, gen_args)
    assert mode == "full"


def test_the_two_mapper_modes_have_different_prompt_hashes():
    assert atlas.prompt_hash("bundle") != atlas.prompt_hash("explore")


def test_a_configured_domain_overrides_the_model(make_repo, make_cfg, gen_args, monkeypatch):
    repo = make_repo("orders-service")
    cfg = make_cfg(domains=["payments"], repo_domains={"orders-*": "payments"})
    monkeypatch.setattr(
        atlas, "run_kiro", lambda *a, **k: {"summary": "s", "domain": "invented", "kind": "service"}
    )
    atlas.process_repo(cfg, "orders-service", repo, gen_args)
    written = json.loads((cfg["atlas_dir"] / "repos" / "orders-service.json").read_text())
    assert written["domain"] == "payments"
    assert written["_meta"]["domain_source"] == "config"


def test_an_unmatched_repo_keeps_the_model_domain(make_repo, make_cfg, gen_args, monkeypatch):
    repo = make_repo("billing")
    cfg = make_cfg(domains=["payments"], repo_domains={"orders-*": "payments"})
    monkeypatch.setattr(
        atlas, "run_kiro", lambda *a, **k: {"summary": "s", "domain": "payments", "kind": "service"}
    )
    atlas.process_repo(cfg, "billing", repo, gen_args)
    written = json.loads((cfg["atlas_dir"] / "repos" / "billing.json").read_text())
    assert written["domain"] == "payments"
    assert written["_meta"]["domain_source"] == "model"


def test_the_first_matching_domain_glob_wins(make_repo, make_cfg, gen_args, monkeypatch):
    repo = make_repo("orders-api")
    cfg = make_cfg(repo_domains={"orders-api": "identity", "orders-*": "payments"})
    monkeypatch.setattr(
        atlas, "run_kiro", lambda *a, **k: {"summary": "s", "domain": "x", "kind": "service"}
    )
    atlas.process_repo(cfg, "orders-api", repo, gen_args)
    written = json.loads((cfg["atlas_dir"] / "repos" / "orders-api.json").read_text())
    assert written["domain"] == "identity"
