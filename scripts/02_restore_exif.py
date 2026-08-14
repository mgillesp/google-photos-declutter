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
no resolvable date (no sidecar, no EXIF) are still copied but reported so you can
fix them -- unless --fill-missing-dates is passed, in which case they're assigned
the earliest capture date found anywhere in the batch (see that flag's help text
for when this is and isn't a reasonable assumption).

Usage:
  python3 scripts/02_restore_exif.py batches/2019-05
  python3 scripts/02_restore_exif.py batches/2019-05 --dry-run
  python3 scripts/02_restore_exif.py batches/2019-05 --fill-missing-dates
"""
from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib import media  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.live_photos import find_live_photo_pairs  # noqa: E402
from lib.preflight import check_tools, explain_missing_takeout  # noqa: E402
from lib.sidecars import VIDEO_EXTS, build_index, iter_media  # noqa: E402

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
    ap.add_argument("--fill-missing-dates", action="store_true",
                    help="For files with truly NO resolvable date (no sidecar, "
                    "no EXIF), assign the earliest capture date found anywhere "
                    "in this batch's decisions.csv (across all reviewed files, "
                    "not just keepers), instead of leaving them undated. Off by "
                    "default -- only sensible for a tightly time-scoped batch "
                    "(e.g. one month); for a full-year batch the 'earliest date' "
                    "could be many months off from where the file actually "
                    "belongs, so review the affected files after using this.")
    args = ap.parse_args()

    check_tools(required=["exiftool"])

    batch_dir = args.batch.resolve()
    takeout = resolve_takeout_dir(batch_dir)
    if not takeout.exists():
        print(explain_missing_takeout(batch_dir, takeout), file=sys.stderr)
        return 2
    out_root = batch_dir if (batch_dir / "takeout").is_dir() else batch_dir
    decisions_csv = out_root / "decisions.csv"
    reupload = out_root / "reupload"

    if not decisions_csv.exists():
        print(f"\nNo decisions file found at:\n  {decisions_csv}\n\n"
              "That file is produced by the review step. Either:\n"
              "  - you haven't run scripts/01_analyze.py on this batch yet, or\n"
              "  - you reviewed in review.html and clicked "
              "'Download decisions.csv', but the file is still sitting in your\n"
              "    Downloads folder. Move it into the batch folder:\n"
              f"      mv ~/Downloads/decisions.csv {decisions_csv}\n",
              file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    write_tags = cfg["exif"]["write_tags"]
    pair_live_photos = bool(cfg["analysis"].get("pair_live_photos", True))

    print("Indexing sidecars for capture dates...")
    index = build_index(takeout)

    # Live Photo pairs re-detected directly from the files present (not read
    # from decisions.csv) so pairing is correct even against a hand-edited
    # CSV that doesn't understand pairing at all.
    live_pairs: dict[str, str] = {}  # {video_rel: photo_rel}
    if pair_live_photos:
        live_pairs = find_live_photo_pairs(takeout, list(iter_media(takeout)))
        if live_pairs:
            print(f"  {len(live_pairs)} Live Photo pair(s) detected; the photo's "
                  "decision governs each pair.")

    with open(decisions_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    decision_by_rel = {r["rel_path"]: r.get("decision", "") for r in rows}

    def effective_decision(rel: str) -> str:
        """The photo governs a Live Photo pair. A paired video's OWN
        decision cell is ignored in favor of its photo partner's, so a
        partial hand-edit (or a stale default in just one of the two rows)
        can't orphan one half of the pair."""
        photo_rel = live_pairs.get(rel)
        if photo_rel is not None and photo_rel in decision_by_rel:
            return decision_by_rel[photo_rel]
        return decision_by_rel.get(rel, "")

    keep_rows = [r for r in rows if not is_delete(effective_decision(r["rel_path"]))]
    delete_n = len(rows) - len(keep_rows)
    print(f"{len(rows)} files: {len(keep_rows)} keep, {delete_n} marked delete.\n")

    earliest_in_batch: datetime | None = None
    if args.fill_missing_dates:
        # Earliest across the WHOLE reviewed set (every row in decisions.csv,
        # not just keepers) -- matches "earliest in the set being reviewed".
        for r in rows:
            cd = (r.get("capture_date") or "").strip()
            if not cd:
                continue
            try:
                d = datetime.strptime(cd, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
            except ValueError:
                continue
            if earliest_in_batch is None or d < earliest_in_batch:
                earliest_in_batch = d
        if earliest_in_batch is None:
            print("  ! --fill-missing-dates: no resolvable dates anywhere in "
                  "decisions.csv to fall back to; date-less files will stay "
                  "undated.", file=sys.stderr)
        else:
            print(f"  --fill-missing-dates enabled: undated files will be "
                  f"assigned {earliest_in_batch.date().isoformat()} (earliest "
                  "in this batch)\n")

    copied = 0
    dated = 0
    unresolved: list[str] = []
    failed: list[str] = []
    mislabeled: list[str] = []
    fallback_dated: list[str] = []

    for r in keep_rows:
        rel = r["rel_path"]
        src = takeout / rel
        if not src.exists():
            failed.append(f"{rel} (source missing)")
            continue
        dest = reupload / rel
        is_video = src.suffix.lower() in VIDEO_EXTS

        dt = index.capture_dt(src) or media.exif_datetime(src)
        if dt is None and rel in live_pairs:
            # A paired video with no resolvable date of its own (Live Photo
            # MOVs sometimes lack useful metadata) borrows its photo
            # partner's date -- same capture moment, and the photo's date is
            # usually the more reliable of the two (real sidecar/EXIF).
            photo_src = takeout / live_pairs[rel]
            if photo_src.exists():
                dt = index.capture_dt(photo_src) or media.exif_datetime(photo_src)
        used_fallback = False
        if dt is None and earliest_in_batch is not None:
            dt = earliest_in_batch
            used_fallback = True

        if args.dry_run:
            if used_fallback:
                tag = f"{dt.date().isoformat()} (batch-earliest fallback)"
            else:
                tag = dt.date().isoformat() if dt else "NO DATE"
            print(f"  keep  {rel}  [{tag}]")
            copied += 1
            if used_fallback:
                fallback_dated.append(rel)
            elif not dt:
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

        if used_fallback:
            fallback_dated.append(rel)
        elif not dt:
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
    if fallback_dated:
        tag = earliest_in_batch.date().isoformat() if earliest_in_batch else "?"
        print(f"  batch-earliest date  : {len(fallback_dated)} (no sidecar/EXIF "
              f"date; assigned {tag} via --fill-missing-dates):")
        for u in fallback_dated[:20]:
            print(f"      - {u}")
        if len(fallback_dated) > 20:
            print(f"      ... and {len(fallback_dated) - 20} more")
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
