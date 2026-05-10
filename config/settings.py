"""
Smart Vision Alert — Configuration Settings
Loads environment variables from .env file and provides typed access.
"""

import os
import sys
import json
from pathlib import Path
from dotenv import load_dotenv


# ── Resolve paths ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / "config" / ".env"


def _load_env():
    """Load .env file, fallback to .env.example if .env doesn't exist."""
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)
    else:
        example = PROJECT_ROOT / "config" / ".env.example"
        if example.exists():
            load_dotenv(example)
            print(
                "[WARNING] .env not found — loaded .env.example defaults. "
                "Copy config/.env.example → config/.env and fill in your values.",
                file=sys.stderr,
            )


_load_env()


# ── Configuration Class ───────────────────────────────────────
class Settings:
    """Typed application settings loaded from environment."""

    # Xiaomi Cloud
    XIAOMI_USERNAME: str = os.getenv("XIAOMI_USERNAME", "")
    XIAOMI_PASSWORD: str = os.getenv("XIAOMI_PASSWORD", "")
    XIAOMI_SERVER_REGION: str = os.getenv("XIAOMI_SERVER_REGION", "sg")

    # Gemini AI
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "super_secret_string_123")

    # App
    CAPTURE_INTERVAL: int = int(os.getenv("CAPTURE_INTERVAL_SECONDS", "300"))
    IMAGE_MAX_SIZE_KB: int = int(os.getenv("IMAGE_MAX_SIZE_KB", "500"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    ALERT_COOLDOWN_MINUTES: int = int(os.getenv("ALERT_COOLDOWN_MINUTES", "15"))
    ALERT_THRESHOLD: str = os.getenv("ALERT_THRESHOLD", "medium")

    # Camera source
    CAMERA_SOURCE: str = os.getenv("CAMERA_SOURCE", "local_folder")
    CAMERA_SNAPSHOT_URL: str = os.getenv("CAMERA_SNAPSHOT_URL", "")

    # Multi-frame / temporal analysis
    MULTI_FRAME_ENABLED: bool = os.getenv("MULTI_FRAME_ENABLED", "true").lower() == "true"
    MULTI_FRAME_COUNT: int = int(os.getenv("MULTI_FRAME_COUNT", "3"))
    MULTI_FRAME_INTERVAL_SECONDS: int = int(os.getenv("MULTI_FRAME_INTERVAL_SECONDS", "5"))

    # Storage
    CAPTURE_RETENTION_DAYS: int = int(os.getenv("CAPTURE_RETENTION_DAYS", "3"))

    # MySQL Database
    DB_HOST: str = os.getenv("DB_HOST", "localhost")
    DB_PORT: int = int(os.getenv("DB_PORT", "3306"))
    DB_USER: str = os.getenv("DB_USER", "")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")
    DB_NAME: str = os.getenv("DB_NAME", "smart_vision_alert")

    # Studio Cameras
    STUDIO_SESSION_FILE: str = os.getenv("STUDIO_SESSION_FILE", "config/.micloud_session_new")
    STUDIO_CAMERA_HOST: str = os.getenv("STUDIO_CAMERA_HOST", "sg.business.smartcamera.api.io.mi.com")
    STUDIO_HOURS_START: int = int(os.getenv("STUDIO_HOURS_START", "9"))
    STUDIO_HOURS_END: int = int(os.getenv("STUDIO_HOURS_END", "23"))

    # Cloud Run integration
    CLOUD_RUN_URL: str    = os.getenv("CLOUD_RUN_URL", "")      # e.g. https://xxx.run.app
    CLOUD_RUN_SECRET: str = os.getenv("CLOUD_RUN_SECRET", "")   # shared secret for /run trigger
    INTERNAL_SECRET: str  = os.getenv("INTERNAL_SECRET", "")    # shared secret for A2 /api/* callbacks
    A2_BASE_URL: str      = os.getenv("A2_BASE_URL", "")        # e.g. https://yourdomain.com

    # Google Cloud Storage (Xiaomi session persistence)
    GCS_BUCKET: str       = os.getenv("GCS_BUCKET", "")
    GCS_SESSION_BLOB: str = os.getenv("GCS_SESSION_BLOB", "xiaomi_session.json")

    @property
    def STUDIO_CAMERAS(self) -> list[dict]:
        """Parse studio cameras from JSON string in env."""
        raw = os.getenv("STUDIO_CAMERAS", "[]")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []

    # Derived paths
    PROJECT_ROOT: Path = PROJECT_ROOT
    CAPTURES_DIR: Path = PROJECT_ROOT / "captures"
    MANUAL_DIR: Path = PROJECT_ROOT / "captures" / "manual"
    HISTORY_DIR: Path = PROJECT_ROOT / "captures" / "history"
    LOGS_DIR: Path = PROJECT_ROOT / "logs"

    # Risk level ordering (for threshold comparison)
    _RISK_LEVELS = {"safe": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

    def risk_exceeds_threshold(self, risk_level: str) -> bool:
        """Check if a given risk level meets or exceeds the alert threshold."""
        level = self._RISK_LEVELS.get(risk_level.lower(), 0)
        threshold = self._RISK_LEVELS.get(self.ALERT_THRESHOLD.lower(), 2)
        return level >= threshold

    def validate(self) -> list[str]:
        """Validate that all required settings are present. Returns list of errors."""
        errors = []

        if not self.GEMINI_API_KEY:
            errors.append("GEMINI_API_KEY is required")

        if not self.TELEGRAM_BOT_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN is required")

        if not self.TELEGRAM_CHAT_ID:
            errors.append("TELEGRAM_CHAT_ID is required")

        if self.CAMERA_SOURCE == "xiaomi_cloud":
            if not self.XIAOMI_USERNAME:
                errors.append("XIAOMI_USERNAME is required for xiaomi_cloud mode")
            if not self.XIAOMI_PASSWORD:
                errors.append("XIAOMI_PASSWORD is required for xiaomi_cloud mode")

        if self.CAMERA_SOURCE == "url" and not self.CAMERA_SNAPSHOT_URL:
            errors.append("CAMERA_SNAPSHOT_URL is required for url mode")

        return errors

    def ensure_dirs(self):
        """Create required directories if they don't exist."""
        self.CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
        self.MANUAL_DIR.mkdir(parents=True, exist_ok=True)
        self.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        self.LOGS_DIR.mkdir(parents=True, exist_ok=True)


# Singleton instance
settings = Settings()
