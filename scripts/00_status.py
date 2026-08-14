#!/usr/bin/env python3
"""00_status.py -- read-only status across every batch in batches/.

STRICTLY READ-ONLY. This script never writes, creates, or modifies any file
or directory. It only looks at what's already there and reports on it.

Stage is inferred purely from which files/dirs are present in a batch folder
(batches/<name>/), most-advanced marker wins:

    takeout/ only              -> "ready"             (ready to analyze)
    review.html + decisions    -> "analyzed"           (analyzed, review may
                                                         be incomplete -- we
                                                         can't see whether a
                                                         human has finished
                                                         clicking through
                                                         review.html, only
                                                         that it exists)
    reupload/                  -> "dates_restored"     (dates restored,
                                                         awaiting the manual
                                                         browser trash step)
    upload_log.json            -> "uploading"          (upload started,
                                                         possibly partial)
    .trashed_confirmed         -> "trashed_confirmed"  (trash step confirmed
                                                         for this batch)

A batch with none of the above present but a directory that exists is
reported as "empty" -- e.g. an empty folder created but nothing dropped in
yet. This mapping is deliberately conservative: the ONLY thing that marks a
batch as trashed is the .trashed_confirmed marker file, which is written by
03_upload.py after you explicitly confirm (interactively, or via
--confirm-trashed) that you already moved that month's originals to Trash in
the Google Photos web UI. This script never infers trashing any other way --
it has no way to see your actual Google Photos library, so it never claims a
month has been trashed unless that marker says so.

For the same reason, the cumulative "items deleted" / "space freed" totals
below only count batches that have reached .trashed_confirmed. A batch
that's merely analyzed or reviewed has *decisions*, not *deletions* --
nothing has actually left your library yet.

Usage:
  python3 scripts/00_status.py              # human-readable table
  python3 scripts/00_status.py --json       # machine-readable, stable schema
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib.sidecars import iter_media  # noqa: E402

DELETE_TOKENS = {"delete", "del", "d", "x", "remove", "rm", "trash", "no"}

STAGE_LABELS = {
    "empty": "empty (nothing dropped in yet)",
    "ready": "ready to analyze",
    "analyzed": "analyzed, review may be incomplete",
    "dates_restored": "dates restored, awaiting the manual trash step",
    "uploading": "upload started, possibly partial",
    "trashed_confirmed": "trash step confirmed",
}
# Order matters: index = how far along the pipeline this stage is.
STAGE_ORDER = ["empty", "ready", "analyzed", "dates_restored", "uploading",
               "trashed_confirmed"]


def is_delete(decision: str) -> bool:
    return decision.strip().lower() in DELETE_TOKENS


def resolve_takeout_dir(batch_dir: Path) -> Path:
    """Same convention as the other scripts: a nested takeout/ subfolder if
    present, otherwise the batch dir itself doubles as the takeout root."""
    if (batch_dir / "takeout").is_dir():
        return batch_dir / "takeout"
    return batch_dir


def read_decisions(batch_dir: Path) -> list[dict] | None:
    path = batch_dir / "decisions.csv"
    if not path.exists():
        return None
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_upload_log(batch_dir: Path) -> dict | None:
    path = batch_dir / "upload_log.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def batch_status(batch_dir: Path) -> dict:
    rows = read_decisions(batch_dir)
    log = read_upload_log(batch_dir)

    if (batch_dir / ".trashed_confirmed").exists():
        stage = "trashed_confirmed"
    elif log is not None:
        stage = "uploading"
    elif (batch_dir / "reupload").is_dir():
        stage = "dates_restored"
    elif (batch_dir / "review.html").exists() or rows is not None:
        stage = "analyzed"
    elif resolve_takeout_dir(batch_dir).is_dir() and \
            any(iter_media(resolve_takeout_dir(batch_dir))):
        stage = "ready"
    else:
        stage = "empty"

    media_count = len(rows) if rows is not None else None
    if media_count is None and stage == "ready":
        media_count = sum(1 for _ in iter_media(resolve_takeout_dir(batch_dir)))

    delete_rows = [r for r in rows if is_delete(r.get("decision", ""))] if rows else []

    def mb(r: dict) -> float:
        try:
            return float(r.get("size_mb") or 0)
        except ValueError:
            return 0.0

    uploaded_count = None
    if log is not None:
        uploaded_count = sum(1 for e in log.values() if e.get("status") == "uploaded")

    try:
        rel_path = str(batch_dir.relative_to(REPO_ROOT))
    except ValueError:
        rel_path = str(batch_dir)

    return {
        "name": batch_dir.name,
        "path": rel_path,
        "stage": stage,
        "stage_label": STAGE_LABELS[stage],
        "media_count": media_count,
        "flagged_delete": len(delete_rows) if rows is not None else None,
        "flagged_delete_mb": round(sum(mb(r) for r in delete_rows), 1) if rows else None,
        "uploaded_count": uploaded_count,
        "trashed_confirmed": stage == "trashed_confirmed",
    }


def collect_batches(batches_root: Path) -> list[dict]:
    if not batches_root.is_dir():
        return []
    dirs = sorted(p for p in batches_root.iterdir() if p.is_dir())
    return [batch_status(d) for d in dirs]


def cumulative_totals(batches: list[dict]) -> dict:
    # Only .trashed_confirmed batches count -- see module docstring for why:
    # this is the one signal we have that deletion actually happened.
    done = [b for b in batches if b["trashed_confirmed"]]
    return {
        "months_processed": len(done),
        "items_deleted": sum(b["flagged_delete"] or 0 for b in done),
        "mb_freed": round(sum(b["flagged_delete_mb"] or 0.0 for b in done), 1),
    }


def print_human(batches: list[dict], totals: dict) -> None:
    if not batches:
        print("No batches found under batches/. Extract a Takeout export into "
              "batches/<name>/takeout/ to get started.")
        return

    name_w = max(4, max(len(b["name"]) for b in batches))
    stage_w = max(5, max(len(b["stage_label"]) for b in batches))
    header = f"{'BATCH':<{name_w}}  {'STAGE':<{stage_w}}  {'FILES':>6}  {'FLAGGED':>8}"
    print(header)
    print("-" * len(header))
    for b in batches:
        files = "-" if b["media_count"] is None else str(b["media_count"])
        flagged = "-" if b["flagged_delete"] is None else str(b["flagged_delete"])
        print(f"{b['name']:<{name_w}}  {b['stage_label']:<{stage_w}}  "
              f"{files:>6}  {flagged:>8}")

    print()
    print(f"Months fully processed (trash confirmed): {totals['months_processed']}")
    print(f"Items deleted so far: {totals['items_deleted']}")
    print(f"Space freed so far: {totals['mb_freed']} MB")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true",
                    help="Emit machine-readable JSON instead of a human table")
    ap.add_argument("--batches-dir", type=Path, default=REPO_ROOT / "batches",
                    help="Override the batches/ root (mainly for testing)")
    args = ap.parse_args()

    batches = collect_batches(args.batches_dir.resolve())
    totals = cumulative_totals(batches)

    if args.json:
        print(json.dumps({"batches": batches, "totals": totals}, indent=2))
    else:
        print_human(batches, totals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
