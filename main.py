#!/usr/bin/env python3
"""
Smart Vision Alert — Main Pipeline Orchestrator
═══════════════════════════════════════════════

Aerial Dance Studio CCTV Safety Monitoring System

Pipeline: Capture Image(s) → Gemini AI Analysis (with temporal comparison) → Telegram Alert

Usage:
    python main.py              # Run one safety check cycle (multi-frame if enabled)
    python main.py --test       # Send a test Telegram message
    python main.py --analyze    # Analyze only (no alert)
    python main.py --burst      # Force burst capture mode (multiple sequential frames)
    python main.py --status     # Check system status
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta

from config.settings import settings
from core.camera import CameraCapture
from core.analyzer import SafetyAnalyzer
from core.notifier import TelegramNotifier
from core.models import AnalysisResult
from utils.logger import setup_logger, get_logger
from utils.image_utils import cleanup_old_captures


# ── Alert Cooldown Tracking ──────────────────────────────────
COOLDOWN_FILE = settings.LOGS_DIR / ".last_alert.json"


def is_in_cooldown(cooldown_minutes: int) -> bool:
    """Check if we're still within the alert cooldown period."""
    if not COOLDOWN_FILE.exists():
        return False

    try:
        with open(COOLDOWN_FILE) as f:
            data = json.load(f)

        last_alert_time = datetime.fromisoformat(data.get("timestamp", ""))
        elapsed = datetime.now() - last_alert_time

        if elapsed < timedelta(minutes=cooldown_minutes):
            remaining = timedelta(minutes=cooldown_minutes) - elapsed
            get_logger().info(
                f"Alert cooldown active: {remaining.seconds // 60}m {remaining.seconds % 60}s remaining"
            )
            return True

    except (json.JSONDecodeError, ValueError, KeyError):
        pass

    return False


