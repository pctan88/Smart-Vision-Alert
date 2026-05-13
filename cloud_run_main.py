#!/usr/bin/env python3
"""
Smart Vision Alert — Google Cloud Run Handler
=============================================
Full monitoring pipeline with ffmpeg support.

Triggered by:
  - A2 Hosting cron   →  POST /run
  - Telegram /check   →  POST /run  {"manual_check": true}

Environment (set via Secret Manager + Cloud Run env vars):
  GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  XIAOMI_USERNAME, XIAOMI_PASSWORD, XIAOMI_SERVER_REGION
  STUDIO_CAMERAS, STUDIO_CAMERA_HOST
  CLOUD_RUN_SECRET   — shared secret with A2 (incoming trigger auth)
  INTERNAL_SECRET    — shared secret with A2 (outgoing callback auth)
  A2_BASE_URL        — e.g. https://yourdomain.com
  GCS_BUCKET         — GCS bucket name for session storage
  GCS_SESSION_BLOB   — blob name (default: xiaomi_session.json)
"""

from __future__ import annotations

import os
import sys
import json
import glob
import time
import tempfile
import subprocess
import datetime
import requests
from contextlib import ExitStack
from zoneinfo import ZoneInfo
from typing import Optional

from flask import Flask, request, jsonify

sys.path.insert(0, os.path.dirname(__file__))

from config.settings import settings
from core.analyzer import SafetyAnalyzer
from core.notifier import TelegramNotifier
from core.models import AnalysisResult
from utils.logger import setup_logger, get_logger

from xiaomi_capture import (
    _camera_api, _make_http_session, _camera_api_url,
    _silent_refresh, _parse_m3u8, _fetch_aes_key,
    _download_decrypt_segment, _segments_for_window,
    download_thumbnail,
    SEGMENT_SECS, FRAME_FPS, MINUTE_MARK,
)

app = Flask(__name__)
LOCAL_TZ       = ZoneInfo("Asia/Kuala_Lumpur")
EVENT_LOOKBACK = int(os.getenv("EVENT_LOOKBACK", "600"))


# ── GCS session ────────────────────────────────────────────────────────────────

def load_session_gcs() -> Optional[dict]:
    """Load Xiaomi session JSON from Google Cloud Storage."""
    try:
        from google.cloud import storage
        client = storage.Client()
        blob = client.bucket(settings.GCS_BUCKET).blob(settings.GCS_SESSION_BLOB)
        if not blob.exists():
            return None
        return json.loads(blob.download_as_text())
    except Exception as e:
        get_logger().error(f"GCS session load failed: {e}")
        return None


def save_session_gcs(state: dict):
    """Save updated Xiaomi session JSON to Google Cloud Storage."""
    try:
        from google.cloud import storage
        client = storage.Client()
        blob = client.bucket(settings.GCS_BUCKET).blob(settings.GCS_SESSION_BLOB)
        blob.upload_from_string(json.dumps(state), content_type="application/json")
        get_logger().info("Session saved to GCS")
    except Exception as e:
        get_logger().error(f"GCS session save failed: {e}")


# ── ffmpeg frame extraction ────────────────────────────────────────────────────

FRAME_INTERVAL_SECS = 10   # one frame every N seconds evenly across the event
MAX_CAPTURE_SECS    = 180  # only capture the last N seconds for long events (3 min)


def _frames_from_raw_ffmpeg(raw_bytes: bytes, label: str, out_dir: str,
                             max_frames: int = 1) -> list[str]:
    """Write decrypted MPEG-TS bytes to temp file, extract frames with ffmpeg."""
    os.makedirs(out_dir, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tmp:
        tmp.write(raw_bytes)
        tmp_path = tmp.name

    pattern = os.path.join(out_dir, f"{label}_%02d.jpg")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", tmp_path,
                "-vf", f"fps={FRAME_FPS}",
                "-vframes", str(max_frames),
                "-q:v", "2",
                pattern,
            ],
            capture_output=True,
            timeout=60,
        )
    except Exception:
        pass
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return sorted(glob.glob(os.path.join(out_dir, f"{label}_*.jpg")))


