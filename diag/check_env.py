#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
"""Tester diagnostic: system, toolchain and repo-layout checks. Free - makes no Kiro calls.

    python3 diag/check_env.py [--only SECTION ...] [--full]

Prints one redacted, paste-able report to stdout. Run after `git pull` and send the maintainer
the output; nothing here writes to the repo or to atlas_dir.
"""

from __future__ import annotations

import json
import locale
import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path

import diaglib
from diaglib import (  # noqa: F401
    Report,
    build_argparser,
    configured_kiro_bin,
    current_host,
    current_user,
    env_summary,
    fs_type,
    has_crlf,
    run,
    scrub,
    try_import_atlas,
    wsl_kind,
)

SECTIONS = ["system", "python", "tools", "kit", "config", "filesystem", "env", "network"]
KIT = diaglib.KIT


def check_system(report: Report) -> None:
    with report.section("system") as s:
        s.ok(f"platform: {scrub(platform.platform())}")
        os_release = Path("/etc/os-release")
        if os_release.is_file():
            s.code(scrub(os_release.read_text(encoding="utf-8", errors="replace")))
        proc_version = Path("/proc/version")
        version_text = (
            proc_version.read_text(encoding="utf-8", errors="replace")
            if proc_version.is_file()
            else ""
        )
        kind = wsl_kind(version_text, os.environ.get("WSL_DISTRO_NAME", ""))
        s.ok(f"WSL: {kind or 'not detected'}")
        comm = Path("/proc/1/comm")
        if comm.is_file():
            init = comm.read_text(encoding="utf-8", errors="replace").strip()
            note = (
                " (cron needs systemd or another init to run)"
                if init not in ("systemd", "init")
                else ""
            )
            s.ok(f"pid 1: {init}{note}")
        wsl_conf = Path("/etc/wsl.conf")
        if wsl_conf.is_file():
            s.code(scrub(wsl_conf.read_text(encoding="utf-8", errors="replace")))


def check_python(report: Report) -> None:
    with report.section("python") as s:
        s.ok(f"running interpreter: {sys.version.splitlines()[0]} at {scrub(sys.executable)}")
        if sys.version_info < (3, 11):
            s.warn("this interpreter is below 3.11; atlas.py and atlas_mcp.py need 3.11+")
        else:
            s.ok("interpreter satisfies atlas's >=3.11 requirement")
        which_python3 = run(["python3", "--version"], timeout=5)
        s.ok(
            f"python3 on PATH: {which_python3.stdout.strip() or which_python3.error or which_python3.stderr.strip()}"
        )
        uv_find = run(["uv", "python", "find", ">=3.11"], timeout=15)
        if uv_find.error:
            s.warn(f"uv python find >=3.11: {uv_find.error}")
        elif uv_find.returncode == 0:
            s.ok(f"uv python find >=3.11: {scrub(uv_find.stdout.strip())}")
        else:
            s.warn(f"uv python find >=3.11 exited {uv_find.returncode}: {uv_find.stderr.strip()}")
        s.ok(f"fcntl available: {_has_fcntl()}")
        s.ok(f"os.killpg available: {hasattr(os, 'killpg')}")
        s.ok(f"preferred encoding: {locale.getpreferredencoding(False)}")
        s.ok(f"utf8_mode: {sys.flags.utf8_mode}")
        if locale.getpreferredencoding(False).lower() not in ("utf-8", "utf8"):
            s.warn(
                "non-UTF-8 locale: run_kiro() reads kiro-cli output with text=True at this encoding"
            )


def _has_fcntl() -> bool:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        return False
    return True


