"""
Smart Vision Alert — Gemini AI Safety Analyzer
Analyzes CCTV images for aerial dance studio safety hazards
using Google Gemini vision model.

Supports two analysis modes:
  - Single-frame: Analyze one image for immediate hazards
  - Multi-frame:  Compare 2-5 sequential images to detect motion/stillness
                  and temporal changes (e.g., person motionless for too long)
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import typing_extensions as typing
from google import genai
from google.genai import types
from PIL import Image

from config.settings import Settings
from core.models import AnalysisResult
from utils.logger import get_logger

log = get_logger()


# ══════════════════════════════════════════════════════════════
# PROMPTS
# ══════════════════════════════════════════════════════════════

# ── Single-Frame Prompt ───────────────────────────────────────
SINGLE_FRAME_PROMPT = """You are an AI safety monitoring system for an **Aerial Dance Studio** equipped with CCTV cameras.

Aerial dance involves performers using apparatus such as aerial silks, aerial hoop (lyra), trapeze, and aerial hammock to perform acrobatic moves while suspended in the air.

Analyze this CCTV image and determine if there is any safety concern, accident, or emergency happening.

## What to check for (aerial dance specific):

### CRITICAL (risk_level = "critical"):
- Person fallen on the floor and appears injured or unconscious
- Person tangled/stuck in aerial silks or apparatus and appears distressed
- Person hanging limp/motionless in the air (possible loss of consciousness)
- Fire, smoke, or sparks in the studio
- Equipment (rigging, mount point) visibly broken or failing

### HIGH (risk_level = "high"):
- Person lying on the floor in an unnatural position
- Apparatus (silks, hoop) appears to be tearing or fraying
- Person appears to be falling or in an uncontrolled descent
- Visible injury (blood, etc.)

### MEDIUM (risk_level = "medium"):
- Person on the floor who may have just fallen (but appears conscious/moving)
- Equipment looks worn or improperly set up
- Unsafe practice observed (no crash mat, too close to walls)

### LOW (risk_level = "low"):
- Minor safety concern (e.g., water spill on floor, cluttered space)
- Mats not properly positioned

### SAFE (risk_level = "safe"):
- Normal aerial practice session in progress
- Studio is empty
- People stretching, warming up, or resting normally
- Performer safely practicing on apparatus (even if inverted — this is normal for aerial)

## IMPORTANT CONTEXT:
- People being upside down, inverted, or in unusual positions on aerial apparatus is **NORMAL** and **NOT an accident**
- People wrapping themselves in fabric while in the air is **NORMAL aerial silk technique**
- The studio may have crash mats, mirrors, and various aerial equipment — this is expected
- Only flag something if there are genuine signs of distress, injury, or equipment failure

## Response format:
Respond ONLY with this JSON (no markdown, no code blocks, just raw JSON):
{
    "is_safe": true or false,
    "risk_level": "safe" or "low" or "medium" or "high" or "critical",
    "description": "Brief description of what you observe in the image",
    "detected_hazards": ["list", "of", "specific", "hazards"] or [] if safe,
    "confidence": 0.0 to 1.0
}
"""


# ── Multi-Frame Temporal Comparison Prompt ────────────────────
MULTI_FRAME_PROMPT = """You are an AI safety monitoring system for an **Aerial Dance Studio** equipped with CCTV cameras.

You are given a **SEQUENCE of {frame_count} images** captured approximately {interval} seconds apart from the same camera.
The images are in chronological order: Image 1 is the OLDEST, Image {frame_count} is the MOST RECENT.

Your task is to:
1. Analyze each image for immediate safety hazards
2. **COMPARE the images** to detect movement, changes, or LACK of movement
3. Pay special attention to **STILLNESS** — a person who hasn't moved between frames may be in danger

## CRITICAL TEMPORAL PATTERNS TO DETECT:

### Person Motionless (HIGHEST PRIORITY):
- If a person is in the **same position** across multiple frames, especially:
  - Hanging in the air without movement → possible unconsciousness/entanglement
  - Lying on the floor without any change in position → possible injury
  - Slumped against equipment without movement → possible medical emergency
- Even subtle differences (slight arm movement, head turn) mean they are likely OK
- Complete stillness across 3+ frames with a person in a vulnerable position = CRITICAL

### Movement Change Patterns:
- Person was on apparatus in earlier frames → now on floor = POSSIBLE FALL
- Person was standing → now lying down = POSSIBLE COLLAPSE
- Equipment position changed dramatically = POSSIBLE EQUIPMENT FAILURE
- Smoke/fire appearing in later frames = FIRE HAZARD

