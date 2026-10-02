"""Shared building blocks for the diag/check_*.py scripts.

These scripts run on a tester's machine, not the maintainer's, so this module has to work back
to Python 3.8 (the oldest interpreter a WSL Ubuntu 20.04 image still ships) even though the rest
of the repo requires 3.11+. `atlas` itself is only imported lazily, through `try_import_atlas`,
so a section that needs it can degrade to a clear SKIP instead of failing to import at all.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import re
import socket
import subprocess
import sys
import types
import unicodedata  # noqa: F401  (kept imported for str width helpers some sections may want)
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

KIT = Path(__file__).resolve().parent.parent
BEGIN = "----- BEGIN ATLAS DIAG -----"
END = "----- END ATLAS DIAG -----"

# What every section's `with report.section(...)` block catches on the tester's behalf. Anything
# else is a bug in the diagnostic script itself and should surface as a real traceback rather
# than a quiet FAIL line, so it is deliberately not caught here.
CAUGHT: tuple[type[BaseException], ...] = (
    OSError,
    subprocess.SubprocessError,
    ValueError,
    UnicodeError,
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MNT_C_USER_RE = re.compile(r"(/mnt/c/Users/)([^/\s]+)")
# `kiro-cli whoami` prints the account's ARN and IAM Identity Center start URL, both of which
# identify the tester's employer, not just their machine.
AWS_ARN_RE = re.compile(r"(arn:aws[a-z0-9-]*:[a-z0-9-]+:[a-z0-9-]*:)\d{12}:\S+")
AWSAPPS_RE = re.compile(r"[A-Za-z0-9-]+\.awsapps\.com")

# Env vars whose value is safe to print as-is (no secrets, no machine-identifying paths beyond
# what scrub() already masks).
ENV_VALUE_WHITELIST = ("LANG", "LC_ALL", "SHELL", "NO_COLOR", "WSL_DISTRO_NAME", "ATLAS_DIR")
# Env vars where only presence matters; the value itself must never be printed.
ENV_PRESENCE_ONLY = ("KIRO_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "UV_INDEX_URL")


def current_user() -> str:
    try:
        return getpass.getuser()
    except OSError:
        return ""


def current_host() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def kiro_home() -> Path:
    """The one place every section resolves ~/.kiro from, so KIRO_HOME can point tests (and a
    tester who keeps Kiro config somewhere nonstandard) at a different directory."""
    return Path(os.environ.get("KIRO_HOME", "~/.kiro")).expanduser()


def try_import_atlas() -> types.ModuleType | None:
    """`atlas` needs Python 3.11+ (tomllib, dt.UTC). Return None rather than raising when the
    running interpreter cannot import it, so callers can SKIP instead of crashing."""
    if sys.version_info < (3, 11):
        return None
    if str(KIT) not in sys.path:
        sys.path.insert(0, str(KIT))
    try:
        import atlas  # type: ignore[import-not-found]
    except ImportError:
        return None
    return atlas


class RunResult:
    """The outcome of `run()`. `error` is None on a clean exit (any returncode); otherwise it is
    a human string ("not found", "timed out after Ns") and `returncode` is None."""

    def __init__(
        self,
        args: Sequence[str],
        returncode: int | None,
        stdout: str,
        stderr: str,
        error: str | None,
    ) -> None:
        self.args = list(args)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.error = error

    def __repr__(self) -> str:
        return (
            f"RunResult(args={self.args!r}, returncode={self.returncode!r}, error={self.error!r})"
        )


def run(
    cmd: Sequence[str],
    *,
    timeout: float = 10,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    input: str | None = None,
) -> RunResult:
    """subprocess.run wrapped so a missing binary or a timeout is data, not an exception a
    section's `with report.section(...)` has to catch itself."""
    try:
        proc = subprocess.run(  # noqa: PLW1510 - check is intentionally omitted, see below
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=cwd,
            input=input,
            stdin=subprocess.DEVNULL if input is None else None,
        )
    except FileNotFoundError:
        return RunResult(cmd, None, "", "", "not found")
    except subprocess.TimeoutExpired as e:
        return RunResult(
            cmd, None, e.stdout or "", e.stderr or "", f"timed out after {timeout:.0f}s"
        )
    # returncode is reported, not raised on: a nonzero exit (e.g. `kiro-cli whoami` when logged
    # out) is exactly the kind of thing this report exists to show, not a crash in the script.
    return RunResult(cmd, proc.returncode, proc.stdout or "", proc.stderr or "", None)


