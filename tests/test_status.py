"""00_status.py -- stage inference for all five stages, JSON schema
stability, and (critically, since this script must be strictly read-only)
that running it never mutates a single byte on disk.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from conftest import import_script

status = import_script("00_status.py")


def _write_decisions(path: Path, rows: list[list[str]]) -> None:
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rel_path", "filename", "capture_date", "date_source", "size_mb",
                    "is_video", "blur_score", "text_words", "flags", "suggested",
                    "decision"])
        for row in rows:
            w.writerow(row)


def _build_five_stage_fixture(root: Path) -> None:
    # ready: takeout/ only
    (root / "2020-01" / "takeout" / "Photos").mkdir(parents=True)
    (root / "2020-01" / "takeout" / "Photos" / "IMG_0001.jpg").touch()

    # analyzed: review.html + decisions.csv
    (root / "2020-02" / "takeout" / "Photos").mkdir(parents=True)
    (root / "2020-02" / "takeout" / "Photos" / "IMG_0002.jpg").touch()
    (root / "2020-02" / "review.html").touch()
    _write_decisions(root / "2020-02" / "decisions.csv", [
        ["Photos/IMG_0002.jpg", "IMG_0002.jpg", "2020-02-01", "sidecar", "2.5",
         "no", "", "", "", "", "delete"],
    ])

    # dates_restored: reupload/ present
    (root / "2020-03" / "takeout" / "Photos").mkdir(parents=True)
    (root / "2020-03" / "reupload" / "Photos").mkdir(parents=True)
    (root / "2020-03" / "review.html").touch()
    _write_decisions(root / "2020-03" / "decisions.csv", [
        ["Photos/IMG_0003.jpg", "IMG_0003.jpg", "2020-03-01", "sidecar", "3.0",
         "no", "", "", "", "", ""],
    ])

    # uploading: upload_log.json present
    (root / "2020-04" / "takeout" / "Photos").mkdir(parents=True)
    (root / "2020-04" / "reupload" / "Photos").mkdir(parents=True)
    (root / "2020-04" / "review.html").touch()
    _write_decisions(root / "2020-04" / "decisions.csv", [
        ["Photos/IMG_0004.jpg", "IMG_0004.jpg", "2020-04-01", "sidecar", "4.0",
         "no", "", "", "", "", ""],
    ])
    (root / "2020-04" / "upload_log.json").write_text(
        json.dumps({"Photos/IMG_0004.jpg": {"status": "uploaded", "mediaItemId": "abc"}}))

    # trashed_confirmed: marker present
    (root / "2020-05" / "takeout" / "Photos").mkdir(parents=True)
    (root / "2020-05" / "reupload" / "Photos").mkdir(parents=True)
    (root / "2020-05" / "review.html").touch()
    _write_decisions(root / "2020-05" / "decisions.csv", [
        ["Photos/IMG_0005.jpg", "IMG_0005.jpg", "2020-05-01", "sidecar", "5.0",
         "no", "", "", "", "", "delete"],
        ["Photos/IMG_0006.jpg", "IMG_0006.jpg", "2020-05-01", "sidecar", "1.2",
         "no", "", "", "", "", ""],
    ])
    (root / "2020-05" / "upload_log.json").write_text(
        json.dumps({"Photos/IMG_0006.jpg": {"status": "uploaded", "mediaItemId": "def"}}))
    (root / "2020-05" / ".trashed_confirmed").write_text("confirmed via test\n")


def _hash_tree(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def test_all_five_stages_detected_correctly(tmp_path: Path) -> None:
    _build_five_stage_fixture(tmp_path)
    batches = status.collect_batches(tmp_path)
    stage_by_name = {b["name"]: b["stage"] for b in batches}
    assert stage_by_name == {
        "2020-01": "ready",
        "2020-02": "analyzed",
        "2020-03": "dates_restored",
        "2020-04": "uploading",
        "2020-05": "trashed_confirmed",
    }


def test_empty_batch_dir_reports_empty(tmp_path: Path) -> None:
    (tmp_path / "2020-09").mkdir()
    batches = status.collect_batches(tmp_path)
    assert batches[0]["stage"] == "empty"


def test_cumulative_totals_only_count_trashed_confirmed(tmp_path: Path) -> None:
    _build_five_stage_fixture(tmp_path)
    batches = status.collect_batches(tmp_path)
    totals = status.cumulative_totals(batches)
    # Only 2020-05 reached .trashed_confirmed: 1 delete row @ 5.0 MB.
    # 2020-02 also has a delete row, but it must NOT be counted -- that
    # batch was only analyzed, never confirmed trashed. This is the hard
    # gate: never claim a month was trashed when we can't see that it was.
    assert totals == {"months_processed": 1, "items_deleted": 1, "mb_freed": 5.0}


def test_json_output_is_valid_and_matches_schema(tmp_path: Path) -> None:
    _build_five_stage_fixture(tmp_path)
    batches = status.collect_batches(tmp_path)
    totals = status.cumulative_totals(batches)
    payload = json.dumps({"batches": batches, "totals": totals})

    parsed = json.loads(payload)  # raises if not valid JSON
    assert set(parsed.keys()) == {"batches", "totals"}
    assert set(parsed["totals"].keys()) == {"months_processed", "items_deleted", "mb_freed"}
    expected_batch_keys = {
        "name", "path", "stage", "stage_label", "media_count",
        "flagged_delete", "flagged_delete_mb", "uploaded_count", "trashed_confirmed",
    }
    for b in parsed["batches"]:
        assert set(b.keys()) == expected_batch_keys
    assert len(parsed["batches"]) == 5


def test_status_never_writes_anything(tmp_path: Path) -> None:
    _build_five_stage_fixture(tmp_path)
    before = _hash_tree(tmp_path)

    batches = status.collect_batches(tmp_path)
    status.cumulative_totals(batches)
    status.print_human(batches, status.cumulative_totals(batches))

    after = _hash_tree(tmp_path)
    assert before == after, "00_status.py must never modify a batch directory"


def test_no_batches_dir_returns_empty_list(tmp_path: Path) -> None:
    assert status.collect_batches(tmp_path / "does-not-exist") == []
