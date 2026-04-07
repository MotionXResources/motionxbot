from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    token: str
    client_id: str
    guild_id: str
    bot_status: str
    port: int
    store_path: Path


def load_config() -> Config:
    return Config(
        token=os.getenv("DISCORD_TOKEN", ""),
        client_id=os.getenv("DISCORD_CLIENT_ID", ""),
        guild_id=os.getenv("DISCORD_GUILD_ID", ""),
        bot_status=os.getenv("BOT_STATUS", "Watching server workflows"),
        port=int(os.getenv("PORT", "3000")),
        store_path=Path(__file__).resolve().parent.parent / "data" / "store.json",
    )
