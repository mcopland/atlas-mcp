"""Tests for the diag/ tester scripts. These never touch a real kiro-cli or MCP client."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parent.parent
DIAG = KIT / "diag"

sys.path.insert(0, str(DIAG))

import diaglib

# ---------- scrub ----------


@pytest.mark.parametrize(
    ("needle_kind", "make_needle", "make_haystack"),
    [
        ("home", lambda: str(Path.home()), lambda n: f"path was {n}/atlas/config.json"),
        ("user", diaglib.current_user, lambda n: f"owned by {n} in ps aux"),
        ("host", diaglib.current_host, lambda n: f"connected to host {n} over ssh"),
        ("email", lambda: "someone@example.com", lambda n: f"git config user.email {n}"),
        ("mnt-c-user", lambda: "alice", lambda n: f"/mnt/c/Users/{n}/atlas-mcp/config.json"),
    ],
)
def test_scrub_masks_identifying_values(needle_kind, make_needle, make_haystack):
    needle = make_needle()
    haystack = make_haystack(needle)
    scrubbed = diaglib.scrub(haystack)
    assert needle not in scrubbed, f"{needle_kind} leaked: {scrubbed!r}"


def test_scrub_applies_atlas_redact_when_available():
    atlas = diaglib.try_import_atlas()
    if atlas is None:
        pytest.skip("atlas needs Python 3.11+")
    fake_key_id = "AKIA" + "Q" * 16  # shaped like an AWS access key id, not a real secret literal
    text = f"AWS key line: {fake_key_id}"
    scrubbed = diaglib.scrub(text, atlas_mod=atlas)
    assert scrubbed != text


def test_scrub_redacts_email_domain_when_local_part_matches_username():
    """Regression: scrub() used to replace the username before running EMAIL_RE, so
    'alice@corp.example.com' became '<user>@corp.example.com' and the company domain leaked."""
    user = diaglib.current_user()
    if not user:
        pytest.skip("current_user() returned empty on this platform")
    text = f"git config user.email {user}@corp.example.com"
    scrubbed = diaglib.scrub(text)
    assert "corp.example.com" not in scrubbed
    assert "<redacted-email>" in scrubbed


def test_scrub_redacts_aws_account_id_in_arn():
    fake_account = "123456789012"  # shaped like an account id, not a real one
    text = f"arn:aws:codewhisperer:us-east-1:{fake_account}:profile/3YAYXXU4DPNH"
    scrubbed = diaglib.scrub(text)
    assert fake_account not in scrubbed


def test_scrub_redacts_awsapps_subdomain():
    text = "Logged in with IAM Identity Center (https://acme-corp.awsapps.com/start)"
    scrubbed = diaglib.scrub(text)
    assert "acme-corp" not in scrubbed


def test_scrub_redacts_awsapps_subdomain_without_a_scheme():
    """Regression: the first AWSAPPS_RE required a literal 'https://' prefix, so a start URL
    printed without a scheme (or as http://) would leak the org-identifying subdomain."""
    text = "Start URL: acme-corp.awsapps.com/start"
    scrubbed = diaglib.scrub(text)
    assert "acme-corp" not in scrubbed


# ---------- clip ----------


def test_clip_leaves_short_text_untouched():
    text = "\n".join(f"line {i}" for i in range(10))
    assert diaglib.clip(text, head=60, tail=20) == text


def test_clip_truncates_long_text_with_marker():
    text = "\n".join(f"line {i}" for i in range(200))
    clipped = diaglib.clip(text, head=5, tail=5)
    lines = clipped.splitlines()
    assert lines[:5] == [f"line {i}" for i in range(5)]
    assert lines[-5:] == [f"line {i}" for i in range(195, 200)]
    assert any("omitted" in line for line in lines)


# ---------- run ----------


def test_run_reports_missing_binary():
    result = diaglib.run(["definitely-not-a-real-binary-xyz"], timeout=5)
    assert result.error == "not found"
    assert result.returncode is None


def test_run_reports_timeout():
    result = diaglib.run(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout=0.2,
    )
    assert result.error == "timed out after 0s"
    assert result.returncode is None


def test_run_reports_nonzero_exit():
    result = diaglib.run([sys.executable, "-c", "import sys; sys.exit(3)"], timeout=5)
    assert result.error is None
    assert result.returncode == 3


# ---------- Report ----------


def test_report_renders_markers_and_header():
    report = diaglib.Report("check_env")
    with report.section("system") as s:
        s.ok("looks fine")
    rendered = report.render()
    assert rendered.startswith(diaglib.BEGIN)
    assert rendered.rstrip().endswith(diaglib.END)
    assert "OK: looks fine" in rendered


def test_report_summarizes_fail_and_warn_counts():
    report = diaglib.Report("check_env")
    with report.section("a") as s:
        s.fail("broken")
    with report.section("b") as s:
        s.warn("iffy")
    with report.section("c") as s:
        s.ok("fine")
    rendered = report.render()
    assert "1 FAIL" in rendered
    assert "1 WARN" in rendered
    assert report.exit_code == 1


def test_report_exit_code_is_zero_without_failures():
    report = diaglib.Report("check_env")
    with report.section("a") as s:
        s.warn("iffy")
    assert report.exit_code == 0


def test_report_section_catches_expected_exceptions():
    report = diaglib.Report("check_env")
    with report.section("boom") as s:
        s.ok("about to explode")
        raise ValueError("kaboom")
    rendered = report.render()
    assert "## boom" in rendered
    assert "FAIL: ValueError: kaboom" in rendered


def test_report_section_reraises_unexpected_exceptions():
    report = diaglib.Report("check_env")
    with pytest.raises(TypeError), report.section("boom"):
        raise TypeError("not caught")


def test_build_argparser_rejects_unknown_section(capsys):
    parser = diaglib.build_argparser("check_env", ["system", "python"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--only", "nonexistent"])
    err = capsys.readouterr().err
    assert "system" in err and "python" in err


def test_build_argparser_accepts_known_section():
    parser = diaglib.build_argparser("check_env", ["system", "python"])
    args = parser.parse_args(["--only", "system"])
    assert args.only == ["system"]


def test_build_argparser_live_flag_only_when_requested():
    parser = diaglib.build_argparser("check_kiro", ["install"], live=True)
    args = parser.parse_args(["--live"])
    assert args.live is True

    parser_no_live = diaglib.build_argparser("check_env", ["system"], live=False)
    with pytest.raises(SystemExit):
        parser_no_live.parse_args(["--live"])


# ---------- WSL / filesystem detection ----------


@pytest.mark.parametrize(
    ("proc_version", "distro_env", "expected"),
    [
        ("Linux version 5.15.90.1-microsoft-standard-WSL2 ...", "Ubuntu", "WSL2"),
        ("Linux version 4.4.0-19041-Microsoft ...", "", "WSL1"),
        ("Linux version 6.8.0-generic (buildd@lcy02) ...", "", None),
    ],
)
def test_wsl_kind_detection(proc_version, distro_env, expected):
    assert diaglib.wsl_kind(proc_version, distro_env) == expected


SAMPLE_MOUNTS = """\
/dev/sdc / ext4 rw,relatime 0 0
C:\\ /mnt/c drvfs rw,noatime 0 0
none /mnt/wsl/instances/x 9p rw 0 0
/dev/sdd /home/mike/atlas-mcp ext4 rw,relatime 0 0
"""


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/mnt/c/Users/mike/atlas-mcp", "drvfs"),
        ("/home/mike/atlas-mcp", "ext4"),
        ("/mnt/wsl/instances/x/foo", "9p"),
        ("/", "ext4"),
    ],
)
def test_fs_type_longest_prefix_match(path, expected):
    assert diaglib.fs_type(path, SAMPLE_MOUNTS) == expected


# ---------- misc checks ----------


def test_has_crlf_detects_windows_line_endings(tmp_path):
    crlf = tmp_path / "crlf.py"
    crlf.write_bytes(b"print('hi')\r\n")
    lf = tmp_path / "lf.py"
    lf.write_bytes(b"print('hi')\n")
    assert diaglib.has_crlf(crlf) is True
    assert diaglib.has_crlf(lf) is False


def test_flag_support_finds_present_and_missing_flags():
    help_text = "usage: kiro-cli chat [--no-interactive] [--wrap MODE] [--model NAME]"
    supported = diaglib.flag_support(
        help_text, ["--no-interactive", "--wrap", "--agent", "--output-format"]
    )
    assert supported["--no-interactive"] is True
    assert supported["--wrap"] is True
    assert supported["--agent"] is False
    assert supported["--output-format"] is False


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/usr/local/bin/kiro-cli", "linux"),
        ("/mnt/c/Users/mike/AppData/Local/Programs/kiro/kiro-cli.exe", "windows-interop"),
        ("kiro-cli", "unknown"),
    ],
)
def test_classify_bin(path, expected):
    assert diaglib.classify_bin(path) == expected


