"""Helpers for turning Kalshi's timestamp fields into unix seconds."""
from datetime import datetime, timezone


def to_unix_ts(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value)
    try:
        return int(text)
    except ValueError:
        pass
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def now_unix_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())
