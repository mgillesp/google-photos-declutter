#!/usr/bin/env python3
"""02_restore_exif.py -- copy 'keep' files and bake in correct capture dates.

After you've reviewed review.html and marked decisions in decisions.csv, this
script:
  1. Copies every KEEP file (decision != "delete") into batches/<month>/reupload/,
     preserving the takeout folder structure.
  2. Uses the Takeout JSON sidecars + exiftool to write the authoritative capture
     date into each file (DateTimeOriginal for images; QuickTime CreateDate for
     videos), so Google Photos re-sorts them into the correct timeline slot on
     re-upload. (The upload API can't set dates; they must be embedded in-file.)

CONSERVATIVE BY DESIGN: a file is only skipped if its decision is explicitly
"delete" (or d/x/remove). Blank/keep/anything-else => kept.

Dates are derived from the sidecar's UTC timestamp and written as that wall-clock
time; timeline placement is accurate to the day (what Google sorts on). Files with
no resolvable sidecar date are still copied but reported so you can fix them.

Usage:
  python3 scripts/02_restore_exif.py batches/2019-05
  python3 scripts/02_restore_exif.py batches/2019-05 --dry-run
"""
from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib import media  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.sidecars import VIDEO_EXTS, build_index  # noqa: E402

DELETE_TOKENS = {"delete", "del", "d", "x", "remove", "rm", "trash", "no"}


def resolve_takeout_dir(batch_arg: Path) -> Path:
    if (batch_arg / "takeout").is_dir():
        return batch_arg / "takeout"
    return batch_arg


def is_delete(decision: str) -> bool:
    return decision.strip().lower() in DELETE_TOKENS


def exiftool_date_args(is_video: bool, date_str: str, write_tags: list[str]) -> list[str]:
    if is_video:
        # QuickTime dates are UTC by spec, matching our UTC-derived timestamp.
        return [
            f"-CreateDate={date_str}", f"-ModifyDate={date_str}",
            f"-TrackCreateDate={date_str}", f"-TrackModifyDate={date_str}",
            f"-MediaCreateDate={date_str}", f"-MediaModifyDate={date_str}",
        ]
    return [f"-{t}={date_str}" for t in write_tags]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("batch", type=Path, help="Batch dir (batches/YYYY-MM)")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would happen without copying or writing")
    args = ap.parse_args()

    if not shutil.which("exiftool"):
        print("ERROR: exiftool not found. Install with: brew install exiftool",
              file=sys.stderr)
        return 3

    batch_dir = args.batch.resolve()
    takeout = resolve_takeout_dir(batch_dir)
    out_root = batch_dir if (batch_dir / "takeout").is_dir() else batch_dir
    decisions_csv = out_root / "decisions.csv"
    reupload = out_root / "reupload"

    if not decisions_csv.exists():
        print(f"ERROR: {decisions_csv} not found. Run 01_analyze.py first.",
              file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    write_tags = cfg["exif"]["write_tags"]

    print("Indexing sidecars for capture dates...")
    index = build_index(takeout)

    with open(decisions_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    keep_rows = [r for r in rows if not is_delete(r.get("decision", ""))]
    delete_n = len(rows) - len(keep_rows)
    print(f"{len(rows)} files: {len(keep_rows)} keep, {delete_n} marked delete.\n")

    copied = 0
    dated = 0
    unresolved: list[str] = []
    failed: list[str] = []
    mislabeled: list[str] = []

    for r in keep_rows:
        rel = r["rel_path"]
        src = takeout / rel
        if not src.exists():
            failed.append(f"{rel} (source missing)")
            continue
        dest = reupload / rel
        is_video = src.suffix.lower() in VIDEO_EXTS

        dt = index.capture_dt(src) or media.exif_datetime(src)
        if args.dry_run:
            tag = dt.date().isoformat() if dt else "NO DATE"
            print(f"  keep  {rel}  [{tag}]")
            copied += 1
            if not dt:
                unresolved.append(rel)
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1

        if not is_video:
            fixed = media.fix_mislabeled_extension(dest)
            if fixed != dest:
                mislabeled.append(f"{rel} -> {fixed.name}")
                dest = fixed

        if not dt:
            unresolved.append(rel)
            continue

        date_str = dt.strftime("%Y:%m:%d %H:%M:%S")
        cmd = ["exiftool", "-overwrite_original", "-P", "-m"] + \
            exiftool_date_args(is_video, date_str, write_tags) + [str(dest)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            dated += 1
        else:
            failed.append(f"{rel} (exiftool: {proc.stderr.strip()[:120]})")

    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Summary:")
    print(f"  copied to reupload/ : {copied}")
    if not args.dry_run:
        print(f"  dates written       : {dated}")
    if mislabeled:
        print(f"  extension corrected : {len(mislabeled)} (real format didn't match "
              "filename extension -- renamed so date-writing and upload MIME "
              "type are both correct):")
        for u in mislabeled[:20]:
            print(f"      - {u}")
        if len(mislabeled) > 20:
            print(f"      ... and {len(mislabeled) - 20} more")
    if unresolved:
        print(f"  NO sidecar date     : {len(unresolved)} (copied but NOT re-dated — "
              "these may land at upload time in the timeline):")
        for u in unresolved[:20]:
            print(f"      - {u}")
        if len(unresolved) > 20:
            print(f"      ... and {len(unresolved) - 20} more")
    if failed:
        print(f"  FAILED              : {len(failed)}")
        for u in failed[:20]:
            print(f"      - {u}")

    if not args.dry_run:
        print(f"\nKeepers staged in: {reupload}")
        print("Next: (browser) trash this month in Google Photos, then run "
              "scripts/03_upload.py")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
