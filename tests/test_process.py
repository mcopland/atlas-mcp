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

    def fake(cfg, repo, prompt, log_path):
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


def test_restamps_when_only_ignored_files_changed(make_repo, make_cfg, make_manifest, gen_args, stub_kiro):
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


def test_updates_and_passes_the_changed_files_to_the_prompt(make_repo, make_cfg, make_manifest, gen_args, stub_kiro):
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


def test_full_regeneration_when_the_old_commit_is_gone(make_repo, make_cfg, make_manifest, gen_args, stub_kiro):
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
    monkeypatch.setattr(atlas, "MAX_PROMPT_BYTES", 200)
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
    monkeypatch.setattr(atlas, "run_kiro", lambda *a, **k: {"summary": "s", "domain": "invented", "kind": "service"})
    atlas.process_repo(cfg, "svc", repo, gen_args)
    written = json.loads((cfg["atlas_dir"] / "repos" / "svc.json").read_text())
    assert written["domain"] == "unassigned"
    assert written["_meta"]["domain_rejected"] == "invented"


def test_unparseable_output_is_retried_once_then_reported(make_repo, make_cfg, gen_args, monkeypatch):
    repo = make_repo("svc")
    cfg = make_cfg()
    attempts = []

    def always_bad(cfg_, repo_, prompt, log_path):
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


def test_default_command_line_uses_the_read_only_mapper_agent(make_cfg, monkeypatch, tmp_path):
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
        json.dumps({"type": "assistant", "text": '<<<ATLAS_JSON\n{"summary": "real"}\nATLAS_JSON>>>'}),
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
