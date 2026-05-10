"""
Smart Vision Alert — MySQL Database Layer
==========================================
Tracks processed events, AI analysis results, and alert history.
Uses pymysql (pure Python) for shared hosting compatibility.
"""

import pymysql
import pymysql.cursors
from datetime import datetime, timedelta
from typing import Optional

from config.settings import Settings
from core.models import AnalysisResult
from utils.logger import get_logger

log = get_logger()


class EventDB:
    """MySQL-backed state tracker for the monitoring pipeline."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._conn: Optional[pymysql.Connection] = None

    # ── connection management ─────────────────────────────────────────────────

    def _get_conn(self) -> pymysql.Connection:
        """Get or create a MySQL connection."""
        if self._conn is None or not self._conn.open:
            self._conn = pymysql.connect(
                host=self.settings.DB_HOST,
                port=self.settings.DB_PORT,
                user=self.settings.DB_USER,
                password=self.settings.DB_PASSWORD,
                database=self.settings.DB_NAME,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
                connect_timeout=10,
                read_timeout=30,
                write_timeout=30,
            )
        return self._conn

    def close(self):
        """Close the database connection."""
        if self._conn and self._conn.open:
            self._conn.close()
            self._conn = None

    # ── table initialization ──────────────────────────────────────────────────

    def init_tables(self):
        """Create tables if they don't exist."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processed_events (
                    id           INT AUTO_INCREMENT PRIMARY KEY,
                    file_id      VARCHAR(64) UNIQUE NOT NULL,
                    camera_did   VARCHAR(32) NOT NULL,
                    camera_name  VARCHAR(64),
                    event_type   VARCHAR(32),
                    event_time   DATETIME NOT NULL,
                    duration_sec FLOAT DEFAULT 0,
                    frames_saved INT DEFAULT 0,
                    capture_dir  VARCHAR(255),
                    processed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_camera_time (camera_did, event_time)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS analysis_results (
                    id              INT AUTO_INCREMENT PRIMARY KEY,
                    file_id         VARCHAR(64) NOT NULL,
                    camera_did      VARCHAR(32) NOT NULL,
                    is_safe         BOOLEAN DEFAULT TRUE,
                    risk_level      VARCHAR(16) DEFAULT 'safe',
                    description     TEXT,
                    hazards         JSON,
                    confidence      FLOAT DEFAULT 0,
                    motion_detected BOOLEAN DEFAULT TRUE,
                    stillness_warn  BOOLEAN DEFAULT FALSE,
                    segment_label   VARCHAR(32),
                    model_used      VARCHAR(64),
                    analyzed_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_risk (risk_level, analyzed_at),
                    INDEX idx_file (file_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS alert_history (
                    id           INT AUTO_INCREMENT PRIMARY KEY,
                    file_id      VARCHAR(64),
                    camera_did   VARCHAR(32),
                    risk_level   VARCHAR(16),
                    description  TEXT,
                    telegram_ok  BOOLEAN DEFAULT FALSE,
                    alerted_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_cooldown (camera_did, alerted_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS manual_triggers (
                    id           INT AUTO_INCREMENT PRIMARY KEY,
                    username     VARCHAR(64),
                    first_name   VARCHAR(64),
                    command      VARCHAR(32),
                    triggered_at DATETIME DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

        log.info("Database tables initialized")

    # ── processed events ──────────────────────────────────────────────────────

    def is_processed(self, file_id: str) -> bool:
        """Check if an event has already been processed."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM processed_events WHERE file_id = %s LIMIT 1",
                (file_id,),
            )
            return cur.fetchone() is not None

    def mark_processed(
        self,
        file_id: str,
        camera_did: str,
        camera_name: str,
        event_type: str,
        event_time: datetime,
        duration_sec: float = 0,
        frames_saved: int = 0,
        capture_dir: str = "",
    ):
        """Record a processed event."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO processed_events
                   (file_id, camera_did, camera_name, event_type,
                    event_time, duration_sec, frames_saved, capture_dir)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE
                    duration_sec = VALUES(duration_sec),
                    frames_saved = VALUES(frames_saved),
                    capture_dir  = VALUES(capture_dir)""",
                (
                    file_id, camera_did, camera_name, event_type,
                    event_time, duration_sec, frames_saved, capture_dir,
                ),
            )
        log.debug(f"Marked processed: {file_id} ({camera_name})")

    # ── analysis results ──────────────────────────────────────────────────────

    def save_analysis(
        self,
        file_id: str,
        camera_did: str,
        result: AnalysisResult,
        segment_label: str = "first",
    ):
        """Save an AI analysis result."""
        import json as _json

        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO analysis_results
                   (file_id, camera_did, is_safe, risk_level, description,
                    hazards, confidence, motion_detected, stillness_warn,
                    segment_label, model_used)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    file_id,
                    camera_did,
                    result.is_safe,
                    result.risk_level,
                    result.description,
                    _json.dumps(result.detected_hazards),
                    result.confidence,
                    result.motion_detected,
                    result.stillness_warning,
                    segment_label,
                    self.settings.GEMINI_MODEL,
                ),
            )
        log.debug(f"Saved analysis: {file_id} → {result.risk_level}")

    def get_analysis(self, file_id: str) -> Optional[AnalysisResult]:
        """Fetch a cached analysis result by file_id."""
        import json as _json

        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM analysis_results WHERE file_id = %s LIMIT 1",
                (file_id,),
            )
            row = cur.fetchone()
            if row:
                return AnalysisResult(
                    is_safe=bool(row["is_safe"]),
                    risk_level=row["risk_level"],
                    description=row["description"],
                    detected_hazards=_json.loads(row["hazards"]) if row["hazards"] else [],
                    confidence=row["confidence"],
                    motion_detected=bool(row["motion_detected"]),
                    stillness_warning=bool(row["stillness_warn"]),
                    temporal_description="",
                    raw_response="",
                    analysis_mode="multi_frame"
                )
        return None

    def get_capture_dir(self, file_id: str) -> str:
        """Fetch the capture directory for a given file_id."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT capture_dir FROM processed_events WHERE file_id = %s LIMIT 1",
                (file_id,),
            )
            row = cur.fetchone()
            return row["capture_dir"] if row else ""

    # ── alert history ─────────────────────────────────────────────────────────

    def save_alert(
        self,
        file_id: str,
        camera_did: str,
        result: AnalysisResult,
        telegram_ok: bool,
    ):
        """Record a sent alert."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO alert_history
                   (file_id, camera_did, risk_level, description, telegram_ok)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    file_id, camera_did, result.risk_level,
                    result.description, telegram_ok,
                ),
            )
        log.info(f"Alert recorded: {file_id} (telegram_ok={telegram_ok})")

    def last_alert_time(self, camera_did: str) -> Optional[datetime]:
        """Get the most recent alert time for a camera (for cooldown)."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT alerted_at FROM alert_history
                   WHERE camera_did = %s
                   ORDER BY alerted_at DESC LIMIT 1""",
                (camera_did,),
            )
            row = cur.fetchone()
            return row["alerted_at"] if row else None

    def is_in_cooldown(self, camera_did: str, cooldown_minutes: int) -> bool:
        """Check if a camera is within the alert cooldown period."""
        last_time = self.last_alert_time(camera_did)
        if last_time is None:
            return False
        elapsed = datetime.now() - last_time
        return elapsed < timedelta(minutes=cooldown_minutes)

    # ── manual triggers ───────────────────────────────────────────────────────

    def log_manual_trigger(self, username: str, first_name: str, command: str = "/check"):
        """Log a manual trigger command to the database."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO manual_triggers (username, first_name, command)
                   VALUES (%s, %s, %s)""",
                (username, first_name, command),
            )
        log.info(f"Manual trigger logged: {username} ({first_name})")

    # ── stats ─────────────────────────────────────────────────────────────────

    def get_today_stats(self) -> dict:
        """Get today's processing statistics."""
        conn = self._get_conn()
        today = datetime.now().strftime("%Y-%m-%d")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM processed_events WHERE DATE(processed_at) = %s",
                (today,),
            )
            events = cur.fetchone()["cnt"]

            cur.execute(
                "SELECT COUNT(*) AS cnt FROM analysis_results WHERE DATE(analyzed_at) = %s",
                (today,),
            )
            analyses = cur.fetchone()["cnt"]

            cur.execute(
                "SELECT COUNT(*) AS cnt FROM alert_history WHERE DATE(alerted_at) = %s",
                (today,),
            )
            alerts = cur.fetchone()["cnt"]

        return {
            "date": today,
            "events_processed": events,
            "ai_analyses": analyses,
            "alerts_sent": alerts,
        }
