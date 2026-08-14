"""lib.live_photos.find_live_photo_pairs -- the six required fixture cases."""
from __future__ import annotations

from pathlib import Path

from lib.live_photos import find_live_photo_pairs
from lib.sidecars import iter_media


def _pairs(root: Path) -> dict[str, str]:
    return find_live_photo_pairs(root, list(iter_media(root)))


def test_clean_pair(tmp_path: Path) -> None:
    (tmp_path / "IMG_0001.HEIC").touch()
    (tmp_path / "IMG_0001.MOV").touch()
    pairs = _pairs(tmp_path)
    assert pairs == {"IMG_0001.MOV": "IMG_0001.HEIC"}


def test_standalone_video_no_matching_image(tmp_path: Path) -> None:
    (tmp_path / "VID_LONE.MOV").touch()
    assert _pairs(tmp_path) == {}


def test_standalone_image_no_matching_video(tmp_path: Path) -> None:
    (tmp_path / "IMG_LONE.HEIC").touch()
    assert _pairs(tmp_path) == {}


def test_pair_with_takeout_suffix_variants(tmp_path: Path) -> None:
    # Google Takeout renames the photo half with "-edited" on export but
    # leaves the video half plain -- reduce_stem must line these back up.
    (tmp_path / "IMG_0002-edited.HEIC").touch()
    (tmp_path / "IMG_0002.MOV").touch()
    pairs = _pairs(tmp_path)
    assert pairs == {"IMG_0002.MOV": "IMG_0002-edited.HEIC"}


def test_same_stem_different_directories_does_not_pair(tmp_path: Path) -> None:
    (tmp_path / "dirA").mkdir()
    (tmp_path / "dirB").mkdir()
    (tmp_path / "dirA" / "IMG_0003.HEIC").touch()
    (tmp_path / "dirB" / "IMG_0003.MOV").touch()
    assert _pairs(tmp_path) == {}


def test_android_style_jpg_mp4_pair(tmp_path: Path) -> None:
    (tmp_path / "PXL_0004.JPG").touch()
    (tmp_path / "PXL_0004.MP4").touch()
    pairs = _pairs(tmp_path)
    assert pairs == {"PXL_0004.MP4": "PXL_0004.JPG"}


def test_multiple_files_sharing_a_stem_bucket_do_not_pair(tmp_path: Path) -> None:
    # Two images + one video sharing a (dir, stem) bucket is ambiguous --
    # guessing which image goes with the video is worse than not pairing.
    (tmp_path / "IMG_0005.HEIC").touch()
    (tmp_path / "IMG_0005(1).HEIC").touch()
    (tmp_path / "IMG_0005.MOV").touch()
    assert _pairs(tmp_path) == {}


def test_mixed_batch_all_cases_together(tmp_path: Path) -> None:
    """Every case from above, all present at once, none interfering."""
    (tmp_path / "IMG_0001.HEIC").touch()
    (tmp_path / "IMG_0001.MOV").touch()
    (tmp_path / "VID_LONE.MOV").touch()
    (tmp_path / "IMG_LONE.HEIC").touch()
    (tmp_path / "IMG_0002-edited.HEIC").touch()
    (tmp_path / "IMG_0002.MOV").touch()
    (tmp_path / "dirA").mkdir()
    (tmp_path / "dirB").mkdir()
    (tmp_path / "dirA" / "IMG_0003.HEIC").touch()
    (tmp_path / "dirB" / "IMG_0003.MOV").touch()
    (tmp_path / "PXL_0004.JPG").touch()
    (tmp_path / "PXL_0004.MP4").touch()

    pairs = _pairs(tmp_path)
    assert pairs == {
        "IMG_0001.MOV": "IMG_0001.HEIC",
        "IMG_0002.MOV": "IMG_0002-edited.HEIC",
        "PXL_0004.MP4": "PXL_0004.JPG",
    }
