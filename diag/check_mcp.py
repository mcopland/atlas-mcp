#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
"""Tester diagnostic: atlas_mcp.py warm-up and a real stdio handshake, including exactly how
Kiro would launch it (from every registration found under ~/.kiro).

    python3 diag/check_mcp.py [--only SECTION ...] [--full]

Speaks raw newline-delimited JSON-RPC over stdio rather than importing the `mcp` package, so it
runs the same on a tester's system Python as it does under `uv run --script`.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import diaglib
from diaglib import Report, build_argparser, inspect_kiro_home, kiro_home, run, scrub

SECTIONS = ["warmup", "handshake", "registration"]
KIT = diaglib.KIT


def check_warmup(report: Report) -> None:
    with report.section("warmup") as s:
        result = run(
            ["uv", "run", "--locked", "--script", str(KIT / "atlas_mcp.py")], timeout=120, input=""
        )
        if result.error:
            s.fail(f"uv run --locked --script atlas_mcp.py: {result.error}")
            return
        s.ok(f"exit={result.returncode}")
        if result.returncode != 0:
            s.fail("warm-up failed; see stderr below (often a PyPI resolve or lockfile drift)")
        if result.stderr.strip():
            s.code(scrub(result.stderr))


def handshake(
    cmd: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    timeout: float = 15,
) -> dict[str, Any]:
    """Speaks the MCP stdio wire protocol directly (newline-delimited JSON-RPC): initialize,
    notifications/initialized, then tools/list. No `mcp` import needed, so this runs the same
    whether or not that package is installed in the caller's interpreter."""
    import subprocess

    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(cmd),
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as e:
        return {"ok": False, "error": f"launch failed: {e}"}

    responses: dict[int, dict[str, Any]] = {}
    lock = threading.Lock()

    def reader() -> None:
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and "id" in msg:
                    with lock:
                        responses[msg["id"]] = msg
        except ValueError:
            return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    def send(obj: dict[str, Any]) -> bool:
        try:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            return False
        return True

    def wait_for(msg_id: int, deadline: float) -> dict[str, Any] | None:
        while time.monotonic() < deadline:
            with lock:
                if msg_id in responses:
                    return responses.pop(msg_id)
            time.sleep(0.02)
        return None

    deadline = start + timeout
    try:
        if not send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "atlas-diag", "version": "1"},
                },
            }
        ):
            return {"ok": False, "error": "could not write to server stdin"}
        init_resp = wait_for(1, deadline)
        if init_resp is None:
            return {
                "ok": False,
                "error": f"no response to initialize within {timeout:.0f}s (timed out)",
            }
        if "error" in init_resp:
            return {"ok": False, "error": f"initialize error: {init_resp['error']}"}
        result = init_resp.get("result", {})
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        list_resp = wait_for(2, deadline)
        if list_resp is None:
            return {
                "ok": False,
                "error": f"no response to tools/list within {timeout:.0f}s (timed out)",
            }
        if "error" in list_resp:
            return {"ok": False, "error": f"tools/list error: {list_resp['error']}"}
        tools = [t["name"] for t in list_resp.get("result", {}).get("tools", [])]
        return {
            "ok": True,
            "server_name": result.get("serverInfo", {}).get("name"),
            "protocol_version": result.get("protocolVersion"),
            "tools": tools,
            "elapsed": time.monotonic() - start,
        }
    finally:
        try:
            proc.stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print(f"diag: {cmd[0]} did not exit after SIGKILL", file=sys.stderr)


def check_handshake(report: Report) -> None:
    with report.section("handshake") as s:
        with tempfile.TemporaryDirectory(prefix="atlas-diag-mcp-") as tmp:
            cmd = ["uv", "run", "--locked", "--script", str(KIT / "atlas_mcp.py")]
            result = handshake(cmd, env={**os.environ, "ATLAS_DIR": tmp}, timeout=60)
        _report_handshake(s, "uv run --locked --script atlas_mcp.py", result)


def _report_handshake(s: diaglib.Section, label: str, result: dict[str, Any]) -> None:
    if not result["ok"]:
        s.fail(f"{label}: {result['error']}")
        return
    s.ok(
        f"{label}: server={result['server_name']!r} protocol={result['protocol_version']} in {result['elapsed']:.2f}s"
    )
    s.ok(f"tools: {', '.join(result['tools'])}")


def check_registration(report: Report) -> None:
    with report.section("registration") as s:
        home = kiro_home()
        if not home.is_dir():
            s.skip(f"{scrub(str(home))} does not exist")
            return
        info = inspect_kiro_home(home, KIT)
        if not info["atlas_servers"]:
            s.skip("no atlas MCP registration found under ~/.kiro")
            return
        for label, entry in info["atlas_servers"].items():
            command = entry.get("command")
            args = entry.get("args") or []
            if not command:
                s.fail(f"{label}: no command set")
                continue
            # Kiro does not inherit the shell PATH (README section 5), so this reproduces that
            # exactly rather than launching with the tester's normal environment.
            minimal_env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "")}
            result = handshake([command, *args], env=minimal_env, timeout=60)
            _report_handshake(s, f"{label} ({scrub(command)})", result)


CHECKS = {
    "warmup": check_warmup,
    "handshake": check_handshake,
    "registration": check_registration,
}


def main() -> int:
    args = build_argparser("check_mcp.py", SECTIONS).parse_args()
    report = Report("check_mcp", full=args.full)
    for name in args.only or SECTIONS:
        CHECKS[name](report)
    sys.stdout.write(report.render())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
