#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
"""Tester diagnostic: kiro-cli install, config and (with --live) real calls.

    python3 diag/check_kiro.py [--only SECTION ...] [--full]
    python3 diag/check_kiro.py --live [--timeout SECONDS]   # spends a handful of requests

The default run never calls kiro-cli with a real prompt: it only inspects --version, --help
and --list-models, and the files under ~/.kiro. --live additionally runs one real mapping
end-to-end, through the exact code path atlas.py uses (atlas.run_kiro), against a tiny
synthetic repo built in a temp dir. That costs roughly 3-4 Kiro requests total. --live needs
Python 3.11+, since it imports atlas.py directly; anything below that gets a SKIP with the
`uv run --script` command to rerun it with the right interpreter.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import diaglib
from diaglib import (
    Report,
    build_argparser,
    classify_bin,
    flag_support,
    inspect_kiro_home,
    kiro_home,
    run,
    scrub,
)

SECTIONS = ["install", "flags", "models", "auth", "kiro-home"]
KIT = diaglib.KIT
KIRO_CHAT_FLAGS = [
    "--no-interactive",
    "--wrap",
    "--model",
    "--agent",
    "--trust-tools",
    "--output-format",
    "--engine",
]


def _kiro_bin() -> str:
    config_path = KIT / "config.json"
    if config_path.is_file():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            return str(cfg.get("kiro_bin", "kiro-cli"))
        except (OSError, ValueError):
            pass
    return "kiro-cli"


def check_install(report: Report) -> None:
    with report.section("install") as s:
        kiro_bin = _kiro_bin()
        resolved = shutil.which(kiro_bin) or kiro_bin
        s.ok(f"kiro_bin: {scrub(kiro_bin)} -> classified as {classify_bin(resolved)}")
        version = run([kiro_bin, "--version"], timeout=10)
        if version.error:
            s.fail(f"kiro-cli --version: {version.error}")
            return
        s.ok(f"kiro-cli --version: {version.stdout.strip() or version.stderr.strip()}")
        help_text = run([kiro_bin, "--help"], timeout=10)
        if help_text.error is None:
            s.code(scrub(help_text.stdout))


def check_flags(report: Report) -> None:
    with report.section("flags") as s:
        kiro_bin = _kiro_bin()
        help_result = run([kiro_bin, "chat", "--help"], timeout=10)
        if help_result.error:
            s.fail(f"kiro-cli chat --help: {help_result.error}")
            return
        support = flag_support(help_result.stdout, KIRO_CHAT_FLAGS)
        for flag, present in support.items():
            (s.ok if present else s.warn)(f"{flag}: {'present' if present else 'not in --help'}")
        s.code(scrub(help_result.stdout))


def check_models(report: Report) -> None:
    with report.section("models") as s:
        kiro_bin = _kiro_bin()
        result = run([kiro_bin, "chat", "--list-models"], timeout=30)
        if result.error:
            s.fail(f"kiro-cli chat --list-models: {result.error}")
            return
        s.code(scrub(result.stdout))
        config_path = KIT / "config.json"
        if config_path.is_file():
            try:
                model = json.loads(config_path.read_text(encoding="utf-8")).get(
                    "model", "claude-haiku-4.5"
                )
            except (OSError, ValueError):
                model = None
            if model and model in result.stdout:
                s.ok(f"configured model {model!r} appears in --list-models")
            elif model:
                s.warn(f"configured model {model!r} does not appear in --list-models output")


def check_auth(report: Report) -> None:
    with report.section("auth") as s:
        s.ok(f"KIRO_API_KEY: {'set' if 'KIRO_API_KEY' in os.environ else 'unset'}")
        kiro_bin = _kiro_bin()
        for args in (["whoami"], ["doctor"]):
            result = run([kiro_bin, *args], timeout=15)
            label = " ".join(args)
            if result.error:
                s.warn(f"kiro-cli {label}: {result.error}")
                continue
            s.ok(f"kiro-cli {label}: exit={result.returncode}")
            s.code(scrub(result.stdout + result.stderr))


def check_kiro_home(report: Report) -> None:
    with report.section("kiro-home") as s:
        home = kiro_home()
        if not home.is_dir():
            s.warn(
                f"{scrub(str(home))} does not exist; the install steps in README section 2 have not been run"
            )
            return
        info = inspect_kiro_home(home, KIT)
        for sub, files in info["files"].items():
            s.ok(f"{sub}/: {', '.join(files) if files else '(empty)'}")
        for name, diff in info.get("agent_diffs", {}).items():
            (s.ok if diff == "identical" else s.warn)(f"agents/{name}: {diff}")
        for label, entry in info["atlas_servers"].items():
            s.ok(f"{label} atlas server: command={scrub(str(entry.get('command')))}")
        if info["other_servers"]:
            s.ok(f"other registered servers (names only): {', '.join(info['other_servers'])}")
        for warning in info["warnings"]:
            s.warn(warning)


CHECKS = {
    "install": check_install,
    "flags": check_flags,
    "models": check_models,
    "auth": check_auth,
    "kiro-home": check_kiro_home,
}


# ---------- --live ----------


def live_config(user_cfg: dict, tmp: Path, mapper_mode: str) -> dict:
    """Builds a config dict for a throwaway atlas run: it carries over only the settings that
    decide how kiro-cli is actually invoked, and points everything else (atlas_dir, repo_roots,
    parallel) at the temp dir this probe owns."""
    cfg: dict = {
        "atlas_dir": str(tmp / "atlas"),
        "repo_roots": [str(tmp / "src")],
        "mapper_mode": mapper_mode,
        "parallel": 1,
        "domains": [],
    }
    for key in ("kiro_bin", "model", "kiro_agent", "kiro_extra_args", "timeout_minutes"):
        if key in user_cfg:
            cfg[key] = user_cfg[key]
    return cfg


def _user_cfg() -> dict:
    config_path = KIT / "config.json"
    if not config_path.is_file():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def live_echo(report: Report, atlas, cfg: dict, tmp: Path) -> None:
    """The cheapest end-to-end check: one real kiro-cli call, through the exact code path
    atlas.py uses, asking for nothing but a fixed ATLAS_JSON echo. Exercises auth, the model id,
    --wrap never and parse_output all at once."""
    with report.section("live-echo") as s:
        prompt = 'Reply with exactly: <<<ATLAS_JSON\n{"ok": true}\nATLAS_JSON>>>'
        repo_dir = tmp / "atlas"
        repo_dir.mkdir(parents=True, exist_ok=True)
        log_path = tmp / "atlas" / "logs" / "live-echo.log"
        loaded = dict(cfg)
        loaded.setdefault("timeout_minutes", 5)
        try:
            result = atlas.run_kiro(loaded, str(repo_dir), prompt, log_path, mapper="bundle")
        except (RuntimeError, ValueError) as e:
            s.fail(str(e))
        else:
            s.ok(f"parsed: {result}")
        if log_path.exists():
            s.code(scrub(log_path.read_text(encoding="utf-8"), atlas_mod=atlas))


def live_generate(report: Report, atlas, cfg: dict, tmp: Path, mapper_mode: str) -> None:
    # bundle and explore each get their own repo: reusing one across both calls leaves the
    # second `git commit` with nothing to commit, since the first call already committed it.
    with report.section(f"live-{mapper_mode}") as s:
        name = f"probe-{mapper_mode}"
        src = tmp / "src" / name
        src.mkdir(parents=True, exist_ok=True)
        (src / "README.md").write_text(f"# {name}\nA throwaway repo for the atlas diag script.\n")
        (src / "package.json").write_text(
            json.dumps({"name": name, "dependencies": {"left-pad": "1.0.0"}})
        )
        (src / ".env.example").write_text("ORDERS_SERVICE_URL=https://orders.internal\n")
        env = dict(os.environ)
        env.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.excludesFile",
                "GIT_CONFIG_VALUE_0": os.devnull,
            }
        )
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
            r = run(["git", "-C", str(src), *args], timeout=15, env=env)
            if r.error or (r.returncode not in (0, None)):
                s.fail(f"git {' '.join(args)}: {r.error or r.stderr.strip()}")
                return

        cfg = dict(cfg, mapper_mode=mapper_mode)
        config_path = tmp / "config.json"
        config_path.write_text(json.dumps(cfg))
        result = subprocess.run(
            [
                sys.executable,
                str(KIT / "atlas.py"),
                "--config",
                str(config_path),
                "generate",
                "--only",
                name,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=cfg.get("timeout_minutes", 5) * 60 + 30,
            env=env,
        )
        s.ok(f"exit={result.returncode}")
        s.code(scrub(result.stdout, atlas_mod=atlas))
        if result.stderr.strip():
            s.code(scrub(result.stderr, atlas_mod=atlas))
        log_path = Path(cfg["atlas_dir"]) / "logs" / f"{name}.log"
        if log_path.exists():
            s.code(scrub(log_path.read_text(encoding="utf-8"), atlas_mod=atlas))
        manifest_path = Path(cfg["atlas_dir"]) / "repos" / f"{name}.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            dropped = manifest.get("_meta", {}).get("dropped_without_evidence", [])
            s.ok(f"manifest keys: {sorted(manifest)}")
            s.ok(f"dropped_without_evidence: {len(dropped)}")


def live_mcp(report: Report, atlas, cfg: dict, tmp: Path) -> None:
    with report.section("live-mcp") as s:
        agent_path = kiro_home() / "agents" / "atlas.json"
        if not agent_path.is_file():
            s.skip(f"{scrub(str(agent_path))} not installed; see README section 5")
            return
        kiro_bin = cfg.get("kiro_bin", "kiro-cli")
        prompt = "Call the atlas MCP server's freshness tool and print exactly what it returns."
        result = run(
            [
                kiro_bin,
                "chat",
                "--no-interactive",
                "--wrap",
                "never",
                "--agent",
                "atlas",
                "--model",
                cfg.get("model", "claude-haiku-4.5"),
            ],
            timeout=cfg.get("timeout_minutes", 5) * 60,
            input=prompt,
        )
        if result.error:
            s.fail(result.error)
            return
        s.ok(f"exit={result.returncode}")
        s.code(scrub(result.stdout + result.stderr, atlas_mod=atlas))


def run_live(report: Report, user_cfg: dict, tmp: Path) -> None:
    if sys.version_info < (3, 11):
        with report.section("live") as s:
            s.skip(
                "--live needs Python 3.11+ to import atlas.py; rerun with "
                "`uv run --script diag/check_kiro.py --live`"
            )
        return
    atlas = diaglib.try_import_atlas()
    if atlas is None:
        with report.section("live") as s:
            s.skip("could not import atlas.py")
        return
    # live_config only fills in the handful of keys that decide how kiro-cli is invoked; running
    # it through atlas.load_config the same way atlas.py itself does fills every other default
    # (kiro_agent, bundle_budget_bytes, ...) that run_kiro and its callees assume exist, and
    # applies the same validation, e.g. rejecting a kiro_extra_args with --trust-all-tools.
    raw_cfg = live_config(user_cfg, tmp, "bundle")
    config_path = tmp / "config.json"
    config_path.write_text(json.dumps(raw_cfg))
    try:
        cfg = atlas.load_config(str(config_path))
    except SystemExit as e:
        with report.section("live") as s:
            s.fail(f"could not build a live config from your settings: {e}")
        return
    live_echo(report, atlas, cfg, tmp)
    live_generate(report, atlas, raw_cfg, tmp, "bundle")
    live_generate(report, atlas, raw_cfg, tmp, "explore")
    live_mcp(report, atlas, cfg, tmp)


def main() -> int:
    parser = build_argparser("check_kiro.py", SECTIONS, live=True)
    args = parser.parse_args()
    report = Report("check_kiro", full=args.full)
    for name in args.only or SECTIONS:
        CHECKS[name](report)
    if args.live:
        with tempfile.TemporaryDirectory(prefix="atlas-diag-") as tmp:
            run_live(report, _user_cfg(), Path(tmp))
    sys.stdout.write(report.render())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
