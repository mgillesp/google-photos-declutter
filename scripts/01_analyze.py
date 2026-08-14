#!/usr/bin/env python3
"""01_analyze.py -- local analysis + offline review-sheet generator.

Runs entirely on your machine. No photo content ever leaves the computer.

Reads an extracted Google Takeout folder for one monthly batch and produces:
  * review.html   -- self-contained (thumbnails inlined), grouped by:
                       duplicates, bursts/near-dupes, blur candidates,
                       oversized videos, and (optional) junk candidates.
  * decisions.csv -- one row per media file with detected flags and an empty
                       `decision` column for you to fill in (keep/delete).

Passes:
  - Exact + near-duplicate detection via czkawka_cli (degrades gracefully if absent)
  - Burst clustering from Takeout sidecar capture times
  - Blur scoring via OpenCV Laplacian variance
  - Oversized-video flagging via ffprobe
  - OPTIONAL --ollama: local vision model (never calls any cloud API)

Usage:
  python3 scripts/01_analyze.py batches/2019-05
  python3 scripts/01_analyze.py batches/2019-05 --ollama
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

# Make `lib` importable when run as a script.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib import media  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.live_photos import find_live_photo_pairs  # noqa: E402
from lib.preflight import (  # noqa: E402
    check_tools,
    explain_empty_batch,
    explain_missing_takeout,
)
from lib.sidecars import (  # noqa: E402
    IMAGE_EXTS,
    VIDEO_EXTS,
    build_index,
    iter_media,
)


# --------------------------------------------------------------------------- #
# czkawka wrapper (JSON output; verified against czkawka_cli 12.x)
# --------------------------------------------------------------------------- #
#   dup   -> {"<size>": [ [ {"path":..}, .. ], .. ], ..}   (dict of groups)
#   image -> [ [ {"path":.., "difference":..}, .. ], .. ]  (list of groups)
# We normalize both to a list-of-groups where each group is a list of paths.
def _extract_groups(data) -> list[list[str]]:
    groups: list[list[str]] = []

    def group_from(entries) -> list[str] | None:
        paths = [e["path"] for e in entries
                 if isinstance(e, dict) and isinstance(e.get("path"), str)]
        return paths if len(paths) >= 2 else None

    if isinstance(data, dict):          # dup: {size: [ [entries], ... ]}
        for buckets in data.values():
            for entries in (buckets or []):
                g = group_from(entries)
                if g:
                    groups.append(g)
    elif isinstance(data, list):        # image: [ [entries], ... ]
        for entries in data:
            g = group_from(entries)
            if g:
                groups.append(g)
    return groups


def run_czkawka(subcommand: str, directory: Path, extra: list[str]) -> list[list[str]]:
    """Run a czkawka subcommand with JSON output and return groups of file paths."""
    czk = shutil.which("czkawka_cli")
    if not czk:
        print(f"  ! czkawka_cli not found; skipping {subcommand} pass "
              "(install with: brew install czkawka)", file=sys.stderr)
        return []
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        # -p pretty JSON, -W suppress found-exit-code, -N/-M silence stdout/messages
        cmd = [czk, subcommand, "-d", str(directory),
               "-p", tmp, "-W", "-N", "-M"] + extra
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"  ! czkawka {subcommand} failed to run: {e}", file=sys.stderr)
            return []
        if proc.returncode != 0:
            print(f"  ! czkawka {subcommand} exited {proc.returncode}: "
                  f"{proc.stderr.strip()[:200]}", file=sys.stderr)
        try:
            with open(tmp, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
        return _extract_groups(data)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --------------------------------------------------------------------------- #
# Video probing
# --------------------------------------------------------------------------- #
def video_info(path: Path) -> tuple[int, float | None]:
    """Return (size_bytes, duration_seconds_or_None)."""
    size = path.stat().st_size
    dur = None
    if shutil.which("ffprobe"):
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nk=1:nw=1", str(path)],
                capture_output=True, text=True, timeout=60,
            )
            txt = out.stdout.strip()
            if txt:
                dur = float(txt)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass
    return size, dur


# --------------------------------------------------------------------------- #
# Optional local Ollama vision classification
# --------------------------------------------------------------------------- #
def ollama_reachable(host: str) -> bool:
    try:
        import requests
        requests.get(f"{host.rstrip('/')}/api/tags", timeout=3).raise_for_status()
        return True
    except Exception:
        return False


def ollama_classify(path: Path, host: str, model: str) -> str | None:
    """Return a junk label (screenshot/receipt/document) or None. Fully local."""
    import base64
    import io

    try:
        import requests
        from PIL import Image, ImageOps
    except Exception:
        return None
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((768, 768))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None

    prompt = (
        "Classify this image with ONE word from: photo, screenshot, receipt, "
        "document, meme. Answer with only that single word."
    )
    try:
        r = requests.post(
            f"{host.rstrip('/')}/api/generate",
            json={"model": model, "prompt": prompt, "images": [b64], "stream": False},
            timeout=120,
        )
        r.raise_for_status()
        answer = (r.json().get("response") or "").strip().lower()
    except Exception:
        return None
    for label in ("screenshot", "receipt", "document", "meme"):
        if label in answer:
            return label
    return None


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def resolve_takeout_dir(batch_arg: Path) -> Path:
    """Accept either batches/<month> or the takeout dir directly."""
    if (batch_arg / "takeout").is_dir():
        return batch_arg / "takeout"
    return batch_arg


class _UnionFind:
    """Tiny union-find so a photo appearing in overlapping dup/sim/burst groups
    gets exactly one default decision instead of conflicting per-group picks."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def compute_default_decisions(records: dict[str, dict], *group_lists) -> None:
    """Set records[*]['default_decision'] to 'keep' or 'delete' in place.

    Everything defaults to 'keep' (matches: anything not flagged stays kept).
    For each connected cluster of overlapping dup/similar/burst groups, pick a
    single keeper (sharpest by blur variance, else largest file) and default
    the rest of that cluster to 'delete'. Using union-find across all group
    types means a photo in both a burst AND a duplicate group gets one
    consistent answer instead of two different group-local guesses.
    """
    for r in records.values():
        r["default_decision"] = "keep"

    uf = _UnionFind()
    all_groups: list[list[dict]] = [members for _, members in
                                    [g for gl in group_lists for g in gl]]
    for members in all_groups:
        keys = [m["rel"] for m in members]
        for k in keys[1:]:
            uf.union(keys[0], k)

    clusters: dict[str, list[dict]] = defaultdict(list)
    seen: set[str] = set()
    for members in all_groups:
        for m in members:
            if m["rel"] in seen:
                continue
            seen.add(m["rel"])
            clusters[uf.find(m["rel"])].append(m)

    def sharpness_key(m: dict):
        return (1, m["blur"]) if m["blur"] is not None else (0, m["size_mb"])

    for members in clusters.values():
        if len(members) < 2:
            continue
        keeper = max(members, key=sharpness_key)
        for m in members:
            m["default_decision"] = "keep" if m is keeper else "delete"