def test_env_whitelist_never_includes_secret_values(monkeypatch):
    monkeypatch.setenv("KIRO_API_KEY", "super-secret-value")
    rendered = diaglib.env_summary()
    assert "super-secret-value" not in rendered
    assert "KIRO_API_KEY: set" in rendered


def test_env_whitelist_reports_unset(monkeypatch):
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    rendered = diaglib.env_summary()
    assert "KIRO_API_KEY: unset" in rendered


# ---------- kiro home inspection ----------


def test_inspect_kiro_home_hides_other_server_envs(tmp_path):
    kiro_home = tmp_path / ".kiro"
    (kiro_home / "settings").mkdir(parents=True)
    (kiro_home / "agents").mkdir(parents=True)
    mcp_json = {
        "mcpServers": {
            "atlas": {"command": "/abs/uv", "args": ["run", "--script", "atlas_mcp.py"], "env": {}},
            "other": {"command": "/abs/other", "args": [], "env": {"OTHER_SECRET": "hunter2"}},
        }
    }
    (kiro_home / "settings" / "mcp.json").write_text(json.dumps(mcp_json))
    result = diaglib.inspect_kiro_home(kiro_home, KIT)
    rendered = json.dumps(result)
    assert "hunter2" not in rendered
    assert "other" in rendered  # named, just not detailed