def clip(text: str, head: int = 60, tail: int = 20) -> str:
    """Cap a command's output to its first `head` and last `tail` lines, so one runaway command
    cannot blow the report past what is comfortable to paste into chat."""
    lines = text.splitlines()
    if len(lines) <= head + tail:
        return text
    omitted = len(lines) - head - tail
    marker = f"... [{omitted} lines omitted; rerun with --full to see everything] ..."
    return "\n".join([*lines[:head], marker, *lines[-tail:]])


def scrub(text: str, atlas_mod: types.ModuleType | None = None) -> str:
    """Best-effort redaction before a report is printed: the tester pastes this into chat, so
    anything identifying (home dir, username, hostname, email) or secret-shaped has to be gone
    before it leaves their machine, not after."""
    # EMAIL_RE runs before the username substitution below: if the username is also an email's
    # local part (tester@corp.example.com), replacing the username first leaves "<user>@corp.
    # example.com" behind, and the trailing company domain survives since '<' and '>' are not
    # valid local-part characters for EMAIL_RE to still match.
    text = EMAIL_RE.sub("<redacted-email>", text)
    text = AWS_ARN_RE.sub(r"\1<account>:<redacted>", text)
    text = AWSAPPS_RE.sub("<org>.awsapps.com", text)
    home = str(Path.home())
    if home and home != "/":
        text = text.replace(home, "~")
    user = current_user()
    if user:
        text = re.sub(rf"\b{re.escape(user)}\b", "<user>", text)
    host = current_host()
    if host:
        text = re.sub(rf"\b{re.escape(host)}\b", "<host>", text)
    text = MNT_C_USER_RE.sub(r"\1<user>", text)
    if atlas_mod is not None:
        text = atlas_mod.redact(text)
    return text


def configured_kiro_bin(kit: Path) -> str:
    """The kiro_bin a tester's config.json actually names, falling back to the bare command a
    PATH lookup would use. Shared by check_kiro (which calls kiro-cli chat) and check_env (which
    probes kiro-cli under a cron-like minimal PATH), so both agree with what atlas.py itself runs
    rather than each guessing independently."""
    config_path = kit / "config.json"
    if config_path.is_file():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "kiro-cli"
        if isinstance(cfg, dict):
            return str(cfg.get("kiro_bin", "kiro-cli"))
    return "kiro-cli"


def env_summary() -> str:
    """Renders the whitelisted env vars plus presence-only flags for the secret-shaped ones.
    Never the actual value of anything in ENV_PRESENCE_ONLY."""
    lines = []
    for name in ENV_VALUE_WHITELIST:
        value = os.environ.get(name)
        lines.append(f"{name}: {scrub(value) if value is not None else '<unset>'}")
    for name in ENV_PRESENCE_ONLY:
        lines.append(f"{name}: {'set' if name in os.environ else 'unset'}")
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    lines.append("PATH entries:")
    lines.extend(f"  {scrub(p)}" for p in path_entries if p)
    return "\n".join(lines)


