#!/usr/bin/env python3
"""
Smart Vision Alert — Cron Trigger (A2 Hosting)
===============================================
Runs every 2 minutes via cPanel cron. Delegates the full monitoring
pipeline to Google Cloud Run via HTTP POST.

Usage:
    python3 monitor_studio.py               # Normal cron trigger
    python3 monitor_studio.py --manual-check # Trigger with manual_check=true
    python3 monitor_studio.py --init-db     # Create MySQL tables (local)
    python3 monitor_studio.py --status      # Show today's stats (local)
"""

from __future__ import annotations

import os
import sys
import json
import fcntl
import argparse
import requests

from config.settings import settings
from utils.logger import setup_logger, get_logger

LOCK_FILE = "/tmp/.sva_monitor.lock"


def acquire_lock() -> int | None:
    """Acquire a file lock to prevent concurrent cron runs."""
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, IOError):
        return None


def release_lock(fd: int):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        os.unlink(LOCK_FILE)
    except Exception:
        pass


def trigger_cloud_run(manual_check: bool = False) -> dict:
    """POST to Cloud Run /run endpoint and return the response dict."""
    url     = f"{settings.CLOUD_RUN_URL}/run"
    headers = {"X-Secret-Token": settings.CLOUD_RUN_SECRET}
    body    = {"manual_check": manual_check}
    resp    = requests.post(url, json=body, headers=headers, timeout=300)
    resp.raise_for_status()
    return resp.json()


def main():
    parser = argparse.ArgumentParser(description="Smart Vision Alert — Cron Trigger")
    parser.add_argument("--manual-check", action="store_true",
                        help="Trigger Cloud Run with manual_check=true")
    parser.add_argument("--init-db",  action="store_true",
                        help="Create MySQL tables (local DB, no Cloud Run)")
    parser.add_argument("--status",   action="store_true",
                        help="Show today's stats from local MySQL")
    args = parser.parse_args()

    setup_logger(level=settings.LOG_LEVEL, log_dir=settings.LOGS_DIR)
    log = get_logger()

    # ── local-only commands ───────────────────────────────────────────────
    if args.init_db:
        from core.database import EventDB
        db = EventDB(settings)
        db.init_tables()
        db.close()
        log.info("Database tables initialized")
        return

    if args.status:
        from core.database import EventDB
        db = EventDB(settings)
        stats = db.get_today_stats()
        db.close()
        print(json.dumps(stats, indent=2))
        return

    # ── Cloud Run trigger ─────────────────────────────────────────────────
    lock_fd = acquire_lock()
    if lock_fd is None:
        log.warning("Another trigger is already running — exiting.")
        return

    try:
        log.info(f"Triggering Cloud Run (manual_check={args.manual_check})...")
        result = trigger_cloud_run(manual_check=args.manual_check)
        log.info(f"Cloud Run response: {json.dumps(result)}")
    except Exception as e:
        log.error(f"Failed to trigger Cloud Run: {e}")
    finally:
        release_lock(lock_fd)


if __name__ == "__main__":
    main()
