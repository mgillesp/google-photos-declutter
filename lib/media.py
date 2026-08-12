"""Media helpers: HEIC-aware loading, blur scoring, and thumbnail data URIs.

iPhone originals are frequently HEIC, which raw OpenCV cannot decode. We register
pillow-heif so Pillow can open HEIC, decode to a numpy array, and hand that to
OpenCV for the Laplacian-variance blur metric. Thumbnails are emitted as inline
base64 JPEG data URIs so the review sheet is fully self-contained and offline.
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    _HEIF_OK = True
except Exception:  # pragma: no cover
    _HEIF_OK = False

try:
    import cv2

    _CV2_OK = True
except Exception:  # pragma: no cover
    _CV2_OK = False


def heif_available() -> bool:
    return _HEIF_OK


def _open_oriented(path: Path) -> Image.Image:
    """Open an image and apply EXIF orientation so thumbnails aren't sideways."""
    im = Image.open(path)
    return ImageOps.exif_transpose(im)


def blur_score(path: Path) -> float | None:
    """Laplacian variance (sharpness). Lower = blurrier. None if undecodable."""
    if not _CV2_OK:
        raise RuntimeError("opencv (cv2) is not installed; run: pip3 install -r requirements.txt")
    try:
        with _open_oriented(path) as im:
            gray = np.asarray(im.convert("L"))
    except Exception:
        return None
    if gray.size == 0:
        return None
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def image_thumb_data_uri(path: Path, max_px: int) -> str | None:
    try:
        with _open_oriented(path) as im:
            im = im.convert("RGB")
            im.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=80)
    except Exception:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def video_thumb_data_uri(path: Path, max_px: int) -> str | None:
    """Grab a frame ~1s in via ffmpeg and return it as a data URI (or None)."""
    if not shutil.which("ffmpeg"):
        return None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "1", "-i", str(path), "-frames:v", "1", tmp_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True,
        )
        with Image.open(tmp_path) as im:
            im = im.convert("RGB")
            im.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=80)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def exif_datetime(path: Path) -> datetime | None:
    """Fallback capture date read straight from the file's own EXIF/QuickTime tags.

    Used when a Takeout JSON sidecar isn't available (e.g. a plain "Download"
    from the Photos web UI instead of a Takeout export) but the file already
    carries a real camera/phone timestamp. Sidecars remain the authoritative
    source when present -- callers should try index.capture_dt() first.
    """
    if not shutil.which("exiftool"):
        return None
    try:
        proc = subprocess.run(
            ["exiftool", "-j", "-DateTimeOriginal", "-CreateDate", "-MediaCreateDate",
             str(path)],
            capture_output=True, text=True, timeout=30,
        )
        rows = json.loads(proc.stdout)
        data = rows[0] if rows else {}
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, IndexError):
        return None
    for tag in ("DateTimeOriginal", "CreateDate", "MediaCreateDate"):
        val = data.get(tag)
        if not isinstance(val, str) or len(val) < 19:
            continue
        try:
            return datetime.strptime(val[:19], "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
