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


def text_word_count(path: Path, max_px: int = 1000) -> int | None:
    """Count recognizable words via local OCR (tesseract) -- a deterministic,
    no-model-download way to flag "low sentimental value" photos: photographed
    book/recipe pages, screenshots of text, receipts, documents, whiteboards.
    Runs fully on-device; returns None if tesseract isn't installed or the
    image can't be read (caller should treat that as "unknown", not "zero").

    The image is downscaled before OCR purely for speed -- word-count is a
    coarse signal, not a transcription, so full resolution isn't needed.
    """
    if not shutil.which("tesseract"):
        return None
    tmp_path = None
    try:
        with _open_oriented(path) as im:
            im = im.convert("RGB")
            im.thumbnail((max_px, max_px))
            fd, tmp_path = tempfile.mkstemp(suffix=".jpg")
            os.close(fd)
            im.save(tmp_path, format="JPEG", quality=85)
        proc = subprocess.run(
            ["tesseract", tmp_path, "stdout"],
            capture_output=True, text=True, timeout=30,
        )
        words = [w for w in proc.stdout.split() if sum(c.isalpha() for c in w) >= 3]
        return len(words)
    except Exception:
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


_FORMAT_EXTS = {
    "JPEG": {".jpg", ".jpeg"},
    "PNG": {".png"},
    "GIF": {".gif"},
    "BMP": {".bmp"},
    "TIFF": {".tiff", ".tif"},
    "WEBP": {".webp"},
    "HEIF": {".heic", ".heif"},
}
_CANONICAL_EXT = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "BMP": ".bmp",
                  "TIFF": ".tiff", "WEBP": ".webp", "HEIF": ".heic"}


def fix_mislabeled_extension(path: Path) -> Path:
    """Rename a file if its real image format doesn't match its extension.

    Older exports (e.g. Google Hangouts screenshots) sometimes save JPEG
    bytes with a .png filename. Left alone this breaks two things
    downstream: exiftool refuses to write EXIF-style date tags into a file
    whose declared type doesn't match its content, and the upload step
    infers the wrong MIME type from the filename. Detects the real format
    from content (not the extension) and renames to match. Returns the
    (possibly unchanged) path.
    """
    try:
        with Image.open(path) as im:
            fmt = im.format
    except Exception:
        return path
    valid_exts = _FORMAT_EXTS.get(fmt)
    if not valid_exts or path.suffix.lower() in valid_exts:
        return path
    new_path = path.with_suffix(_CANONICAL_EXT[fmt])
    n = 1
    while new_path.exists():
        new_path = path.with_name(f"{path.stem}_{n}{_CANONICAL_EXT[fmt]}")
        n += 1
    path.rename(new_path)
    return new_path