### Normal Movement (NOT emergencies):
- Person changing poses on aerial silks/hoop = NORMAL practice
- Person climbing up or coming down from apparatus = NORMAL
- Person resting between attempts (sitting/standing normally) = NORMAL
- Person leaving or entering the frame = NORMAL
- Gradual, controlled transitions between positions = NORMAL

## IMPORTANT CONTEXT:
- People being upside down, inverted, or in unusual positions on aerial apparatus is **NORMAL**
- People wrapping themselves in fabric while in the air is **NORMAL aerial silk technique**
- A person maintaining the SAME unusual position for many seconds WITHOUT any motion is CONCERNING
- The key question is: **Is the person CHOOSING to be still, or are they UNABLE to move?**
- Signs of distress: limp body, head drooping, arms dangling without tension

## Response format:
Respond ONLY with this JSON (no markdown, no code blocks, just raw JSON):
{{
    "is_safe": true or false,
    "risk_level": "safe" or "low" or "medium" or "high" or "critical",
    "description": "What you observe across the sequence of images",
    "detected_hazards": ["list", "of", "specific", "hazards"] or [] if safe,
    "confidence": 0.0 to 1.0,
    "motion_detected": true or false,
    "scene_change_level": "none" or "minimal" or "moderate" or "significant",
    "stillness_warning": true or false,
    "temporal_description": "Describe what changed or didn't change between frames"
}}

## OUTPUT INSTRUCTIONS:
- You MUST output ONLY a valid JSON object.
- DO NOT include any preamble, conversational filler, or internal monologue.
- Keep `description` and `temporal_description` concise (max 2 sentences each).
- Ensure all fields in the schema are present.