def test_inspect_kiro_home_warns_on_double_registration(tmp_path):
    kiro_home = tmp_path / ".kiro"
    (kiro_home / "settings").mkdir(parents=True)
    (kiro_home / "agents").mkdir(parents=True)
    entry = {"command": "/abs/uv", "args": ["run", "--script", "atlas_mcp.py"], "env": {}}
    (kiro_home / "settings" / "mcp.json").write_text(json.dumps({"mcpServers": {"atlas": entry}}))
    (kiro_home / "agents" / "atlas.json").write_text(
        json.dumps({"name": "atlas", "mcpServers": {"atlas": entry}})
    )
    result = diaglib.inspect_kiro_home(kiro_home, KIT)
    assert any("both" in w.lower() for w in result["warnings"])


def test_inspect_kiro_home_warns_on_relative_command(tmp_path):
    kiro_home = tmp_path / ".kiro"
    (kiro_home / "settings").mkdir(parents=True)
    (kiro_home / "agents").mkdir(parents=True)
    entry = {"command": "uv", "args": ["run", "--script", "atlas_mcp.py"], "env": {}}
    (kiro_home / "settings" / "mcp.json").write_text(json.dumps({"mcpServers": {"atlas": entry}}))
    result = diaglib.inspect_kiro_home(kiro_home, KIT)
    assert any("relative" in w.lower() for w in result["warnings"])


# ---------- check_kiro.py live helpers ----------


