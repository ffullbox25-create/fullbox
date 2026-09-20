#!/usr/bin/env python3
"""Reject deploy scripts that can interrupt Fullbox or install unreadable files."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


DISRUPTIVE_SERVICE_PATTERNS = (
    re.compile(
        r"\bsystemctl\s+(?:restart|try-restart|stop|start)\s+fullbox(?:\.service)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bservice\s+fullbox(?:\.service)?\s+(?:restart|stop|start)\b",
        re.IGNORECASE,
    ),
)

PRODUCTION_APP_ROOT_PATTERN = re.compile(
    r"/opt/fullbox/fullbox(?:/|\b)",
    re.IGNORECASE,
)
PRODUCTION_ROOT_PATTERN = re.compile(r"/opt/fullbox(?:/|\b)", re.IGNORECASE)
RELATIVE_APP_PATH_PATTERN = re.compile(
    r"[\"']fullbox/[A-Za-z0-9_./{}-]+[\"']",
    re.IGNORECASE,
)
ABSOLUTE_APP_FILE_PATTERN = re.compile(
    r"/opt/fullbox/fullbox/[A-Za-z0-9_./{}-]+",
    re.IGNORECASE,
)
PRODUCTION_WRITE_PATTERNS = (
    re.compile(r"\b(?:cp|mv|install|rsync|scp)\s", re.IGNORECASE),
    re.compile(r"\bsftp\.put\s*\(", re.IGNORECASE),
    re.compile(r"\b(?:os\.)?replace\s*\(", re.IGNORECASE),
    re.compile(r"\bshutil\.(?:copy|copy2|copyfile|move)\s*\(", re.IGNORECASE),
    re.compile(r"\.(?:write_text|write_bytes)\s*\(", re.IGNORECASE),
)
OWNER_NORMALIZATION_PATTERN = re.compile(
    r"\bchown\s+(?:-[^\s]+\s+)*user:user\b",
    re.IGNORECASE,
)
MODE_NORMALIZATION_PATTERN = re.compile(
    r"\bchmod\s+(?:-[^\s]+\s+)*0?644\b",
    re.IGNORECASE,
)


def disruptive_matches(source: str) -> list[str]:
    matches: list[str] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        if any(pattern.search(line) for pattern in DISRUPTIVE_SERVICE_PATTERNS):
            matches.append(f"line {line_number}: {line.strip()}")
    return matches


def production_write_matches(source: str) -> list[str]:
    """Return likely remote writes when the script targets the production app tree."""
    targets_production_app = bool(PRODUCTION_APP_ROOT_PATTERN.search(source)) or (
        bool(PRODUCTION_ROOT_PATTERN.search(source))
        and bool(RELATIVE_APP_PATH_PATTERN.search(source))
    )
    if not targets_production_app:
        return []

    matches: list[str] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        if any(pattern.search(line) for pattern in PRODUCTION_WRITE_PATTERNS):
            matches.append(f"line {line_number}: {line.strip()}")
    return matches


def permission_problems(source: str) -> tuple[list[str], list[str]]:
    writes = production_write_matches(source)
    if not writes:
        return [], []

    problems: list[str] = []
    literal_write_targets = {
        target.rstrip("./")
        for match in writes
        for target in ABSOLUTE_APP_FILE_PATTERN.findall(match)
    }
    owner_targets = {
        target.rstrip("./")
        for line in source.splitlines()
        if OWNER_NORMALIZATION_PATTERN.search(line)
        for target in ABSOLUTE_APP_FILE_PATTERN.findall(line)
    }
    mode_targets = {
        target.rstrip("./")
        for line in source.splitlines()
        if MODE_NORMALIZATION_PATTERN.search(line)
        for target in ABSOLUTE_APP_FILE_PATTERN.findall(line)
    }

    if not OWNER_NORMALIZATION_PATTERN.search(source):
        problems.append(
            "нет обязательного 'chown user:user' после замены production-файлов"
        )
    elif literal_write_targets and not literal_write_targets.issubset(owner_targets):
        missing = ", ".join(sorted(literal_write_targets - owner_targets))
        problems.append(f"нет 'chown user:user' для target: {missing}")
    if not MODE_NORMALIZATION_PATTERN.search(source):
        problems.append(
            "нет обязательного 'chmod 644' после замены production-файлов"
        )
    elif literal_write_targets and not literal_write_targets.issubset(mode_targets):
        missing = ", ".join(sorted(literal_write_targets - mode_targets))
        problems.append(f"нет 'chmod 644' для target: {missing}")
    return writes, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deploy_script", help="Deploy script to inspect before execution")
    parser.add_argument(
        "--allow-restart",
        action="store_true",
        help="Allow a disruptive action after separate explicit confirmation",
    )
    parser.add_argument(
        "--restart-reason",
        default="",
        help="Why graceful reload is technically insufficient",
    )
    args = parser.parse_args(argv)

    path = Path(args.deploy_script)
    if not path.is_file():
        parser.error(f"deploy script does not exist: {path}")

    source = path.read_text(encoding="utf-8")
    matches = disruptive_matches(source)
    production_writes, file_permission_problems = permission_problems(source)
    restart_allowed = args.allow_restart and len(args.restart_reason.strip()) >= 20

    blocked = False
    if file_permission_problems:
        blocked = True
        print("DEPLOY FILE PERMISSION GUARD: BLOCKED", file=sys.stderr)
        print(
            "Сценарий записывает файлы в /opt/fullbox/fullbox/**, но не гарантирует "
            "владельца user:user и режим 644 для установленных файлов.",
            file=sys.stderr,
        )
        for problem in file_permission_problems:
            print(f"- {problem}", file=sys.stderr)
        for match in production_writes[:10]:
            print(f"- write candidate: {match}", file=sys.stderr)

    if matches and not restart_allowed:
        blocked = True
        print("DEPLOY SERVICE ACTION GUARD: BLOCKED", file=sys.stderr)
        print(
            "Use 'systemctl reload fullbox' for a routine code deploy. "
            "A full restart requires separate confirmation, --allow-restart, and --restart-reason.",
            file=sys.stderr,
        )
        for match in matches:
            print(f"- {match}", file=sys.stderr)

    if blocked:
        return 2

    if not matches and not production_writes:
        print("DEPLOY SERVICE ACTION GUARD: OK")
        print(f"script: {path}")
        return 0

    if production_writes:
        print("DEPLOY FILE PERMISSION GUARD: OK")
        print(f"script: {path}")

    if matches and restart_allowed:
        print("DEPLOY SERVICE ACTION GUARD: EXPLICIT RESTART APPROVED")
        print(f"script: {path}")
        print(f"reason: {args.restart_reason.strip()}")
    elif production_writes:
        print("DEPLOY SERVICE ACTION GUARD: OK")
        print(f"script: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