def extract_frame_at(m3u8_url: str, out_dir: str, label: str,
                      at_sec: float,
                      auth_session: Optional[requests.Session] = None) -> Optional[str]:
    """Fetch, decrypt, and extract ONE frame at the given timestamp from an HLS stream."""
    sess     = auth_session or requests.Session()
    window   = SEGMENT_SECS  # download a small window around the target second

    r = sess.get(m3u8_url, timeout=15)
    if not r.ok:
        return None
    manifest = _parse_m3u8(r.text)
    if not manifest["segments"]:
        return None

    try:
        aes_key = _fetch_aes_key(manifest["key_url"], sess)
    except Exception:
        return None

    iv          = manifest["iv"]
    target_segs = _segments_for_window(manifest["segments"], at_sec, window)
    if not target_segs:
        target_segs = manifest["segments"]

    raw_chunks = []
    for seg in target_segs:
        try:
            raw = _download_decrypt_segment(seg["url"], aes_key, iv, sess)
            raw_chunks.append(raw)
        except Exception:
            continue

    if not raw_chunks:
        return None

    combined = b"".join(raw_chunks)
    frames   = _frames_from_raw_ffmpeg(combined, label, out_dir, max_frames=1)
    return frames[0] if frames else None


# ── A2 internal API ────────────────────────────────────────────────────────────

def _a2_headers() -> dict:
    return {"X-Internal-Secret": settings.INTERNAL_SECRET}


def a2_is_processed(file_id: str) -> bool:
    """Check if an event is already processed via A2 internal API."""
    try:
        r = requests.post(
            f"{settings.A2_BASE_URL}/api/is-processed",
            json={"file_id": file_id},
            headers=_a2_headers(),
            timeout=10,
        )
        return r.json().get("processed", False)
    except Exception:
        return False


def a2_save_result(
    payload: dict,
    image_path: Optional[str] = None,
    image_paths: Optional[list[str]] = None,
):
    """POST event result to A2 for DB write + image storage."""
    try:
        upload_paths = [p for p in (image_paths or []) if p and os.path.exists(p)]
        if not upload_paths and image_path and os.path.exists(image_path):
            upload_paths = [image_path]

        if upload_paths:
            with ExitStack() as stack:
                files = []
                for idx, path in enumerate(upload_paths):
                    fh = stack.enter_context(open(path, "rb"))
                    filename = f"{idx:03d}_{os.path.basename(path)}"
                    field_name = "images" if len(upload_paths) > 1 else "image"
                    files.append((field_name, (filename, fh, "image/jpeg")))
                requests.post(
                    f"{settings.A2_BASE_URL}/api/save-result",
                    data={"payload": json.dumps(payload)},
                    files=files,
                    headers=_a2_headers(),
                    timeout=120,
                )
        else:
            requests.post(
                f"{settings.A2_BASE_URL}/api/save-result",
                json=payload,
                headers=_a2_headers(),
                timeout=30,
            )
    except Exception as e:
        get_logger().error(f"A2 save-result failed: {e}")


# ── event fetching ─────────────────────────────────────────────────────────────

