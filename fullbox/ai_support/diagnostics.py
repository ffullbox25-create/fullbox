"""Allowlisted, read-only diagnostics for employee support incidents."""

from __future__ import annotations

import re
import subprocess
from urllib.parse import urlsplit

from django.utils import timezone


ALLOWED_SERVICE_UNITS = ("fullbox.service", "fullbox-asgi.service")
ERROR_MARKERS = (
    " error ",
    "[error]",
    "traceback",
    "exception",
    "internal server error",
    "status=500",
    " 500 ",
    "slow_request",
)
MAX_JOURNAL_LINES = 80
MAX_JOURNAL_CHARS = 12_000

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_IP_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_USER_RE = re.compile(r"\b(user|username|login)=([^\s&]+)", re.I)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I)
_SECRET_RE = re.compile(
    r"\b(token|secret|password|authorization|api[_-]?key|session|csrf|signature)=([^\s&]+)",
    re.I,
)


def redact_diagnostic_text(value: str) -> str:
    text = str(value or "")
    text = _EMAIL_RE.sub("<email>", text)
    text = _IP_RE.sub("<ip>", text)
    text = _USER_RE.sub(lambda match: f"{match.group(1)}=<user>", text)
    text = _BEARER_RE.sub("Bearer <secret>", text)
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=<secret>", text)
    return text


def _run_readonly(command: list[str], *, timeout: int = 8) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _service_states() -> dict[str, str]:
    states: dict[str, str] = {}
    for unit in ALLOWED_SERVICE_UNITS:
        try:
            result = _run_readonly(["systemctl", "is-active", unit], timeout=4)
            state = (result.stdout or result.stderr or "unknown").strip().splitlines()[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            state = "unavailable"
        states[unit] = state[:40]
    return states


def _incident_markers(incident) -> tuple[str, ...]:
    values = [incident.object_id, incident.object_type, incident.zone]
    path = urlsplit(str(incident.source_url or "")).path
    if path and path != "/":
        values.append(path)
    return tuple(
        value.casefold()
        for value in (str(item or "").strip() for item in values)
        if len(value) >= 3
    )


def _relevant_line(line: str, markers: tuple[str, ...]) -> bool:
    folded = f" {line.casefold()} "
    return any(marker in folded for marker in ERROR_MARKERS) or any(
        marker in folded for marker in markers
    )


def _recent_journal_lines(incident) -> list[str]:
    markers = _incident_markers(incident)
    selected: list[str] = []
    for unit in ALLOWED_SERVICE_UNITS:
        try:
            result = _run_readonly(
                [
                    "journalctl",
                    "--unit",
                    unit,
                    "--since",
                    "30 minutes ago",
                    "--no-pager",
                    "--output",
                    "short-iso",
                    "--lines",
                    "400",
                ]
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for raw_line in result.stdout.splitlines():
            if _relevant_line(raw_line, markers):
                selected.append(redact_diagnostic_text(raw_line)[:1000])
    selected = selected[-MAX_JOURNAL_LINES:]
    while selected and sum(len(line) + 1 for line in selected) > MAX_JOURNAL_CHARS:
        selected.pop(0)
    return selected


def collect_safe_diagnostics(incident) -> dict:
    """Collect a small diagnostic snapshot without accepting model commands."""

    log_lines = _recent_journal_lines(incident)
    return {
        "collected_at": timezone.now().isoformat(),
        "read_only": True,
        "services": _service_states(),
        "log_lines": log_lines,
        "log_line_count": len(log_lines),
    }
