#!/usr/bin/env python3
"""04_cleanup.py -- reclaim local disk space after a batch is fully uploaded.

Run this LAST, after 03_upload.py reports every keeper uploaded successfully.
Deletes the bulky local media for a finished batch (Takeout zip(s), the
extracted takeout/ folder, the reupload/ staging copies, and review.html --
review.html embeds a real thumbnail of every flagged photo, so it counts as
media content too, not just a decisions record).

Keeps the actual work record: decisions.csv (every keep/delete decision) and
upload_log.json (proof of what was uploaded, with real mediaItemIds).

SAFETY: refuses to delete anything unless every non-deleted row in
decisions.csv has a matching "uploaded" entry in upload_log.json. This is a
hard requirement, not a --force-able default -- a batch with any missing or
failed upload is left completely untouched. The 60-day Google Photos trash
window is a second safety net, not a substitute for this check: once local
copies are gone, a script bug or a value mismatch can't be caught by hand
before it costs someone a real photo.

Usage:
  python3 scripts/04_cleanup.py batches/2014-08 --dry-run   # preview only
  python3 scripts/04_cleanup.py batches/2014-08             # asks to confirm
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DELETE_TOKENS = {"delete", "del", "d", "x", "remove", "rm", "trash", "no"}


def is_delete(decision: str) -> bool:
    return decision.strip().lower() in DELETE_TOKENS


def verify_fully_uploaded(batch_dir: Path) -> tuple[bool, list[str], int]:
    """Cross-check decisions.csv keepers against upload_log.json.

    Returns (ok, problems, keeper_count). `ok` is False if decisions.csv or
    upload_log.json is missing, or if any keeper isn't logged as uploaded
    with a real mediaItemId.
    """
    decisions_csv = batch_dir / "decisions.csv"
    log_path = batch_dir / "upload_log.json"
    problems: list[str] = []

    if not decisions_csv.exists():
        return False, [f"{decisions_csv} not found"], 0
    with open(decisions_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    keepers = [r["rel_path"] for r in rows if not is_delete(r.get("decision", ""))]

    if not keepers:
        return False, ["no keeper rows found in decisions.csv -- nothing to "
                       "verify, refusing to guess"], 0

    if not log_path.exists():
        return False, [f"{log_path} not found -- has 03_upload.py been run "
                       "for this batch?"], len(keepers)
    log = json.loads(log_path.read_text())

    for rel in keepers:
        entry = log.get(rel)
        if entry is None:
            # The keeper's on-disk name may have changed via
            # fix_mislabeled_extension() (e.g. .png -> .jpg); check by stem.
            stem_matches = [k for k in log if Path(k).stem == Path(rel).stem
                            and Path(k).parent == Path(rel).parent]
            if len(stem_matches) == 1:
                entry = log[stem_matches[0]]
            else:
                problems.append(f"{rel}: no upload_log.json entry")
                continue
        if entry.get("status") != "uploaded" or not entry.get("mediaItemId"):
            problems.append(f"{rel}: status={entry.get('status')!r}, "
                            f"mediaItemId={entry.get('mediaItemId')!r}")

    return (len(problems) == 0), problems, len(keepers)


def collect_deletion_targets(batch_dir: Path) -> tuple[list[Path], list[Path]]:
    """Return (to_delete, to_keep) as absolute paths."""
    to_delete: list[Path] = []
    for p in batch_dir.iterdir():
        if p.name in ("decisions.csv", "upload_log.json", ".trashed_confirmed"):
            continue
        if p.name == "review.html":
            to_delete.append(p)
        elif p.name in ("takeout", "reupload"):
            to_delete.append(p)
        elif p.suffix.lower() == ".zip":
            to_delete.append(p)
    to_keep = [batch_dir / "decisions.csv", batch_dir / "upload_log.json"]
    to_keep = [p for p in to_keep if p.exists()]
    return to_delete, to_keep


def human_size(path: Path) -> str:
    total = 0
    if path.is_dir():
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    elif path.is_file():
        total = path.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024:
            return f"{total:.0f}{unit}"
        total /= 1024
    return f"{total:.1f}TB"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("batch", type=Path, help="Batch dir (batches/YYYY-MM or batches/YYYY)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be deleted/kept without deleting anything")
    ap.add_argument("--yes", action="store_true",
                    help="Skip the interactive confirmation prompt")
    args = ap.parse_args()

    batch_dir = args.batch.resolve()
    if not batch_dir.is_dir():
        print(f"ERROR: {batch_dir} is not a directory.", file=sys.stderr)
        return 2

    ok, problems, keeper_count = verify_fully_uploaded(batch_dir)
    if not ok:
        print(f"REFUSING to clean up {batch_dir} -- not verified as fully "
              f"uploaded ({keeper_count} keeper(s) expected):", file=sys.stderr)
        for p in problems[:30]:
            print(f"  - {p}", file=sys.stderr)
        if len(problems) > 30:
            print(f"  ... and {len(problems) - 30} more", file=sys.stderr)
        print("\nRun scripts/03_upload.py for this batch until every keeper "
              "succeeds, then re-run cleanup.", file=sys.stderr)
        return 1

    print(f"Verified: all {keeper_count} keeper file(s) confirmed uploaded "
          "with a real mediaItemId.\n")

    to_delete, to_keep = collect_deletion_targets(batch_dir)
    if not to_delete:
        print("Nothing to clean up -- no takeout/reupload/zip/review.html "
              "found in this batch folder.")
        return 0

    print("Will DELETE (local media -- already safely in Google Photos):")
    for p in to_delete:
        print(f"  - {p.name}  ({human_size(p)})")
    print("\nWill KEEP (your work record):")
    for p in to_keep:
        print(f"  - {p.name}")

    if args.dry_run:
        print("\nDRY RUN -- nothing deleted.")
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print("\nERROR: non-interactive session and --yes not passed; "
                  "refusing to delete without explicit confirmation.",
                  file=sys.stderr)
            return 1
        answer = input(
            f'\nType "yes" to permanently delete the {len(to_delete)} item(s) '
            "above, or anything else to abort: "
        ).strip().lower()
        if answer != "yes":
            print("Aborted -- nothing deleted.")
            return 1

    for p in to_delete:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        print(f"  deleted: {p.name}")

    print(f"\nDone. {batch_dir} now contains only decisions.csv and "
          "upload_log.json (your record of this batch).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
