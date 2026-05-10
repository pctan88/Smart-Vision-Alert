"""
Smart Vision Alert — Telegram Alert Notifier
Sends safety alert messages with images to a Telegram group.
Uses raw HTTP requests (no heavy SDK) for shared hosting compatibility.
"""

import requests
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

_LOCAL_TZ = ZoneInfo("Asia/Kuala_Lumpur")

from config.settings import Settings
from core.models import AnalysisResult
from utils.logger import get_logger

log = get_logger()

# Emoji mapping for risk levels
RISK_EMOJI = {
    "safe": "✅",
    "low": "📝",
    "medium": "⚠️",
    "high": "🔴",
    "critical": "🚨",
}

RISK_LABEL = {
    "safe": "SAFE",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH ⚡",
    "critical": "CRITICAL 🆘",
}


class TelegramNotifier:
    """Send formatted safety alerts with images to Telegram."""

    API_BASE = "https://api.telegram.org/bot{token}"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self.api_url = self.API_BASE.format(token=self.token)
        self.offset_file = settings.LOGS_DIR.parent / "config" / ".telegram_offset"

    def get_new_commands(self) -> list[str]:
        """Fetch new commands sent to the bot since the last check."""
        if not self.token:
            return []

        url = f"{self.api_url}/getUpdates"
        offset = 0
        if self.offset_file.exists():
            try:
                offset = int(self.offset_file.read_text().strip())
            except ValueError:
                pass

        try:
            resp = requests.get(url, params={"offset": offset, "timeout": 5}).json()
            if not resp.get("ok"):
                return []

            commands = []
            highest_id = offset - 1
            for res in resp.get("result", []):
                update_id = res["update_id"]
                if update_id >= offset:
                    highest_id = max(highest_id, update_id)

                message = res.get("message", {})
                text = message.get("text", "")
                if text.startswith("/"):
                    commands.append(text.split("@")[0].lower())  # Handle /check@bot_name

            if highest_id >= offset:
                self.offset_file.write_text(str(highest_id + 1))

            return commands

        except Exception as e:
            log.error(f"Failed to fetch Telegram updates: {e}")
            return []

    def send_text(self, text: str) -> bool:
        """Send a plain text message (auto-escaped) to the Telegram group."""
        return self._send_text(self._escape_md(text))

    def send_alert(self, result: AnalysisResult, image_path: str) -> bool:
        """
        Send a safety alert with the CCTV image to the Telegram group.

        Returns True if sent successfully, False otherwise.
        """
        if not self.token or not self.chat_id:
            log.error("Telegram bot token or chat ID not configured")
            return False

        try:
            # Build formatted message
            message = self._format_alert_message(result)

            # Send photo with caption
            success = self._send_photo(image_path, message)

            if success:
                log.info(f"✅ Telegram alert sent (risk: {result.risk_level})")
            else:
                # Fallback: send text-only if photo fails
                log.warning("Photo send failed, trying text-only fallback")
                success = self._send_text(message)

            return success

        except Exception as e:
            log.error(f"Failed to send Telegram alert: {e}", exc_info=True)
            return False

    def send_test_message(self) -> bool:
        """Send a test message to verify bot configuration."""
        now = datetime.now(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
        message = (
            f"🔧 *{self._escape_md('Smart Vision Alert — Test')}*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ {self._escape_md('Bot is connected and working!')}\n"
            f"🕐 {self._escape_md(now)}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"_{self._escape_md('Aerial Studio CCTV Safety Monitor')}_"
        )
        return self._send_text(message)

    def _format_alert_message(self, result: AnalysisResult) -> str:
        """Format a rich alert message for Telegram."""
        emoji = RISK_EMOJI.get(result.risk_level, "⚠️")
        label = RISK_LABEL.get(result.risk_level, "UNKNOWN")
        now = datetime.now(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

        # Build hazard list
        hazards_text = "None"
        if result.detected_hazards:
            hazards_text = ", ".join(result.detected_hazards)

        # Confidence as percentage
        confidence_pct = f"{result.confidence * 100:.0f}%"

        # Escape special markdown characters for MarkdownV2
        description = self._escape_md(result.description)
        hazards_text = self._escape_md(hazards_text)

        message = (
            f"{emoji} *SAFETY ALERT — Aerial Studio CCTV*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷️ Risk Level: *{label}*\n"
            f"📋 {description}\n"
            f"🔍 Hazards: {hazards_text}\n"
            f"🎯 Confidence: {confidence_pct}\n"
        )

        # Add temporal analysis info if multi-frame
        if result.analysis_mode == "multi_frame":
            motion_icon = "✅" if result.motion_detected else "🔴"
            motion_text = "Detected" if result.motion_detected else "NO MOTION"
            message += f"━━━━━━━━━━━━━━━━━━━━\n"
            message += f"🎬 Frames Analyzed: {result.frames_analyzed}\n"
            message += f"{motion_icon} Motion: {motion_text}\n"
            if result.stillness_warning:
                message += f"🔴 *STILLNESS WARNING*\n"
            if result.temporal_description:
                temporal = self._escape_md(result.temporal_description)
                message += f"🔄 {temporal}\n"

        # Dynamic footer based on risk
        footer = "_Please check the studio immediately\\!_"
        if result.risk_level == "safe":
            footer = "✅ _No action required\\. Scene is safe\\._"

        message += (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Time: {self._escape_md(now)}\n"
            f"{footer}"
        )

        return message

    def _send_photo(self, image_path: str, caption: str) -> bool:
        """Send a photo with caption to the Telegram group."""
        url = f"{self.api_url}/sendPhoto"

        try:
            with open(image_path, "rb") as photo:
                payload = {
                    "chat_id": self.chat_id,
                    "caption": caption,
                    "parse_mode": "MarkdownV2",
                }
                files = {"photo": photo}

                resp = requests.post(url, data=payload, files=files, timeout=30)

            if resp.status_code == 200 and resp.json().get("ok"):
                return True

            log.error(f"Telegram sendPhoto failed: {resp.status_code} — {resp.text}")
            return False

        except Exception as e:
            log.error(f"sendPhoto error: {e}")
            return False

    def _send_text(self, text: str) -> bool:
        """Send a text-only message to the Telegram group."""
        url = f"{self.api_url}/sendMessage"

        try:
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "MarkdownV2",
            }

            resp = requests.post(url, json=payload, timeout=30)

            if resp.status_code == 200 and resp.json().get("ok"):
                return True

            log.error(f"Telegram sendMessage failed: {resp.status_code} — {resp.text}")
            return False

        except Exception as e:
            log.error(f"sendMessage error: {e}")
            return False

    @staticmethod
    def _escape_md(text: str) -> str:
        """Escape special characters for Telegram MarkdownV2."""
        special_chars = r"_*[]()~`>#+-=|{}.!"
        escaped = ""
        for char in text:
            if char in special_chars:
                escaped += f"\\{char}"
            else:
                escaped += char
        return escaped