def check_tools(report: Report) -> None:
    with report.section("tools") as s:
        for name, args in (("git", ["--version"]), ("uv", ["--version"])):
            r = run([name, *args], timeout=10)
            s.ok(f"{name}: {r.stdout.strip() or r.error or r.stderr.strip()}")
        for name in ("kiro-cli", "kiro-cli.exe", "flock"):
            r = run([name, "--version"], timeout=10)
            if r.error == "not found":
                s.warn(f"{name}: not found on PATH")
            elif r.error:
                s.warn(f"{name}: {r.error}")
            else:
                first_line = (r.stdout or r.stderr).strip().splitlines()[:1]
                s.ok(f"{name}: {first_line[0] if first_line else f'exit {r.returncode}'}")
        # crontab has no --version; asking for one just reports its usage-error stderr as if it
        # were a version string, so this resolves it on PATH instead.
        crontab_path = shutil.which("crontab")
        if crontab_path:
            s.ok(f"crontab: {scrub(crontab_path)}")
        else:
            s.warn("crontab: not found on PATH")

        kiro_bin = configured_kiro_bin(KIT)
        minimal_env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "")}
        if os.path.isabs(kiro_bin):
            r = run([kiro_bin, "--version"], timeout=10, env=minimal_env)
            if r.error:
                s.warn(
                    f"kiro_bin {scrub(kiro_bin)} does not resolve under a cron-like minimal "
                    f"PATH: {r.error}"
                )
            else:
                s.ok("kiro_bin resolves under a cron-like minimal PATH")
        else:
            r = run([kiro_bin, "--version"], timeout=10, env=minimal_env)
            if r.error == "not found":
                s.warn(
                    f"{scrub(kiro_bin)} not found under a cron-like minimal PATH; "
                    "set kiro_bin to an absolute path"
                )
            elif r.error:
                s.warn(
                    f"{scrub(kiro_bin)} did not resolve under a cron-like minimal PATH: {r.error}"
                )
            else:
                s.ok(f"{scrub(kiro_bin)} resolves under a cron-like minimal PATH")


