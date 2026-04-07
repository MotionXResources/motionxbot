from __future__ import annotations

from datetime import datetime


def parse_assignments(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}

    assignments: dict[str, str] = {}
    for part in raw.split(","):
        segment = part.strip()
        if not segment or "=" not in segment:
            continue
        key, value = segment.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key:
            assignments[key] = value

    return assignments


def build_base_placeholders(*, guild, channel, user) -> dict[str, str]:
    now = datetime.now().astimezone()
    return {
        "server": getattr(guild, "name", "server"),
        "server_id": str(getattr(guild, "id", "")),
        "channel": str(channel) if channel else "#channel",
        "channel_id": str(getattr(channel, "id", "")),
        "user": str(user) if user else "user",
        "user_id": str(getattr(user, "id", "")),
        "date": now.strftime("%x"),
        "time": now.strftime("%X"),
        "iso_date": now.isoformat(),
    }


def render_template_text(template: str, placeholders: dict[str, str] | None = None) -> str:
    placeholders = placeholders or {}
    rendered = str(template or "")
    for key, value in placeholders.items():
        rendered = rendered.replace(f"{{{key}}}", str(value))
    return rendered
