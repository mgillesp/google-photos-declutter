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
    for p in media_files:
        rel = p.relative_to(takeout)
        epoch = index.lookup(p)
        dt = index.capture_dt(p)
        records[str(p)] = {
            "path": p,
            "rel": str(rel),
            "name": p.name,
            "is_video": p.suffix.lower() in VIDEO_EXTS,
            "epoch": epoch,
            "capture_date": dt.date().isoformat() if dt else "",
            "size_mb": round(p.stat().st_size / (1024 * 1024), 1),
            "blur": None,
            "flags": set(),
        }

    unresolved = sum(1 for r in records.values() if r["epoch"] is None)
    if unresolved:
        print(f"    NOTE: {unresolved} files had no resolvable capture date from sidecars.")

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
    print("\nNext: open review.html, then mark keep/delete in decisions.csv, "
          "then run scripts/02_restore_exif.py")
    return 0


def write_decisions_csv(path: Path, records: dict[str, dict]) -> None:
    rows = sorted(records.values(), key=lambda r: (r["capture_date"], r["rel"]))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rel_path", "filename", "capture_date", "size_mb", "is_video",
                    "blur_score", "flags", "suggested", "decision"])
        for r in rows:
            flags = ";".join(sorted(r["flags"]))
            suggested = "review" if r["flags"] else "keep"
            w.writerow([r["rel"], r["name"], r["capture_date"], r["size_mb"],
                        "yes" if r["is_video"] else "no",
                        "" if r["blur"] is None else r["blur"],
                        flags, suggested, ""])


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
    return (f'<figure class="card"><div class="thumb">{_thumb(r, max_px)}</div>'
            f'<figcaption><span class="fn">{meta}</span>'
            f'<span class="mt">{" · ".join(sub)}</span></figcaption></figure>')


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

    section("Exact duplicates", "Byte/near-byte identical files (czkawka). Safe to keep one.",
            group_block(dup_tagged, "keep one, delete the rest"), len(dup_tagged))
    section("Similar images", "Visually near-identical (czkawka similarity). Review each group.",
            group_block(sim_tagged, "near-duplicates"), len(sim_tagged))
    section("Bursts (by time)", "Photos taken within seconds of each other — likely a burst.",
            group_block(burst_tagged, "keep the best 1-2"), len(burst_tagged))
    section("Blur candidates", "Low Laplacian-variance (possibly blurry/accidental).",
            flat_block(blur_candidates), len(blur_candidates))
    section("Oversized videos", "Large videos you may want to move off Photos.",
            flat_block(oversized,
                       lambda r: f'{r.get("duration") and round(r["duration"])}s'
                       if r.get("duration") else ""),
            len(oversized))
    section("Junk candidates (Ollama)",
            "Screenshots/receipts/documents flagged by the local vision model.",
            flat_block(junk, lambda r: next((f.split(":")[1] for f in r["flags"]
                                             if f.startswith("junk:")), "")),
            len(junk))

    total = len(records)
    flagged = sum(1 for r in records.values() if r["flags"])
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review sheet — {html.escape(takeout.parent.name)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 -apple-system, system-ui, sans-serif; margin: 0; padding: 1.5rem;
         max-width: 1400px; margin-inline: auto; }}
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
  .card {{ margin: 0; border: 1px solid #8882; border-radius: 6px; overflow: hidden;
          background: #8881; }}
  .thumb {{ aspect-ratio: 1; display: flex; align-items: center; justify-content: center;
           background: #0001; overflow: hidden; }}
  .thumb img {{ width: 100%; height: 100%; object-fit: cover; }}
  .noimg {{ color: #999; font-size: .8rem; }}
  figcaption {{ padding: .35rem .45rem; font-size: .72rem; }}
  .fn {{ display: block; font-weight: 600; word-break: break-all; }}
  .mt {{ color: #888; }}
</style></head><body>
<header>
  <h1>📸 Review sheet — {html.escape(takeout.parent.name)}</h1>
  <p class="summary">{flagged} of {total} files flagged for review.
  Nothing is deleted by this tool. Mark <b>keep</b>/<b>delete</b> in
  <code>decisions.csv</code>. This file is local and offline; no image left your machine.</p>
</header>
{''.join(parts)}
<footer style="margin-top:3rem;color:#999;font-size:.8rem">
Generated by google-photos-declutter · deterministic local analysis.
</footer>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


if __name__ == "__main__":
    raise SystemExit(main())