def wsl_kind(proc_version_text: str, distro_env: str) -> str | None:
    """WSL2 runs a real Linux kernel ("-microsoft-standard-WSL2" in /proc/version); WSL1
    translates syscalls and its kernel string just says "-Microsoft". Neither substring nor
    WSL_DISTRO_NAME alone is reliable across WSL1/2, so this checks both."""
    text = proc_version_text.lower()
    if "microsoft-standard-wsl2" in text:
        return "WSL2"
    if "microsoft" in text:
        return "WSL1"
    if distro_env:
        return "WSL2"  # WSL_DISTRO_NAME is set but the kernel string didn't say so either way
    return None


def fs_type(path: str, mounts_text: str) -> str | None:
    """Longest matching mount point in a /proc/mounts-style text wins, the same rule the kernel
    itself uses to pick which mount a path belongs to."""
    path = os.path.normpath(path)
    best_point, best_fs = "", None
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        _device, mount_point, fs = parts[0], parts[1], parts[2]
        matches = (
            path == mount_point
            or path.startswith(mount_point.rstrip("/") + "/")
            or mount_point == "/"
        )
        if matches and len(mount_point) >= len(best_point):
            best_point, best_fs = mount_point, fs
    return best_fs


def has_crlf(path: str | Path) -> bool:
    return b"\r\n" in Path(path).read_bytes()


def flag_support(help_text: str, flags: Iterable[str]) -> dict[str, bool]:
    return {flag: flag in help_text for flag in flags}


def classify_bin(path: str) -> str:
    """Distinguishes a native Linux binary from a Windows binary reached through WSL interop
    (`/mnt/c/.../*.exe`), since atlas shells out to `kiro_bin` directly and the two behave very
    differently under WSL (path translation, line endings, signal handling)."""
    if path.startswith("/mnt/") and path.lower().endswith(".exe"):
        return "windows-interop"
    if path.startswith("/"):
        return "linux"
    return "unknown"


def inspect_kiro_home(kiro_home: Path, kit: Path) -> dict[str, Any]:
    """Compares what's installed under ~/.kiro against this repo's kiro/ files, and flags the
    two registration mistakes the README explicitly warns about: registering `atlas` twice, and
    a relative `command` (Kiro does not inherit the shell PATH)."""
    result: dict[str, Any] = {"files": {}, "atlas_servers": {}, "other_servers": [], "warnings": []}
    for sub in ("agents", "settings", "steering"):
        d = kiro_home / sub
        result["files"][sub] = sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def read_json(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    sources = {
        "agents/atlas.json": kiro_home / "agents" / "atlas.json",
        "settings/mcp.json": kiro_home / "settings" / "mcp.json",
    }
    for label, path in sources.items():
        data = read_json(path)
        servers = (data or {}).get("mcpServers", {}) if isinstance(data, dict) else {}
        for name, entry in servers.items():
            if name == "atlas":
                result["atlas_servers"][label] = {
                    "command": entry.get("command"),
                    "args": entry.get("args"),
                }
            elif name not in result["other_servers"]:
                result["other_servers"].append(name)

    if len(result["atlas_servers"]) > 1:
        result["warnings"].append(
            "atlas is registered in both agents/atlas.json and settings/mcp.json; "
            "the README says to use only one"
        )
    for label, entry in result["atlas_servers"].items():
        command = entry.get("command") or ""
        if command and not os.path.isabs(command):
            result["warnings"].append(f"{label}: command {command!r} is relative, not absolute")

    for name in ("atlas.json", "atlas-bundle.json", "atlas-mapper.json"):
        installed = kiro_home / "agents" / name
        repo_copy = kit / "kiro" / "agents" / name
        if not installed.is_file() or not repo_copy.is_file():
            continue
        same = installed.read_text(encoding="utf-8") == repo_copy.read_text(encoding="utf-8")
        result.setdefault("agent_diffs", {})[name] = (
            "identical" if same else "differs from repo copy"
        )

    return result


class Section:
    def __init__(self, name: str, *, full: bool = False) -> None:
        self.name = name
        self.lines: list[tuple[str, str]] = []
        # The report-wide --full default; an individual code() call can still override it.
        self.full = full

    def ok(self, msg: str) -> None:
        self.lines.append(("OK", msg))

    def warn(self, msg: str) -> None:
        self.lines.append(("WARN", msg))

    def fail(self, msg: str) -> None:
        self.lines.append(("FAIL", msg))

    def skip(self, msg: str) -> None:
        self.lines.append(("SKIP", msg))

    def code(self, text: str, *, head: int = 60, tail: int = 20, full: bool | None = None) -> None:
        use_full = self.full if full is None else full
        body = text if use_full else clip(text, head=head, tail=tail)
        self.lines.append(("CODE", body))

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for level, _ in self.lines:
            out[level] = out.get(level, 0) + 1
        return out


class _SectionCtx:
    def __init__(self, report: Report, name: str) -> None:
        self.report = report
        self.section = Section(name, full=report.full)

    def __enter__(self) -> Section:
        self.report.sections.append(self.section)
        return self.section

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _tb: types.TracebackType | None,
    ) -> bool:
        if exc_type is None:
            return False
        if issubclass(exc_type, CAUGHT):
            self.section.fail(f"{exc_type.__name__}: {exc}")
            return True
        return False  # not one of ours: propagate so it surfaces as a real traceback


