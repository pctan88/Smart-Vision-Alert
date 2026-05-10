"""
Xiaomi Camera Cloud Capture Pipeline
=====================================
Fetches cloud events and extracts frames for AI processing.

Pipeline entry points
---------------------
  get_session_state()              -> dict   load / refresh auth
  get_events(state, start_ms, end_ms) -> list  deduplicated events
  capture_event(state, event, out_dir) -> CaptureResult
  capture_time_range(state, start_ms, end_ms, out_dir) -> list[CaptureResult]
"""

from __future__ import annotations

import glob
import os
import json
import tempfile
import time
import base64
import hashlib
import datetime
import pickle
import requests
import cv2
from dataclasses import dataclass, field, asdict
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Optional

from micloud.miutils import get_session, gen_nonce, signed_nonce, generate_enc_params, decrypt_rc4
from Crypto.Cipher import ARC4
from dotenv import load_dotenv

load_dotenv("config/.env")

# ── config ────────────────────────────────────────────────────────────────────

DEVICE_ID     = "294183200"
CAMERA_MODEL  = "isa.camera.hlc6"
CAMERA_HOST   = "sg.business.smartcamera.api.io.mi.com"
COUNTRY       = os.getenv("XIAOMI_SERVER_REGION", "cn")
SESSION_FILE  = "config/.micloud_session"
LOCAL_TZ      = ZoneInfo("Asia/Kuala_Lumpur")  # UTC+8

# Segment config
SEGMENT_SECS  = 5    # seconds to capture per segment
FRAME_FPS     = 1    # frames per second to extract
MINUTE_MARK   = 60   # interval (seconds) for mid-event captures


# ── dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class EventSegment:
    label: str          # "first", "last", "t60s", "t120s" ...
    start_sec: float    # offset into video
    frames: list[str] = field(default_factory=list)


@dataclass
class CaptureResult:
    file_id:    str
    event_type: str
    create_time_ms: int
    duration_sec: Optional[float]
    m3u8_url:   str
    thumbnail:  Optional[str]           # local path
    segments:   list[EventSegment] = field(default_factory=list)
    error:      Optional[str] = None

    @property
    def all_frames(self) -> list[str]:
        return [f for seg in self.segments for f in seg.frames]

    def summary(self) -> dict:
        ts = datetime.datetime.fromtimestamp(self.create_time_ms / 1000, tz=LOCAL_TZ)
        return {
            "fileId":      self.file_id,
            "eventType":   self.event_type,
            "time_local":  ts.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "duration":    self.duration_sec,
            "thumbnail":   self.thumbnail,
            "frame_count": len(self.all_frames),
            "segments":    [{
                "label": s.label,
                "start_sec": s.start_sec,
                "frames": s.frames,
            } for s in self.segments],
            "error":       self.error,
        }


# ── session management ────────────────────────────────────────────────────────

def _load_session() -> Optional[dict]:
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _save_session(state: dict):
    with open(SESSION_FILE, "wb") as f:
        pickle.dump(state, f)


def _silent_refresh(state: dict) -> Optional[dict]:
    """Use passToken to silently renew serviceToken (no captcha/2FA)."""
    from micloud.miutils import get_session as _gs
    session = _gs()
    session.cookies.update({"userId": str(state["user_id"]), "passToken": state["pass_token"]})
    AUTH_BASE = "https://account.xiaomi.com"
    r1 = session.get(f"{AUTH_BASE}/pass/serviceLogin?sid=xiaomiio&_json=true")
    j1 = json.loads(r1.text.replace("&&&START&&&", ""))
    location  = j1.get("location", "")
    ssecurity = j1.get("ssecurity", state.get("ssecurity", ""))
    if not location:
        return None
    r2 = session.get(location)
    if r2.status_code == 403:
        return None
    token = r2.cookies.get("serviceToken") or session.cookies.get("serviceToken", "")
    return {**state, "service_token": token, "ssecurity": ssecurity} if token else None