## DECISION GUIDE:
- motion_detected = false + person visible in aerial position → risk_level "high" or "critical"
- motion_detected = false + person on floor → risk_level "high" or "critical"
- motion_detected = false + studio empty → risk_level "safe" (no one to be in danger)
- motion_detected = true + normal movement → risk_level "safe"
- scene_change_level "none" + person present = CONCERNING (potential stillness emergency)
"""


class AnalysisSchema(typing.TypedDict):
    is_safe: bool
    risk_level: str
    description: str
    detected_hazards: list[str]
    confidence: float
    motion_detected: bool
    scene_change_level: str
    stillness_warning: bool
    temporal_description: str


class SafetyAnalyzer:
    """Analyze camera images for aerial dance safety hazards using Gemini AI."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._configure_api()

    def _configure_api(self):
        """Configure the Gemini API client."""
        if not self.settings.GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is not configured")

        self.client = genai.Client(api_key=self.settings.GEMINI_API_KEY)

        self._gen_config = types.GenerateContentConfig(
            temperature=0.1,
            top_p=0.95,
            max_output_tokens=2048,
            response_mime_type="application/json",
            response_schema=AnalysisSchema,
            safety_settings=[
                types.SafetySetting(
                    category="HARM_CATEGORY_HARASSMENT",
                    threshold="BLOCK_NONE",
                ),
                types.SafetySetting(
                    category="HARM_CATEGORY_HATE_SPEECH",
                    threshold="BLOCK_NONE",
                ),
                types.SafetySetting(
                    category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    threshold="BLOCK_NONE",
                ),
                types.SafetySetting(
                    category="HARM_CATEGORY_DANGEROUS_CONTENT",
                    threshold="BLOCK_NONE",
                ),
            ],
        )

        log.info(f"Gemini AI configured ({self.settings.GEMINI_MODEL})")

    # ── Single-Frame Analysis ─────────────────────────────────
    def analyze(self, image_path: str) -> AnalysisResult:
        """
        Analyze a single CCTV image for safety hazards.

        Args:
            image_path: Path to the image file to analyze.

        Returns:
            AnalysisResult with safety assessment.
        """
        try:
            log.info(f"[Single-Frame] Analyzing image: {image_path}")

            img_part = self._load_image_part(image_path)

            response = self.client.models.generate_content(
                model=self.settings.GEMINI_MODEL,
                contents=[SINGLE_FRAME_PROMPT, img_part],
                config=self._gen_config,
            )

            raw_text = response.text.strip()
            log.debug(f"Gemini raw response: {raw_text}")

            result = self._parse_response(raw_text)
            result.raw_response = raw_text
            result.analysis_mode = "single"
            result.frames_analyzed = 1

            self._log_result(result)
            return result

        except Exception as e:
            log.error(f"Single-frame analysis failed: {e}", exc_info=True)
            return AnalysisResult.error_result(str(e))

    # ── Multi-Frame Temporal Analysis ─────────────────────────
    def analyze_multi_frame(self, image_paths: list[str]) -> AnalysisResult:
        """
        Analyze multiple sequential CCTV images for safety hazards
        with temporal/motion comparison.

        Args:
            image_paths: List of image file paths in chronological order
                         (oldest first, newest last).

        Returns:
            AnalysisResult with safety + temporal assessment.
        """
        if not image_paths:
            return AnalysisResult.error_result("No images provided for multi-frame analysis")

        if len(image_paths) == 1:
            log.info("Only 1 frame available — falling back to single-frame analysis")
            return self.analyze(image_paths[0])

        frame_count = len(image_paths)
        interval = self.settings.MULTI_FRAME_INTERVAL_SECONDS

        try:
            log.info(
                f"[Multi-Frame] Analyzing {frame_count} frames "
                f"(~{interval}s apart)"
            )

            prompt = MULTI_FRAME_PROMPT.format(
                frame_count=frame_count,
                interval=interval,
            )

            # Build content: [prompt, "Image 1:", part1, "Image 2:", part2, ...]
            contents = [prompt]
            for i, path in enumerate(image_paths):
                contents.append(f"\n--- Image {i + 1} of {frame_count} (T+{i * interval}s) ---")
                contents.append(self._load_image_part(path))
                log.debug(f"  Frame {i + 1}: {Path(path).name}")

            response = self.client.models.generate_content(
                model=self.settings.GEMINI_MODEL,
                contents=contents,
                config=self._gen_config,
            )

            raw_text = response.text.strip()
            log.debug(f"Gemini multi-frame response: {raw_text}")

            result = self._parse_response(raw_text)
            result.raw_response = raw_text
            result.analysis_mode = "multi_frame"
            result.frames_analyzed = frame_count

            # Escalate risk if person appears completely still
            if result.stillness_warning and not result.motion_detected:
                if result.risk_level in ("safe", "low"):
                    log.warning(
                        "⚠️ Stillness detected across frames — "
                        "escalating risk from %s to medium",
                        result.risk_level,
                    )
                    result.risk_level = "medium"
                    result.is_safe = False
                    if "stillness_detected" not in result.detected_hazards:
                        result.detected_hazards.append("stillness_detected")

            self._log_result(result)
            return result

        except Exception as e:
            log.error(f"Multi-frame analysis failed: {e}", exc_info=True)
            log.info("Falling back to single-frame analysis of latest image")
            return self.analyze(image_paths[-1])

    # ── Compare With Previous (simple 2-frame comparison) ─────
    def analyze_with_previous(
        self, current_path: str, previous_path: str | None
    ) -> AnalysisResult:
        """
        Analyze current image, comparing with a previous capture.

        Convenience method that wraps analyze_multi_frame for the common
        case of comparing just 2 images (previous + current).
        """
        if previous_path and Path(previous_path).exists():
            return self.analyze_multi_frame([previous_path, current_path])
        else:
            return self.analyze(current_path)

    # ── Helpers ───────────────────────────────────────────────
    def _load_image_part(self, image_path: str) -> types.Part:
        """Load an image file and return it as a Gemini API Part."""
        img = Image.open(image_path)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        return types.Part.from_bytes(
            data=buffer.getvalue(),
            mime_type="image/jpeg",
        )

    def _log_result(self, result: AnalysisResult):
        """Log analysis result details."""
        log.info(
            f"Analysis [{result.analysis_mode}|{result.frames_analyzed}f]: "
            f"safe={result.is_safe}, risk={result.risk_level}, "
            f"confidence={result.confidence:.0%}"
        )

        if result.analysis_mode == "multi_frame":
            log.info(
                f"  Motion: {'yes' if result.motion_detected else '⚠️ NO MOTION'} | "
                f"Scene change: {result.scene_change_level} | "
                f"Stillness warning: {'⚠️ YES' if result.stillness_warning else 'no'}"
            )
            if result.temporal_description:
                log.info(f"  Temporal: {result.temporal_description}")

        if not result.is_safe:
            log.warning(f"⚠️ UNSAFE detected: {result.description}")
            log.warning(f"   Hazards: {result.detected_hazards}")

    def _parse_response(self, raw_text: str) -> AnalysisResult:
        """Parse Gemini's JSON response into an AnalysisResult."""
        try:
            data = json.loads(raw_text)
            return AnalysisResult.from_dict(data)

        except Exception as e:
            log.warning(f"Failed to parse or map JSON: {e}")
            # Try to extract JSON from markdown code block
            json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw_text, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(1))
                    return AnalysisResult.from_dict(data)
                except Exception:
                    pass

            # Try to find JSON object in the text
            json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(0))
                    return AnalysisResult.from_dict(data)
                except Exception:
                    pass

            log.warning(f"Could not parse Gemini response as JSON. Raw text: {repr(raw_text)}")
            return AnalysisResult(
                is_safe=True,
                risk_level="safe",
                description="Could not parse AI response",
                detected_hazards=[],
                confidence=0.0,
                raw_response=raw_text,
            )