def test_live_config_carries_kiro_settings_and_overrides_rest(tmp_path):
    check_kiro = pytest.importorskip("check_kiro")
    user_cfg = {
        "kiro_bin": "/abs/kiro-cli",
        "model": "claude-haiku-4.5",
        "kiro_agent": "atlas-bundle",
        "kiro_extra_args": ["--trust-tools", "fs_read"],
        "timeout_minutes": 3,
        "repo_roots": ["~/atlas-src"],
        "parallel": 5,
    }
    cfg = check_kiro.live_config(user_cfg, tmp_path, "bundle")
    assert cfg["kiro_bin"] == "/abs/kiro-cli"
    assert cfg["model"] == "claude-haiku-4.5"
    assert cfg["kiro_agent"] == "atlas-bundle"
    assert cfg["kiro_extra_args"] == ["--trust-tools", "fs_read"]
    assert cfg["timeout_minutes"] == 3
    assert cfg["mapper_mode"] == "bundle"
    assert cfg["parallel"] == 1
    assert cfg["atlas_dir"] == str(tmp_path / "atlas")
    assert cfg["repo_roots"] == [str(tmp_path / "src")]


def test_live_echo_records_success(monkeypatch, tmp_path):
    atlas = pytest.importorskip("atlas")
    check_kiro = pytest.importorskip("check_kiro")

    def fake_run_kiro(cfg, repo, prompt, log_path, mapper="explore"):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("fake log\n")
        return {"ok": True}

    monkeypatch.setattr(atlas, "run_kiro", fake_run_kiro)
    report = diaglib.Report("check_kiro")
    cfg = check_kiro.live_config({}, tmp_path, "bundle")
    check_kiro.live_echo(report, atlas, cfg, tmp_path)
    rendered = report.render()
    assert "OK" in rendered
    assert "fake log" in rendered


def test_live_echo_records_runtime_error(monkeypatch, tmp_path):
    atlas = pytest.importorskip("atlas")
    check_kiro = pytest.importorskip("check_kiro")

    def fake_run_kiro(cfg, repo, prompt, log_path, mapper="explore"):
        raise RuntimeError("kiro-cli exited 127; see log")

    monkeypatch.setattr(atlas, "run_kiro", fake_run_kiro)
    report = diaglib.Report("check_kiro")
    cfg = check_kiro.live_config({}, tmp_path, "bundle")
    check_kiro.live_echo(report, atlas, cfg, tmp_path)
    rendered = report.render()
    assert "FAIL" in rendered


def test_run_live_passes_atlas_a_fully_defaulted_config(monkeypatch, tmp_path):
    """live_config only sets the handful of keys it overrides; run_kiro reads defaults
    (kiro_agent, kiro_extra_args, ...) that only exist after a real atlas.load_config. Regression
    test for a KeyError on cfg["kiro_agent"] when live_echo got the raw dict directly."""
    atlas = pytest.importorskip("atlas")
    check_kiro = pytest.importorskip("check_kiro")
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "no-such-kiro-home"))

    seen_cfgs = []

    def fake_run_kiro(cfg, repo, prompt, log_path, mapper="explore"):
        seen_cfgs.append(dict(cfg))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("fake log\n")
        return {"ok": True}

    def fake_subprocess_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(_args[0] if _args else [], 0, "done\n", "")

    monkeypatch.setattr(atlas, "run_kiro", fake_run_kiro)
    monkeypatch.setattr(check_kiro.subprocess, "run", fake_subprocess_run)

    report = diaglib.Report("check_kiro")
    check_kiro.run_live(report, {}, tmp_path)

    assert seen_cfgs, "atlas.run_kiro was never called"
    assert seen_cfgs[0]["kiro_agent"] is None  # only present once load_config has defaulted it
    assert seen_cfgs[0]["kiro_bin"] == "kiro-cli"
    rendered = report.render()
    assert "FAIL: could not build a live config" not in rendered


def test_run_live_reports_invalid_user_config_without_crashing(monkeypatch, tmp_path):
    pytest.importorskip("atlas")
    check_kiro = pytest.importorskip("check_kiro")
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "no-such-kiro-home"))

    report = diaglib.Report("check_kiro")
    # kiro_extra_args starting with --trust-all is the one thing atlas.load_config sys.exits on
    # that a tester's real config.json could plausibly carry into a live run.
    check_kiro.run_live(report, {"kiro_extra_args": ["--trust-all-tools"]}, tmp_path)
    rendered = report.render()
    assert "FAIL: could not build a live config" in rendered


