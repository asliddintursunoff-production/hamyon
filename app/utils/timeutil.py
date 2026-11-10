from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(dt: datetime, tz: ZoneInfo) -> datetime:
    """Attach tz to naive datetimes (interpreted as local wall time)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def period_range(period: str, tz: ZoneInfo, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return [start, end) in tz-aware datetimes for a named period."""
    now = (now or utcnow()).astimezone(tz)
    today = now.date()
    if period == "today":
        return local_day_bounds(today, tz)
    if period == "week":
        start_day = today - timedelta(days=today.weekday())
        start = datetime.combine(start_day, time.min, tzinfo=tz)
        return start, start + timedelta(days=7)
    if period == "month":
        start = datetime.combine(today.replace(day=1), time.min, tzinfo=tz)
        return start, _add_month(start)
    if period == "last_month":
        this_month = datetime.combine(today.replace(day=1), time.min, tzinfo=tz)
        prev_day = this_month - timedelta(days=1)
        start = datetime.combine(prev_day.date().replace(day=1), time.min, tzinfo=tz)
        return start, this_month
    if period == "year":
        start = datetime.combine(today.replace(month=1, day=1), time.min, tzinfo=tz)
        return start, start.replace(year=start.year + 1)
    raise ValueError(f"unknown period {period!r}")


def _add_month(dt: datetime) -> datetime:
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1)
    return dt.replace(month=dt.month + 1)