def cluster_bursts(files_with_time: list[tuple[Path, int]], window: int) -> list[list[Path]]:
    """Group images whose capture epoch is within `window` seconds of the previous."""
    timed = sorted((t, p) for p, t in files_with_time if t is not None)
    clusters: list[list[Path]] = []
    cur: list[Path] = []
    last_t: int | None = None
    for t, p in timed:
        if last_t is not None and (t - last_t) <= window:
            cur.append(p)
        else:
            if len(cur) >= 2:
                clusters.append(cur)
            cur = [p]
        last_t = t
    if len(cur) >= 2:
        clusters.append(cur)
    return clusters


def cluster_video_bursts(files_with_time_and_duration: list[tuple[Path, int, float | None]],
                         window: int) -> list[list[Path]]:
    """Group videos recorded back-to-back, accounting for each clip's own
    duration -- a 2-minute video's start time is naturally ~2 minutes after
    the previous clip's start simply because it takes that long to record,
    which alone doesn't mean the clips are unrelated. Clusters by the gap
    between one video's END time (start + duration) and the next video's
    START time, not raw start-to-start proximity like the photo version.
    """
    timed = sorted(((t, (d or 0.0), p) for p, t, d in files_with_time_and_duration
                    if t is not None), key=lambda x: x[0])
    clusters: list[list[Path]] = []
    cur: list[Path] = []
    last_end: float | None = None
    for start, dur, p in timed:
        if last_end is not None and (start - last_end) <= window:
            cur.append(p)
        else:
            if len(cur) >= 2:
                clusters.append(cur)
            cur = [p]
        last_end = start + dur
    if len(cur) >= 2:
        clusters.append(cur)
    return clusters


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("batch", type=Path,
                    help="Batch dir (batches/YYYY-MM) or extracted takeout dir")
    ap.add_argument("--ollama", action="store_true",
                    help="Also run the local Ollama vision junk-classification pass")
    ap.add_argument("--skip-video-dedup", action="store_true",
                    help="Skip video-burst clustering and czkawka similar-video "
                    "detection. These are the slowest passes on a batch with many "
                    "or long videos. Oversized-video flagging still runs -- this "
                    "only skips the dedup-focused passes.")
    ap.add_argument("--config", type=Path, default=None)
    args = ap.parse_args()

    # Fail early and legibly if the Homebrew tools aren't there. None of these
    # are strictly required to produce *a* review sheet, but without czkawka
    # you lose duplicate detection, which is the whole point.
    check_tools(optional=["czkawka_cli", "ffprobe", "tesseract"])

    batch_dir = args.batch.resolve()
    if not batch_dir.exists():
        print(f"ERROR: {batch_dir} does not exist.\n\n"
              "Create it and put this month's extracted Takeout inside, e.g.:\n"
              f"  mkdir -p {batch_dir}/takeout\n"
              "then extract the Takeout zip into that folder.",
              file=sys.stderr)
        return 2
    takeout = resolve_takeout_dir(batch_dir)
    if not takeout.exists():
        print(explain_missing_takeout(batch_dir, takeout), file=sys.stderr)
        return 2
    # Output goes to the batch dir (parent of takeout, or the arg itself).
    out_dir = batch_dir if (batch_dir / "takeout").is_dir() else batch_dir
    cfg = load_config(args.config)
    acfg = cfg["analysis"]

    print(f"Analyzing: {takeout}")

    # 1) Sidecar index (authoritative capture dates)
    print("  - indexing Takeout JSON sidecars...")
    index = build_index(takeout)
    print(f"    indexed {index.count} sidecars")

    media_files = list(iter_media(takeout))
    print(f"  - found {len(media_files)} media files")
    if not media_files:
        print(explain_empty_batch(batch_dir, takeout), file=sys.stderr)
        return 1

    # Live Photo pairing (IMG_1234.HEIC + IMG_1234.MOV = one capture, not two
    # independent items). Detected up front so record-building below can tag
    # both halves. {video_rel: photo_rel}
    pair_live_photos = bool(acfg.get("pair_live_photos", True))
    live_pairs = find_live_photo_pairs(takeout, media_files) if pair_live_photos else {}
    if live_pairs:
        print(f"  - {len(live_pairs)} Live Photo pair(s) detected "
              "(photo+video treated as one unit)")

    # Per-file record
    records: dict[str, dict] = {}
    exif_fallback_used = 0
    for p in media_files:
        rel = p.relative_to(takeout)
        dt = index.capture_dt(p)
        date_source = "sidecar" if dt else None
        if dt is None:
            dt = media.exif_datetime(p)  # e.g. direct "Download" with no sidecars
            if dt is not None:
                date_source = "exif"
                exif_fallback_used += 1
        epoch = int(dt.timestamp()) if dt else None
        records[str(p)] = {
            "path": p,
            "rel": str(rel),
            "name": p.name,
            "is_video": p.suffix.lower() in VIDEO_EXTS,
            "epoch": epoch,
            "date_source": date_source or "",
            "capture_date": dt.date().isoformat() if dt else "",
            "size_mb": round(p.stat().st_size / (1024 * 1024), 1),
            "blur": None,
            "text_words": None,
            "live_photo_partner": None,
            "flags": set(),
        }

    # Tag both halves of each pair with each other's rel path, so the photo
    # can show a "+video" badge and the video can be excluded from
    # video-dedup candidacy below. Keys reconstructed from media_files
    # directly (not by re-joining takeout/rel) to avoid any path-
    # normalization mismatch with how `records` itself is keyed.
    rel_to_key = {str(p.relative_to(takeout)): str(p) for p in media_files}
    for video_rel, photo_rel in live_pairs.items():
        video_key = rel_to_key.get(video_rel)
        photo_key = rel_to_key.get(photo_rel)
        if video_key in records and photo_key in records:
            records[video_key]["live_photo_partner"] = photo_rel
            records[photo_key]["live_photo_partner"] = video_rel

    if exif_fallback_used:
        print(f"    NOTE: {exif_fallback_used} files had no sidecar match; "
              "used their own embedded EXIF date instead.")
    unresolved = sum(1 for r in records.values() if r["epoch"] is None)
    if unresolved:
        print(f"    NOTE: {unresolved} files had NO resolvable capture date "
              "(no sidecar, no EXIF).")

    # 2) czkawka exact duplicates + near-duplicate images
    print("  - running czkawka duplicate detection...")
    dup_groups = run_czkawka("dup", takeout, [])
    print(f"    exact-duplicate groups: {len(dup_groups)}")
    max_diff = str(acfg["czkawka_max_difference"])
    print(f"  - running czkawka similar-image detection (max-difference {max_diff})...")
    sim_groups = run_czkawka("image", takeout, ["-s", max_diff])
    print(f"    similar-image groups: {len(sim_groups)}")

    # 3) Burst clustering (timestamp) among images
    img_times = [(r["path"], r["epoch"]) for r in records.values()
                 if not r["is_video"] and r["path"].suffix.lower() in IMAGE_EXTS]
    burst_groups = cluster_bursts(img_times, int(acfg["burst_window_seconds"]))
    print(f"  - timestamp burst groups: {len(burst_groups)}")

    # 3b) Video duration (ffprobe) -- computed once here, reused by both video
    # burst clustering below and the oversized-video check later, instead of
    # probing each video twice.
    video_records = [r for r in records.values() if r["is_video"]]
    if video_records:
        print("  - probing video durations (ffprobe)...")
        for r in video_records:
            _, dur = video_info(r["path"])
            r["duration"] = dur

    # 3c) Burst clustering among videos, accounting for each clip's own
    # duration (see cluster_video_bursts docstring) -- a wider window than
    # photos by default since videos naturally space out more.
    #
    # Live Photo videos are excluded from candidacy here and from the
    # similarity pass below entirely -- they're not independent clips, and
    # a phone's worth of ~3-second Live Photo videos would otherwise flood
    # both passes with near-identical short clips that were never meant to
    # be compared against each other.
    dedup_eligible_video_records = [r for r in video_records if not r["live_photo_partner"]]
    skipped_live_photo_videos = len(video_records) - len(dedup_eligible_video_records)

    video_burst_groups: list[list[Path]] = []
    video_sim_groups: list[list[str]] = []
    if args.skip_video_dedup:
        if video_records:
            print(f"  - skipping video burst/similarity detection (--skip-video-dedup, "
                  f"{len(video_records)} video(s) affected)")
    else:
        if skipped_live_photo_videos:
            print(f"  - excluding {skipped_live_photo_videos} Live Photo video(s) "
                  "from video-dedup candidacy")
        vid_times = [(r["path"], r["epoch"], r.get("duration"))
                    for r in dedup_eligible_video_records]
        video_burst_groups = cluster_video_bursts(
            vid_times, int(acfg["video_burst_window_seconds"]))
        print(f"  - video burst groups: {len(video_burst_groups)}")

        # 3d) Similar videos (czkawka content-hash comparison). Skip videos
        # already caught by the timestamp-burst pass above (already flagged
        # for review, no need to show the same clip in two sections) and
        # Live Photo videos (never dedup-eligible at all). czkawka scans the
        # whole directory regardless, so both exclusions are applied to its
        # raw results afterward rather than by restricting what it scans.
        if dedup_eligible_video_records:
            vid_tolerance = str(acfg["czkawka_video_tolerance"])
            print(f"  - running czkawka similar-video detection (tolerance {vid_tolerance})...")
            raw_video_sim_groups = run_czkawka("video", takeout, ["-t", vid_tolerance])
            already_burst = {str(p) for g in video_burst_groups for p in g}
            live_photo_video_keys = {str(r["path"]) for r in video_records
                                     if r["live_photo_partner"]}
            excluded = already_burst | live_photo_video_keys
            for g in raw_video_sim_groups:
                remaining = [k for k in g if k not in excluded]
                if len(remaining) >= 2:
                    video_sim_groups.append(remaining)
            print(f"    similar-video groups: {len(video_sim_groups)} "
                  f"({len(raw_video_sim_groups) - len(video_sim_groups)} skipped -- "
                  "already flagged as a video burst or a Live Photo video)")
        elif video_records:
            print("  - skipping similar-video detection (all videos are Live "
                  "Photo videos, not independently dedup-eligible)")

    # 4) Blur scoring (images only). A photo already shown in a Bursts group
    # doesn't get a *second* review entry here -- it's already up for review
    # there, and it's usually the same handful of near-identical shots. The
    # "blur" flag is still recorded on the record (used by CSV/flags and by
    # the sharpness tie-break when picking a burst's default keeper); only
    # the separate "Blur candidates" review.html section is trimmed.
    already_in_photo_burst = {str(p) for g in burst_groups for p in g}
    print("  - scoring blur (Laplacian variance)...")
    blur_thresh = float(acfg["blur_variance_threshold"])
    blur_candidates = []
    for r in records.values():
        if r["is_video"]:
            continue
        score = media.blur_score(r["path"])
        r["blur"] = round(score, 1) if score is not None else None
        if score is not None and score < blur_thresh:
            r["flags"].add("blur")
            if str(r["path"]) not in already_in_photo_burst:
                blur_candidates.append(r)
    print(f"    blur candidates: {len(blur_candidates)}")

    # 5) Oversized videos (reuses the duration probed in step 3b above)
    max_bytes = float(acfg["video_max_mb"]) * 1024 * 1024
    max_secs = float(acfg["video_max_seconds"] or 0)
    oversized = []
    for r in video_records:
        dur = r.get("duration")
        too_big = (r["size_mb"] * 1024 * 1024) > max_bytes
        too_long = max_secs > 0 and dur is not None and dur > max_secs
        if too_big or too_long:
            r["flags"].add("oversized-video")
            oversized.append(r)
    print(f"    oversized videos: {len(oversized)}")

    # 6) Text/document detection (local OCR) -- catches photographed book/
    # recipe pages, screenshots of text, receipts: often low sentimental
    # value, fully deterministic, no model download needed.
    text_candidates = []
    if shutil.which("tesseract"):
        print("  - scanning for text-heavy images (tesseract OCR)...")
        text_thresh = int(acfg["text_word_threshold"])
        for r in records.values():
            if r["is_video"]:
                continue
            count = media.text_word_count(r["path"])
            r["text_words"] = count
            if count is not None and count >= text_thresh:
                r["flags"].add("text-heavy")
                text_candidates.append(r)
        print(f"    text-heavy candidates: {len(text_candidates)}")
    else:
        print("  ! tesseract not found; skipping text/document detection "
              "(install with: brew install tesseract)", file=sys.stderr)

    # 7) Optional Ollama junk classification
    junk = []
    if args.ollama:
        ocfg = acfg["ollama"]
        if not ollama_reachable(ocfg["host"]):
            print(f"  - Ollama pass SKIPPED: {ocfg['host']} unreachable. "
                  "Start it (`ollama serve`) and pull a vision model "
                  f"(`ollama pull {ocfg['model']}`). Deterministic passes still ran.",
                  file=sys.stderr)
        else:
            print(f"  - Ollama junk pass ({ocfg['model']} @ {ocfg['host']})...")
            images = [r for r in records.values() if not r["is_video"]]
            for r in images:
                label = ollama_classify(r["path"], ocfg["host"], ocfg["model"])
                if label:
                    r["flags"].add(f"junk:{label}")
                    junk.append(r)
            print(f"    junk candidates: {len(junk)}")

    # Tag duplicate/similar/burst group membership on records
    def tag_groups(groups: list[list[str]], prefix: str):
        tagged = []
        for i, g in enumerate(groups):
            members = [records[f] for f in g if f in records]
            if len(members) < 2:
                continue
            gid = f"{prefix}{i+1}"
            for m in members:
                m["flags"].add(gid)
            tagged.append((gid, members))
        return tagged

    dup_tagged = tag_groups(dup_groups, "dup")
    sim_tagged = tag_groups(sim_groups, "sim")
    burst_tagged = tag_groups([[str(p) for p in g] for g in burst_groups], "burst")
    video_burst_tagged = tag_groups(
        [[str(p) for p in g] for g in video_burst_groups], "vburst")
    video_sim_tagged = tag_groups(video_sim_groups, "vsim")

    # One default keep/delete decision per file (sharpest/largest wins within
    # each duplicate/similar/burst cluster); everything else defaults to keep.
    # For video-only clusters, "sharpest" doesn't apply (blur is None), so the
    # largest file wins instead -- see compute_default_decisions/sharpness_key.
    compute_default_decisions(records, dup_tagged, sim_tagged, burst_tagged,
                              video_burst_tagged, video_sim_tagged)

    # Live Photo pairs: the photo governs, always. Not folded into the
    # union-find clustering above, because that clustering's sharpest/
    # largest tie-break has no sensible meaning for a photo+video pair --
    # "largest file" would frequently just mean "keep the video, delete the
    # photo," which is backwards. Explicit override instead.
    for video_rel, photo_rel in live_pairs.items():
        video_key = rel_to_key.get(video_rel)
        photo_key = rel_to_key.get(photo_rel)
        if video_key in records and photo_key in records:
            records[video_key]["default_decision"] = records[photo_key]["default_decision"]

    # Write outputs
    csv_path = out_dir / "decisions.csv"
    html_path = out_dir / "review.html"
    write_decisions_csv(csv_path, records)
    write_review_html(html_path, takeout, records,
                      dup_tagged, sim_tagged, burst_tagged,
                      video_burst_tagged, video_sim_tagged,
                      blur_candidates, oversized, text_candidates, junk, acfg)

    flagged = sum(1 for r in records.values() if r["flags"])
    print("\nDone.")
    print(f"  review sheet : {html_path}")
    print(f"  decisions    : {csv_path}")
    print(f"  {flagged} of {len(records)} files carry at least one flag.")
    print("\nNext: open review.html and click through with your reviewer. "
          "Click a photo to toggle keep/delete (duplicates/bursts start with "
          "a suggested keeper pre-selected). When done, click 'Download "
          "decisions.csv' and move the downloaded file into this batch "
          f"folder ({out_dir}), overwriting the one just generated. Then run "
          "scripts/02_restore_exif.py.")
    return 0