def test_live_probes_skip_below_python_3_11(monkeypatch, tmp_path):
    check_kiro = pytest.importorskip("check_kiro")
    monkeypatch.setattr(check_kiro.sys, "version_info", (3, 10, 0))
    report = diaglib.Report("check_kiro")
    check_kiro.run_live(report, {}, tmp_path)
    rendered = report.render()
    assert "SKIP" in rendered
    assert "uv run --script diag/check_kiro.py --live" in rendered


# ---------- diaglib.configured_kiro_bin ----------


def test_configured_kiro_bin_reads_config(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"kiro_bin": "/abs/kiro-cli"}))
    assert diaglib.configured_kiro_bin(tmp_path) == "/abs/kiro-cli"


def test_configured_kiro_bin_falls_back_without_config(tmp_path):
    assert diaglib.configured_kiro_bin(tmp_path) == "kiro-cli"


def test_configured_kiro_bin_falls_back_on_invalid_json(tmp_path):
    (tmp_path / "config.json").write_text("{not json")
    assert diaglib.configured_kiro_bin(tmp_path) == "kiro-cli"


# ---------- check_kiro.py: auth, flags ----------


def test_check_auth_never_invokes_doctor(monkeypatch, tmp_path):
    """Regression: `kiro-cli doctor` writes into the kiro-cli-term socket rather than stdout, so
    its "Testing kiro-cli-term..." text lands in the tester's shell input buffer after the script
    exits. whoami alone covers what auth needs to check."""
    check_kiro = pytest.importorskip("check_kiro")
    monkeypatch.setattr(check_kiro, "KIT", tmp_path)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return diaglib.RunResult(cmd, 0, "ok", "", None)

    monkeypatch.setattr(check_kiro, "run", fake_run)
    report = diaglib.Report("check_kiro")
    check_kiro.check_auth(report)
    assert all("doctor" not in cmd for cmd in calls)


FAKE_CHAT_HELP = (
    "Options:\n"
    "  --no-interactive\n"
    "  --wrap\n"
    "  --model\n"
    "  --agent\n"
    "  --trust-tools\n"
    "  --output-format\n"
)  # deliberately omits --engine, matching kiro-cli 2.27.0's V3-default --help


def test_check_flags_optional_flag_missing_is_ok_without_extra_args(monkeypatch, tmp_path):
    check_kiro = pytest.importorskip("check_kiro")
    monkeypatch.setattr(check_kiro, "KIT", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"kiro_extra_args": []}))

    def fake_run(cmd, **kwargs):
        return diaglib.RunResult(cmd, 0, FAKE_CHAT_HELP, "", None)

    monkeypatch.setattr(check_kiro, "run", fake_run)
    report = diaglib.Report("check_kiro")
    check_kiro.check_flags(report)
    rendered = report.render()
    assert "WARN: --engine" not in rendered


def test_check_flags_warns_when_extra_args_need_a_missing_optional_flag(monkeypatch, tmp_path):
    check_kiro = pytest.importorskip("check_kiro")
    monkeypatch.setattr(check_kiro, "KIT", tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps({"kiro_extra_args": ["--output-format", "stream-json", "--engine", "v3"]})
    )

    def fake_run(cmd, **kwargs):
        return diaglib.RunResult(cmd, 0, FAKE_CHAT_HELP, "", None)

    monkeypatch.setattr(check_kiro, "run", fake_run)
    report = diaglib.Report("check_kiro")
    check_kiro.check_flags(report)
    rendered = report.render()
    assert "WARN: --engine" in rendered


