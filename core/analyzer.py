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
# PER-CAMERA LAYOUT DESCRIPTIONS
# ══════════════════════════════════════════════════════════════
# Key = camera DID (from Xiaomi). Update the keys once you confirm
# which DID maps to which physical camera (see migrate_session_to_gcs.py).
# If a camera DID is not listed here, GENERIC_LAYOUT is used as fallback.

CAMERA_LAYOUTS: dict[str, str] = {

    # ── Camera 1066815174: top-corner bird's-eye view ─────────
    # High corner mount, steep downward angle. Mirror on LEFT, windows on RIGHT.
    "1066815174": """
- **Camera angle**: Mounted HIGH in a corner, angled STEEPLY DOWNWARD (bird's eye view).
  People appear small and viewed from above — legs/feet prominent, faces less visible.
- **Mirror**: Large floor-to-ceiling mirror on the LEFT wall ONLY.
  Reflections appear on the LEFT side of the frame — do NOT count them as real people.
- **Windows**: Two large windows on the RIGHT wall — bright natural light from the right.
  Silhouettes near the right are normal, not hazards.
- **Main practice area**: CENTRE open floor — focus all safety analysis here.
- **Right-side storage**: Bags, shoes, and equipment permanently stored along the right wall — NOT hazards.
- **Aerial apparatus**: Hoops (lyra) hang from the ceiling in the centre area.
- **Timestamp overlay**: Top-left corner shows Xiaomi camera date/time — ignore for safety analysis.""",

    # ── Camera 1066840805: side-elevated view facing the door ─
    # Elevated but more side-on, facing the door entry. Window on LEFT, mirror on RIGHT (door wall).
    "1066840805": """
- **Camera angle**: Elevated, side-on view facing the door entry. More perspective depth than
  the other camera — people appear fuller in frame.
- **Left side**: Large window with curtains — bright backlight from the left is normal.
  People near the left window may appear as silhouettes — not a hazard.
- **Left-centre foreground**: Aerial silks/ropes hang from the ceiling and are visible as
  dark vertical lines in the foreground — this is permanent equipment, not a hazard.
- **Mirror**: On the RIGHT/door-side wall. Reflections appear on the RIGHT side — not real people.
- **Back-right storage area**: Aerial hoops leaning against the wall, colourful silks hanging,
  crash mats stacked — all permanent fixtures, NOT hazards.
- **Right side**: Door entry, trolley, fan/AC unit — permanent, not hazards.
- **Main practice area**: CENTRE open floor — focus all safety analysis here.
- **Aerial apparatus**: Hoops (lyra) and silks in the centre and left-centre area.
- **Timestamp overlay**: Top-left corner shows Xiaomi camera date/time — ignore for safety analysis.""",
}

# Fallback if camera DID not yet mapped
GENERIC_LAYOUT = """
- **Mirror**: One or more large floor-to-ceiling mirrors on a wall — reflections are NOT real people.
- **Windows**: Large windows provide bright natural light — silhouettes near windows are normal.
- **Storage area**: Bags, shoes, equipment, hoops, and mats stored near walls — NOT hazards.
- **Main practice area**: The open centre floor — focus safety analysis here.
- **Aerial apparatus**: Hoops (lyra) and silks hang from the ceiling — normal equipment.
- **Timestamp overlay**: Top-left corner shows Xiaomi camera date/time — ignore for safety analysis."""


def get_camera_layout(camera_did: str) -> str:
    """Return the layout description for a specific camera DID, or the generic fallback."""
    return CAMERA_LAYOUTS.get(camera_did, GENERIC_LAYOUT)


# ══════════════════════════════════════════════════════════════
# PROMPTS
# ══════════════════════════════════════════════════════════════

