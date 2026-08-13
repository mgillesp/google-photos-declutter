#!/usr/bin/env python3
"""03_upload.py -- re-upload corrected keeper files via the Google Photos API.

No browser automation of the Photos grid. This talks to the Google Photos Library
API directly:
  1. Upload each file's bytes  -> receive an upload token
  2. mediaItems.batchCreate    -> create the media items (<=50 per call)

Because each file already carries the correct capture date (from 02_restore_exif),
Google Photos slots it back into the right place in the timeline automatically.

Scope: photoslibrary.appendonly only (the sole scope that still works for uploads
after Google's March 2025 API changes).

Auth: OAuth "Desktop app" client, project in Testing mode. Refresh tokens expire
after ~7 days in Testing mode; this script detects that and transparently re-runs a
~30-second browser authorization (that's Google's consent screen, NOT the Photos
grid) instead of dying with a cryptic error.

Idempotent: writes upload_log.json and skips files already uploaded, so a re-run
after re-auth resumes cleanly.

Usage:
  python3 scripts/03_upload.py batches/2019-05
  python3 scripts/03_upload.py batches/2019-05 --dry-run
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib.config import expand, load_config  # noqa: E402
from lib.sidecars import MEDIA_EXTS  # noqa: E402

UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
BATCH_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"

SETUP_DOC = "docs/GOOGLE_CLOUD_SETUP.md"


def get_credentials(cfg: dict):
    """Load/refresh OAuth creds, re-authorizing interactively when needed."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_secret = expand(cfg["upload"]["client_secret_path"])
    token_path = expand(cfg["upload"]["token_path"])
    scopes = [cfg["upload"]["scope"]]

    if not client_secret.exists():
        print(f"ERROR: OAuth client secret not found at {client_secret}\n"
              f"Follow {SETUP_DOC} to create a Desktop OAuth client and download it "
              f"there.", file=sys.stderr)
        raise SystemExit(3)

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)
        except (ValueError, json.JSONDecodeError):
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(token_path, creds)
            return creds
        except RefreshError:
            print("Refresh token expired (Testing-mode ~7-day limit). "
                  "Opening a quick re-authorization in your browser...\n",
                  file=sys.stderr)
            creds = None

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), scopes)
    print("A browser window will open for Google authorization "
          "(this is the consent screen, not the Photos grid).")
    creds = flow.run_local_server(port=0, prompt="consent")
    _save_token(token_path, creds)
    return creds


def _save_token(token_path: Path, creds) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    try:
        token_path.chmod(0o600)
    except OSError:
        pass


def upload_bytes(session, token: str, path: Path) -> str | None:
    """Upload one file's bytes, returning its upload token (or None on failure).

    A dropped connection/SSL error (Wi-Fi blip, laptop sleep, etc.) is retried
    once after a short pause instead of crashing the whole run -- past that,
    the file is reported as failed and the run continues with the rest.
    """
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "X-Goog-Upload-Content-Type": mime,
        "X-Goog-Upload-Protocol": "raw",
    }
    for attempt in (1, 2):
        try:
            with open(path, "rb") as f:
                resp = session.post(UPLOAD_URL, data=f, headers=headers, timeout=300)
        except requests.exceptions.RequestException as e:
            if attempt == 1:
                print(f"    ! network error uploading {path.name}, retrying once: {e}",
                      file=sys.stderr)
                time.sleep(3)
                continue
            print(f"    ! upload failed for {path.name} after retry: {e}",
                  file=sys.stderr)
            return None
        if resp.status_code == 200 and resp.text:
            return resp.text
        print(f"    ! upload failed for {path.name}: {resp.status_code} "
              f"{resp.text[:160]}", file=sys.stderr)
        return None
    return None


