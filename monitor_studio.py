#!/usr/bin/env python3
"""
Smart Vision Alert — Studio Monitor Daemon
============================================
Production entry point. Designed to run every 2 minutes via cPanel cron.

Pipeline:
  1. Load Xiaomi Cloud session for studio cameras
  2. Query recent events (last 10 min lookback)
  3. Skip already-processed events (via MySQL)
  4. Extract frames from new events
  5. Run Gemini AI safety analysis
  6. Send Telegram alert if risk >= threshold

Usage:
    python3 monitor_studio.py               # Normal monitoring run
    python3 monitor_studio.py --init-db     # Create MySQL tables
    python3 monitor_studio.py --dry-run     # Test pipeline without AI/Telegram
    python3 monitor_studio.py --test-one    # Process only the first new event
    python3 monitor_studio.py --status      # Show today's stats
"""

from __future__ import annotations

import os
import sys
import json
import time
import glob
import fcntl
import pickle
import argparse
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config.settings import settings
from core.database import EventDB
from core.analyzer import SafetyAnalyzer
from core.notifier import TelegramNotifier
from core.models import AnalysisResult
from utils.logger import setup_logger, get_logger

# Import Xiaomi capture utilities from xiaomi_capture
from xiaomi_capture import (
    _camera_api, _make_http_session, _camera_api_url,
    _silent_refresh, _save_session, download_thumbnail,
    extract_segment, get_video_duration_from_url, SEGMENT_SECS,
)

# ── constants ─────────────────────────────────────────────────────────────────

LOCAL_TZ         = ZoneInfo("Asia/Kuala_Lumpur")
EVENT_LOOKBACK   = 600        # 10-minute lookback window (seconds)
LOCK_FILE        = "/tmp/.sva_monitor.lock"
CAPTURES_BASE    = "captures/studio"

# ── helpers ───────────────────────────────────────────────────────────────────

def ms_to_local(ms: int) -> str:
    """Convert milliseconds timestamp to local time string."""
    if not ms:
        return "N/A"
    return datetime.datetime.fromtimestamp(
        ms / 1000, tz=LOCAL_TZ
    ).strftime("%Y-%m-%d %H:%M:%S %Z")


def ms_to_datetime(ms: int) -> datetime.datetime:
    """Convert milliseconds timestamp to datetime object."""
    return datetime.datetime.fromtimestamp(ms / 1000, tz=LOCAL_TZ)


def load_studio_session() -> dict | None:
    """Load the studio camera session file."""
    session_file = settings.STUDIO_SESSION_FILE
    if not os.path.exists(session_file):
        return None
    try:
        with open(session_file, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def save_studio_session(state: dict):
    """Save refreshed session back to file."""
    with open(settings.STUDIO_SESSION_FILE, "wb") as f:
        pickle.dump(state, f)


def acquire_lock() -> int | None:
    """Acquire a file lock to prevent concurrent runs. Returns fd or None."""
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, IOError):
        return None


def release_lock(fd: int):
    """Release file lock."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        os.unlink(LOCK_FILE)
    except Exception:
        pass


# ── event fetching ────────────────────────────────────────────────────────────

def get_events(state: dict, cam: dict, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch events for a single camera with pagination."""
    all_events, seen, cursor_end = [], set(), end_ms
    host = settings.STUDIO_CAMERA_HOST

    while True:
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
            "endTime":   cursor_end,
            "limit":     50,
        })
        units = (resp.get("data") or {}).get("thirdPartPlayUnits") or []
        for ev in units:
            fid = ev.get("fileId")
            if fid and fid not in seen:
                seen.add(fid)
                all_events.append(ev)

        data = resp.get("data") or {}
        if not data.get("isContinue") or not data.get("nextTime"):
            break
        next_time = data["nextTime"]
        if next_time >= cursor_end or next_time == 0:
            break
        cursor_end = next_time

    return all_events


# ── frame capture ─────────────────────────────────────────────────────────────

def get_m3u8_url(state: dict, cam: dict, event: dict) -> str:
    """Get the M3U8 video URL for an event."""
    return _camera_api_url(state, settings.STUDIO_CAMERA_HOST, "common/app/m3u8", {
        "did":        cam["did"],
        "model":      cam["model"],
        "fileId":     event["fileId"],
        "isAlarm":    event.get("isAlarm", False),
        "videoCodec": "H264",
        "region":     "CN",
    })


