#!/usr/bin/env python3
"""
Test: Upload a full video directly to Gemini File API for analysis.
No frame extraction — Gemini analyses the entire video natively.

Usage:
    python3 test_video_direct.py
    .venv/bin/python3 test_video_direct.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from config.settings import settings
from core.notifier import TelegramNotifier
from utils.logger import setup_logger, get_logger

from google import genai
from google.genai import types

VIDEO_PATH = "captures/sample/WhatsApp Video 2026-05-10 at 17.33.06 (1).mp4"  # 36s video

PROMPT = """
You are a safety monitoring AI for an aerial/dance studio CCTV system.
You are watching a full continuous video — you can observe motion, timing, and repetition across the entire clip.

═══════════════════════════════════════════
STUDIO FACTS — memorise before you watch
═══════════════════════════════════════════
• MIRRORS: ALL walls are covered floor-to-ceiling in mirrors. Every real person produces
  multiple reflections. A reflection moves in perfect lateral symmetry with the original
  and cannot cast its own independent shadow on the floor.
  → Count ONLY real people: those with a distinct floor shadow or independent floor contact.
• AERIAL HOOPS (lyra): Metal circles suspended from the ceiling. Normal use includes
  standing beside, resting a leg on the rim, hanging, or spinning on it.
• CRASH MATS: Blue or grey foam pads on the floor — normal and expected.

═══════════════════════════════════════════
PRIMARY HAZARD — PARTIAL BODY LOCK (subtle struggling)
═══════════════════════════════════════════
This is the hardest pattern. The person IS moving overall, but ONE body part appears
ANCHORED to the apparatus while the rest of the body moves around it.

Watch carefully for:
• A leg, foot, or hand that does NOT change position relative to the hoop even as the
  torso, arms, or body weight shifts around it.
• The person bending TOWARD the stuck point then pulling away — a pull-release cycle
  that does not free the limb (repeated ≥ 2 times at the same spot = struggling).
• Body weight leaning AWAY from the hoop while one contact point stays fixed — they are
  trying to pull free, not deliberately resting.
• Clothing (waistband, trouser hem, sleeve) visibly bunched or creased at the hoop
  contact point in a way that persists across multiple moments in the video.
• Facial or postural signs of effort/frustration directed at the apparatus rather than
  at the exercise itself.

If partial body lock is observed → risk_level at least "medium", hazards must include
"possible entanglement — limb or clothing may be caught on apparatus".
If the person cannot free themselves by the end of the video → risk_level "high".

═══════════════════════════════════════════
OTHER HAZARDS TO DETECT
═══════════════════════════════════════════
• Full-body stillness sustained for many seconds in a vulnerable position → "high"/"critical"
• Person falls from apparatus and does not get up → "high"
• Person unconscious or limp → "critical"
• Fire, smoke, broken rigging → "critical"

═══════════════════════════════════════════
NORMAL — do NOT flag these
═══════════════════════════════════════════
• Inverted, wrapped, or unusual positions on apparatus — this is normal aerial training
• Deliberate slow holds where the person is clearly in control
• Resting sitting or standing between attempts

═══════════════════════════════════════════
OUTPUT — JSON only, no markdown fences
═══════════════════════════════════════════
{
  "is_safe": true or false,
  "risk_level": "safe" | "low" | "medium" | "high" | "critical",
  "people_count": <integer, real people only — NOT mirror reflections>,
  "description": "<one sentence: what is actually happening>",
  "detected_hazards": ["<specific hazard>"] or [],
  "confidence": 0.0 to 1.0,
  "motion_detected": true or false,
  "partial_body_lock": true or false,
  "stillness_warning": true or false,
  "temporal_description": "<describe how the situation evolves over time; explicitly state whether any limb or clothing appears stuck on the hoop and at what point in the video>"
}
"""


def main():
    setup_logger(level=settings.LOG_LEVEL, log_dir=settings.LOGS_DIR)
    log      = get_logger()
    notifier = TelegramNotifier(settings)
    client   = genai.Client(api_key=settings.GEMINI_API_KEY)

    if not os.path.exists(VIDEO_PATH):
        log.error(f"Video not found: {VIDEO_PATH}")
        sys.exit(1)

    size_mb = os.path.getsize(VIDEO_PATH) / (1024 * 1024)
    log.info(f"Uploading video: {os.path.basename(VIDEO_PATH)} ({size_mb:.1f} MB)")
    notifier.send_text(f"📤 Uploading video to Gemini ({size_mb:.1f} MB)...")

    # Upload video to Gemini File API
    video_file = client.files.upload(
        file=VIDEO_PATH,
        config=types.UploadFileConfig(mime_type="video/mp4"),
    )
    log.info(f"Uploaded: {video_file.name} — waiting for processing...")

    # Wait until Gemini finishes processing the video
    while video_file.state.name == "PROCESSING":
        time.sleep(3)
        video_file = client.files.get(name=video_file.name)
        log.info(f"Processing... state={video_file.state.name}")

    if video_file.state.name != "ACTIVE":
        log.error(f"File processing failed: {video_file.state.name}")
        notifier.send_text(f"❌ Gemini video processing failed: {video_file.state.name}")
        return

    log.info("Video ready. Running AI analysis...")
    notifier.send_text("🤖 Video processed. Running AI analysis...")

    # Send video to Gemini for analysis
    response = client.models.generate_content(
        model=settings.GEMINI_MODEL,
        contents=[
            types.Part.from_uri(
                file_uri=video_file.uri,
                mime_type="video/mp4",
            ),
            PROMPT,
        ],
        config=types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=2048,
        ),
    )

    raw_text = response.text.strip()
    log.info(f"Raw AI response:\n{raw_text}")

    # Clean up uploaded file from Gemini
    try:
        client.files.delete(name=video_file.name)
        log.info("Cleaned up uploaded file from Gemini")
    except Exception:
        pass

    # Send full AI response to Telegram as plain text
    notifier.send_text(
        f"🎬 Video Analysis Result\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{raw_text}"
    )
    log.info("Result sent to Telegram.")


if __name__ == "__main__":
    main()
