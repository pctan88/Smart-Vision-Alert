#!/usr/bin/env python3
"""
Dry-run test: process sample videos from captures/sample/,
extract frames with ffmpeg, run Gemini AI, send results via Telegram.

Usage:
    python3 test_sample_videos.py
"""

import os
import sys
import glob
import subprocess
import tempfile
import shutil
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))

from config.settings import settings
from core.analyzer import SafetyAnalyzer
from core.notifier import TelegramNotifier
from utils.logger import setup_logger, get_logger

SAMPLE_DIR      = "captures/sample"
FRAME_INTERVAL  = 10  # one frame every N seconds evenly across the video


def get_video_duration(video_path: str) -> float:
    """Get video duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True, text=True, timeout=15,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def extract_frame_at(video_path: str, out_dir: str, label: str,
                     at_sec: float) -> Optional[str]:
    """Extract a single frame at the given timestamp from a local video file."""
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{label}.jpg")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(at_sec),
                "-i", video_path,
                "-vframes", "1",
                "-q:v", "2",
                out_path,
            ],
            capture_output=True, timeout=30,
        )
    except Exception as e:
        get_logger().warning(f"ffmpeg error at T+{at_sec}s: {e}")

    return out_path if os.path.exists(out_path) else None


def process_video(video_path: str, work_dir: str) -> dict:
    """Extract one frame every FRAME_INTERVAL seconds evenly across the video."""
    log      = get_logger()
    duration = get_video_duration(video_path)
    log.info(f"Duration: {duration:.1f}s")

    all_frames = []
    marks      = range(0, max(1, int(duration) + 1), FRAME_INTERVAL)

    for mark in marks:
        lbl   = f"t{mark:04d}s"
        frame = extract_frame_at(
            video_path,
            os.path.join(work_dir, lbl),
            lbl,
            at_sec=float(mark),
        )
        if frame:
            all_frames.append(frame)
            log.info(f"  Frame T+{mark}s → {os.path.basename(frame)}")

    return {"all_frames": all_frames, "frames": len(all_frames), "duration": duration}


def main():
    setup_logger(level=settings.LOG_LEVEL, log_dir=settings.LOGS_DIR)
    log      = get_logger()
    analyzer = SafetyAnalyzer(settings)
    notifier = TelegramNotifier(settings)

    videos = sorted(glob.glob(os.path.join(SAMPLE_DIR, "*.mp4")) +
                    glob.glob(os.path.join(SAMPLE_DIR, "*.MP4")))

    if not videos:
        log.error(f"No MP4 files found in {SAMPLE_DIR}/")
        sys.exit(1)

    log.info(f"Found {len(videos)} video(s) to process")
    notifier.send_text(f"🎬 Starting dry-run test: {len(videos)} sample videos...")

    for i, video_path in enumerate(videos, 1):
        name = os.path.basename(video_path)
        log.info(f"\n[{i}/{len(videos)}] {name}")

        work_dir = tempfile.mkdtemp(prefix=f"sva_test_{i}_")
        try:
            # Extract one frame every FRAME_INTERVAL seconds
            capture = process_video(video_path, work_dir)
            log.info(f"Captured {capture['frames']} frame(s) "
                     f"({FRAME_INTERVAL}s interval, {capture['duration']:.0f}s video)")

            if not capture["all_frames"]:
                notifier.send_text(f"⚠️ Video {i}: No frames extracted from {name}")
                continue

            # Run AI on all evenly-spaced frames
            result = analyzer.analyze_multi_frame(capture["all_frames"])
            log.info(f"AI result: {'SAFE' if result.is_safe else 'UNSAFE'} "
                     f"risk={result.risk_level} confidence={result.confidence:.0%}")

            # Send Telegram alert with first frame as image
            alert_image = capture["all_frames"][0]
            notifier.send_alert(result, alert_image)
            log.info(f"Telegram alert sent for video {i}")

        except Exception as e:
            log.error(f"Failed to process {name}: {e}", exc_info=True)
            notifier.send_text(f"❌ Error processing video {i} ({name}): {e}")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    notifier.send_text(f"✅ Dry-run complete. Processed {len(videos)} video(s).")
    log.info("Dry-run complete.")


if __name__ == "__main__":
    main()
