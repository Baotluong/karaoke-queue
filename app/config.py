"""Settings from environment variables (or a .env file in the project root)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    db_path: str = str(ROOT / "karaoke.db")
    auth_file: Path = ROOT / ".lounge_auth.json"
    yt_api_key: str | None = None
    fake_tv: bool = False
    fake_song_seconds: float = 20.0

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        return cls(
            db_path=os.getenv("KQ_DB", cls.db_path),
            auth_file=Path(os.getenv("KQ_AUTH_FILE", str(cls.auth_file))),
            yt_api_key=os.getenv("YT_API_KEY") or None,
            fake_tv=os.getenv("KQ_FAKE_TV", "").lower() in ("1", "true", "yes"),
            fake_song_seconds=float(os.getenv("KQ_FAKE_SONG_SECONDS", cls.fake_song_seconds)),
        )