def get_session_state() -> Optional[dict]:
    """
    Load saved session, silently refresh if expired.
    Returns None only if full interactive re-login is required.
    Call test_cloud.full_login() in that case.
    """
    state = _load_session()
    if not state:
        return None

    # Quick validation
    try:
        url     = ("" if COUNTRY.lower() == "cn" else COUNTRY.lower() + ".") + "api.io.mi.com/app"
        url     = f"https://{url}/home/device_list"
        session = _make_http_session(state)
        nonce   = gen_nonce()
        sn      = signed_nonce(state["ssecurity"], nonce)
        params  = {"data": '{"getVirtualModel":true,"getHuamiDevices":1}'}
        pdata   = generate_enc_params(url, "POST", sn, nonce, params, state["ssecurity"])
        resp    = session.post(url, data=pdata)
        raw     = decrypt_rc4(signed_nonce(state["ssecurity"], pdata["_nonce"]), resp.text)
        j       = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        if j.get("result") is None and j.get("code") != 0:
            raise ValueError("bad")
    except Exception:
        state = _silent_refresh(state)
        if state:
            _save_session(state)

    return state


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _make_http_session(state: dict) -> requests.Session:
    session = get_session()
    session.headers.update({
        "Accept-Encoding":            "identity",
        "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
        "content-type":               "application/x-www-form-urlencoded",
        "MIOT-ENCRYPT-ALGORITHM":     "ENCRYPT-RC4",
    })
    session.cookies.update({
        "userId":                 str(state["user_id"]),
        "yetAnotherServiceToken": state["service_token"],
        "serviceToken":           state["service_token"],
        "locale":                 state.get("locale", "en_US"),
        "timezone":               state.get("timezone", "GMT+08:00"),
        "channel":                "MI_APP_STORE",
    })
    return session


def _gen_camera_sig(method: str, path: str, snonce: str, params: dict) -> str:
    parts = [method.upper(), path]
    for k, v in params.items():
        parts.append(f"{k}={v}")
    parts.append(snonce)
    return base64.b64encode(hashlib.sha1("&".join(parts).encode()).digest()).decode()


def _build_camera_enc_params(state: dict, path: str, params: dict) -> dict:
    """Build encrypted query params for camera API. Returns enc dict + nonce."""
    nonce = gen_nonce()
    sn    = signed_nonce(state["ssecurity"], nonce)
    enc   = {"data": json.dumps(params)}
    enc["rc4_hash__"] = _gen_camera_sig("GET", f"/{path}", sn, enc)
    for k, v in enc.items():
        r = ARC4.new(base64.b64decode(sn))
        r.encrypt(bytes(1024))
        enc[k] = base64.b64encode(r.encrypt(v.encode())).decode()
    enc["signature"] = _gen_camera_sig("GET", f"/{path}", sn, enc)
    enc["ssecurity"] = state["ssecurity"]
    enc["_nonce"]    = nonce
    enc["yetAnotherServiceToken"] = state["service_token"]
    return enc, nonce


def _camera_api(state: dict, host: str, path: str, params: dict) -> dict:
    """Encrypted GET to Xiaomi smart-camera API. Returns parsed JSON response."""
    url            = f"https://{host}/{path}"
    session        = _make_http_session(state)
    enc, nonce     = _build_camera_enc_params(state, path, params)
    resp           = session.get(url, params=enc)
    try:
        raw = decrypt_rc4(signed_nonce(state["ssecurity"], nonce), resp.text)
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        try:
            return json.loads(resp.text)
        except Exception:
            return {"_raw": resp.text}


def _camera_api_url(state: dict, host: str, path: str, params: dict) -> str:
    """Return the full signed URL for a camera API call (for streaming to OpenCV)."""
    import urllib.parse
    enc, _ = _build_camera_enc_params(state, path, params)
    return f"https://{host}/{path}?" + urllib.parse.urlencode(enc)


# ── event fetching ────────────────────────────────────────────────────────────

