#!/usr/bin/env python3
"""
identify_cameras.py — Fetch the latest event thumbnail from each configured
camera and send to Telegram with the camera DID, so you can visually match
DID → physical camera location.

Usage:
    .venv/bin/python3 identify_cameras.py
"""

import os
import sys
import datetime
import tempfile
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))

from config.settings import settings
from core.notifier import TelegramNotifier
from utils.logger import setup_logger, get_logger
from xiaomi_capture import (
    get_session_state, _camera_api, _make_http_session, download_thumbnail,
)

LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=8))  # MYT


def get_latest_event(state: dict, cam: dict) -> Optional[dict]:
    """Fetch the single most recent event for a camera (last 7 days)."""
    now_ms   = int(datetime.datetime.now(LOCAL_TZ).timestamp() * 1000)
    start_ms = now_ms - 7 * 24 * 60 * 60 * 1000  # last 7 days

    host = settings.STUDIO_CAMERA_HOST
    try:
        resp = _camera_api(state, host, "common/app/get/eventlist", {
            "did":       cam["did"],
            "model":     cam["model"],
            "doorBell":  0,
            "eventType": "Default",
            "needMerge": True,
            "sortType":  "DESC",
            "region":    "CN",
            "language":  "en_US",
            "beginTime": start_ms,
            "endTime":   now_ms,
            "limit":     1,
        })
        events = (resp.get("data") or {}).get("thirdPartPlayUnits") or []
        return events[0] if events else None
    except Exception as e:
        get_logger().warning(f"Event fetch failed for {cam['did']}: {e}")
        return None


def main():
    setup_logger(level=settings.LOG_LEVEL, log_dir=settings.LOGS_DIR)
    log      = get_logger()
    notifier = TelegramNotifier(settings)

    log.info("Loading Xiaomi session...")
    state = get_session_state()
    if not state:
        log.error("Could not load Xiaomi session — run migrate_session_to_gcs.py first")
        sys.exit(1)

    cameras = settings.STUDIO_CAMERAS
    log.info(f"Found {len(cameras)} camera(s) in config")
    notifier.send_text(f"📷 Identifying {len(cameras)} camera(s)... fetching latest thumbnails")

    http = _make_http_session(state)

    for cam in cameras:
        did  = cam["did"]
        name = cam.get("name", did)
        log.info(f"\nCamera: {name} | DID: {did}")

        event = get_latest_event(state, cam)
        if not event:
            log.warning(f"  No recent events found for {did}")
            notifier.send_text(
                f"📷 Camera: {name}\n"
                f"🔑 DID: {did}\n"
                f"⚠️ No events in last 24h — could not fetch snapshot"
            )
            continue

        # Download thumbnail
        with tempfile.TemporaryDirectory() as tmp:
            thumb_path = os.path.join(tmp, "thumb.jpg")
            saved = download_thumbnail(
                state, event, thumb_path,
                did=did, model=cam["model"],
            )

            if not saved:
                log.warning(f"  Thumbnail download failed for {did}")
                notifier.send_text(
                    f"📷 Camera: {name}\n"
                    f"🔑 DID: {did}\n"
                    f"⚠️ Thumbnail download failed"
                )
                continue

            event_time = datetime.datetime.fromtimestamp(
                event.get("createTime", 0) / 1000, tz=LOCAL_TZ
            ).strftime("%Y-%m-%d %H:%M:%S MYT")

            caption = (
                f"📷 Camera: {name}\n"
                f"🔑 DID: `{did}`\n"
                f"🕐 Last event: {event_time}\n"
                f"👉 Reply here with which camera this is"
            )

            # Send photo via raw Telegram (notifier send_text won't send photos)
            import requests as req
            url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendPhoto"
            with open(saved, "rb") as photo:
                resp = req.post(url, data={
                    "chat_id":    settings.TELEGRAM_CHAT_ID,
                    "caption":    caption,
                    "parse_mode": "Markdown",
                }, files={"photo": photo}, timeout=30)

            if resp.status_code == 200:
                log.info(f"  ✅ Sent thumbnail for {name} (DID: {did})")
            else:
                log.error(f"  ❌ Telegram failed: {resp.text}")

    notifier.send_text("✅ Done — reply with which DID matches which camera location")
    log.info("Done.")


if __name__ == "__main__":
    main()
