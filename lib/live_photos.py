"""Live Photo pairing.

An iPhone Live Photo exports from Takeout as two files representing one
capture -- e.g. IMG_1234.HEIC + IMG_1234.MOV -- not two independent items.
Treated independently, two things go wrong: deleting one orphans the other,
and the lone video half pollutes video-burst/similarity clustering with a
flood of short, visually-similar clips that were never meant to be compared
against each other in the first place.

This module only detects pairs; it doesn't decide anything. Callers (the
analyzer and the EXIF-restore step) decide what to do with that information.
"""
from __future__ import annotations

from pathlib import Path

from lib.sidecars import IMAGE_EXTS, VIDEO_EXTS, reduce_stem


def find_live_photo_pairs(root: Path, media_files: list[Path]) -> dict[str, str]:
    """Return {video_rel: photo_rel} for every detected Live Photo pair.

    Keys and values are POSIX-ish path strings relative to `root`
    (str(path.relative_to(root))) -- the same representation used for
    record["rel"] in 01_analyze.py and the rel_path column in
    decisions.csv, so callers can look pairs up directly without any path
    conversion.

    Pairing rule: same directory, same reduced stem (Google's "-edited"/
    "(n)" suffix variants stripped -- see lib.sidecars.reduce_stem, the
    exact same normalization sidecar-title matching already uses), exactly
    one image extension and exactly one video extension sharing that stem.

    Deliberately narrow:
      - Same stem in a DIFFERENT directory is not a pair. That's a naming
        coincidence, not a Live Photo.
      - If more than one image or more than one video shares a (directory,
        reduced-stem) bucket, nothing in that bucket is paired -- guessing
        which file goes with which risks pairing the wrong two, which is
        worse than not pairing at all.
    """
    by_dir_stem: dict[tuple[Path, str], list[Path]] = {}
    for p in media_files:
        ext = p.suffix.lower()
        if ext not in IMAGE_EXTS and ext not in VIDEO_EXTS:
            continue
        key = (p.parent, reduce_stem(p.stem))
        by_dir_stem.setdefault(key, []).append(p)

    pairs: dict[str, str] = {}
    for files in by_dir_stem.values():
        images = [f for f in files if f.suffix.lower() in IMAGE_EXTS]
        videos = [f for f in files if f.suffix.lower() in VIDEO_EXTS]
        if len(images) == 1 and len(videos) == 1:
            video_rel = str(videos[0].relative_to(root))
            photo_rel = str(images[0].relative_to(root))
            pairs[video_rel] = photo_rel
    return pairs
