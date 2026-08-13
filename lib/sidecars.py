"""Resolve authoritative capture dates from Google Takeout JSON sidecars.

Google Takeout ships a JSON sidecar alongside (most) media files carrying the true
capture date in `photoTakenTime.timestamp` (epoch seconds, UTC). This is more
reliable than EXIF because screenshots, WhatsApp images, etc. often lack real EXIF.

Matching sidecars to media is notoriously messy:
  * Sidecar filename may be `<media>.json` OR (2024+) `<media>.supplemental-metadata.json`.
  * Google truncates long sidecar *filenames* (~46-51 chars), and the
    ".supplemental-metadata" part is itself sometimes truncated.
  * Duplicate media get a `(1)` index whose placement in the sidecar filename does
    NOT mirror the media filename.

The robust trick used here: the sidecar *filename* may be mangled, but the JSON
*contents* carry a clean `title` field = the original media filename. So we index by
(parent_dir, title) read from inside each JSON, which is immune to filename truncation
and `(n)` placement quirks. Duplicate media of the same original share a capture date
anyway, so any residual ambiguity between `IMG.JPG` and `IMG(1).JPG` is harmless.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

# Media extensions we care about resolving dates for.
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp",
    ".heic", ".heif", ".dng", ".raw", ".cr2", ".nef",
}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

_EDITED_SUFFIX = re.compile(r"-(edited|edit|bearbeitet|modifié)$", re.IGNORECASE)


class SidecarIndex:
    """Index of Takeout sidecars, matchable to media files."""

    def __init__(self) -> None:
        # (parent_dir_str, title_lower) -> epoch_seconds
        self._by_dir_title: dict[tuple[str, str], int] = {}
        # title_lower -> epoch_seconds  (global fallback, last write wins)
        self._by_title: dict[str, int] = {}
        self.count = 0

    def add(self, parent: Path, title: str, epoch: int) -> None:
        key = (str(parent), title.lower())
        self._by_dir_title.setdefault(key, epoch)
        self._by_title.setdefault(title.lower(), epoch)
        self.count += 1

    def lookup(self, media_path: Path) -> int | None:
        """Return epoch seconds for a media file, or None if unresolved."""
        parent = media_path.parent
        for name in _title_candidates(media_path.name):
            hit = self._by_dir_title.get((str(parent), name.lower()))
            if hit is not None:
                return hit
        # Global fallback (different folder / heavily reorganized export).
        for name in _title_candidates(media_path.name):
            hit = self._by_title.get(name.lower())
            if hit is not None:
                return hit
        return None

    def capture_dt(self, media_path: Path) -> datetime | None:
        epoch = self.lookup(media_path)
        if epoch is None:
            return None
        return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _title_candidates(media_name: str) -> list[str]:
    """Original-name variants to try matching against sidecar `title` fields."""
    cands = [media_name]
    stem, dot, ext = media_name.rpartition(".")
    if dot:
        # Iteratively strip Google's "-edited" suffix and a trailing "(n)"
        # duplicate index, in either order/combination, so a COMPOUND suffix
        # like "-edited(1)" fully reduces to the original base name instead
        # of stopping after only one stripping pass:
        #   IMG_1234-edited(1) -> IMG_1234-edited -> IMG_1234
        base = stem
        changed = True
        while changed:
            changed = False
            new_base = _EDITED_SUFFIX.sub("", base)
            if new_base != base:
                base = new_base
                cands.append(f"{base}.{ext}")
                changed = True
            m = re.match(r"^(.*)\(\d+\)$", base)
            if m and m.group(1) != base:
                base = m.group(1)
                cands.append(f"{base}.{ext}")
                changed = True
    # De-dupe preserving order
    seen: set[str] = set()
    out = []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _extract_epoch(data: dict) -> int | None:
    """Pull capture epoch seconds from a parsed sidecar dict."""
    for key in ("photoTakenTime", "creationTime"):
        node = data.get(key)
        if isinstance(node, dict):
            ts = node.get("timestamp")
            if ts is not None:
                try:
                    return int(ts)
                except (TypeError, ValueError):
                    pass
    return None


def build_index(root: Path) -> SidecarIndex:
    """Walk `root`, parse every Photos JSON sidecar, and index by inner `title`."""
    index = SidecarIndex()
    for jpath in root.rglob("*.json"):
        try:
            with open(jpath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        epoch = _extract_epoch(data)
        if epoch is None:
            continue  # album/settings JSON, not a media sidecar
        # Prefer the inner title; fall back to deriving from the sidecar filename.
        title = data.get("title")
        if not title:
            title = _title_from_sidecar_name(jpath.name)
        if title:
            index.add(jpath.parent, str(title), epoch)
    return index


def _title_from_sidecar_name(sidecar_name: str) -> str | None:
    """Best-effort media title from a sidecar filename when JSON lacks `title`.

    e.g. 'IMG_1234.JPG.supplemental-metadata.json' -> 'IMG_1234.JPG'
         'IMG_1234.JPG.json'                        -> 'IMG_1234.JPG'
    """
    name = sidecar_name
    if name.endswith(".json"):
        name = name[: -len(".json")]
    # Drop a trailing ".supplemental-metadata" (possibly truncated) segment.
    name = re.sub(r"\.supplemental-me\w*$", "", name)
    return name or None


def iter_media(root: Path):
    """Yield every media file under root (images + videos), skipping sidecars."""
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
            yield p