def record_alert(result: AnalysisResult):
    """Record the timestamp of the last sent alert."""
    settings.LOGS_DIR.mkdir(parents=True, exist_ok=True)

    data = {
        "timestamp": datetime.now().isoformat(),
        "risk_level": result.risk_level,
        "description": result.description,
        "analysis_mode": result.analysis_mode,
        "frames_analyzed": result.frames_analyzed,
        "stillness_warning": result.stillness_warning,
    }

    with open(COOLDOWN_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Main Pipeline ────────────────────────────────────────────
def run_safety_check(force_burst: bool = False):
    """
    Execute one full safety check cycle:
    1. Capture latest image(s) from camera
    2. Analyze with Gemini AI for safety hazards (with temporal comparison)
    3. Send Telegram alert if unsafe (respecting cooldown)
    4. Save frame to history for future comparisons
    5. Cleanup old captures

    Args:
        force_burst: If True, force burst capture mode regardless of settings.
    """
    log = get_logger()
    log.info("=" * 60)
    log.info("🎯 Smart Vision Alert — Safety Check Started")
    log.info("=" * 60)

    # ── Step 0: Validate configuration ──
    errors = settings.validate()
    if errors:
        for err in errors:
            log.error(f"Config error: {err}")
        log.error("Fix configuration in config/.env and try again.")
        return False

    settings.ensure_dirs()

    camera = CameraCapture(settings)
    analyzer = SafetyAnalyzer(settings)

    use_multi_frame = force_burst or settings.MULTI_FRAME_ENABLED

    if use_multi_frame:
        result, latest_capture = _run_multi_frame_check(camera, analyzer, force_burst)
    else:
        result, latest_capture = _run_single_frame_check(camera, analyzer)

    if result is None:
        log.error("❌ Analysis failed — aborting")
        return False

    log.info(f"📊 Analysis result: safe={result.is_safe}, risk={result.risk_level}")
    log.info(f"📝 Description: {result.description}")

    if result.analysis_mode == "multi_frame":
        log.info(
            f"🔄 Temporal: motion={'yes' if result.motion_detected else '⚠️ NONE'}, "
            f"scene_change={result.scene_change_level}, "
            f"stillness={'⚠️ YES' if result.stillness_warning else 'no'}"
        )

    # ── Step 3: Alert if unsafe ──
    if not result.is_safe and settings.risk_exceeds_threshold(result.risk_level):
        log.warning(f"🚨 UNSAFE DETECTED — Risk: {result.risk_level}")

        if result.stillness_warning:
            log.warning("🔴 STILLNESS DETECTED — Person may be unable to move!")

        if is_in_cooldown(settings.ALERT_COOLDOWN_MINUTES):
            log.info("⏳ Alert suppressed (cooldown active)")
        else:
            log.info("📱 Step 3: Sending Telegram alert...")
            notifier = TelegramNotifier(settings)
            image_for_alert = latest_capture.file_path if latest_capture else None
            sent = notifier.send_alert(result, image_for_alert)

            if sent:
                record_alert(result)
                log.info("✅ Alert sent to Telegram group")
            else:
                log.error("❌ Failed to send Telegram alert")
    else:
        log.info("✅ Studio appears safe — no alert needed")

    # ── Step 4: Save to history & cleanup ──
    if latest_capture:
        camera.save_to_history(latest_capture)
    camera.cleanup_history(max_files=10)
    cleanup_old_captures(settings.CAPTURES_DIR, settings.CAPTURE_RETENTION_DAYS)

    log.info("🏁 Safety check complete")
    log.info("=" * 60)
    return True


def _run_single_frame_check(camera, analyzer):
    """Run a single-frame safety check, comparing with previous if available."""
    log = get_logger()
    log.info("📷 Step 1: Capturing single image...")

    capture = camera.capture_latest()
    if capture is None:
        log.error("❌ Failed to capture image")
        return None, None

    log.info(f"✅ Image captured: {capture.file_path} ({capture.file_size_kb:.1f} KB)")

    # Check if we have a previous capture for comparison
    previous = camera.get_previous_capture()

    log.info("🤖 Step 2: Analyzing with Gemini AI...")
    if previous:
        log.info(f"📊 Comparing with previous capture: {Path(previous).name}")
        result = analyzer.analyze_with_previous(capture.file_path, previous)
    else:
        log.info("📊 No previous capture — single-frame analysis")
        result = analyzer.analyze(capture.file_path)

    return result, capture


def _run_multi_frame_check(camera, analyzer, force_burst: bool):
    """
    Run multi-frame safety check.

    Strategy:
    - If burst mode: capture N frames sequentially (with delay between each)
    - Otherwise: use current capture + historical frames from previous runs
    """
    log = get_logger()
    frame_count = settings.MULTI_FRAME_COUNT
    interval = settings.MULTI_FRAME_INTERVAL_SECONDS

    if force_burst or settings.CAMERA_SOURCE.lower() != "local_folder":
        # ── Burst Mode: capture N frames now with delays ──
        log.info(f"📷 Step 1: Burst capture ({frame_count} frames, {interval}s apart)...")

        captures = camera.capture_burst(
            count=frame_count,
            interval_seconds=interval,
        )

        if not captures:
            log.error("❌ Burst capture failed — no frames captured")
            return None, None

        image_paths = [c.file_path for c in captures]
        latest_capture = captures[-1]

        log.info(f"✅ Burst captured {len(captures)} frame(s)")

    else:
        # ── History Mode: current frame + previous history ──
        log.info("📷 Step 1: Capturing current image + loading history...")

        capture = camera.capture_latest()
        if capture is None:
            log.error("❌ Failed to capture image")
            return None, None

        latest_capture = capture

        # Get historical frames
        history = camera.get_history_frames(count=frame_count - 1)
        image_paths = history + [capture.file_path]

        log.info(
            f"✅ Current frame + {len(history)} historical frame(s) "
            f"= {len(image_paths)} total"
        )

    # ── Analyze ──
    log.info(f"🤖 Step 2: Multi-frame analysis ({len(image_paths)} frames)...")
    result = analyzer.analyze_multi_frame(image_paths)

    return result, latest_capture


# ── CLI Commands ─────────────────────────────────────────────
def cmd_test():
    """Send a test message to Telegram to verify configuration."""
    log = get_logger()
    log.info("Sending test message to Telegram...")

    errors = settings.validate()
    if errors:
        for err in errors:
            log.error(f"Config error: {err}")
        return False

    notifier = TelegramNotifier(settings)
    if notifier.send_test_message():
        log.info("✅ Test message sent! Check your Telegram group.")
        return True
    else:
        log.error("❌ Failed to send test message. Check bot token and chat ID.")
        return False


def cmd_analyze(use_burst: bool = False):
    """Analyze the latest image(s) without sending an alert."""
    log = get_logger()
    settings.ensure_dirs()

    camera = CameraCapture(settings)
    analyzer = SafetyAnalyzer(settings)

    if use_burst or settings.MULTI_FRAME_ENABLED:
        # Multi-frame analysis
        if use_burst:
            captures = camera.capture_burst(
                count=settings.MULTI_FRAME_COUNT,
                interval_seconds=settings.MULTI_FRAME_INTERVAL_SECONDS,
            )
            if not captures:
                log.error("No images captured in burst mode")
                return False
            image_paths = [c.file_path for c in captures]
        else:
            capture = camera.capture_latest()
            if capture is None:
                log.error("No image to analyze")
                return False
            history = camera.get_history_frames(count=settings.MULTI_FRAME_COUNT - 1)
            image_paths = history + [capture.file_path]

        result = analyzer.analyze_multi_frame(image_paths)
    else:
        capture = camera.capture_latest()
        if capture is None:
            log.error("No image to analyze")
            return False
        result = analyzer.analyze(capture.file_path)

    print("\n" + "=" * 55)
    print("  ANALYSIS RESULT")
    print("=" * 55)
    print(f"  Mode:       {result.analysis_mode} ({result.frames_analyzed} frame(s))")
    print(f"  Safe:       {result.is_safe}")
    print(f"  Risk Level: {result.risk_level}")
    print(f"  Confidence: {result.confidence:.0%}")
    print(f"  Description: {result.description}")
    print(f"  Hazards:    {result.detected_hazards}")

    if result.analysis_mode == "multi_frame":
        print("-" * 55)
        print("  TEMPORAL ANALYSIS")
        print("-" * 55)
        print(f"  Motion Detected:    {'✅ Yes' if result.motion_detected else '⚠️  NO MOTION'}")
        print(f"  Scene Change:       {result.scene_change_level}")
        print(f"  Stillness Warning:  {'🔴 YES' if result.stillness_warning else '✅ No'}")
        if result.temporal_description:
            print(f"  Changes:            {result.temporal_description}")

    print("=" * 55 + "\n")

    # Save to history for next comparison
    if not use_burst:
        capture = camera.capture_latest()
        if capture:
            camera.save_to_history(capture)

    return True


def cmd_status():
    """Show system status and configuration."""
    print("\n" + "=" * 55)
    print("  SMART VISION ALERT — System Status")
    print("=" * 55)
    print(f"  Camera Source:    {settings.CAMERA_SOURCE}")
    print(f"  Gemini API Key:   {'✅ Set' if settings.GEMINI_API_KEY else '❌ Missing'}")
    print(f"  Telegram Token:   {'✅ Set' if settings.TELEGRAM_BOT_TOKEN else '❌ Missing'}")
    print(f"  Telegram Chat:    {'✅ Set' if settings.TELEGRAM_CHAT_ID else '❌ Missing'}")
    print(f"  Alert Threshold:  {settings.ALERT_THRESHOLD}")
    print(f"  Cooldown:         {settings.ALERT_COOLDOWN_MINUTES} min")

    # Multi-frame settings
    print("-" * 55)
    print("  MULTI-FRAME ANALYSIS")
    print("-" * 55)
    print(f"  Enabled:          {'✅ Yes' if settings.MULTI_FRAME_ENABLED else '❌ No'}")
    print(f"  Frame Count:      {settings.MULTI_FRAME_COUNT}")
    print(f"  Frame Interval:   {settings.MULTI_FRAME_INTERVAL_SECONDS}s")

    # Check history
    history_dir = settings.HISTORY_DIR
    if history_dir.exists():
        history_files = [
            f for f in history_dir.iterdir()
            if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png")
        ]
        print(f"  History Frames:   {len(history_files)}")
    else:
        print(f"  History Frames:   0")

    # Check cooldown
    print("-" * 55)
    if COOLDOWN_FILE.exists():
        try:
            with open(COOLDOWN_FILE) as f:
                data = json.load(f)
            last = data.get("timestamp", "unknown")
            mode = data.get("analysis_mode", "unknown")
            stillness = data.get("stillness_warning", False)
            print(f"  Last Alert:       {last}")
            print(f"  Alert Mode:       {mode}")
            if stillness:
                print(f"  Stillness:        ⚠️ Yes (was detected)")
        except Exception:
            pass
    else:
        print("  Last Alert:       None")

    # Check captures
    captures = list(settings.CAPTURES_DIR.glob("*.jpg"))
    manual = [f for f in settings.MANUAL_DIR.glob("*") if f.is_file()]
    print(f"  Captures:         {len(captures)} file(s)")
    print(f"  Manual Folder:    {len(manual)} file(s)")

    errors = settings.validate()
    if errors:
        print(f"\n  ⚠️ Config Issues:")
        for err in errors:
            print(f"    - {err}")
    else:
        print(f"\n  ✅ Configuration OK")

    print("=" * 55 + "\n")


# ── Entry Point ──────────────────────────────────────────────
def main():
    """CLI entry point with subcommands."""
    parser = argparse.ArgumentParser(
        description="Smart Vision Alert — Aerial Studio CCTV Safety Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py              Run one safety check (auto multi-frame if enabled)
  python main.py --burst      Force burst capture (N sequential frames)
  python main.py --test       Send test Telegram message
  python main.py --analyze    Analyze image only (no alert)
  python main.py --status     Show system status
        """,
    )
    parser.add_argument("--test", action="store_true", help="Send a test Telegram message")
    parser.add_argument("--analyze", action="store_true", help="Analyze only, no alert")
    parser.add_argument("--burst", action="store_true", help="Force burst capture mode")
    parser.add_argument("--status", action="store_true", help="Show system status")

    args = parser.parse_args()

    # Initialize logger
    setup_logger(log_dir=settings.LOGS_DIR, level=settings.LOG_LEVEL)

    if args.test:
        sys.exit(0 if cmd_test() else 1)
    elif args.analyze:
        sys.exit(0 if cmd_analyze(use_burst=args.burst) else 1)
    elif args.status:
        cmd_status()
        sys.exit(0)
    else:
        sys.exit(0 if run_safety_check(force_burst=args.burst) else 1)


if __name__ == "__main__":
    main()
