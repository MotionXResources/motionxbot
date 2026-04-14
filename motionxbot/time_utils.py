from __future__ import annotations

import re

_DURATION_PATTERN = re.compile(
    r"(\d+)\s*(weeks?|wks?|wk|w|days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)",
    re.IGNORECASE,
)
_UNIT_MULTIPLIERS = {
    "w": 7 * 24 * 60 * 60 * 1000,
    "d": 24 * 60 * 60 * 1000,
    "h": 60 * 60 * 1000,
    "m": 60 * 1000,
    "s": 1000,
}

_UNIT_ALIASES = {
    "week": "w",
    "weeks": "w",
    "wk": "w",
    "wks": "w",
    "w": "w",
    "day": "d",
    "days": "d",
    "d": "d",
    "hour": "h",
    "hours": "h",
    "hr": "h",
    "hrs": "h",
    "h": "h",
    "minute": "m",
    "minutes": "m",
    "min": "m",
    "mins": "m",
    "m": "m",
    "second": "s",
    "seconds": "s",
    "sec": "s",
    "secs": "s",
    "s": "s",
}


def parse_duration(raw: str | None) -> int | None:
    if not raw or not isinstance(raw, str):
        return None

    normalized = raw.strip().lower()
    if not normalized:
        return None

    total = 0
    for match in _DURATION_PATTERN.finditer(normalized):
        unit = _UNIT_ALIASES[match.group(2).lower()]
        total += int(match.group(1)) * _UNIT_MULTIPLIERS[unit]

    compact = re.sub(r"\s+", "", normalized)
    consumed = "".join(match.group(0).replace(" ", "") for match in _DURATION_PATTERN.finditer(normalized))

    if total == 0 and normalized.isdigit():
        return int(normalized) * 60 * 1000

    if total == 0 or compact != consumed:
        return None

    return total


def format_duration(milliseconds: int) -> str:
    if milliseconds < 1000:
        return "under a second"

    remaining = milliseconds
    parts: list[str] = []
    for label, value in (
        ("w", _UNIT_MULTIPLIERS["w"]),
        ("d", _UNIT_MULTIPLIERS["d"]),
        ("h", _UNIT_MULTIPLIERS["h"]),
        ("m", _UNIT_MULTIPLIERS["m"]),
        ("s", _UNIT_MULTIPLIERS["s"]),
    ):
        if remaining >= value:
            amount = remaining // value
            remaining -= amount * value
            parts.append(f"{amount}{label}")
        if len(parts) == 2:
            break

    return " ".join(parts)


def render_discord_timestamp(timestamp_ms: int, style: str = "f") -> str:
    return f"<t:{timestamp_ms // 1000}:{style}>"