def write_decisions_csv(path: Path, records: dict[str, dict]) -> None:
    """Write decisions.csv pre-filled with the same defaults review.html starts
    from, so hand-editing the CSV directly (skipping the interactive page)
    still gets sensible starting values instead of an all-blank column."""
    rows = sorted(records.values(), key=lambda r: (r["capture_date"], r["rel"]))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rel_path", "filename", "capture_date", "date_source", "size_mb",
                    "is_video", "blur_score", "text_words", "flags", "suggested",
                    "decision"])
        for r in rows:
            flags = ";".join(sorted(r["flags"]))
            suggested = "review" if r["flags"] else "keep"
            decision = "delete" if r.get("default_decision") == "delete" else ""
            w.writerow([r["rel"], r["name"], r["capture_date"], r["date_source"],
                        r["size_mb"], "yes" if r["is_video"] else "no",
                        "" if r["blur"] is None else r["blur"],
                        "" if r.get("text_words") is None else r["text_words"],
                        flags, suggested, decision])


def _thumb(r: dict, max_px: int) -> str:
    if r["is_video"]:
        uri = media.video_thumb_data_uri(r["path"], max_px)
    else:
        uri = media.image_thumb_data_uri(r["path"], max_px)
    if uri:
        return f'<img loading="lazy" src="{uri}" alt="">'
    return '<div class="noimg">no preview</div>'


