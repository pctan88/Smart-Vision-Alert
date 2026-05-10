"""
Smart Vision Alert — Image Utilities
Helper functions for image download, resize, encoding, and cleanup.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from datetime import datetime, timedelta
from io import BytesIO

import requests
from PIL import Image

from utils.logger import get_logger

log = get_logger()


def download_image(url: str, save_path: Path, timeout: int = 30) -> bool:
    """
    Download an image from a URL and save to disk.

    Returns True on success, False on failure.
    """
    try:
        resp = requests.get(url, timeout=timeout, stream=True)
        resp.raise_for_status()

        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        log.info(f"Downloaded image: {save_path} ({save_path.stat().st_size / 1024:.1f} KB)")
        return True

    except requests.RequestException as e:
        log.error(f"Failed to download image from {url}: {e}")
        return False


def resize_image(image_path: Path, max_kb: int = 500) -> Path:
    """
    Resize / compress an image to stay under max_kb.
    Saves the resized image in-place.

    Returns the path to the (possibly resized) image.
    """
    file_size_kb = image_path.stat().st_size / 1024

    if file_size_kb <= max_kb:
        return image_path

    log.info(f"Resizing image {image_path.name}: {file_size_kb:.0f}KB → target <{max_kb}KB")

    img = Image.open(image_path)

    # Convert RGBA to RGB if needed (JPEG doesn't support alpha)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # Progressive quality reduction
    quality = 85
    while quality >= 20:
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=quality, optimize=True)
        size_kb = buffer.tell() / 1024

        if size_kb <= max_kb:
            # Write the compressed version
            with open(image_path, "wb") as f:
                f.write(buffer.getvalue())
            log.info(f"Resized to {size_kb:.0f}KB (quality={quality})")
            return image_path

        quality -= 10

    # If still too large, reduce dimensions
    scale = 0.7
    while scale >= 0.3:
        new_size = (int(img.width * scale), int(img.height * scale))
        resized = img.resize(new_size, Image.LANCZOS)

        buffer = BytesIO()
        resized.save(buffer, format="JPEG", quality=60, optimize=True)
        size_kb = buffer.tell() / 1024

        if size_kb <= max_kb:
            with open(image_path, "wb") as f:
                f.write(buffer.getvalue())
            log.info(f"Resized to {new_size}, {size_kb:.0f}KB")
            return image_path

        scale -= 0.1

    log.warning(f"Could not reduce image below {max_kb}KB — using best effort")
    with open(image_path, "wb") as f:
        f.write(buffer.getvalue())

    return image_path


def generate_capture_filename(prefix: str = "capture") -> str:
    """Generate a timestamped filename for a captured image."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}.jpg"


def cleanup_old_captures(captures_dir: Path, retention_days: int = 3):
    """Delete captured images older than retention_days."""
    if not captures_dir.exists():
        return

    cutoff = time.time() - (retention_days * 86400)
    removed = 0

    for f in captures_dir.glob("*.jpg"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1

    for f in captures_dir.glob("*.jpeg"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1

    for f in captures_dir.glob("*.png"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1

    if removed:
        log.info(f"Cleaned up {removed} old capture(s) (>{retention_days} days)")


def get_latest_file(directory: Path, extensions: tuple = (".jpg", ".jpeg", ".png")) -> Path | None:
    """Get the most recently modified image file from a directory."""
    files = [
        f for f in directory.iterdir()
        if f.is_file() and f.suffix.lower() in extensions
    ]

    if not files:
        return None

    return max(files, key=lambda f: f.stat().st_mtime)