def test_check_flags_recognizes_equals_form_extra_args(monkeypatch, tmp_path):
    """Regression: `flag in extra_args` missed '--trust-tools=fs_read,fs_write' (the equals-sign
    form kiro-cli's own --help documents), reporting it as unused even though it is passed."""
    check_kiro = pytest.importorskip("check_kiro")
    monkeypatch.setattr(check_kiro, "KIT", tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps({"kiro_extra_args": ["--trust-tools=fs_read,fs_write"]})
    )
    help_without_trust_tools = FAKE_CHAT_HELP.replace("  --trust-tools\n", "")

    def fake_run(cmd, **kwargs):
        return diaglib.RunResult(cmd, 0, help_without_trust_tools, "", None)

    monkeypatch.setattr(check_kiro, "run", fake_run)
    report = diaglib.Report("check_kiro")
    check_kiro.check_flags(report)
    rendered = report.render()
    assert "WARN: --trust-tools" in rendered


# ---------- check_env.py internals ----------


def test_check_config_reports_invalid_config_without_crashing(monkeypatch, tmp_path):
    """atlas.load_config() calls sys.exit() on a bad config; check_config must turn that into a
    FAIL line, not let SystemExit escape and take the rest of the report down with it."""
    pytest.importorskip("atlas")
    check_env = pytest.importorskip("check_env")
    monkeypatch.setattr(check_env, "KIT", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"parallel": 0}))  # load_config rejects this
    report = diaglib.Report("check_env")
    check_env.check_config(report)  # must not raise SystemExit
    rendered = report.render()
    assert "FAIL" in rendered


def test_check_tools_resolves_crontab_via_which(monkeypatch):
    """Regression: crontab has no --version flag, so the old probe reported its usage-error
    stderr ("crontab: invalid option -- '-'") as if it were a version string."""
    check_env = pytest.importorskip("check_env")
    monkeypatch.setattr(
        check_env.shutil, "which", lambda name: "/usr/bin/crontab" if name == "crontab" else None
    )
    report = diaglib.Report("check_env")
    check_env.check_tools(report)
    rendered = report.render()
    assert "invalid option" not in rendered
    assert "crontab: /usr/bin/crontab" in rendered or "crontab: ~" in rendered


