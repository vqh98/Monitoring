from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    report_chat_id: int
    owner_id: int
    viral_chat_id: int | None
    initial_channels: tuple[str, ...]
    timezone: ZoneInfo
    day_start_hour: int
    daily_report_hour: int
    similarity_threshold: float
    match_window_hours: int
    poll_interval_seconds: int
    proofreading_interval_seconds: int
    viral_scan_interval_seconds: int
    miss_grace_minutes: int
    important_min_channels: int
    database_path: Path
    ai_api_key: str
    ai_base_url: str
    ai_model: str


def load_config() -> Config:
    # Load .env next to the application, independent of the process cwd.
    # MONITORING_BASE_DIR can be used by a service manager to relocate data.
    base_dir = Path(os.getenv("MONITORING_BASE_DIR", Path(__file__).resolve().parent.parent))
    load_dotenv(base_dir / ".env")
    required = ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN", "REPORT_CHAT_ID"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("تنظیمات اجباری ناقص است: " + ", ".join(missing))

    return Config(
        api_id=int(os.environ["TELEGRAM_API_ID"]),
        api_hash=os.environ["TELEGRAM_API_HASH"],
        bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        report_chat_id=int(os.environ["REPORT_CHAT_ID"]),
        owner_id=int(os.getenv("OWNER_ID", os.environ["REPORT_CHAT_ID"])),
        viral_chat_id=int(value) if (value := os.getenv("VIRAL_CHAT_ID", "").strip()) else None,
        initial_channels=tuple(x.strip() for x in os.getenv("INITIAL_CHANNELS", "").split(",") if x.strip()),
        timezone=ZoneInfo(os.getenv("TIMEZONE", "Asia/Tehran")),
        day_start_hour=int(os.getenv("DAY_START_HOUR", "8")),
        daily_report_hour=int(os.getenv("DAILY_REPORT_HOUR", "0")),
        similarity_threshold=float(os.getenv("SIMILARITY_THRESHOLD", "0.56")),
        match_window_hours=int(os.getenv("MATCH_WINDOW_HOURS", "48")),
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        proofreading_interval_seconds=max(2, int(os.getenv("PROOFREADING_INTERVAL_SECONDS", "5"))),
        viral_scan_interval_seconds=int(os.getenv("VIRAL_SCAN_INTERVAL_SECONDS", "300")),
        miss_grace_minutes=int(os.getenv("MISS_GRACE_MINUTES", "120")),
        important_min_channels=int(os.getenv("IMPORTANT_MIN_CHANNELS", "3")),
        database_path=_resolve_path(base_dir, os.getenv("DATABASE_PATH", "data/monitor.db")),
        ai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        ai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        ai_model=os.getenv("OPENAI_MODEL", "claude-sonnet-5").strip(),
    )


def _resolve_path(base_dir: Path, value: str) -> Path:
    """Resolve relative persistent-data paths from the application directory."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else base_dir / path