def _card(r: dict, max_px: int, extra: str = "", rel_index: dict | None = None) -> str:
    meta = html.escape(r["name"])
    sub = []
    if r["capture_date"]:
        sub.append(html.escape(r["capture_date"]))
    sub.append(f'{r["size_mb"]} MB')
    if r["blur"] is not None:
        sub.append(f'blur {r["blur"]}')
    if extra:
        sub.append(extra)
    live_badge = ""
    partner_rel = r.get("live_photo_partner")
    # Only the photo half gets the badge/caption -- the video half never
    # gets its own card anywhere, so this branch never fires for it.
    if partner_rel and not r["is_video"] and rel_index is not None:
        partner = rel_index.get(partner_rel)
        if partner:
            sub.append(f'Live Photo (+{partner["size_mb"]}MB video)')
            live_badge = (f'<span class="live-badge" '
                         f'title="Includes {html.escape(partner["name"])}">LIVE</span>')
    rid = html.escape(r["rel"], quote=True)
    default = "delete" if r.get("default_decision") == "delete" else "keep"
    return (
        f'<figure class="card" data-id="{rid}" data-default="{default}" tabindex="-1">'
        f'<div class="thumb">{_thumb(r, max_px)}'
        f'<div class="mark"></div>'
        f'{live_badge}'
        f'<button class="zoom" type="button" data-zoom="{rid}" '
        f'title="View larger" aria-label="View larger">&#128269;</button>'
        f'</div>'
        f'<figcaption><span class="fn">{meta}</span>'
        f'<span class="mt">{" · ".join(sub)}</span></figcaption></figure>'
    )