def capture_event_frames(state: dict, cam: dict, event: dict) -> dict:
    """
    Extract frames from an event for AI analysis.

    Strategy (shared hosting):
      1. PRIMARY — download event thumbnail via Xiaomi processor API (always works).
      2. SECONDARY — attempt cv2 video frame extraction (silently fails on most
         shared hosts due to swscaler resource restrictions; kept for future use).

    Returns: {fileId, eventType, time, duration, segments: [...], frames, dir}
    """
    log    = get_logger()
    fid    = event["fileId"]
    ev_dir = os.path.join(CAPTURES_BASE, cam["did"], fid)
    os.makedirs(ev_dir, exist_ok=True)

    segments_captured = []

    # ── 1. Thumbnail (primary, always reliable) ───────────────────────────────
    first_dir  = os.path.join(ev_dir, "first")
    os.makedirs(first_dir, exist_ok=True)
    thumb_path = os.path.join(first_dir, "thumb_00.jpg")
    saved = download_thumbnail(state, event, thumb_path,
                               did=cam["did"], model=cam["model"])
    if saved:
        segments_captured.append({"label": "first", "frames": [saved]})
        log.info(f"Thumbnail captured: {os.path.basename(saved)}")
    else:
        log.warning(f"Thumbnail download failed for event {fid}")

    # ── 2. Video frame extraction (best-effort; fails on swscaler-restricted hosts) ──
    try:
        http     = _make_http_session(state)
        m3u8_url = get_m3u8_url(state, cam, event)
        duration = get_video_duration_from_url(m3u8_url, auth_session=http) or 0

        # First 5 seconds
        frames = extract_segment(m3u8_url, ev_dir, "first_vid", start_sec=0.0, auth_session=http)
        if frames:
            segments_captured.append({"label": "first_vid", "frames": frames})

        # Last 5 seconds (if long enough)
        if duration > SEGMENT_SECS * 2:
            last_start = max(0.0, duration - SEGMENT_SECS)
            frames = extract_segment(m3u8_url, ev_dir, "last", start_sec=last_start, auth_session=http)
            if frames:
                segments_captured.append({"label": "last", "frames": frames})

        # Every 60s interval (if > 1 minute)
        if duration > 60:
            mark = 60.0
            while mark < duration - SEGMENT_SECS:
                lbl = f"t{int(mark):04d}s"
                frames = extract_segment(m3u8_url, ev_dir, lbl, start_sec=mark, auth_session=http)
                if frames:
                    segments_captured.append({"label": lbl, "frames": frames})
                mark += 60.0

    except Exception as e:
        log.debug(f"Video extraction skipped: {e}")

    total = sum(len(s["frames"]) for s in segments_captured)
    return {
        "fileId":    fid,
        "eventType": event.get("eventType"),
        "time":      ms_to_local(event.get("createTime", 0)),
        "duration":  duration,
        "segments":  segments_captured,
        "frames":    total,
        "dir":       ev_dir,
    }


# ── main pipeline ─────────────────────────────────────────────────────────────

