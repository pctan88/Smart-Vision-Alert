"""
Test event fetching + frame capture for shared studio cameras.
Uses config/.micloud_session_new (QR-logged-in account, SG region).

Run:
  python3 test_studio_cam.py
"""

import json
import os
import pickle
import datetime
import time

from zoneinfo import ZoneInfo
from xiaomi_capture import (
    _camera_api, _make_http_session, _build_camera_enc_params, _camera_api_url,
    _silent_refresh, _save_session, extract_segment, get_video_duration_from_url,
    _parse_m3u8, _fetch_aes_key, _download_decrypt_segment, _frames_from_raw,
    _segments_for_window, SEGMENT_SECS, FRAME_FPS,
)

# ── studio cam config ──────────────────────────────────────────────────────────
SESSION_FILE  = "config/.micloud_session_new"
CAMERA_HOST   = "sg.business.smartcamera.api.io.mi.com"
LOCAL_TZ      = ZoneInfo("Asia/Kuala_Lumpur")

CAMERAS = [
    {"name": "C300 #1",  "did": "1066815174", "model": "xiaomi.camera.c01a01"},
    {"name": "C300 #2",  "did": "1066840805", "model": "xiaomi.camera.c01a01"},
]


def load_session() -> dict | None:
    if not os.path.exists(SESSION_FILE):
        print(f"Session file not found: {SESSION_FILE}")
        return None
    with open(SESSION_FILE, "rb") as f:
        return pickle.load(f)


def save_session(state: dict):
    with open(SESSION_FILE, "wb") as f:
        pickle.dump(state, f)


def validate_session(state: dict) -> bool:
    """Try silent refresh if needed."""
    refreshed = _silent_refresh(state)
    if refreshed:
        save_session(refreshed)
        return True
    return bool(state.get("service_token"))


def get_events(state: dict, cam: dict, start_ms: int, end_ms: int, limit: int = 50) -> list[dict]:
    """Fetch events for a single camera."""
    all_events, seen, cursor_end = [], set(), end_ms
    while True:
        resp = _camera_api(state, CAMERA_HOST, "common/app/get/eventlist", {
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

    # If no events via eventlist, show raw response for debugging
    if not all_events and not units:
        print(f"    [debug] raw response: {json.dumps(resp)[:300]}")

    return all_events


def get_m3u8_url(state: dict, cam: dict, event: dict) -> str:
    return _camera_api_url(state, CAMERA_HOST, "common/app/m3u8", {
        "did":        cam["did"],
        "model":      cam["model"],
        "fileId":     event["fileId"],
        "isAlarm":    event.get("isAlarm", False),
        "videoCodec": "H264",
        "region":     "CN",
    })


def capture_event(state: dict, cam: dict, event: dict, out_base: str) -> dict:
    """
    Full capture for one event (mirrors xiaomi_capture logic):
      • first 5s
      • last 5s  (if duration > 10s)
      • every 60s interval (if duration > 60s)
    Returns summary dict.
    """
    fid    = event["fileId"]
    ev_dir = os.path.join(out_base, cam["did"], fid)
    os.makedirs(ev_dir, exist_ok=True)

    http     = _make_http_session(state)
    m3u8_url = get_m3u8_url(state, cam, event)
    duration = get_video_duration_from_url(m3u8_url, auth_session=http) or 0

    segments_captured = []

    # ── first 5s ──────────────────────────────────────────────────────────
    frames = extract_segment(m3u8_url, ev_dir, "first", start_sec=0.0, auth_session=http)
    if frames:
        segments_captured.append({"label": "first", "frames": frames})

    # ── last 5s ───────────────────────────────────────────────────────────
    if duration > SEGMENT_SECS * 2:
        last_start = max(0.0, duration - SEGMENT_SECS)
        frames = extract_segment(m3u8_url, ev_dir, "last", start_sec=last_start, auth_session=http)
        if frames:
            segments_captured.append({"label": "last", "frames": frames})

    # ── every 60s interval ────────────────────────────────────────────────
    if duration > 60:
        mark = 60.0
        while mark < duration - SEGMENT_SECS:
            lbl    = f"t{int(mark):04d}s"
            frames = extract_segment(m3u8_url, ev_dir, lbl, start_sec=mark, auth_session=http)
            if frames:
                segments_captured.append({"label": lbl, "frames": frames})
            mark += 60.0

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


def ms_to_local(ms: int) -> str:
    if not ms:
        return "N/A"
    return datetime.datetime.fromtimestamp(ms / 1000, tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def today_range_ms() -> tuple[int, int]:
    """Return today 00:00 → now in ms (local time)."""
    now   = datetime.datetime.now(LOCAL_TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000), int(now.timestamp() * 1000)


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    state = load_session()
    if not state:
        print("No session. Run python3 login_qr.py first.")
        return

    print(f"Session loaded. userId={state['user_id']}")
    validate_session(state)

    start_ms, end_ms = today_range_ms()
    start_str = ms_to_local(start_ms)
    end_str   = ms_to_local(end_ms)
    print(f"Fetching today's events: {start_str} → {end_str}\n")

    out_base = "captures/studio"
    os.makedirs(out_base, exist_ok=True)

    for cam in CAMERAS:
        print(f"── {cam['name']} (did={cam['did']}) ────────────────────────")
        events = get_events(state, cam, start_ms, end_ms)
        print(f"   Found {len(events)} event(s)")

        if not events:
            print()
            continue

        # Save event list
        cam_dir = os.path.join(out_base, cam["did"])
        os.makedirs(cam_dir, exist_ok=True)
        with open(os.path.join(cam_dir, "events.json"), "w") as f:
            json.dump(events, f, indent=2, ensure_ascii=False)
        print(f"   Saved events.json → {cam_dir}/events.json")

        # Full capture for each event
        results = []
        for i, ev in enumerate(events):
            ts    = ms_to_local(ev.get("createTime", 0))
            etype = ev.get("eventType", "?")
            fid   = ev.get("fileId", "?")
            print(f"\n   [{i+1}/{len(events)}] {etype} @ {ts}  fileId={fid}")

            r = capture_event(state, cam, ev, out_base)
            print(f"       duration: {r['duration']:.1f}s  frames: {r['frames']}  dir: {r['dir']}")
            for seg in r["segments"]:
                print(f"         {seg['label']}: {len(seg['frames'])} frame(s)")
            results.append(r)

        # Save summary
        summary_path = os.path.join(cam_dir, "summary.json")
        with open(summary_path, "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\n   Summary saved → {summary_path}")
        print()

    print("Done. Check captures/studio/ for frames.")


if __name__ == "__main__":
    main()