# ── Single-Frame Prompt ───────────────────────────────────────
SINGLE_FRAME_PROMPT = """You are an AI safety monitoring system for an **Aerial Dance Studio** equipped with CCTV cameras.

Aerial dance involves performers using apparatus such as aerial silks, aerial hoop (lyra), trapeze, and aerial hammock to perform acrobatic moves while suspended in the air.

## STUDIO LAYOUT — know this before analysing:
{camera_layout}

## What to check for (aerial dance specific):

### CRITICAL (risk_level = "critical"):
- Person fallen on the floor and appears injured or unconscious
- Person tangled/stuck in aerial apparatus and appears distressed
- Person hanging limp/motionless in the air (possible loss of consciousness)
- Fire, smoke, or sparks in the studio
- Equipment (rigging, mount point) visibly broken or failing

### HIGH (risk_level = "high"):
- Person lying on the floor in an unnatural position
- Apparatus (silks, hoop) appears to be tearing or fraying
- Person appears to be falling or in an uncontrolled descent
- Visible injury

### MEDIUM (risk_level = "medium"):
- Person on the floor who may have just fallen (but appears conscious/moving)
- Equipment looks worn or improperly set up
- Unsafe practice observed (no crash mat, too close to walls)

### LOW (risk_level = "low"):
- Minor safety concern (e.g., water spill on floor, mats not positioned)

### SAFE (risk_level = "safe"):
- Normal aerial practice in progress
- Studio is empty
- People stretching, warming up, or resting normally
- Performer safely practising on apparatus (even if inverted — this is NORMAL for aerial)

## IMPORTANT:
- Inverted, wrapped, or unusual positions on apparatus = **NORMAL**, not an accident
- Mirror reflections on left wall = **NOT real people**
- Silhouettes near right windows = **normal lighting**, not a hazard
- Stored bags/items on right side = **permanent fixtures**, not hazards
- Only flag genuine signs of distress, injury, or equipment failure

## Response format:
Respond ONLY with this JSON (no markdown, no code blocks, just raw JSON):
{{
    "is_safe": true or false,
    "risk_level": "safe" or "low" or "medium" or "high" or "critical",
    "people_count": integer (real people only in the centre area, not mirror reflections),
    "description": "Brief description of what you observe in the image",
    "detected_hazards": ["list", "of", "specific", "hazards"] or [] if safe,
    "confidence": 0.0 to 1.0
}}
"""


