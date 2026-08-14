"""01_analyze.py -- a Live Photo's video half must never appear in the
video-burst / similar-video candidate groups. Left in, the flood of short,
visually-similar clips a Live Photo's .MOV produces pollutes those groups
with noise that was never meant to be compared against anything.
"""
from __future__ import annotations

import csv
from pathlib import Path

from conftest import make_test_image, make_test_video, requires_full_toolchain, \
    run_script, write_sidecar


def _flags_by_rel(decisions_csv: Path) -> dict[str, set[str]]:
    with open(decisions_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {r["rel_path"]: set(r["flags"].split(";")) if r["flags"] else set()
            for r in rows}


@requires_full_toolchain
def test_live_photo_video_excluded_from_video_burst(tmp_path: Path) -> None:
    batch = tmp_path / "batch"
    photos = batch / "takeout" / "Photos"

    # Live Photo pair: image + video sharing a stem. This video must NEVER
    # get a vburst/vsim flag no matter how close its timestamp is to
    # anything else.
    lp_photo = photos / "PXL_LP.JPG"
    lp_video = photos / "PXL_LP.MP4"
    make_test_image(lp_photo)
    make_test_video(lp_video, duration=0.5)
    write_sidecar(lp_photo, "PXL_LP.JPG", timestamp=1_000_000_000)
    write_sidecar(lp_video, "PXL_LP.MP4", timestamp=1_000_000_000)

    # Two independent videos, recorded back-to-back (1s apart, well inside
    # the default 20s video_burst_window_seconds) with no image partner --
    # these SHOULD cluster into a vburst group, proving the fixture actually
    # exercises burst detection and the negative result above isn't vacuous.
    vid_a = photos / "VID_A.MP4"
    vid_b = photos / "VID_B.MP4"
    make_test_video(vid_a, duration=0.5, color="blue")
    make_test_video(vid_b, duration=0.5, color="green")
    write_sidecar(vid_a, "VID_A.MP4", timestamp=2_000_000_000)
    write_sidecar(vid_b, "VID_B.MP4", timestamp=2_000_000_001)

    result = run_script("01_analyze.py", str(batch))
    assert result.returncode == 0, result.stderr

    decisions_csv = batch / "decisions.csv"
    assert decisions_csv.exists(), result.stdout

    flags = _flags_by_rel(decisions_csv)
    lp_video_flags = flags["Photos/PXL_LP.MP4"]
    assert not any(f.startswith("vburst") or f.startswith("vsim") for f in lp_video_flags), \
        f"Live Photo video must be excluded from video-dedup passes, got flags: {lp_video_flags}"

    vid_a_flags = flags["Photos/VID_A.MP4"]
    vid_b_flags = flags["Photos/VID_B.MP4"]
    assert any(f.startswith("vburst") for f in vid_a_flags), \
        f"fixture sanity check failed: VID_A should have clustered into a vburst group, got {vid_a_flags}"
    assert any(f.startswith("vburst") for f in vid_b_flags), \
        f"fixture sanity check failed: VID_B should have clustered into a vburst group, got {vid_b_flags}"
