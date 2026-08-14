"""Shared pytest fixtures/helpers.

A few scripts under scripts/ have filenames that start with a digit
(01_analyze.py, 02_restore_exif.py, 00_status.py) so they can't be imported
with a normal `import` statement -- Python identifiers can't start with a
digit. `import_script()` loads them by file path via importlib instead.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def import_script(filename: str):
    """Load scripts/<filename> as a module, e.g. import_script('02_restore_exif.py')."""
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(filename: str, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run scripts/<filename> as a subprocess, matching how a real user invokes it."""
    return subprocess.run(
        [sys.executable, str(SCRIPTS / filename), *args],
        capture_output=True, text=True, cwd=cwd or REPO_ROOT,
    )


def write_sidecar(media_path: Path, title: str, timestamp: int) -> None:
    sidecar = media_path.with_name(media_path.name + ".json")
    sidecar.write_text(json.dumps({
        "title": title,
        "photoTakenTime": {"timestamp": str(timestamp)},
    }))


def make_test_image(path: Path, size: tuple[int, int] = (64, 64),
                    color: tuple[int, int, int] = (120, 60, 60)) -> None:
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, quality=85)


def make_test_video(path: Path, duration: float = 0.5, color: str = "red") -> None:
    """A tiny, real, decodable video file via ffmpeg -- needed so ffprobe (used
    by the analyzer for duration/size) and exiftool (used by the restore step)
    both have something genuine to work with."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"color=c={color}:size=32x32:duration={duration}:rate=5",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


HAVE_CZKAWKA = subprocess.run(["which", "czkawka_cli"], capture_output=True).returncode == 0
HAVE_FFPROBE = subprocess.run(["which", "ffprobe"], capture_output=True).returncode == 0
HAVE_FFMPEG = subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0
HAVE_EXIFTOOL = subprocess.run(["which", "exiftool"], capture_output=True).returncode == 0

requires_full_toolchain = pytest.mark.skipif(
    not (HAVE_CZKAWKA and HAVE_FFPROBE and HAVE_FFMPEG and HAVE_EXIFTOOL),
    reason="needs czkawka_cli + ffmpeg/ffprobe + exiftool installed",
)
requires_ffmpeg_exiftool = pytest.mark.skipif(
    not (HAVE_FFMPEG and HAVE_EXIFTOOL),
    reason="needs ffmpeg + exiftool installed",
)