def check_kit(report: Report) -> None:
    with report.section("kit") as s:
        s.ok(f"kit path: {scrub(str(KIT))}")
        mounts = _read_mounts()
        if mounts:
            s.ok(f"kit filesystem: {fs_type(str(KIT), mounts) or 'unknown'}")
        head = run(["git", "-C", str(KIT), "rev-parse", "--short", "HEAD"], timeout=5)
        branch = run(["git", "-C", str(KIT), "rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
        dirty = run(["git", "-C", str(KIT), "status", "--porcelain"], timeout=5)
        if head.error is None and head.returncode == 0:
            s.ok(f"HEAD {head.stdout.strip()} on {branch.stdout.strip()}")
            if dirty.stdout.strip():
                s.warn("working tree is dirty:")
                s.code(scrub(dirty.stdout))
        else:
            s.fail(f"git rev-parse failed: {head.error or head.stderr.strip()}")
        for key in ("core.autocrlf", "core.filemode"):
            r = run(["git", "-C", str(KIT), "config", "--get", key], timeout=5)
            s.ok(f"{key}: {r.stdout.strip() or '(unset)'}")
        for name in ("atlas.py", "atlas_mcp.py"):
            if has_crlf(KIT / name):
                s.fail(f"{name} has CRLF line endings; this breaks its `uv run --script` shebang")
            else:
                s.ok(f"{name}: LF line endings")


def _read_mounts() -> str:
    proc_mounts = Path("/proc/mounts")
    if proc_mounts.is_file():
        return proc_mounts.read_text(encoding="utf-8", errors="replace")
    return ""


def check_config(report: Report) -> None:
    with report.section("config") as s:
        config_path = KIT / "config.json"
        if not config_path.is_file():
            s.warn("no config.json; copy config.example.json and edit it")
            return
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            s.fail(f"could not parse config.json: {e}")
            return
        s.ok(f"config.json keys: {', '.join(sorted(cfg))}")
        kiro_bin = cfg.get("kiro_bin", "kiro-cli")
        if os.path.isabs(kiro_bin):
            s.ok(f"kiro_bin is absolute: {scrub(kiro_bin)} (exists: {os.path.exists(kiro_bin)})")
        else:
            s.warn(f"kiro_bin {kiro_bin!r} is not absolute; cron has a minimal PATH (see README)")
        s.ok(f"model: {cfg.get('model', '(default)')}")
        s.ok(f"mapper_mode: {cfg.get('mapper_mode', 'bundle')}")
        mounts = _read_mounts()
        for root in cfg.get("repo_roots", []):
            expanded = os.path.expanduser(root)
            exists = os.path.isdir(expanded)
            fs = fs_type(expanded, mounts) if mounts and exists else None
            s.ok(f"repo_root {scrub(root)}: exists={exists}" + (f" fs={fs}" if fs else ""))

        atlas = try_import_atlas()
        if atlas is None:
            s.skip(
                "atlas.load_config needs Python 3.11+; rerun with `uv run --script diag/check_env.py`"
            )
            return
        try:
            loaded = atlas.load_config(str(config_path))
        except SystemExit as e:
            s.fail(f"atlas.load_config rejected config.json: {e}")
            return
        repos = atlas.discover_repos(loaded)
        s.ok(f"discover_repos found {len(repos)} repo(s)")
        if repos:
            sample_name, sample_path = min(repos.items())
            timed = run(["git", "-C", str(sample_path), "ls-files", "-z", "--cached"], timeout=30)
            s.ok(
                f"git ls-files on {scrub(sample_name)}: exit={timed.returncode}, error={timed.error}"
            )


def _nearest_existing_ancestor(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.is_dir():
            return candidate
    return Path(tempfile.gettempdir())


def check_filesystem(report: Report) -> None:
    with report.section("filesystem") as s:
        atlas = try_import_atlas()
        config_path = KIT / "config.json"
        atlas_dir = None
        if atlas is not None and config_path.is_file():
            try:
                atlas_dir = atlas.load_config(str(config_path))["atlas_dir"]
            except SystemExit:
                atlas_dir = None
        if atlas_dir is None:
            atlas_dir = Path(os.environ.get("ATLAS_DIR", "~/atlas")).expanduser()
        atlas_dir = Path(atlas_dir)
        # atlas_dir itself may not exist yet (a fresh install), so probe the nearest ancestor
        # that does: that is the filesystem atlas_dir will actually be created on, and the one a
        # TemporaryDirectory needs to exist to be usable as `dir=`.
        probe_root = _nearest_existing_ancestor(atlas_dir)
        mounts = _read_mounts()
        with tempfile.TemporaryDirectory(dir=str(probe_root)) as tmp:
            tmp_path = Path(tmp)
            if mounts:
                note = (
                    ""
                    if probe_root == atlas_dir
                    else f" (nearest existing ancestor of {scrub(str(atlas_dir))})"
                )
                s.ok(
                    f"{scrub(str(probe_root))} filesystem: {fs_type(str(probe_root), mounts) or 'unknown'}{note}"
                )
            probe = tmp_path / "probe"
            probe.mkdir()
            probe.chmod(0o700)
            mode = probe.stat().st_mode & 0o777
            if mode == 0o700:
                s.ok("chmod 0700 sticks on this filesystem")
            else:
                s.warn(
                    f"chmod 0700 did not stick (got {oct(mode)}); likely a drvfs mount without metadata"
                )
            try:
                import fcntl

                lock_file = tmp_path / "lock"
                fh = open(lock_file, "a+", encoding="utf-8")  # noqa: SIM115
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    s.ok("fcntl.flock works here")
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
                    fh.close()
            except ImportError:
                s.warn("fcntl not available; atlas falls back to an O_EXCL lock file")


def check_env_section(report: Report) -> None:
    with report.section("env") as s:
        s.code(env_summary())


def check_network(report: Report) -> None:
    with report.section("network") as s:
        import urllib.error
        import urllib.request

        url = "https://pypi.org/simple/mcp/"
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=5) as resp:
                s.ok(f"reached {url}: HTTP {resp.status}")
        except urllib.error.URLError as e:
            s.warn(f"could not reach {url}: {e}")


CHECKS = {
    "system": check_system,
    "python": check_python,
    "tools": check_tools,
    "kit": check_kit,
    "config": check_config,
    "filesystem": check_filesystem,
    "env": check_env_section,
    "network": check_network,
}


def main() -> int:
    args = build_argparser("check_env.py", SECTIONS).parse_args()
    report = Report("check_env", full=args.full)
    for name in args.only or SECTIONS:
        CHECKS[name](report)
    sys.stdout.write(report.render())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
