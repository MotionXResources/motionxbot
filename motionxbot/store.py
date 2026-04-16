from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def create_guild_defaults() -> dict[str, Any]:
    return {
        "channelTimers": [],
        "warnings": [],
        "audioReview": {
            "reviewCategoryId": None,
            "reviewerRoleId": None,
            "destinationType": None,
            "destinationId": None,
            "logChannelId": None,
            "closeDelayMs": 10 * 60 * 1000,
        },
        "audioSubmissions": [],
    }


def normalize_guild_data(data: dict[str, Any] | None = None) -> dict[str, Any]:
    incoming = data or {}
    return {
        **create_guild_defaults(),
        **incoming,
        "channelTimers": list(incoming.get("channelTimers") or []),
        "warnings": list(incoming.get("warnings") or []),
        "audioReview": {
            **create_guild_defaults()["audioReview"],
            **dict(incoming.get("audioReview") or {}),
        },
        "audioSubmissions": list(incoming.get("audioSubmissions") or []),
    }


class Store:
    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self.data: dict[str, Any] = {"guilds": {}, "dmLogs": []}
        self.load()

    def load(self) -> dict[str, Any]:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.store_path.exists():
            self.save()
            return self.data

        raw = self.store_path.read_text(encoding="utf-8").strip()
        parsed = json.loads(raw) if raw else {}
        guilds = parsed.get("guilds", {})
        self.data = {"guilds": {}, "dmLogs": list(parsed.get("dmLogs") or [])}
        for guild_id, guild_data in guilds.items():
            self.data["guilds"][str(guild_id)] = normalize_guild_data(guild_data)
        self.save()
        return self.data

    def save(self) -> None:
        self.store_path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def get_guild_data(self, guild_id: int | str) -> dict[str, Any]:
        key = str(guild_id)
        if key not in self.data["guilds"]:
            self.data["guilds"][key] = create_guild_defaults()
            self.save()
        return self.data["guilds"][key]

    def append_dm_log(self, entry: dict[str, Any], *, max_entries: int = 500) -> None:
        self.data.setdefault("dmLogs", []).append(entry)
        self.data["dmLogs"] = self.data["dmLogs"][-max_entries:]
        self.save()