# ── Multi-Frame Temporal Comparison Prompt ────────────────────
MULTI_FRAME_PROMPT = """You are an AI safety monitoring system for an **Aerial Dance Studio** equipped with CCTV cameras.

You are given a **SEQUENCE of {frame_count} images** captured approximately {interval} seconds apart from the same camera.
Each image is labelled with its capture time offset (T+0s, T+{interval}s, …).
The images are in chronological order: Image 1 is the OLDEST, Image {frame_count} is the MOST RECENT.

## STUDIO LAYOUT — know this before analysing:
{camera_layout}
- **Crash mats**: Blue/grey foam pads on the floor are normal and expected.

## YOUR ANALYSIS TASKS (in order of priority):

### 1. PARTIAL BODY LOCK — Subtle Struggling (HIGHEST PRIORITY)
This is the hardest pattern to detect. The person IS moving overall, but ONE body part
(leg, foot, hand, or clothing) appears anchored/stuck to the apparatus while the rest of the body
moves around it.

**IMPORTANT CONTEXT FOR AERIAL DANCE:**
Aerial movements are naturally fast and dynamic — positions change every few seconds.
If a body part stays in the same position relative to the apparatus for an unusually long time
while the rest of the body is actively moving, that is a strong signal of entanglement.

Signs to look for across frames:
- A limb that **does not change its position relative to the hoop/silk** even as the torso,
  other limbs, or body weight shifts — especially a leg/foot that stays hooked or pressed against
  the hoop rim across multiple frames while the upper body moves freely.
- The person **bending toward the stuck point** then pulling away — a pull-release cycle that
  does not free the limb (same micro-movement repeated at the same spot).
- Body weight leaning AWAY from the apparatus while one contact point stays fixed.
- Clothing (waistband, trouser hem, sleeve) visibly bunched or creased at the contact point
  in a way that persists across frames.

**STEP A — Count how many consecutive frames show the stuck body part.**
Use the T+ timestamps to determine when the issue started and ended.
Set `partial_body_lock_frames` to the total number of frames where the stuck body part was observed.

**STEP B — Check the final frames. Is the person free and moving normally by the end?**
Set `partial_body_lock_resolved` to true if the person is visibly free and moving normally
in the last 1–2 frames. Set to false if they are still stuck at the end of the sequence.

**STEP C — Assign risk using BOTH duration AND resolution:**

Person is STILL STUCK at end of sequence (partial_body_lock_resolved = false):
- 1–2 frames stuck (~{interval_x2}s)  → "low"    (brief, may be intentional)
- 3–4 frames stuck (~{interval_x3}s–{interval_x4}s) → "medium"  (likely struggling)
- 5–6 frames stuck (~{interval_x5}s–{interval_x6}s) → "high"    (clearly stuck, needs help)
- 6+ frames stuck                        → "high"    (prolonged entanglement)
- Any duration + visible distress (hard pulling, bent over, unable to stand) → "critical"

Person has RESOLVED and is back to normal (partial_body_lock_resolved = true):
- Was stuck < {interval_x3}s (1–2 frames) → "safe"   (brief contact, self-resolved quickly)
- Was stuck {interval_x3}s–{interval_x5}s (3–4 frames) → "low"  (struggled for ~30–40s but recovered)
- Was stuck {interval_x6}s+ (6+ frames)   → "low"    (prolonged struggle but self-resolved — worth noting)
- Was stuck + showed visible distress, now resolved → "low" (serious event but recovered)

Add to detected_hazards: "entanglement — [body part] stuck on apparatus for ~Xs [resolved/unresolved]"

### 2. FULL-BODY STILLNESS (HIGHEST PRIORITY for unconsciousness):
- Person in the **same overall position** across 3+ frames in a vulnerable situation:
  - Hanging in the air without any movement → possible unconsciousness
  - Lying on the floor without position change → possible injury/collapse
  - Slumped against equipment motionless → possible medical emergency
- Even a small head turn or arm shift means they are likely conscious and OK.
- Complete stillness across 3+ frames + vulnerable position = CRITICAL.

### 3. MOVEMENT CHANGE PATTERNS:
- On apparatus in early frames → on floor in later frames = POSSIBLE FALL → "high"
- Standing → lying down = POSSIBLE COLLAPSE → "high"
- Equipment swinging uncontrolled or dramatically repositioned = EQUIPMENT FAILURE → "high"
- Fire/smoke appearing = FIRE HAZARD → "critical"

### 4. NORMAL MOVEMENT (do NOT flag these):
- Changing poses on silks/hoop, climbing, descending = normal practice
- Resting sitting or standing between attempts = normal
- Deliberate slow holds or balances where the person is visibly in control = normal
- Gradual controlled transitions = normal

## KEY QUESTION TO ASK FOR EVERY FRAME SEQUENCE:
"Is each body part moving in a **purposeful, free** way — or does any body part appear
**unable to move freely** even though the person is trying to move it?"

## Response format:
Respond ONLY with this JSON (no markdown, no code blocks, just raw JSON):
{{
    "is_safe": true or false,
    "risk_level": "safe" or "low" or "medium" or "high" or "critical",
    "people_count": integer (real people only, not mirror reflections),
    "description": "What you observe across the sequence of images",
    "detected_hazards": ["list", "of", "specific", "hazards"] or [] if safe,
    "confidence": 0.0 to 1.0,
    "motion_detected": true or false,
    "partial_body_lock": true or false,
    "partial_body_lock_frames": integer (0 if none, else number of frames where stuck body part was observed),
    "partial_body_lock_resolved": true or false (true = person is free and normal by end of video),
    "scene_change_level": "none" or "minimal" or "moderate" or "significant",
    "stillness_warning": true or false,
    "temporal_description": "Describe what changed across frames. If partial body lock detected: state which body part, from which T+ timestamp it started, how many seconds it lasted, and whether it was resolved by the end."
}}

## OUTPUT INSTRUCTIONS:
- You MUST output ONLY a valid JSON object.
- DO NOT include any preamble, conversational filler, or internal monologue.
- Keep `description` concise (max 2 sentences). `temporal_description` may be up to 4 sentences.
- Ensure all fields in the schema are present.

## DECISION GUIDE (apply in order):
ENTANGLEMENT — still stuck at end:
  partial_body_lock_frames 1–2,  resolved=false → "low"
  partial_body_lock_frames 3–4,  resolved=false → "medium"
  partial_body_lock_frames 5–6+, resolved=false → "high"
  partial_body_lock + distress,  resolved=false → "critical"

ENTANGLEMENT — self-resolved by end of video:
  partial_body_lock_frames 1–2,  resolved=true  → "safe"  (brief, no concern)
  partial_body_lock_frames 3–5,  resolved=true  → "low"   (struggled ~30–50s, recovered)
  partial_body_lock_frames 6+,   resolved=true  → "low"   (prolonged but self-resolved)
  partial_body_lock + distress,  resolved=true  → "low"   (serious event, now recovered)

FULL-BODY STILLNESS:
  motion_detected=false + person in aerial/floor position → "high" or "critical"
  motion_detected=false + studio empty → "safe"

NORMAL:
  motion_detected=true + partial_body_lock=false + normal movement → "safe"
  scene_change_level "none" + person present → "high" or "critical"
"""