def get_events(state: dict, cam: dict, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch events for a single camera with automatic pagination."""
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


def get_m3u8_url(state: dict, cam: dict, event: dict) -> str:
    return _camera_api_url(state, settings.STUDIO_CAMERA_HOST, "common/app/m3u8", {
        "did":        cam["did"],
        "model":      cam["model"],
        "fileId":     event["fileId"],
        "isAlarm":    event.get("isAlarm", False),
        "videoCodec": "H264",
        "region":     "CN",
    })


# ── frame capture ──────────────────────────────────────────────────────────────

def capture_event_frames(state: dict, cam: dict, event: dict, ev_dir: str) -> dict:
    """
    Extract frames for AI analysis using ffmpeg (full support on Cloud Run).

    Strategy: one frame every FRAME_INTERVAL_SECS across the full event duration.
    - Thumbnail : Xiaomi keyframe (used as Telegram alert image)
    - t0000s    : frame at T+0s
    - t0010s    : frame at T+10s
    - t0020s    : frame at T+20s  … and so on
    """
    log      = get_logger()
    fid      = event["fileId"]
    duration = 0.0
    os.makedirs(ev_dir, exist_ok=True)

    all_frames   = []
    alert_image  = None  # thumbnail used for Telegram photo

    # ── thumbnail (alert image only — not sent to AI) ──────────────────────
    thumb_dir  = os.path.join(ev_dir, "thumb")
    os.makedirs(thumb_dir, exist_ok=True)
    thumb_path = os.path.join(thumb_dir, "thumb_00.jpg")
    saved = download_thumbnail(state, event, thumb_path,
                               did=cam["did"], model=cam["model"])
    if saved:
        alert_image = saved
        log.info(f"Thumbnail captured: {os.path.basename(saved)}")
    else:
        log.warning(f"Thumbnail download failed for {fid}")

    # ── HLS frames at every FRAME_INTERVAL_SECS ────────────────────────────
    try:
        http     = _make_http_session(state)
        m3u8_url = get_m3u8_url(state, cam, event)

        r = http.get(m3u8_url, timeout=15)
        if r.ok:
            duration = _parse_m3u8(r.text).get("total_duration", 0) or 0

        if duration > 0:
            start_offset = max(0, int(duration) - MAX_CAPTURE_SECS)
            marks = range(start_offset, int(duration) + 1, FRAME_INTERVAL_SECS)
        else:
            marks = [0]  # fallback: at least try T+0s

        for mark in marks:
            lbl     = f"t{mark:04d}s"
            out_dir_frame = os.path.join(ev_dir, lbl)
            frame = extract_frame_at(
                m3u8_url, out_dir_frame, lbl,
                at_sec=float(mark), auth_session=http,
            )
            if frame:
                all_frames.append(frame)
                log.info(f"  Frame T+{mark}s → {os.path.basename(frame)}")

    except Exception as e:
        log.warning(f"Video extraction failed for {fid}: {e}")

    # Fallback: if no video frames, use thumbnail for AI too
    if not all_frames and alert_image:
        all_frames = [alert_image]
        log.warning("No video frames extracted — using thumbnail for AI analysis")

    log.info(f"Total frames for AI: {len(all_frames)} over {duration:.0f}s "
             f"(1 frame every {FRAME_INTERVAL_SECS}s)")

    return {
        "fileId":       fid,
        "eventType":    event.get("eventType"),
        "time":         datetime.datetime.fromtimestamp(
                            event.get("createTime", 0) / 1000, tz=LOCAL_TZ
                        ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "duration":     duration,
        "frames":       len(all_frames),
        "all_frames":   all_frames,
        "alert_image":  alert_image,
        "dir":          ev_dir,
    }


# ── main pipeline ──────────────────────────────────────────────────────────────

def run_pipeline(manual_check: bool = False) -> dict:
    """Full monitoring pipeline: fetch → capture → analyze → alert → report."""
    log      = get_logger()
    analyzer = SafetyAnalyzer(settings)
    notifier = TelegramNotifier(settings)

    # ── session ────────────────────────────────────────────────────────────
    state = load_session_gcs()
    if not state:
        msg = "Xiaomi session not found in GCS. Re-login required."
        log.error(msg)
        notifier.send_text(f"❌ {msg}")
        return {"error": msg}

    refreshed = _silent_refresh(state)
    if refreshed:
        save_session_gcs(refreshed)
        state = refreshed
        log.info("Session token refreshed")

    cameras = settings.STUDIO_CAMERAS
    if not cameras:
        return {"error": "No cameras configured (STUDIO_CAMERAS)"}

    now_ms = int(time.time() * 1000)

    if manual_check:
        # /check: fetch from midnight today (MYT) — find the last event of the day
        midnight = datetime.datetime.now(LOCAL_TZ).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start_ms = int(midnight.timestamp() * 1000)
    else:
        start_ms = now_ms - (EVENT_LOOKBACK * 1000)

    results = {"cameras": [], "total_new": 0, "total_skip": 0}

    for cam in cameras:
        cam_name = cam.get("name", cam["did"])
        log.info(f"── {cam_name} (did={cam['did']}) ──")

        # ── fetch events ───────────────────────────────────────────────
        try:
            events = get_events(state, cam, start_ms, now_ms)
            events.sort(key=lambda x: x.get("createTime", 0), reverse=True)
        except Exception as e:
            log.error(f"Failed to fetch events for {cam_name}: {e}")
            continue

        if not events:
            log.info(f"No events found for {cam_name} today")
            if manual_check:
                notifier.send_text(
                    f"✅ {cam_name}: No motion detected today. Studio is quiet."
                )
            continue

        if manual_check:
            # Only process the single latest event, bypass DB check
            events = [events[0]]
            log.info(f"Manual check: using latest event only (fid={events[0].get('fileId')})")
        else:
            log.info(f"Found {len(events)} event(s)")

        for ev in events:
            fid = ev.get("fileId")
            if not fid:
                continue

            is_latest = True  # always True — manual_check uses 1 event, cron marks latest

            # ── skip already processed (cron only) ────────────────────
            if not manual_check and a2_is_processed(fid):
                results["total_skip"] += 1
                continue

            results["total_new"] += 1
            log.info(f"NEW: {ev.get('eventType', '?')} fid={fid}")

            # ── capture frames ─────────────────────────────────────────
            ev_dir = f"/tmp/captures/{cam['did']}/{fid}"
            try:
                capture = capture_event_frames(state, cam, ev, ev_dir)
            except Exception as e:
                log.error(f"Frame capture failed for {fid}: {e}")
                continue

            if not capture["all_frames"]:
                log.warning(f"No frames captured for {fid}")
                continue

            # ── AI analysis (all frames, evenly spaced) ────────────────
            try:
                result = analyzer.analyze_multi_frame(capture["all_frames"],
                                                       camera_did=cam.get("did", ""))
                log.info(
                    f"AI: {'SAFE' if result.is_safe else 'UNSAFE'} "
                    f"risk={result.risk_level} confidence={result.confidence:.0%}"
                )
            except Exception as e:
                log.error(f"AI analysis failed for {fid}: {e}")
                continue

            # ── Telegram alert (FIRST) ─────────────────────────────────
            alert_sent  = False
            telegram_ok = False
            force_alert = manual_check and is_latest

            if force_alert or settings.risk_exceeds_threshold(result.risk_level):
                # Pick best frame: ask AI for hazard frame, else last frame, else thumbnail
                if capture["all_frames"] and result.risk_level != "safe":
                    try:
                        best_idx = analyzer.identify_best_frame(
                            capture["all_frames"], result
                        )
                        alert_image = capture["all_frames"][best_idx]
                        log.info(f"Alert image: best frame [{best_idx}] "
                                 f"{os.path.basename(alert_image)}")
                    except Exception as e:
                        log.warning(f"Best-frame selection failed: {e}")
                        alert_image = (capture["all_frames"][-1]
                                       or capture["alert_image"])
                else:
                    alert_image = (capture["alert_image"]
                                   or (capture["all_frames"][0]
                                       if capture["all_frames"] else None))

                try:
                    telegram_ok = notifier.send_alert(result, alert_image)
                    alert_sent  = True
                    log.info(f"Telegram alert sent: ok={telegram_ok}")
                except Exception as e:
                    log.error(f"Telegram alert failed: {e}")

            # ── POST results to A2 (AFTER alert) ──────────────────────
            event_dt    = datetime.datetime.fromtimestamp(
                ev.get("createTime", 0) / 1000, tz=LOCAL_TZ
            )
            capture_dir = f"captures/studio/{cam['did']}/{fid}"
            payload = {
                "file_id":      fid,
                "camera_did":   cam["did"],
                "camera_name":  cam_name,
                "event_type":   ev.get("eventType", ""),
                "event_time":   event_dt.isoformat(),
                "duration_sec": capture["duration"],
                "frames_saved": capture["frames"],
                "capture_dir":  capture_dir,
                "analysis": {
                    "is_safe":              result.is_safe,
                    "risk_level":           result.risk_level,
                    "description":          result.description,
                    "hazards":              result.detected_hazards,
                    "confidence":           result.confidence,
                    "motion_detected":      result.motion_detected,
                    "partial_body_lock":    result.partial_body_lock,
                    "partial_body_lock_frames": result.partial_body_lock_frames,
                    "stillness_warn":       result.stillness_warning,
                },
                "alert_sent":   alert_sent,
                "telegram_ok":  telegram_ok,
            }

            alert_image_path = capture["alert_image"] or (
                capture["all_frames"][0] if capture["all_frames"] else None
            )
            # Include thumbnail + all video frames so portal can display the full sequence
            upload_frames = []
            if alert_image_path and os.path.exists(alert_image_path):
                upload_frames.append(alert_image_path)
            for f in capture["all_frames"]:
                if f and os.path.exists(f) and f not in upload_frames:
                    upload_frames.append(f)
            if not upload_frames:
                upload_frames = [alert_image_path] if alert_image_path else []
            a2_save_result(payload, alert_image_path, image_paths=upload_frames)

        results["cameras"].append(cam_name)

    log.info(
        f"Run complete: {results['total_new']} new, "
        f"{results['total_skip']} skipped"
    )
    return results


# ── Flask routes ───────────────────────────────────────────────────────────────

@app.route("/run", methods=["POST"])
def trigger():
    """Main pipeline trigger — called by A2 cron or Telegram /check webhook."""
    if request.headers.get("X-Secret-Token") != settings.CLOUD_RUN_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    body         = request.get_json(silent=True) or {}
    manual_check = bool(body.get("manual_check", False))

    log = get_logger()
    log.info(f"Pipeline triggered (manual_check={manual_check})")

    try:
        result = run_pipeline(manual_check=manual_check)
        return jsonify({"status": "ok", "result": result}), 200
    except Exception as e:
        log.error(f"Pipeline error: {e}", exc_info=True)
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    setup_logger(level=os.getenv("LOG_LEVEL", "INFO"))
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
