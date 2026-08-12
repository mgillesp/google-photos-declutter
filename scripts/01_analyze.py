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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("batch", type=Path,
                    help="Batch dir (batches/YYYY-MM) or extracted takeout dir")
    ap.add_argument("--ollama", action="store_true",
                    help="Also run the local Ollama vision junk-classification pass")
    ap.add_argument("--config", type=Path, default=None)
    args = ap.parse_args()

    batch_dir = args.batch.resolve()
    if not batch_dir.exists():
        print(f"ERROR: {batch_dir} does not exist.", file=sys.stderr)
        return 2
    takeout = resolve_takeout_dir(batch_dir)
    if not takeout.exists():
        print(f"ERROR: takeout dir {takeout} does not exist. Extract the zip there.",
              file=sys.stderr)
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
        print("No media found; nothing to do.", file=sys.stderr)
        return 1

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
            "flags": set(),
        }

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

    # 4) Blur scoring (images only)
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
            blur_candidates.append(r)
    print(f"    blur candidates: {len(blur_candidates)}")

    # 5) Oversized videos
    print("  - probing videos (ffprobe)...")
    max_bytes = float(acfg["video_max_mb"]) * 1024 * 1024
    max_secs = float(acfg["video_max_seconds"] or 0)
    oversized = []
    for r in records.values():
        if not r["is_video"]:
            continue
        size, dur = video_info(r["path"])
        r["duration"] = dur
        too_big = size > max_bytes
        too_long = max_secs > 0 and dur is not None and dur > max_secs
        if too_big or too_long:
            r["flags"].add("oversized-video")
            oversized.append(r)
    print(f"    oversized videos: {len(oversized)}")

    # 6) Optional Ollama junk classification
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

    # One default keep/delete decision per file (sharpest/largest wins within
    # each duplicate/similar/burst cluster); everything else defaults to keep.
    compute_default_decisions(records, dup_tagged, sim_tagged, burst_tagged)

    # Write outputs
    csv_path = out_dir / "decisions.csv"
    html_path = out_dir / "review.html"
    write_decisions_csv(csv_path, records)
    write_review_html(html_path, takeout, records,
                      dup_tagged, sim_tagged, burst_tagged,
                      blur_candidates, oversized, junk, acfg)

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
                    "is_video", "blur_score", "flags", "suggested", "decision"])
        for r in rows:
            flags = ";".join(sorted(r["flags"]))
            suggested = "review" if r["flags"] else "keep"
            decision = "delete" if r.get("default_decision") == "delete" else ""
            w.writerow([r["rel"], r["name"], r["capture_date"], r["date_source"],
                        r["size_mb"], "yes" if r["is_video"] else "no",
                        "" if r["blur"] is None else r["blur"],
                        flags, suggested, decision])


def _thumb(r: dict, max_px: int) -> str:
    if r["is_video"]:
        uri = media.video_thumb_data_uri(r["path"], max_px)
    else:
        uri = media.image_thumb_data_uri(r["path"], max_px)
    if uri:
        return f'<img loading="lazy" src="{uri}" alt="">'
    return '<div class="noimg">no preview</div>'


def _card(r: dict, max_px: int, extra: str = "") -> str:
    meta = html.escape(r["name"])
    sub = []
    if r["capture_date"]:
        sub.append(html.escape(r["capture_date"]))
    sub.append(f'{r["size_mb"]} MB')
    if r["blur"] is not None:
        sub.append(f'blur {r["blur"]}')
    if extra:
        sub.append(extra)
    rid = html.escape(r["rel"], quote=True)
    default = "delete" if r.get("default_decision") == "delete" else "keep"
    return (
        f'<figure class="card" data-id="{rid}" data-default="{default}">'
        f'<div class="thumb">{_thumb(r, max_px)}'
        f'<div class="mark"></div>'
        f'<button class="zoom" type="button" data-zoom="{rid}" '
        f'title="View larger" aria-label="View larger">&#128269;</button>'
        f'</div>'
        f'<figcaption><span class="fn">{meta}</span>'
        f'<span class="mt">{" · ".join(sub)}</span></figcaption></figure>'
    )


def write_review_html(path: Path, takeout: Path, records, dup_tagged, sim_tagged,
                      burst_tagged, blur_candidates, oversized, junk, acfg) -> None:
    max_px = int(acfg["thumbnail_max_px"])
    parts: list[str] = []

    def section(title: str, desc: str, body: str, count: int):
        parts.append(
            f'<section><h2>{html.escape(title)} '
            f'<span class="badge">{count}</span></h2>'
            f'<p class="desc">{html.escape(desc)}</p>{body}</section>'
        )

    def group_block(tagged, note):
        if not tagged:
            return '<p class="empty">None found.</p>'
        blocks = []
        for gid, members in tagged:
            cards = "".join(_card(m, max_px) for m in members)
            blocks.append(f'<div class="group"><div class="glabel">{html.escape(gid)} '
                          f'· {len(members)} items — {html.escape(note)}</div>'
                          f'<div class="grid">{cards}</div></div>')
        return "".join(blocks)

    def flat_block(items, extra_fn=None):
        if not items:
            return '<p class="empty">None found.</p>'
        cards = "".join(_card(r, max_px, extra_fn(r) if extra_fn else "") for r in items)
        return f'<div class="grid">{cards}</div>'

    section("Exact duplicates",
            "Byte/near-byte identical files. Sharpest/largest is pre-selected to "
            "keep — tap another to change.",
            group_block(dup_tagged, "one is pre-selected to keep"), len(dup_tagged))
    section("Similar images",
            "Visually near-identical. Sharpest is pre-selected to keep — tap "
            "any to change (tap more than one to keep several).",
            group_block(sim_tagged, "one is pre-selected to keep"), len(sim_tagged))
    section("Bursts (by time)",
            "Taken within seconds of each other — likely a burst. Sharpest is "
            "pre-selected to keep.",
            group_block(burst_tagged, "one is pre-selected to keep"), len(burst_tagged))
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
</style></head><body>
<div id="toolbar">
  <strong>📸 {html.escape(batch_label)}</strong>
  <span class="counts">{flagged} flagged ·
    <b id="cnt-keep">0</b> keep · <b id="cnt-delete">0</b> delete</span>
  <span class="spacer"></span>
  <button type="button" class="secondary" id="btn-reset">Reset to suggested</button>
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
    var keep = 0, del = 0;
    var seen = {{}};
    document.querySelectorAll(".card[data-id]").forEach(function(el) {{
      var id = el.getAttribute("data-id");
      if (seen[id]) return;
      seen[id] = true;
      if (decisionFor(id) === "delete") del++; else keep++;
    }});
    document.getElementById("cnt-keep").textContent = keep;
    document.getElementById("cnt-delete").textContent = del;
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
    var card = e.target.closest(".card[data-id]");
    if (card) toggle(card.getAttribute("data-id"));
  }});
  document.addEventListener("keydown", function(e) {{
    if (e.key === "Escape") closeLightbox();
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
                  "is_video", "blur_score", "flags", "suggested", "decision"];
    var lines = [header.join(",")];
    records.forEach(function(r) {{
      var decision = decisionFor(r.id);
      var decisionOut = decision === "delete" ? "delete" : "";
      var row = [r.id, r.name, r.capture_date, r.date_source, r.size_mb,
                r.is_video ? "yes" : "no",
                (r.blur_score === null || r.blur_score === undefined) ? "" : r.blur_score,
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

  refreshAll();
}})();
</script>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


if __name__ == "__main__":
    raise SystemExit(main())