def find_absorbed_sim_groups(sim_tagged, burst_tagged):
    """Split similar-image groups into (still shown, absorbed into a burst).

    A sim group whose members are ALL also members of one single burst group
    isn't showing the reviewer anything new -- it's the same handful of shots
    they're already reviewing as a burst. Rather than a second, redundant
    "Similar images" block for the identical photos, that sim group is
    dropped from its own section and the burst group gets a short note
    instead. Underlying tags/CSV flags are untouched -- this only changes
    what review.html renders.

    Returns (visible_sim_tagged, burst_extra_sim_gids) where
    burst_extra_sim_gids maps a burst gid to the list of sim gids absorbed
    into it.
    """
    burst_member_keys = {gid: {m["rel"] for m in members} for gid, members in burst_tagged}
    visible: list[tuple[str, list[dict]]] = []
    absorbed_into: dict[str, list[str]] = {}
    for gid, members in sim_tagged:
        member_keys = {m["rel"] for m in members}
        host = next((bgid for bgid, bkeys in burst_member_keys.items()
                    if member_keys <= bkeys), None)
        if host is not None:
            absorbed_into.setdefault(host, []).append(gid)
        else:
            visible.append((gid, members))
    return visible, absorbed_into


def write_review_html(path: Path, takeout: Path, records, dup_tagged, sim_tagged,
                      burst_tagged, video_burst_tagged, video_sim_tagged,
                      blur_candidates, oversized, text_candidates,
                      junk, acfg) -> None:
    max_px = int(acfg["thumbnail_max_px"])
    parts: list[str] = []
    rel_index = {r["rel"]: r for r in records.values()}

    def section(title: str, desc: str, body: str, count: int):
        parts.append(
            f'<section><h2>{html.escape(title)} '
            f'<span class="badge">{count}</span></h2>'
            f'<p class="desc">{html.escape(desc)}</p>{body}</section>'
        )

    def group_block(tagged, note, extra_notes=None):
        if not tagged:
            return '<p class="empty">None found.</p>'
        blocks = []
        for gid, members in tagged:
            cards = "".join(_card(m, max_px, rel_index=rel_index) for m in members)
            extra = ""
            if extra_notes and gid in extra_notes:
                n = len(extra_notes[gid])
                extra = (f' · also matched as similar image{"s" if n != 1 else ""} '
                         f'({", ".join(extra_notes[gid])}) — not shown separately')
            blocks.append(f'<div class="group"><div class="glabel">{html.escape(gid)} '
                          f'· {len(members)} items — {html.escape(note)}{html.escape(extra)}</div>'
                          f'<div class="grid">{cards}</div></div>')
        return "".join(blocks)

    def flat_block(items, extra_fn=None):
        if not items:
            return '<p class="empty">None found.</p>'
        cards = "".join(_card(r, max_px, extra_fn(r) if extra_fn else "", rel_index=rel_index)
                        for r in items)
        return f'<div class="grid">{cards}</div>'

    visible_sim_tagged, burst_absorbed_sims = find_absorbed_sim_groups(sim_tagged, burst_tagged)

    section("Exact duplicates",
            "Byte/near-byte identical files. Sharpest/largest is pre-selected to "
            "keep — tap another to change.",
            group_block(dup_tagged, "one is pre-selected to keep"), len(dup_tagged))
    section("Similar images",
            "Visually near-identical. Sharpest is pre-selected to keep — tap "
            "any to change (tap more than one to keep several). Groups that are "
            "entirely the same photos as a burst below aren't repeated here.",
            group_block(visible_sim_tagged, "one is pre-selected to keep"),
            len(visible_sim_tagged))
    section("Bursts (by time)",
            "Taken within seconds of each other — likely a burst. Sharpest is "
            "pre-selected to keep.",
            group_block(burst_tagged, "one is pre-selected to keep",
                       extra_notes=burst_absorbed_sims), len(burst_tagged))
    section("Video bursts (by time)",
            "Clips recorded back-to-back (accounting for each clip's own "
            "length, not just start time). Largest file is pre-selected to "
            "keep.",
            group_block(video_burst_tagged, "one is pre-selected to keep"),
            len(video_burst_tagged))
    section("Similar videos",
            "Visually near-identical content (czkawka). Clips already caught "
            "by Video bursts above are skipped here to avoid double-review. "
            "Largest file is pre-selected to keep.",
            group_block(video_sim_tagged, "one is pre-selected to keep"),
            len(video_sim_tagged))
    section("Blur candidates",
            "Low sharpness score (possibly blurry/accidental). Defaults to keep "
            "— tap any you want to mark for delete.",
            flat_block(blur_candidates), len(blur_candidates))
    section("Oversized videos",
            "Large videos you may want to move off Photos. Defaults to keep — "
            "tap to mark for delete.",
            flat_block(oversized,
                       lambda r: f'{r.get("duration") and round(r["duration"])}s'
                       if r.get("duration") else ""),
            len(oversized))
    section("Text-heavy / document photos (OCR)",
            "Lots of recognizable text detected — photographed pages, receipts, "
            "screenshots. Often low sentimental value. Defaults to keep — tap to "
            "mark for delete.",
            flat_block(text_candidates,
                       lambda r: f'{r.get("text_words")} words'
                       if r.get("text_words") else ""),
            len(text_candidates))
    section("Junk candidates (Ollama)",
            "Screenshots/receipts/documents flagged by the local vision model. "
            "Defaults to keep — tap to mark for delete.",
            flat_block(junk, lambda r: next((f.split(":")[1] for f in r["flags"]
                                             if f.startswith("junk:")), "")),
            len(junk))

    total = len(records)
    flagged = sum(1 for r in records.values() if r["flags"])
    batch_label = takeout.parent.name

    all_records = [
        {
            "id": r["rel"],
            "name": r["name"],
            "capture_date": r["capture_date"],
            "date_source": r["date_source"],
            "size_mb": r["size_mb"],
            "is_video": r["is_video"],
            "blur_score": r["blur"],
            "text_words": r.get("text_words"),
            "flags": sorted(r["flags"]),
            "default_decision": r.get("default_decision", "keep"),
        }
        for r in sorted(records.values(), key=lambda r: (r["capture_date"], r["rel"]))
    ]
    # Guard against a filename ever containing a literal "</script>" sequence.
    records_json = json.dumps(all_records).replace("</", "<\\/")
    storage_key = json.dumps(f"gphotos-declutter:review:{batch_label}")

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review sheet — {html.escape(batch_label)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font: 15px/1.5 -apple-system, system-ui, sans-serif; margin: 0; padding: 1.5rem;
         max-width: 1400px; margin-inline: auto; padding-top: 4.5rem; }}
  header h1 {{ margin: 0 0 .25rem; }}
  .summary {{ color: #666; margin-bottom: 1.5rem; }}
  h2 {{ border-bottom: 2px solid #8884; padding-bottom: .3rem; margin-top: 2.5rem; }}
  .badge {{ font-size: .7em; background: #8883; padding: .1em .55em; border-radius: 1em;
           vertical-align: middle; }}
  .desc {{ color: #777; margin-top: -.3rem; }}
  .empty {{ color: #999; font-style: italic; }}
  .group {{ border: 1px solid #8883; border-radius: 8px; padding: .6rem .8rem;
           margin-bottom: 1rem; }}
  .glabel {{ font-weight: 600; font-size: .85rem; color: #a15; margin-bottom: .5rem; }}
  .grid {{ display: grid; gap: .6rem;
          grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }}
  .card {{ margin: 0; border: 3px solid transparent; border-radius: 8px; overflow: hidden;
          background: #8881; cursor: pointer; user-select: none; transition: .1s; }}
  .card:hover {{ border-color: #8886; }}
  .card.is-keep {{ border-color: #2a8f4788; }}
  .card.is-delete {{ border-color: #d1394688; }}
  .card.is-delete .thumb img {{ opacity: .35; filter: grayscale(60%); }}
  .thumb {{ position: relative; aspect-ratio: 1; display: flex; align-items: center;
           justify-content: center; background: #0001; overflow: hidden; }}
  .thumb img {{ width: 100%; height: 100%; object-fit: cover; transition: opacity .1s; }}
  .noimg {{ color: #999; font-size: .8rem; }}
  .mark {{ position: absolute; top: 5px; left: 5px; width: 24px; height: 24px;
          border-radius: 50%; display: flex; align-items: center; justify-content: center;
          font-size: 14px; font-weight: 900; color: #fff; pointer-events: none; }}
  .is-keep .mark {{ background: #2a8f47; }}
  .is-keep .mark::after {{ content: "✓"; }}
  .is-delete .mark {{ background: #d13946; }}
  .is-delete .mark::after {{ content: "✕"; }}
  .zoom {{ position: absolute; bottom: 5px; right: 5px; width: 26px; height: 26px;
          border-radius: 50%; border: none; background: #000a; color: #fff;
          font-size: 13px; cursor: zoom-in; display: flex; align-items: center;
          justify-content: center; padding: 0; }}
  .live-badge {{ position: absolute; top: 5px; right: 5px; background: #000a;
                color: #fff; font-size: 9px; font-weight: 700; letter-spacing: .04em;
                padding: .2em .5em; border-radius: 1em; pointer-events: none; }}
  figcaption {{ padding: .35rem .45rem; font-size: .72rem; }}
  .fn {{ display: block; font-weight: 600; word-break: break-all; }}
  .mt {{ color: #888; }}
  #toolbar {{ position: fixed; top: 0; left: 0; right: 0; z-index: 50;
             background: canvas; border-bottom: 1px solid #8884;
             padding: .6rem 1.5rem; display: flex; align-items: center; gap: 1rem;
             font-size: .85rem; flex-wrap: wrap; }}
  #toolbar .counts {{ color: #777; }}
  #toolbar .counts b {{ color: canvastext; }}
  #toolbar button {{ font: inherit; padding: .45rem .9rem; border-radius: 6px;
                     border: 1px solid #8886; background: #2a8f47; color: #fff;
                     cursor: pointer; font-weight: 600; }}
  #toolbar button.secondary {{ background: transparent; color: canvastext; }}
  #toolbar .spacer {{ flex: 1; }}
  #lightbox {{ position: fixed; inset: 0; background: #000d; z-index: 100;
              display: none; align-items: center; justify-content: center;
              padding: 3rem; cursor: zoom-out; }}
  #lightbox.open {{ display: flex; }}
  #lightbox img {{ max-width: 100%; max-height: 100%; border-radius: 6px;
                   box-shadow: 0 10px 40px #0008; }}
  #toast {{ position: fixed; bottom: 1.2rem; left: 50%; transform: translateX(-50%);
           background: canvastext; color: canvas; padding: .6rem 1.1rem; border-radius: 6px;
           font-size: .85rem; opacity: 0; pointer-events: none; transition: opacity .2s; z-index: 60; }}
  #toast.show {{ opacity: 1; }}
  .card.kbd-focus {{ outline: 3px solid #2a6df4; outline-offset: 2px; }}
  #help-overlay {{ position: fixed; inset: 0; background: #000a; z-index: 110;
                  display: none; align-items: center; justify-content: center; }}
  #help-overlay.open {{ display: flex; }}
  #help-overlay .box {{ background: canvas; color: canvastext; border-radius: 10px;
                       padding: 1.5rem 2rem; max-width: 420px; box-shadow: 0 10px 40px #0008; }}
  #help-overlay h3 {{ margin-top: 0; }}
  #help-overlay table {{ border-collapse: collapse; width: 100%; }}
  #help-overlay td {{ padding: .3rem 0; font-size: .88rem; }}
  #help-overlay td:first-child {{ font-family: ui-monospace, monospace; color: #2a6df4;
                                  white-space: nowrap; padding-right: 1rem; }}
  #help-overlay .close-hint {{ margin-top: 1rem; color: #888; font-size: .8rem; }}
</style></head><body>
<div id="toolbar">
  <strong>📸 {html.escape(batch_label)}</strong>
  <span class="counts">{flagged} flagged ·
    <b id="cnt-keep">0</b> keep · <b id="cnt-delete">0</b> delete ·
    <b id="cnt-mb">0</b> MB freed</span>
  <span class="spacer"></span>
  <button type="button" class="secondary" id="btn-reset">Reset to suggested</button>
  <button type="button" class="secondary" id="btn-help">⌨ Shortcuts (?)</button>
  <button type="button" id="btn-download">⬇ Download decisions.csv</button>
</div>
<header>
  <h1>Review sheet — {html.escape(batch_label)}</h1>
  <p class="summary">{flagged} of {total} files flagged for review.
  <b>Tap a photo</b> to toggle keep (✓ green) / delete (✕ red). Duplicate,
  similar, and burst groups start with the sharpest shot pre-selected to keep.
  <b>Anything not shown below is not flagged and stays kept automatically</b> —
  you don't need to review it. Nothing is deleted by this tool; when you're
  done, click <b>Download decisions.csv</b> above — in Chrome/Edge this opens
  a save dialog where you can navigate straight into this batch's folder; in
  other browsers it downloads normally and you'll move the file there
  yourself, replacing the one already present. Fully offline — no image ever
  left this machine.</p>
</header>
{''.join(parts)}
<footer style="margin-top:3rem;color:#999;font-size:.8rem">
Generated by google-photos-declutter · deterministic local analysis.
</footer>
<div id="lightbox"><img id="lightbox-img" alt=""></div>
<div id="help-overlay"><div class="box">
  <h3>Keyboard shortcuts</h3>
  <table>
    <tr><td>&#8595; / &#8594; / j</td><td>Next photo</td></tr>
    <tr><td>&#8593; / &#8592; / k</td><td>Previous photo</td></tr>
    <tr><td>Space</td><td>Toggle keep / delete</td></tr>
    <tr><td>Enter</td><td>Accept this group as-is, jump to the next one</td></tr>
    <tr><td>?</td><td>Toggle this help</td></tr>
    <tr><td>Esc</td><td>Close lightbox / this help</td></tr>
  </table>
  <p class="close-hint">Shortcuts are disabled while the lightbox is open.</p>
</div></div>
<div id="toast"></div>
<script id="all-records" type="application/json">{records_json}</script>
<script>
(function() {{
  "use strict";
  var STORAGE_KEY = {storage_key};
  var records = JSON.parse(document.getElementById("all-records").textContent);
  var recordsById = {{}};
  records.forEach(function(r) {{ recordsById[r.id] = r; }});

  var state = {{}};
  try {{
    var saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{{}}");
    if (saved && typeof saved === "object") state = saved;
  }} catch (e) {{ state = {{}}; }}

  // Keyboard review: allCards is a snapshot in DOM order (the page never
  // adds/removes cards after load, so this is safe to build once).
  var allCards = [];
  var focusedIndex = -1;

  // localStorage can throw on file:// pages in some browsers (e.g. Safari).
  // Treat it as best-effort persistence: toggling must still work for the
  // current page session even if saving/clearing fails.
  function saveState() {{
    try {{ localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); }} catch (e) {{}}
  }}
  function clearSavedState() {{
    try {{ localStorage.removeItem(STORAGE_KEY); }} catch (e) {{}}
  }}

  // Avoid depending on CSS.escape(): filter by attribute value in JS instead
  // of building a CSS attribute-selector string from an arbitrary file path.
  function cardsWithId(id) {{
    return Array.prototype.filter.call(
      document.querySelectorAll(".card[data-id]"),
      function(el) {{ return el.getAttribute("data-id") === id; }}
    );
  }}

  function decisionFor(id) {{
    if (Object.prototype.hasOwnProperty.call(state, id)) return state[id];
    var r = recordsById[id];
    return (r && r.default_decision === "delete") ? "delete" : "keep";
  }}

  function applyCardVisual(el) {{
    var id = el.getAttribute("data-id");
    var d = decisionFor(id);
    el.classList.toggle("is-keep", d === "keep");
    el.classList.toggle("is-delete", d === "delete");
  }}

  function refreshAll() {{
    document.querySelectorAll(".card[data-id]").forEach(applyCardVisual);
    updateCounts();
  }}

  function updateCounts() {{
    var keep = 0, del = 0, savedMb = 0;
    var seen = {{}};
    document.querySelectorAll(".card[data-id]").forEach(function(el) {{
      var id = el.getAttribute("data-id");
      if (seen[id]) return;
      seen[id] = true;
      if (decisionFor(id) === "delete") {{
        del++;
        var r = recordsById[id];
        if (r && typeof r.size_mb === "number") savedMb += r.size_mb;
      }} else {{
        keep++;
      }}
    }});
    document.getElementById("cnt-keep").textContent = keep;
    document.getElementById("cnt-delete").textContent = del;
    document.getElementById("cnt-mb").textContent = savedMb.toFixed(1);
  }}

  function toggle(id) {{
    var next = decisionFor(id) === "keep" ? "delete" : "keep";
    state[id] = next;
    saveState();
    cardsWithId(id).forEach(applyCardVisual);
    updateCounts();
  }}

  function openLightbox(id) {{
    var card = cardsWithId(id)[0];
    var img = card && card.querySelector(".thumb img");
    if (!img) return;
    document.getElementById("lightbox-img").src = img.src;
    document.getElementById("lightbox").classList.add("open");
  }}
  function closeLightbox() {{
    document.getElementById("lightbox").classList.remove("open");
  }}

  document.addEventListener("click", function(e) {{
    var zoomBtn = e.target.closest(".zoom");
    if (zoomBtn) {{
      e.stopPropagation();
      openLightbox(zoomBtn.getAttribute("data-zoom"));
      return;
    }}
    if (e.target.closest("#lightbox")) {{ closeLightbox(); return; }}
    if (e.target.closest("#help-overlay")) {{ hideHelp(); return; }}
    var card = e.target.closest(".card[data-id]");
    if (card) toggle(card.getAttribute("data-id"));
  }});

  // --- Keyboard review -------------------------------------------------
  // A "unit" is the group a card belongs to (the .group div for dup/
  // similar/burst sections) or, for flat single-item sections (blur,
  // oversized video, text-heavy, junk) where there's no multi-item group
  // to move past, the card itself -- so Enter there behaves like "next
  // item" instead of skipping the whole section.
  function cardUnit(card) {{
    return card.closest(".group") || card;
  }}

  function setFocus(index) {{
    if (allCards.length === 0) return;
    index = Math.max(0, Math.min(allCards.length - 1, index));
    if (focusedIndex >= 0 && allCards[focusedIndex]) {{
      allCards[focusedIndex].classList.remove("kbd-focus");
    }}
    focusedIndex = index;
    var card = allCards[focusedIndex];
    card.classList.add("kbd-focus");
    card.focus({{preventScroll: true}});
    card.scrollIntoView({{block: "center", behavior: "smooth"}});
  }}

  function moveFocus(delta) {{
    if (focusedIndex < 0) {{ setFocus(0); return; }}
    setFocus(focusedIndex + delta);
  }}

  function toggleFocused() {{
    if (focusedIndex < 0 || !allCards[focusedIndex]) {{ setFocus(0); return; }}
    toggle(allCards[focusedIndex].getAttribute("data-id"));
  }}

  function acceptGroupAndAdvance() {{
    if (focusedIndex < 0 || !allCards[focusedIndex]) {{ setFocus(0); return; }}
    var unit = cardUnit(allCards[focusedIndex]);
    var next = -1;
    for (var i = focusedIndex + 1; i < allCards.length; i++) {{
      if (cardUnit(allCards[i]) !== unit) {{ next = i; break; }}
    }}
    if (next === -1) {{
      toast("That's the last group.");
      return;
    }}
    setFocus(next);
  }}

  function showHelp() {{ document.getElementById("help-overlay").classList.add("open"); }}
  function hideHelp() {{ document.getElementById("help-overlay").classList.remove("open"); }}

  document.getElementById("btn-help").addEventListener("click", showHelp);

  document.addEventListener("keydown", function(e) {{
    if (e.key === "Escape") {{
      if (document.getElementById("help-overlay").classList.contains("open")) {{
        hideHelp();
      }} else {{
        closeLightbox();
      }}
      return;
    }}

    // Disabled while the lightbox or help overlay is open, or while focus
    // is in a text field -- the page has none today, but this keeps the
    // shortcuts from ever hijacking typing if one gets added later.
    var lightboxOpen = document.getElementById("lightbox").classList.contains("open");
    var helpOpen = document.getElementById("help-overlay").classList.contains("open");
    var active = document.activeElement;
    var inTextField = active && (["INPUT", "TEXTAREA", "SELECT"].indexOf(active.tagName) !== -1
                      || active.isContentEditable);
    if (lightboxOpen || helpOpen || inTextField) return;

    switch (e.key) {{
      case "ArrowDown": case "ArrowRight": case "j": case "J":
        e.preventDefault(); moveFocus(1); break;
      case "ArrowUp": case "ArrowLeft": case "k": case "K":
        e.preventDefault(); moveFocus(-1); break;
      case " ":
        e.preventDefault(); toggleFocused(); break;
      case "Enter":
        e.preventDefault(); acceptGroupAndAdvance(); break;
      case "?":
        e.preventDefault(); showHelp(); break;
    }}
  }});

  document.getElementById("btn-reset").addEventListener("click", function() {{
    state = {{}};
    clearSavedState();
    refreshAll();
    toast("Reset to suggested defaults.");
  }});

  function csvEscape(v) {{
    var s = (v === null || v === undefined) ? "" : String(v);
    return /[",\\r\\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }}

  function buildCsvText() {{
    var header = ["rel_path", "filename", "capture_date", "date_source", "size_mb",
                  "is_video", "blur_score", "text_words", "flags", "suggested",
                  "decision"];
    var lines = [header.join(",")];
    records.forEach(function(r) {{
      var decision = decisionFor(r.id);
      var decisionOut = decision === "delete" ? "delete" : "";
      var row = [r.id, r.name, r.capture_date, r.date_source, r.size_mb,
                r.is_video ? "yes" : "no",
                (r.blur_score === null || r.blur_score === undefined) ? "" : r.blur_score,
                (r.text_words === null || r.text_words === undefined) ? "" : r.text_words,
                r.flags.join(";"), r.flags.length ? "review" : "keep", decisionOut];
      lines.push(row.map(csvEscape).join(","));
    }});
    return lines.join("\\r\\n");
  }}

  function downloadCsvFallback(text) {{
    var blob = new Blob([text], {{type: "text/csv"}});
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "decisions.csv";
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast("Downloaded decisions.csv — move it into this batch's folder.");
  }}

  document.getElementById("btn-download").addEventListener("click", async function() {{
    var text = buildCsvText();
    // Chromium browsers: a real native Save dialog, so you can navigate
    // straight to this batch's folder instead of downloading + moving the
    // file yourself. Falls back to a normal download everywhere else
    // (Safari/Firefox don't support this API).
    if (window.showSaveFilePicker) {{
      try {{
        var handle = await window.showSaveFilePicker({{
          suggestedName: "decisions.csv",
          types: [{{ description: "CSV file", accept: {{"text/csv": [".csv"]}} }}],
        }});
        var writable = await handle.createWritable();
        await writable.write(text);
        await writable.close();
        toast("Saved decisions.csv.");
        return;
      }} catch (err) {{
        if (err && err.name === "AbortError") return; // user cancelled the picker
        // Any other failure (older browser flag, permission issue, etc.):
        // fall through to the plain download below.
      }}
    }}
    downloadCsvFallback(text);
  }});

  var toastTimer = null;
  function toast(msg) {{
    var el = document.getElementById("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function() {{ el.classList.remove("show"); }}, 3200);
  }}

  allCards = Array.prototype.slice.call(document.querySelectorAll(".card[data-id]"));
  refreshAll();
}})();
</script>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


if __name__ == "__main__":
    raise SystemExit(main())
