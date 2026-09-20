from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from django.utils import timezone


BUSINESS_TIME_ZONE = ZoneInfo("Europe/Moscow")


def business_localtime(value: datetime | None = None) -> datetime:
    current = value if value is not None else timezone.now()
    if timezone.is_naive(current):
        return timezone.make_aware(current, BUSINESS_TIME_ZONE)
    return current.astimezone(BUSINESS_TIME_ZONE)


def parse_business_datetime(raw: str | None) -> datetime:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("empty datetime")
    parsed = datetime.fromisoformat(text)
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, BUSINESS_TIME_ZONE)
    return parsed.astimezone(BUSINESS_TIME_ZONE)