def test_check_tools_no_minimal_path_warn_with_working_absolute_kiro_bin(monkeypatch, tmp_path):
    """Regression: the minimal-PATH probe always ran bare `kiro-cli`, ignoring a configured
    absolute kiro_bin that actually resolves, producing a false WARN."""
    check_env = pytest.importorskip("check_env")
    fake_bin = tmp_path / "fake-kiro-cli"
    fake_bin.write_text("#!/usr/bin/env sh\necho fake-kiro-cli 1.0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(check_env, "KIT", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"kiro_bin": str(fake_bin)}))
    report = diaglib.Report("check_env")
    check_env.check_tools(report)
    rendered = report.render()
    assert "does not resolve under a cron-like minimal PATH" not in rendered
    assert "resolves under a cron-like minimal PATH" in rendered


def test_check_tools_minimal_path_probes_configured_relative_kiro_bin(monkeypatch, tmp_path):
    """Regression: a non-absolute, non-default kiro_bin (e.g. a custom wrapper script name) was
    never actually probed; the minimal-PATH check always ran the literal 'kiro-cli'."""
    check_env = pytest.importorskip("check_env")
    monkeypatch.setattr(check_env, "KIT", tmp_path)
    monkeypatch.setattr(check_env.shutil, "which", lambda name: None)
    (tmp_path / "config.json").write_text(json.dumps({"kiro_bin": "my-custom-kiro"}))
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return diaglib.RunResult(cmd, 0, "my-custom-kiro 1.0", "", None)

    monkeypatch.setattr(check_env, "run", fake_run)
    report = diaglib.Report("check_env")
    check_env.check_tools(report)
    assert any(cmd[0] == "my-custom-kiro" for cmd in calls)


def test_check_tools_minimal_path_reports_non_not_found_errors(monkeypatch, tmp_path):
    """Regression: the non-absolute branch's `if ... == "not found": ... elif ... is None: ...`
    chain silently dropped any other error (e.g. a timeout) from the report."""
    check_env = pytest.importorskip("check_env")
    monkeypatch.setattr(check_env, "KIT", tmp_path)
    monkeypatch.setattr(check_env.shutil, "which", lambda name: None)
    (tmp_path / "config.json").write_text(json.dumps({"kiro_bin": "kiro-cli"}))

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["kiro-cli", "--version"] and kwargs.get("env"):
            return diaglib.RunResult(cmd, None, "", "", "timed out after 10s")
        return diaglib.RunResult(cmd, 0, "ok", "", None)

    monkeypatch.setattr(check_env, "run", fake_run)
    report = diaglib.Report("check_env")
    check_env.check_tools(report)
    rendered = report.render()
    assert "cron-like minimal PATH" in rendered
    assert "timed out" in rendered


def test_nearest_existing_ancestor_walks_up_to_an_existing_dir(tmp_path):
    check_env = pytest.importorskip("check_env")
    missing = tmp_path / "does" / "not" / "exist" / "atlas"
    assert check_env._nearest_existing_ancestor(missing) == tmp_path


def test_nearest_existing_ancestor_returns_a_dir_that_already_exists(tmp_path):
    check_env = pytest.importorskip("check_env")
    assert check_env._nearest_existing_ancestor(tmp_path) == tmp_path


def test_check_filesystem_probes_atlas_dirs_ancestor_not_system_tmp(monkeypatch, tmp_path):
    """Regression test: atlas_dir itself may not exist yet (a fresh install), and the probe must
    run on the filesystem atlas_dir will actually land on, not silently fall back to the system
    temp dir while still labeling the report with atlas_dir's own path."""
    check_env = pytest.importorskip("check_env")
    missing_atlas_dir = tmp_path / "atlas"  # deliberately does not exist
    monkeypatch.setenv("ATLAS_DIR", str(missing_atlas_dir))
    monkeypatch.setattr(check_env, "KIT", tmp_path / "kit-without-a-config")

    seen_dir_args = []
    real_temporary_directory = check_env.tempfile.TemporaryDirectory

    def spy(*args, **kwargs):
        seen_dir_args.append(kwargs.get("dir"))
        return real_temporary_directory(*args, **kwargs)

    monkeypatch.setattr(check_env.tempfile, "TemporaryDirectory", spy)
    report = diaglib.Report("check_env")
    check_env.check_filesystem(report)
    assert seen_dir_args == [str(tmp_path)]


# ---------- check_mcp.py handshake ----------


def test_handshake_lists_tools_from_real_server():
    pytest.importorskip("mcp", reason="install the mcp package to run the MCP server tests")
    check_mcp = pytest.importorskip("check_mcp")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        result = check_mcp.handshake(
            [sys.executable, str(KIT / "atlas_mcp.py")],
            env={"ATLAS_DIR": tmp},
            timeout=30,
        )
    assert result["ok"] is True, result.get("error")
    assert "freshness" in result["tools"]
    assert result["server_name"] == "atlas"


def test_handshake_times_out_on_silent_child():
    check_mcp = pytest.importorskip("check_mcp")
    start = time.monotonic()
    result = check_mcp.handshake(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=0.5,
    )
    elapsed = time.monotonic() - start
    assert result["ok"] is False
    assert "timed out" in result["error"] or "timeout" in result["error"].lower()
    assert elapsed < 10


# ---------- syntax floor ----------


@pytest.mark.parametrize("path", sorted(DIAG.glob("*.py")), ids=lambda p: p.name)
def test_diag_scripts_parse_on_python_3_8(path):
    src = path.read_text(encoding="utf-8")
    ast.parse(src, filename=str(path), feature_version=(3, 8))


# ---------- smoke ----------


@pytest.mark.parametrize(
    ("script", "only"),
    [
        ("check_env.py", "env"),
        ("check_mcp.py", "warmup"),
    ],
)
def test_script_smoke_runs_cheap_section(script, only):
    proc = subprocess.run(
        [sys.executable, str(DIAG / script), "--only", only],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert diaglib.BEGIN in proc.stdout
    assert diaglib.END in proc.stdout


def test_check_kiro_smoke_without_kiro_installed():
    proc = subprocess.run(
        [sys.executable, str(DIAG / "check_kiro.py"), "--only", "install"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert diaglib.BEGIN in proc.stdout
    assert diaglib.END in proc.stdout
