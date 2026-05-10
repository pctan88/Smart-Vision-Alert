"""
Smart Vision Alert — Camera Image Capture Module
Supports multiple image sources:
  - xiaomi_cloud: Pull latest snapshot from Xiaomi Cloud (via token)
  - local_folder: Read latest image from captures/manual/ (for testing / NAS sync)
  - url: Download from a direct snapshot URL
"""

from __future__ import annotations

import hashlib
import json
import time
import hmac
import base64
import os
from pathlib import Path
from datetime import datetime

import requests

from config.settings import Settings
from core.models import CaptureInfo
from utils.logger import get_logger
from utils.image_utils import (
    download_image,
    resize_image,
    generate_capture_filename,
    get_latest_file,
)

log = get_logger()


class CameraCapture:
    """Capture images from configured camera source."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def capture_latest(self) -> CaptureInfo | None:
        """
        Capture the latest image based on configured CAMERA_SOURCE.

        Returns CaptureInfo on success, None on failure.
        """
        source = self.settings.CAMERA_SOURCE.lower()
        log.info(f"Capturing image (source: {source})")

        if source == "local_folder":
            return self._capture_from_folder()
        elif source == "url":
            return self._capture_from_url()
        elif source == "xiaomi_cloud":
            return self._capture_from_xiaomi_cloud()
        else:
            log.error(f"Unknown camera source: {source}")
            return None

    def capture_burst(self, count: int = 3, interval_seconds: int = 5) -> list[CaptureInfo]:
        """
        Capture multiple images in sequence for temporal comparison.

        For local_folder mode: reads the N most recent images from the folder.
        For url/xiaomi_cloud mode: captures N images with interval_seconds delay.

        Args:
            count: Number of frames to capture (default 3).
            interval_seconds: Seconds between captures (default 5).

        Returns:
            List of CaptureInfo in chronological order (oldest first).
        """
        source = self.settings.CAMERA_SOURCE.lower()
        log.info(f"Burst capture: {count} frames, {interval_seconds}s interval (source: {source})")

        captures = []

        if source == "local_folder":
            # For local folder: get the N most recent files
            captures = self._burst_from_folder(count)
        else:
            # For url/xiaomi_cloud: capture sequentially with delays
            for i in range(count):
                if i > 0:
                    log.info(f"  Waiting {interval_seconds}s before frame {i + 1}...")
                    import time as _time
                    _time.sleep(interval_seconds)

                capture = self.capture_latest()
                if capture:
                    captures.append(capture)
                    log.info(f"  Frame {i + 1}/{count} captured: {Path(capture.file_path).name}")
                else:
                    log.warning(f"  Frame {i + 1}/{count} failed — skipping")

        log.info(f"Burst capture complete: {len(captures)}/{count} frames")
        return captures

    def save_to_history(self, capture: CaptureInfo) -> str:
        """
        Save a copy of the captured image to the history directory.
        History is used for comparing with previous captures.

        Returns the history file path.
        """
        import shutil
        history_dir = self.settings.HISTORY_DIR
        history_dir.mkdir(parents=True, exist_ok=True)

        src = Path(capture.file_path)
        dest = history_dir / src.name

        shutil.copy2(src, dest)
        log.debug(f"Saved to history: {dest.name}")
        return str(dest)

    def get_previous_capture(self) -> str | None:
        """
        Get the most recent image from the history directory.
        This represents the previous capture for temporal comparison.

        Returns path to the previous image, or None if no history exists.
        """
        history_dir = self.settings.HISTORY_DIR

        if not history_dir.exists():
            return None

        previous = get_latest_file(history_dir)

        if previous:
            log.info(f"Previous capture found: {previous.name}")
            return str(previous)

        return None

    def get_history_frames(self, count: int = 3) -> list[str]:
        """
        Get the N most recent images from the history directory.

        Args:
            count: Max number of historical frames to retrieve.

        Returns:
            List of file paths in chronological order (oldest first).
        """
        history_dir = self.settings.HISTORY_DIR

        if not history_dir.exists():
            return []

        files = [
            f for f in history_dir.iterdir()
            if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png")
        ]

        if not files:
            return []

        # Sort by modification time, newest last
        files.sort(key=lambda f: f.stat().st_mtime)

        # Take the last N
        recent = files[-count:]
        log.info(f"Retrieved {len(recent)} historical frame(s)")
        return [str(f) for f in recent]

    def cleanup_history(self, max_files: int = 10):
        """
        Keep only the most recent max_files in history to save disk space.
        """
        history_dir = self.settings.HISTORY_DIR

        if not history_dir.exists():
            return

        files = [
            f for f in history_dir.iterdir()
            if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png")
        ]

        if len(files) <= max_files:
            return

        # Sort oldest first, delete excess
        files.sort(key=lambda f: f.stat().st_mtime)
        to_delete = files[: len(files) - max_files]

        for f in to_delete:
            f.unlink()

        log.info(f"History cleanup: removed {len(to_delete)} old frame(s)")

    def _burst_from_folder(self, count: int) -> list[CaptureInfo]:
        """Get the N most recent images from the manual folder for burst analysis."""
        manual_dir = self.settings.MANUAL_DIR

        if not manual_dir.exists():
            return []

        files = [
            f for f in manual_dir.iterdir()
            if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png")
        ]

        if not files:
            return []

        # Sort by modification time (oldest first)
        files.sort(key=lambda f: f.stat().st_mtime)

        # Take the last N files
        recent = files[-count:]

        captures = []
        for f in recent:
            # Copy to captures dir with timestamp
            dest_name = generate_capture_filename(f"burst_{len(captures)}")
            dest = self.settings.CAPTURES_DIR / dest_name

            import shutil
            shutil.copy2(f, dest)
            resize_image(dest, self.settings.IMAGE_MAX_SIZE_KB)

            captures.append(CaptureInfo(
                file_path=str(dest),
                source="local_folder",
                file_size_kb=dest.stat().st_size / 1024,
            ))

        return captures

    # ── Strategy 1: Local Folder (Testing / NAS Sync) ─────────
    def _capture_from_folder(self) -> CaptureInfo | None:
        """Read the latest image from captures/manual/ directory."""
        manual_dir = self.settings.MANUAL_DIR

        if not manual_dir.exists():
            manual_dir.mkdir(parents=True, exist_ok=True)
            log.warning(f"Manual capture folder created: {manual_dir}")
            log.warning("Place test images in this folder to use local_folder mode.")
            return None

        latest = get_latest_file(manual_dir)

        if latest is None:
            log.warning(f"No images found in {manual_dir}")
            return None

        # Copy to captures/ with timestamp name
        dest_name = generate_capture_filename("local")
        dest = self.settings.CAPTURES_DIR / dest_name

        # Just reference the file directly (avoid unnecessary copy for local testing)
        import shutil
        shutil.copy2(latest, dest)

        # Resize if needed
        resize_image(dest, self.settings.IMAGE_MAX_SIZE_KB)

        return CaptureInfo(
            file_path=str(dest),
            source="local_folder",
            file_size_kb=dest.stat().st_size / 1024,
        )

    # ── Strategy 2: Direct URL ────────────────────────────────
    def _capture_from_url(self) -> CaptureInfo | None:
        """Download image from a direct snapshot URL."""
        url = self.settings.CAMERA_SNAPSHOT_URL

        if not url:
            log.error("CAMERA_SNAPSHOT_URL is not configured")
            return None

        dest_name = generate_capture_filename("url")
        dest = self.settings.CAPTURES_DIR / dest_name

        if download_image(url, dest):
            resize_image(dest, self.settings.IMAGE_MAX_SIZE_KB)
            return CaptureInfo(
                file_path=str(dest),
                source="url",
                file_size_kb=dest.stat().st_size / 1024,
            )

        return None

    # ── Strategy 3: Xiaomi Cloud API ──────────────────────────
    def _capture_from_xiaomi_cloud(self) -> CaptureInfo | None:
        """
        Pull latest camera snapshot from Xiaomi Cloud.

        This uses the Xiaomi Cloud private API. The authentication flow:
        1. Login with username/password → get serviceToken + cookies
        2. Call the camera cloud API to list recent recordings
        3. Download the latest thumbnail/snapshot

        Note: This is an unofficial API and may break if Xiaomi changes it.
        """
        try:
            session = self._xiaomi_login()
            if session is None:
                return None

            # Get device list to find camera
            devices = self._get_xiaomi_devices(session)
            if not devices:
                log.error("No devices found in Xiaomi Cloud")
                return None

            # Find camera device (filter by type or name)
            camera = None
            for device in devices:
                name = device.get("name", "").lower()
                model = device.get("model", "").lower()
                # Common Xiaomi camera model prefixes
                if any(kw in model for kw in ["camera", "chuangmi", "isa", "imi"]):
                    camera = device
                    break
                if any(kw in name for kw in ["camera", "cam", "cctv"]):
                    camera = device
                    break

            if camera is None:
                # Fall back to first device if no camera keyword match
                log.warning("Could not auto-detect camera device, listing all devices:")
                for d in devices:
                    log.info(f"  - {d.get('name', 'N/A')} (model: {d.get('model', 'N/A')}, did: {d.get('did', 'N/A')})")
                log.warning("Using first device as fallback")
                camera = devices[0]

            log.info(f"Using camera: {camera.get('name')} (model: {camera.get('model')})")

            # Try to get a snapshot/thumbnail from cloud storage
            snapshot_url = self._get_latest_snapshot_url(session, camera)
            if snapshot_url is None:
                log.error("Could not retrieve snapshot URL from Xiaomi Cloud")
                return None

            # Download the snapshot
            dest_name = generate_capture_filename("xiaomi")
            dest = self.settings.CAPTURES_DIR / dest_name

            if download_image(snapshot_url, dest):
                resize_image(dest, self.settings.IMAGE_MAX_SIZE_KB)
                return CaptureInfo(
                    file_path=str(dest),
                    source="xiaomi_cloud",
                    file_size_kb=dest.stat().st_size / 1024,
                )

            return None

        except Exception as e:
            log.error(f"Xiaomi Cloud capture failed: {e}", exc_info=True)
            return None

    def _xiaomi_login(self) -> requests.Session | None:
        """Login to Xiaomi Cloud and return an authenticated session."""
        username = self.settings.XIAOMI_USERNAME
        password = self.settings.XIAOMI_PASSWORD
        region = self.settings.XIAOMI_SERVER_REGION

        if not username or not password:
            log.error("Xiaomi credentials not configured")
            return None

        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Android-7.1.1-1.0.0-ONEPLUS A3010-136-"
                "ABPeerAppMi498-release/2.3.1.092300-"
                "SDK-22-WiFi-1"
            )
        })

        try:
            # Step 1: Get sign (login form)
            sign_url = "https://account.xiaomi.com/pass/serviceLogin"
            params = {"sid": "xiaomiio", "_json": "true"}
            resp = session.get(sign_url, params=params)
            resp.raise_for_status()

            # Parse the _sign value from response
            data_text = resp.text.replace("&&&START&&&", "")
            sign_data = json.loads(data_text)
            _sign = sign_data.get("_sign", "")

            # Step 2: Login with credentials
            login_url = "https://account.xiaomi.com/pass/serviceLoginAuth2"
            login_data = {
                "sid": "xiaomiio",
                "hash": hashlib.md5(password.encode()).hexdigest().upper(),
                "callback": "https://sts.api.io.mi.com/sts",
                "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
                "user": username,
                "_sign": _sign,
                "_json": "true",
            }

            resp = session.post(login_url, data=login_data)
            resp.raise_for_status()

            login_text = resp.text.replace("&&&START&&&", "")
            login_result = json.loads(login_text)

            if "location" not in login_result:
                log.error(f"Xiaomi login failed: {login_result.get('desc', 'Unknown error')}")
                return None

            # Step 3: Follow redirect to get service token
            location = login_result["location"]
            resp = session.get(location)
            resp.raise_for_status()

            # Extract serviceToken from cookies
            service_token = session.cookies.get("serviceToken")
            if not service_token:
                log.error("Failed to obtain serviceToken from Xiaomi")
                return None

            # Set the correct API base URL for the region
            if region == "cn":
                session.base_url = "https://api.io.mi.com/app"
            else:
                session.base_url = f"https://{region}.api.io.mi.com/app"

            log.info("Xiaomi Cloud login successful")
            return session

        except Exception as e:
            log.error(f"Xiaomi login error: {e}", exc_info=True)
            return None

    def _get_xiaomi_devices(self, session: requests.Session) -> list:
        """Get list of devices from Xiaomi Cloud."""
        try:
            url = f"{session.base_url}/home/device_list"
            data = {"getVirtualModel": False, "getHuamiDevices": 0}

            resp = session.post(
                url,
                data={"data": json.dumps(data)},
            )
            resp.raise_for_status()

            result = resp.json()
            devices = result.get("result", {}).get("list", [])
            log.info(f"Found {len(devices)} device(s) in Xiaomi Cloud")
            return devices

        except Exception as e:
            log.error(f"Failed to get device list: {e}")
            return []

    def _get_latest_snapshot_url(self, session: requests.Session, camera: dict) -> str | None:
        """
        Attempt to get the latest snapshot/thumbnail URL from Xiaomi Cloud camera storage.

        This tries the cloud file storage API to find recent recordings with thumbnails.
        """
        try:
            did = camera.get("did", "")

            # Try to get file list from camera cloud storage
            url = f"{session.base_url}/v2/homeroom/sub_device_file_list"
            end_time = int(time.time())
            start_time = end_time - 3600  # Last hour

            data = {
                "did": did,
                "start_time": start_time,
                "end_time": end_time,
                "limit": 1,
                "type": "image",
            }

            resp = session.post(
                url,
                data={"data": json.dumps(data)},
            )

            if resp.status_code == 200:
                result = resp.json()
                files = result.get("result", {}).get("list", [])
                if files:
                    file_url = files[0].get("url") or files[0].get("img_url")
                    if file_url:
                        log.info(f"Got snapshot URL from cloud storage")
                        return file_url

            # Alternative: try camera event API
            url2 = f"{session.base_url}/v2/homeroom/sub_device_event_list"
            data2 = {
                "did": did,
                "start_time": start_time,
                "end_time": end_time,
                "limit": 1,
            }

            resp2 = session.post(
                url2,
                data={"data": json.dumps(data2)},
            )

            if resp2.status_code == 200:
                result2 = resp2.json()
                events = result2.get("result", {}).get("list", [])
                if events:
                    # Events often have thumbnail/snapshot URLs
                    for event in events:
                        img_url = event.get("imgUrl") or event.get("img_url") or event.get("thumbnail")
                        if img_url:
                            log.info("Got snapshot URL from event API")
                            return img_url

            log.warning("No recent snapshots found in Xiaomi Cloud")
            return None

        except Exception as e:
            log.error(f"Failed to get snapshot URL: {e}", exc_info=True)
            return None