def run_monitor(dry_run: bool = False, test_one: bool = False):
    """Main monitoring pipeline — called by cron every 2 minutes."""
    log = get_logger()

    # ── initialize services ───────────────────────────────────────────────
    db = EventDB(settings)
    analyzer = None if dry_run else SafetyAnalyzer(settings)
    notifier = None if dry_run else TelegramNotifier(settings)

    manual_check = getattr(settings, 'MANUAL_CHECK_FLAG', False)

    # Helper for system errors
    def notify_system_error(err_type: str, err_did: str, message: str, exc: Exception = None):
        if notifier and not db.is_in_cooldown(err_did, 60):
            err_msg = f"❌ **{err_type}**\n{message}"
            if exc:
                err_msg += f"\n`{str(exc)[:150]}`"
            notifier.send_text(err_msg)
            
            err_res = AnalysisResult(
                is_safe=False, risk_level="system_error", description=message[:200],
                detected_hazards=[], confidence=0.0, motion_detected=False,
                stillness_warning=False, temporal_description="",
                raw_response="", analysis_mode="system"
            )
            db.save_alert(f"ERR_{int(time.time())}", err_did, err_res, True)

    # ── load session ──────────────────────────────────────────────────────
    try:
        state = load_studio_session()
        if not state:
            log.error(f"Session file not found: {settings.STUDIO_SESSION_FILE}")
            notify_system_error("System Error", "SYSTEM_XIAOMI", "Xiaomi Cloud session not found. Please re-login via QR.")
            return

        # Silent refresh
        refreshed = _silent_refresh(state)
        if refreshed:
            save_studio_session(refreshed)
            state = refreshed
            log.info("Session token refreshed")
    except Exception as e:
        log.error(f"Failed to load or refresh session: {e}", exc_info=True)
        notify_system_error("System Error", "SYSTEM_XIAOMI", "Failed to connect to Xiaomi Cloud. Token might be expired.", e)
        return

    cameras = settings.STUDIO_CAMERAS
    if not cameras:
        log.error("No studio cameras configured. Set STUDIO_CAMERAS in .env")
        return

    # ── time window ───────────────────────────────────────────────────────
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - (EVENT_LOOKBACK * 1000)

    total_new  = 0
    total_skip = 0

    for cam in cameras:
        cam_name = cam.get("name", cam["did"])
        log.info(f"── {cam_name} (did={cam['did']}) ──")

        # ── fetch events ──────────────────────────────────────────────
        try:
            events = get_events(state, cam, start_ms, now_ms)
            # Sort events by createTime descending so the newest is first
            events.sort(key=lambda x: x.get("createTime", 0), reverse=True)
        except Exception as e:
            log.error(f"Failed to fetch events for {cam_name}: {e}")
            notify_system_error("Camera Error", cam["did"], f"Failed to fetch events for {cam_name}. Connection issue with Xiaomi Cloud.", e)
            continue

        if not events:
            log.info(f"   No events in last {EVENT_LOOKBACK // 60} min")
            if manual_check and notifier:
                notifier.send_text(f"✅ **{cam_name}**: No motion detected in the last {EVENT_LOOKBACK // 60} minutes. Studio is quiet.")
            continue

        log.info(f"   Found {len(events)} event(s)")
        latest_fid = events[0].get("fileId") if events else None

        for ev in events:
            fid = ev.get("fileId")
            if not fid:
                continue

            is_latest = (fid == latest_fid)

            # ── skip already processed ────────────────────────────────
            if db.is_processed(fid):
                if manual_check and is_latest and notifier:
                    log.info(f"   [Manual Check] Sending cached result for {fid}")
                    cached_result = db.get_analysis(fid)
                    capture_dir = db.get_capture_dir(fid)
                    if cached_result and capture_dir:
                        # Find the first image in the dir
                        img_path = None
                        first_dir = os.path.join(capture_dir, "first")
                        if os.path.exists(first_dir):
                            images = sorted(glob.glob(os.path.join(first_dir, "*.jpg")))
                            if images:
                                img_path = images[0]
                        
                        if img_path:
                            cached_result.description = f"[Manual Check] {cached_result.description}"
                            notifier.send_alert(cached_result, img_path)
                
                total_skip += 1
                continue

            total_new += 1
            ts = ms_to_local(ev.get("createTime", 0))
            log.info(f"   NEW: {ev.get('eventType', '?')} @ {ts}  fid={fid}")

            # ── capture frames ────────────────────────────────────────
            try:
                capture = capture_event_frames(state, cam, ev)
                log.info(
                    f"   Captured: {capture['frames']} frames, "
                    f"{capture['duration']:.1f}s, dir={capture['dir']}"
                )
            except Exception as e:
                log.error(f"   Frame capture failed for {fid}: {e}")
                continue

            # ── mark as processed in DB ───────────────────────────────
            event_dt = ms_to_datetime(ev.get("createTime", 0))
            db.mark_processed(
                file_id=fid,
                camera_did=cam["did"],
                camera_name=cam_name,
                event_type=ev.get("eventType", ""),
                event_time=event_dt,
                duration_sec=capture["duration"],
                frames_saved=capture["frames"],
                capture_dir=capture["dir"],
            )

            # ── AI analysis (skip in dry-run) ─────────────────────────
            if dry_run:
                log.info("   [dry-run] Skipping AI analysis")
                if test_one:
                    break
                continue

            # Find frames for AI analysis (prioritize 'last' for manual check, else 'first')
            analyze_seg = None
            if manual_check:
                analyze_seg = next((s for s in capture["segments"] if s["label"] == "last"), None)
            
            # Fallback to 'first' if 'last' isn't available or not a manual check
            if not analyze_seg:
                analyze_seg = next((s for s in capture["segments"] if s["label"] == "first"), None)
                
            if not analyze_seg or not analyze_seg["frames"]:
                log.warning(f"   No frames to analyze for {fid}")
                continue

            try:
                result = analyzer.analyze_multi_frame(analyze_seg["frames"])
                db.save_analysis(fid, cam["did"], result, segment_label=analyze_seg["label"])

                log.info(
                    f"   AI: {'✅ SAFE' if result.is_safe else '⚠️ UNSAFE'} "
                    f"| risk={result.risk_level} | confidence={result.confidence:.0%}"
                )

                if result.temporal_description:
                    log.info(f"   Motion: {result.temporal_description}")

            except Exception as e:
                log.error(f"   AI analysis failed for {fid}: {e}")
                notify_system_error("AI Error", "SYSTEM_GEMINI", f"Gemini AI analysis failed for {cam_name}.", e)
                continue

            # ── alert if needed ───────────────────────────────────────
            force_alert = (manual_check and is_latest)
            if force_alert or settings.risk_exceeds_threshold(result.risk_level):
                if not force_alert and db.is_in_cooldown(cam["did"], settings.ALERT_COOLDOWN_MINUTES):
                    log.info(
                        f"   Alert suppressed (cooldown {settings.ALERT_COOLDOWN_MINUTES}min)"
                    )
                else:
                    # Pick the best image to send
                    alert_image = analyze_seg["frames"][0]
                    log.warning(
                        f"   🚨 ALERTING: {result.risk_level} — {result.description}"
                    )
                    try:
                        telegram_ok = notifier.send_alert(result, alert_image)
                        db.save_alert(fid, cam["did"], result, telegram_ok)
                    except Exception as e:
                        log.error(f"   Telegram alert failed: {e}")

            if test_one:
                break

        if test_one and total_new > 0:
            break

    # ── summary ───────────────────────────────────────────────────────────
    stats = db.get_today_stats()
    log.info(
        f"Run complete: {total_new} new, {total_skip} skipped | "
        f"Today: {stats['events_processed']} events, "
        f"{stats['ai_analyses']} analyses, {stats['alerts_sent']} alerts"
    )
    db.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Smart Vision Alert — Studio Monitor")
    parser.add_argument("--init-db",  action="store_true", help="Create MySQL tables")
    parser.add_argument("--dry-run",  action="store_true", help="Test pipeline without AI/Telegram")
    parser.add_argument("--test-one", action="store_true", help="Process only the first new event")
    parser.add_argument("--status",   action="store_true", help="Show today's stats")
    parser.add_argument("--force",    action="store_true", help="Run even outside studio hours")
    parser.add_argument("--manual-check", action="store_true", help="Force a manual check (triggered via webhook)")
    args = parser.parse_args()

    setup_logger(level=settings.LOG_LEVEL, log_dir=settings.LOGS_DIR)
    log = get_logger()

    if args.manual_check:
        settings.MANUAL_CHECK_FLAG = True

    # ── init-db ───────────────────────────────────────────────────────────
    if args.init_db:
        log.info("Initializing database tables...")
        db = EventDB(settings)
        db.init_tables()
        stats = db.get_today_stats()
        log.info(f"Database ready. Today's stats: {json.dumps(stats)}")
        db.close()
        return

    # ── status ────────────────────────────────────────────────────────────
    if args.status:
        db = EventDB(settings)
        stats = db.get_today_stats()
        print(f"📊 Smart Vision Alert — Status")
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"  Date:             {stats['date']}")
        print(f"  Events Processed: {stats['events_processed']}")
        print(f"  AI Analyses:      {stats['ai_analyses']}")
        print(f"  Alerts Sent:      {stats['alerts_sent']}")
        print(f"  Model:            {settings.GEMINI_MODEL}")
        print(f"  Studio Hours:     {settings.STUDIO_HOURS_START}:00 – {settings.STUDIO_HOURS_END}:00")
        print(f"  Cameras:          {len(settings.STUDIO_CAMERAS)}")
        for cam in settings.STUDIO_CAMERAS:
            print(f"    • {cam.get('name', '?')} (did={cam.get('did', '?')})")
        db.close()
        return

    # ── prevent concurrent runs ───────────────────────────────────────────
    lock_fd = acquire_lock()
    if lock_fd is None:
        log.warning("Another monitor instance is already running. Exiting.")
        return

    try:
        run_monitor(dry_run=args.dry_run, test_one=args.test_one)

    except Exception as e:
        log.error(f"Monitor crashed: {e}", exc_info=True)
    finally:
        release_lock(lock_fd)


if __name__ == "__main__":
    main()
