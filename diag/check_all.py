#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
"""Runs every free diagnostic (check_env, check_kiro, check_mcp) and prints one combined,
paste-able report. Never calls kiro-cli with a real prompt; pass --live to check_kiro.py
separately for that.

    python3 diag/check_all.py [--full]
"""

from __future__ import annotations

import argparse
import sys

import check_env
import check_kiro
import check_mcp
from diaglib import Report


def main() -> int:
    parser = argparse.ArgumentParser(prog="check_all.py")
    parser.add_argument("--full", action="store_true", help="do not cap command output")
    args = parser.parse_args()

    report = Report("check_all", full=args.full)
    for name in check_env.SECTIONS:
        check_env.CHECKS[name](report)
    for name in check_kiro.SECTIONS:
        check_kiro.CHECKS[name](report)
    for name in check_mcp.SECTIONS:
        check_mcp.CHECKS[name](report)

    sys.stdout.write(report.render())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