def get_events(state: dict, start_ms: int, end_ms: int, limit: int = 50) -> list[dict]:
    """
    Fetch deduplicated events from Xiaomi cloud in the given ms timestamp range.
    Handles pagination automatically.
    """
    all_events: list[dict] = []
    seen:       set[str]   = set()
    cursor_end = end_ms

    while True:
        resp = _camera_api(state, CAMERA_HOST, "common/app/get/eventlist", {
            "did":       DEVICE_ID,
            "model":     CAMERA_MODEL,
            "doorBell":  0,
            "eventType": "Default",
            "needMerge": True,
            "sortType":  "DESC",
            "region":    "CN",
            "language":  "en_US",
            "beginTime": start_ms,
            "endTime":   cursor_end,
            "limit":     limit,
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


def local_time_range_ms(date: datetime.date,
                         start_hour: int, start_min: int,
                         end_hour:   int, end_min:   int) -> tuple[int, int]:
    """
    Build ms timestamps for a local-time range on a given date.
    Example: local_time_range_ms(today, 21, 0, 22, 0) → 9pm–10pm MYT as UTC ms.
    """
    tz  = LOCAL_TZ
    s   = datetime.datetime(date.year, date.month, date.day, start_hour, start_min, tzinfo=tz)
    e   = datetime.datetime(date.year, date.month, date.day, end_hour,   end_min,   tzinfo=tz)
    return int(s.timestamp() * 1000), int(e.timestamp() * 1000)


# ── video / thumbnail fetching ────────────────────────────────────────────────

def get_m3u8_url(state: dict, event: dict) -> str:
    """
    Return signed URL to the HLS stream for a cloud event.
    The URL is passed directly to OpenCV — no pre-request needed.
    """
    return _camera_api_url(state, CAMERA_HOST, "common/app/m3u8", {
        "did":        DEVICE_ID,
        "model":      CAMERA_MODEL,
        "fileId":     event["fileId"],
        "isAlarm":    event.get("isAlarm", False),
        "videoCodec": "H264",
        "region":     "CN",
    })


def download_thumbnail(state: dict, event: dict, out_path: str) -> Optional[str]:
    """
    Download the event thumbnail image via processor API.
    Returns local file path or None on failure.
    """
    img_store_id = event.get("imgStoreId")
    if not img_store_id:
        return None
    try:
        resp = _camera_api(state,
            host="sg.app.processor.smartcamera.api.io.mi.com",
            path="common/app/play/v1/img",
            params={
                "did":        DEVICE_ID,
                "model":      CAMERA_MODEL,
                "fileId":     event["fileId"],
                "imgStoreId": img_store_id,
                "region":     "CN",
            }
        )
        img_url = (resp.get("data") or {}).get("url") or resp.get("url") or ""
        if not img_url:
            return None
        r = requests.get(img_url, timeout=15)
        if r.status_code == 200:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(r.content)
            return out_path
    except Exception:
        pass
    return None


# ── M3U8 parsing + AES-128 decryption ────────────────────────────────────────

import re
from Crypto.Cipher import AES


def _parse_m3u8(m3u8_text: str) -> dict:
    """
    Parse HLS M3U8 into structured form.
    Returns: {
        "key_url": str,
        "iv": bytes,
        "segments": [{"url": str, "start": float, "duration": float}, ...]
    }
    """
    key_url  = ""
    iv_bytes = b"\x00" * 16
    segments = []
    cursor   = 0.0
    current_dur = None

    key_match = re.search(r'#EXT-X-KEY:.*?URI="([^"]+)"', m3u8_text)
    if key_match:
        key_url = key_match.group(1)
    iv_match = re.search(r'IV=0x([0-9A-Fa-f]+)', m3u8_text)
    if iv_match:
        iv_bytes = bytes.fromhex(iv_match.group(1).zfill(32))

    for line in m3u8_text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            current_dur = float(line.split(":")[1].rstrip(","))
        elif line.startswith("http") and current_dur is not None:
            segments.append({"url": line, "start": cursor, "duration": current_dur})
            cursor += current_dur
            current_dur = None

    return {"key_url": key_url, "iv": iv_bytes, "segments": segments,
            "total_duration": cursor}


def _fetch_aes_key(key_url: str, session: requests.Session) -> bytes:
    r = session.get(key_url, timeout=15)
    r.raise_for_status()
    return r.content  # 16 bytes


def _decrypt_segment(data: bytes, key: bytes, iv: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.decrypt(data)


def _download_decrypt_segment(url: str, key: bytes, iv: bytes,
                               session: requests.Session) -> bytes:
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return _decrypt_segment(r.content, key, iv)


def _segments_for_window(segments: list, start_sec: float,
                          window_sec: float) -> list:
    """Return segments that overlap [start_sec, start_sec + window_sec)."""
    end_sec = start_sec + window_sec
    return [s for s in segments
            if s["start"] + s["duration"] > start_sec and s["start"] < end_sec]


def _frames_from_raw(raw_bytes: bytes, label: str, out_dir: str,
                      max_frames: int = SEGMENT_SECS * FRAME_FPS) -> list[str]:
    """Write decrypted segment bytes to temp file, extract frames with cv2."""
    os.makedirs(out_dir, exist_ok=True)
    saved = []
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(raw_bytes)
        tmp_path = tmp.name
    try:
        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            return []
        src_fps    = cap.get(cv2.CAP_PROP_FPS) or 25
        frame_step = max(1, int(src_fps / FRAME_FPS))
        frame_idx = cap_idx = 0
        while cap_idx < max_frames:
            ret, frame = cap.read()
            if not ret:
                break
            if frame_idx % frame_step == 0:
                fname = os.path.join(out_dir, f"{label}_{cap_idx:02d}.jpg")
                cv2.imwrite(fname, frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
                saved.append(fname)
                cap_idx += 1
            frame_idx += 1
        cap.release()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return saved


# ── public frame extraction API ───────────────────────────────────────────────

def extract_segment(m3u8_url: str, out_dir: str, label: str,
                    start_sec: float, duration_sec: float = SEGMENT_SECS,
                    auth_session: Optional[requests.Session] = None) -> list[str]:
    """
    Fetch, decrypt, and extract frames from an HLS segment window.
    m3u8_url: the signed URL returned by get_m3u8_url()
    """
    if auth_session is None:
        auth_session = requests.Session()

    # Fetch M3U8 manifest
    r = auth_session.get(m3u8_url, timeout=15)
    if not r.ok:
        return []
    manifest = _parse_m3u8(r.text)
    if not manifest["segments"]:
        return []

    # Fetch AES key once
    try:
        aes_key = _fetch_aes_key(manifest["key_url"], auth_session)
    except Exception:
        return []

    iv = manifest["iv"]

    # Download + decrypt only the segments we need
    target_segs = _segments_for_window(manifest["segments"], start_sec, duration_sec)
    if not target_segs:
        # fallback: use all segments if window exceeds manifest
        target_segs = manifest["segments"]

    raw_chunks = []
    for seg in target_segs:
        try:
            raw = _download_decrypt_segment(seg["url"], aes_key, iv, auth_session)
            raw_chunks.append(raw)
        except Exception:
            continue

    if not raw_chunks:
        return []

    combined = b"".join(raw_chunks)
    return _frames_from_raw(combined, label, out_dir,
                             max_frames=int(duration_sec * FRAME_FPS))


def get_video_duration_from_url(m3u8_url: str,
                                 auth_session: Optional[requests.Session] = None) -> Optional[float]:
    """Parse M3U8 manifest to get total video duration in seconds."""
    sess = auth_session or requests.Session()
    try:
        r = sess.get(m3u8_url, timeout=15)
        if r.ok:
            return _parse_m3u8(r.text)["total_duration"] or None
    except Exception:
        pass
    return None


# ── capture pipeline ──────────────────────────────────────────────────────────

def capture_event(state: dict, event: dict, out_base: str) -> CaptureResult:
    """
    Capture frame segments for one event:
      • Thumbnail image (instant)
      • First {SEGMENT_SECS}s of video
      • Last {SEGMENT_SECS}s of video
      • Every {MINUTE_MARK}s interval, {SEGMENT_SECS}s each (for long events)

    Directory layout:
      out_base/
        {fileId}/
          thumbnail.jpg
          first_00.jpg ... first_04.jpg
          last_00.jpg  ... last_04.jpg
          t0060s_00.jpg ...             (1-min intervals)
    """
    file_id   = event["fileId"]
    ts_ms     = event.get("createTime", 0)
    ev_dir    = os.path.join(out_base, file_id)
    os.makedirs(ev_dir, exist_ok=True)

    result = CaptureResult(
        file_id       = file_id,
        event_type    = event.get("eventType", "Unknown"),
        create_time_ms= ts_ms,
        duration_sec  = None,
        m3u8_url      = "",
        thumbnail     = None,
    )

    # Shared authenticated session for all downloads in this event
    http = _make_http_session(state)

    # ── thumbnail ──────────────────────────────────────────────────────────
    thumb_path = os.path.join(ev_dir, "thumbnail.jpg")
    result.thumbnail = download_thumbnail(state, event, thumb_path)

    # ── m3u8 stream ────────────────────────────────────────────────────────
    m3u8_url = get_m3u8_url(state, event)
    result.m3u8_url = m3u8_url

    # ── video duration (from manifest, no download) ────────────────────────
    duration = get_video_duration_from_url(m3u8_url, auth_session=http)
    result.duration_sec = duration or 0

    # ── first segment ──────────────────────────────────────────────────────
    seg_first = EventSegment(label="first", start_sec=0.0)
    seg_first.frames = extract_segment(m3u8_url, ev_dir, "first",
                                        start_sec=0.0, auth_session=http)
    result.segments.append(seg_first)

    # ── last segment (if longer than 2×SEGMENT_SECS) ──────────────────────
    if duration and duration > SEGMENT_SECS * 2:
        last_start = max(0.0, duration - SEGMENT_SECS)
        seg_last   = EventSegment(label="last", start_sec=last_start)
        seg_last.frames = extract_segment(m3u8_url, ev_dir, "last",
                                           start_sec=last_start, auth_session=http)
        result.segments.append(seg_last)

    # ── mid-event intervals (every MINUTE_MARK seconds) ───────────────────
    if duration and duration > MINUTE_MARK:
        mark = float(MINUTE_MARK)
        while mark < duration - SEGMENT_SECS:
            lbl     = f"t{int(mark):04d}s"
            seg_mid = EventSegment(label=lbl, start_sec=mark)
            seg_mid.frames = extract_segment(m3u8_url, ev_dir, lbl,
                                              start_sec=mark, auth_session=http)
            result.segments.append(seg_mid)
            mark += MINUTE_MARK

    return result


def capture_time_range(state: dict,
                        start_ms: int,
                        end_ms:   int,
                        out_base: str = "captures") -> list[CaptureResult]:
    """
    Full pipeline: fetch all events in range → capture each → save metadata.

    Returns list of CaptureResult (all frames stored locally under out_base/).
    Also writes out_base/events.json with full summary.
    """
    os.makedirs(out_base, exist_ok=True)

    print(f"Fetching events {_ms_to_local(start_ms)} → {_ms_to_local(end_ms)} ...")
    events = get_events(state, start_ms, end_ms)
    print(f"Found {len(events)} unique events.\n")

    results: list[CaptureResult] = []
    for i, ev in enumerate(events):
        ts_str = _ms_to_local(ev.get("createTime", 0))
        print(f"  [{i+1}/{len(events)}] {ev.get('eventType')} @ {ts_str}")
        result = capture_event(state, ev, out_base)
        n_frames = len(result.all_frames)
        print(f"    thumbnail: {'✓' if result.thumbnail else '✗'}  "
              f"frames: {n_frames}  "
              f"duration: {result.duration_sec:.1f}s" if result.duration_sec else
              f"    thumbnail: {'✓' if result.thumbnail else '✗'}  frames: {n_frames}")
        if result.error:
            print(f"    warning: {result.error}")
        results.append(result)

    # Save metadata
    meta_path = os.path.join(out_base, "events.json")
    with open(meta_path, "w") as f:
        json.dump([r.summary() for r in results], f, indent=2, ensure_ascii=False)
    print(f"\nMetadata saved → {meta_path}")

    return results


def _ms_to_local(ms: int) -> str:
    if not ms:
        return "N/A"
    dt = datetime.datetime.fromtimestamp(ms / 1000, tz=LOCAL_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
