"""02_restore_exif.py -- the photo governs a Live Photo pair's decision, and
both halves get staged + date-stamped correctly.
"""
from __future__ import annotations

import csv
import subprocess
from pathlib import Path

from conftest import import_script, make_test_image, make_test_video, \
    requires_ffmpeg_exiftool, run_script, write_sidecar

restore_exif = import_script("02_restore_exif.py")


# --------------------------------------------------------------------------- #
# effective_decision() -- photo governs regardless of what the video's own
# cell says, in both directions. This is what makes a hand-edited/partially
# stale decisions.csv unable to orphan one half of a pair.
# --------------------------------------------------------------------------- #
def test_photo_delete_overrides_video_keep() -> None:
    live_pairs = {"IMG.MOV": "IMG.HEIC"}
    decision_by_rel = {"IMG.HEIC": "delete", "IMG.MOV": ""}  # video's cell says keep

    def effective(rel: str) -> str:
        photo_rel = live_pairs.get(rel)
        if photo_rel is not None and photo_rel in decision_by_rel:
            return decision_by_rel[photo_rel]
        return decision_by_rel.get(rel, "")

    assert restore_exif.is_delete(effective("IMG.HEIC"))
    assert restore_exif.is_delete(effective("IMG.MOV"))  # follows the photo, not its own cell


def test_photo_keep_overrides_video_delete() -> None:
    live_pairs = {"IMG.MOV": "IMG.HEIC"}
    decision_by_rel = {"IMG.HEIC": "", "IMG.MOV": "delete"}  # video's cell says delete

    def effective(rel: str) -> str:
        photo_rel = live_pairs.get(rel)
        if photo_rel is not None and photo_rel in decision_by_rel:
            return decision_by_rel[photo_rel]
        return decision_by_rel.get(rel, "")

    assert not restore_exif.is_delete(effective("IMG.HEIC"))
    assert not restore_exif.is_delete(effective("IMG.MOV"))  # follows the photo, not its own cell


# --------------------------------------------------------------------------- #
# End-to-end: real files through the actual script.
# --------------------------------------------------------------------------- #
@requires_ffmpeg_exiftool
def test_end_to_end_pair_staged_and_dated_together(tmp_path: Path) -> None:
    batch = tmp_path / "batch"
    takeout = batch / "takeout" / "Photos"
    photo = takeout / "PXL_0100.JPG"
    video = takeout / "PXL_0100.MP4"
    make_test_image(photo)
    make_test_video(video)
    # 1_000_000_000 epoch seconds = 2001-09-09 (UTC) -- a fixed, recognizable date.
    write_sidecar(photo, "PXL_0100.JPG", timestamp=1_000_000_000)
    # Deliberately NO sidecar for the video -- it must inherit the photo's
    # date via the fallback path in 02_restore_exif.py.

    decisions_csv = batch / "decisions.csv"
    decisions_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(decisions_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rel_path", "filename", "capture_date", "date_source", "size_mb",
                    "is_video", "blur_score", "text_words", "flags", "suggested",
                    "decision"])
        # Photo says keep; video's own cell deliberately says delete, to prove
        # the photo governs even in a real end-to-end run, not just in the
        # effective_decision() unit tests above.
        w.writerow(["Photos/PXL_0100.JPG", "PXL_0100.JPG", "2001-09-09", "sidecar",
                    "0.1", "no", "", "", "", "keep", ""])
        w.writerow(["Photos/PXL_0100.MP4", "PXL_0100.MP4", "", "", "0.1",
                    "yes", "", "", "", "keep", "delete"])

    result = run_script("02_restore_exif.py", str(batch))
    assert result.returncode == 0, result.stderr

    reupload = batch / "reupload" / "Photos"
    kept_photo = reupload / "PXL_0100.JPG"
    kept_video = reupload / "PXL_0100.MP4"
    assert kept_photo.exists(), "photo should be staged (its own decision is keep)"
    assert kept_video.exists(), \
        "video should ALSO be staged -- the photo governs, not the video's own 'delete' cell"

    def exif_date(path: Path, tag: str) -> str:
        out = subprocess.run(["exiftool", f"-{tag}", "-s3", str(path)],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()

    photo_date = exif_date(kept_photo, "DateTimeOriginal")
    video_date = exif_date(kept_video, "CreateDate")
    assert photo_date.startswith("2001:09:09")
    # The video had no sidecar of its own -- it must have inherited the
    # photo's date via the Live-Photo fallback, not been left undated.
    assert video_date.startswith("2001:09:09"), \
        f"video should inherit the photo's date via fallback, got {video_date!r}"


@requires_ffmpeg_exiftool
def test_end_to_end_photo_delete_drops_both(tmp_path: Path) -> None:
    batch = tmp_path / "batch"
    takeout = batch / "takeout" / "Photos"
    photo = takeout / "PXL_0200.JPG"
    video = takeout / "PXL_0200.MP4"
    make_test_image(photo)
    make_test_video(video)
    write_sidecar(photo, "PXL_0200.JPG", timestamp=1_000_000_000)
    write_sidecar(video, "PXL_0200.MP4", timestamp=1_000_000_001)

    decisions_csv = batch / "decisions.csv"
    decisions_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(decisions_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rel_path", "filename", "capture_date", "date_source", "size_mb",
                    "is_video", "blur_score", "text_words", "flags", "suggested",
                    "decision"])
        # Photo says delete; video's own cell deliberately says keep.
        w.writerow(["Photos/PXL_0200.JPG", "PXL_0200.JPG", "2001-09-09", "sidecar",
                    "0.1", "no", "", "", "", "delete", "delete"])
        w.writerow(["Photos/PXL_0200.MP4", "PXL_0200.MP4", "2001-09-09", "sidecar",
                    "0.1", "yes", "", "", "", "keep", ""])

    result = run_script("02_restore_exif.py", str(batch))
    assert result.returncode == 0, result.stderr

    reupload = batch / "reupload" / "Photos"
    assert not (reupload / "PXL_0200.JPG").exists()
    assert not (reupload / "PXL_0200.MP4").exists(), \
        "video should ALSO be dropped -- the photo governs, not the video's own 'keep' cell"
