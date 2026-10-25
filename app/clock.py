from datetime import datetime, timedelta, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def utc_hours_ago_iso(hours: int) -> str:
    return (utc_now() - timedelta(hours=hours)).isoformat()