class Report:
    def __init__(self, title: str, *, full: bool = False) -> None:
        self.title = title
        self.full = full
        self.sections: list[Section] = []

    def section(self, name: str) -> _SectionCtx:
        return _SectionCtx(self, name)

    def _header(self) -> list[str]:
        lines = [BEGIN, f"# {self.title}"]
        commit = run(["git", "-C", str(KIT), "rev-parse", "--short", "HEAD"], timeout=5)
        branch = run(["git", "-C", str(KIT), "rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
        status = run(["git", "-C", str(KIT), "status", "--porcelain"], timeout=5)
        if commit.error is None and commit.returncode == 0:
            dirty = "yes" if status.returncode == 0 and status.stdout.strip() else "no"
            branch_name = branch.stdout.strip() if branch.returncode == 0 else "?"
            lines.append(f"kit: {commit.stdout.strip()} ({branch_name}, dirty: {dirty})")
        else:
            lines.append("kit: not a git checkout")
        return lines

    def _summary(self) -> str | None:
        totals: dict[str, int] = {}
        for section in self.sections:
            for level, count in section.counts().items():
                totals[level] = totals.get(level, 0) + count
        parts = [
            f"{totals[level]} {level}"
            for level in ("FAIL", "WARN", "OK", "SKIP")
            if totals.get(level)
        ]
        return "summary: " + ", ".join(parts) if parts else None

    @property
    def exit_code(self) -> int:
        return 1 if any(s.counts().get("FAIL") for s in self.sections) else 0

    def render(self) -> str:
        out = self._header()
        summary = self._summary()
        if summary:
            out.append(summary)
        for section in self.sections:
            out.append("")
            out.append(f"## {section.name}")
            for level, text in section.lines:
                if level == "CODE":
                    out.append("```")
                    out.append(text)
                    out.append("```")
                else:
                    out.append(f"{level}: {text}")
        out.append("")
        out.append(END)
        return "\n".join(out) + "\n"


def build_argparser(
    prog: str, sections: Sequence[str], live: bool = False
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument(
        "--only",
        nargs="+",
        choices=list(sections),
        help=f"run only these sections: {', '.join(sections)}",
    )
    parser.add_argument("--full", action="store_true", help="do not cap command output")
    if live:
        parser.add_argument(
            "--live",
            action="store_true",
            help="also make real kiro-cli calls (spends a handful of requests)",
        )
        parser.add_argument(
            "--timeout", type=float, default=300, help="seconds per live kiro-cli call"
        )
    return parser


def print_report(report: Report) -> int:
    sys.stdout.write(report.render())
    return report.exit_code


@contextlib.contextmanager
def chdir(path: str | Path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)