def batch_create(session, token: str, items: list[tuple[str, str]]) -> list[dict]:
    """items: list of (upload_token, filename). Returns per-item result dicts.

    Same network-error retry as upload_bytes -- a dropped connection here
    would otherwise crash the run even though the file bytes already made it
    to Google (just not yet turned into library items).
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"newMediaItems": [
        {"simpleMediaItem": {"uploadToken": ut, "fileName": fn}} for ut, fn in items
    ]}
    for attempt in (1, 2):
        try:
            resp = session.post(BATCH_URL, headers=headers, json=body, timeout=120)
        except requests.exceptions.RequestException as e:
            if attempt == 1:
                print(f"    ! network error on batchCreate, retrying once: {e}",
                      file=sys.stderr)
                time.sleep(3)
                continue
            print(f"    ! batchCreate failed after retry: {e}", file=sys.stderr)
            return []
        if resp.status_code != 200:
            print(f"    ! batchCreate failed: {resp.status_code} {resp.text[:200]}",
                  file=sys.stderr)
            return []
        return resp.json().get("newMediaItemResults", [])
    return []


def confirm_trashed(batch_dir: Path, confirm_flag: bool) -> bool:
    """Gate re-upload on having already trashed this month's originals.

    Uploading before trashing creates duplicates in the library instead of a
    clean re-sort -- exactly the opposite of decluttering. A marker file
    remembers a prior confirmation so retries/resumes for the same batch
    (e.g. after a 7-day re-auth) don't have to re-confirm every time.
    """
    marker = batch_dir / ".trashed_confirmed"
    if marker.exists():
        return True
    if confirm_flag:
        marker.write_text("confirmed via --confirm-trashed\n")
        return True
    if not sys.stdin.isatty():
        print("ERROR: re-upload hasn't been confirmed yet and this doesn't look "
              "like an interactive session. Re-run with --confirm-trashed once "
              "you've moved this month's originals to Trash in Google Photos.",
              file=sys.stderr)
        return False

    print()
    print("Before re-uploading: have you ALREADY moved this month's original")
    print("photos to Trash in Google Photos (search the month, select all,")
    print("move to trash)? If you upload before trashing, you'll end up with")
    print("duplicates in the library instead of a clean re-sort.")
    print()
    answer = input('Type "yes" to confirm you have trashed them, or anything '
                    'else to abort: ').strip().lower()
    if answer == "yes":
        marker.write_text("confirmed interactively\n")
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("batch", type=Path, help="Batch dir (batches/YYYY-MM)")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be uploaded without authenticating/uploading")
    ap.add_argument("--confirm-trashed", action="store_true",
                    help="Skip the interactive trash-confirmation prompt (for a "
                    "resumed/retry run once you've already confirmed once for "
                    "this batch)")
    args = ap.parse_args()

    batch_dir = args.batch.resolve()
    reupload = batch_dir / "reupload"
    if not reupload.is_dir():
        print(f"ERROR: {reupload} not found. Run 02_restore_exif.py first.",
              file=sys.stderr)
        return 2

    files = sorted(p for p in reupload.rglob("*")
                   if p.is_file() and p.suffix.lower() in MEDIA_EXTS)
    if not files:
        print(f"No media found in {reupload}; nothing to upload.")
        return 0

    log_path = batch_dir / "upload_log.json"
    log: dict = {}
    if log_path.exists():
        try:
            log = json.loads(log_path.read_text())
        except json.JSONDecodeError:
            log = {}

    pending = [p for p in files
               if log.get(str(p.relative_to(reupload)), {}).get("status") != "uploaded"]
    done = len(files) - len(pending)
    print(f"{len(files)} keeper files ({done} already uploaded, {len(pending)} pending).")

    if args.dry_run:
        for p in pending:
            print(f"  would upload: {p.relative_to(reupload)}")
        return 0
    if not pending:
        print("Nothing to do — all files already uploaded.")
        return 0

    if not confirm_trashed(batch_dir, args.confirm_trashed):
        print("\nAborted: re-upload not started. Trash this month's originals "
              "in Google Photos first, then re-run this script.", file=sys.stderr)
        return 1

    cfg = load_config(args.config)
    creds = get_credentials(cfg)
    batch_size = int(cfg["upload"]["batch_size"])
    session = requests.Session()

    uploaded = 0
    failed = 0
    # Process in batches: upload bytes, then batchCreate.
    for i in range(0, len(pending), batch_size):
        chunk = pending[i:i + batch_size]
        tokens: list[tuple[str, str, Path]] = []  # (upload_token, filename, path)
        for p in chunk:
            print(f"  uploading {p.relative_to(reupload)} ...")
            ut = upload_bytes(session, creds.token, p)
            if ut:
                tokens.append((ut, p.name, p))
            else:
                failed += 1
                log[str(p.relative_to(reupload))] = {"status": "upload_failed"}

        if not tokens:
            continue
        results = batch_create(session, creds.token,
                               [(ut, fn) for ut, fn, _ in tokens])
        if len(results) != len(tokens):
            # Whole batchCreate request failed (e.g. non-200) or returned a
            # mismatched result count -- don't silently drop these from the
            # failed count, mark every item in this chunk as failed.
            for ut, fn, path in tokens:
                failed += 1
                log[str(path.relative_to(reupload))] = {
                    "status": "create_failed",
                    "detail": "batchCreate request failed or returned no result",
                }
        else:
            # Map results back to files by position (API preserves order).
            for (ut, fn, path), res in zip(tokens, results):
                rel = str(path.relative_to(reupload))
                status = (res.get("status") or {})
                if status.get("message", "").lower() in ("success", "ok") or \
                        res.get("mediaItem"):
                    uploaded += 1
                    log[rel] = {"status": "uploaded",
                                "mediaItemId": res.get("mediaItem", {}).get("id")}
                else:
                    failed += 1
                    log[rel] = {"status": "create_failed", "detail": status}
                    print(f"    ! create failed for {fn}: {status}", file=sys.stderr)
        # Persist after each chunk so an interruption doesn't lose progress.
        log_path.write_text(json.dumps(log, indent=2))

    print(f"\nDone. uploaded={uploaded}, failed={failed}, "
          f"already-done={done}. Log: {log_path}")
    if failed:
        print("Some items failed — re-run this script to retry just those "
              "(successful ones are skipped).", file=sys.stderr)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