class AnalysisSchema(typing.TypedDict):
    is_safe: bool
    risk_level: str
    people_count: int
    description: str
    detected_hazards: list[str]
    confidence: float
    motion_detected: bool
    partial_body_lock: bool
    partial_body_lock_frames: int
    partial_body_lock_resolved: bool
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
            max_output_tokens=8192,
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
    def analyze(self, image_path: str, camera_did: str = "") -> AnalysisResult:
        """
        Analyze a single CCTV image for safety hazards.

        Args:
            image_path:  Path to the image file to analyze.
            camera_did:  Xiaomi device ID — used to inject camera-specific layout context.

        Returns:
            AnalysisResult with safety assessment.
        """
        try:
            log.info(f"[Single-Frame] Analyzing image: {image_path}")

            prompt   = SINGLE_FRAME_PROMPT.format(camera_layout=get_camera_layout(camera_did))
            img_part = self._load_image_part(image_path)

            response = self.client.models.generate_content(
                model=self.settings.GEMINI_MODEL,
                contents=[prompt, img_part],
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
    def analyze_multi_frame(self, image_paths: list[str],
                             camera_did: str = "") -> AnalysisResult:
        """
        Analyze multiple sequential CCTV images for safety hazards
        with temporal/motion comparison.

        Args:
            image_paths: List of image file paths in chronological order
                         (oldest first, newest last).
            camera_did:  Xiaomi device ID — used to inject camera-specific layout context.

        Returns:
            AnalysisResult with safety + temporal assessment.
        """
        if not image_paths:
            return AnalysisResult.error_result("No images provided for multi-frame analysis")

        if len(image_paths) == 1:
            log.info("Only 1 frame available — falling back to single-frame analysis")
            return self.analyze(image_paths[0], camera_did=camera_did)

        frame_count = len(image_paths)
        interval    = self.settings.MULTI_FRAME_INTERVAL_SECONDS

        try:
            log.info(
                f"[Multi-Frame] Analyzing {frame_count} frames "
                f"(~{interval}s apart)"
            )

            prompt = MULTI_FRAME_PROMPT.format(
                camera_layout=get_camera_layout(camera_did),
                frame_count=frame_count,
                interval=interval,
                interval_x2=interval * 2,
                interval_x3=interval * 3,
                interval_x4=interval * 4,
                interval_x5=interval * 5,
                interval_x6=interval * 6,
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

    # ── Best Frame for Alert Image ────────────────────────────
    def identify_best_frame(self, image_paths: list[str],
                             result: AnalysisResult) -> int:
        """
        Ask Gemini which frame (0-based index) most clearly shows the hazard.
        Falls back to the last frame if the call fails.
        """
        if len(image_paths) <= 1:
            return 0

        try:
            hazards_str = (
                ", ".join(result.detected_hazards)
                if result.detected_hazards
                else result.description
            )
            prompt = (
                f"You are reviewing {len(image_paths)} CCTV frames "
                f"(indexed 0 to {len(image_paths) - 1}).\n"
                f"A safety risk was detected: {result.risk_level} — {hazards_str}\n"
                f"Which single frame index most clearly shows the detected hazard "
                f"or the person in the most dangerous position?\n"
                f"Reply with ONLY one integer (0-based index). No other text."
            )

            contents = [prompt]
            for i, path in enumerate(image_paths):
                contents.append(f"Frame {i}:")
                contents.append(self._load_image_part(path))

            cfg = types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=5,
            )
            response = self.client.models.generate_content(
                model=self.settings.GEMINI_MODEL,
                contents=contents,
                config=cfg,
            )

            idx = int(response.text.strip())
            if 0 <= idx < len(image_paths):
                log.info(f"Best alert frame: index {idx} of {len(image_paths)}")
                return idx

        except Exception as e:
            log.warning(f"identify_best_frame failed: {e} — using last frame")

        return len(image_paths) - 1

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
                is_safe=False,
                risk_level="unknown",
                description="AI response parse failed — manual review required",
                detected_hazards=["ai_parse_failure"],
                confidence=0.0,
                raw_response=raw_text,
            )
