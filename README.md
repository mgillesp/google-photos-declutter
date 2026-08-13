# google-photos-declutter

Local, privacy-preserving tooling to declutter a Google Photos library **one
calendar month at a time**, with a human-in-the-loop review before anything is
deleted.

## Design constraints (why it works this way)

1. **No cloud LLM ever sees your photos.** All analysis is local and deterministic
   (perceptual hashing, Laplacian-variance blur scoring, ffprobe), or an *optional*
   vision model running entirely on your own machine via Ollama. Nothing is uploaded
   to Claude/OpenAI/etc.
2. **No browser automation of the Google Photos grid.** The steps that touch the
   Photos website (exporting via Takeout, and moving a month to Trash) are done by
   **you**, manually, in a normal browser. The scripts never screenshot or drive that
   UI. Re-upload goes through the official **Google Photos Library API**, not the web
   UI.
3. **Human-in-the-loop before deletion.** You review a generated sheet and mark
   keep/delete yourself. The scripts never delete anything in your library.

The review sheet and your photos stay on your machine. This repo contains **only
code** — a strict `.gitignore` keeps all media, review sheets, tokens, and
credentials out of git.

---

## One-time setup

```bash
# 1. External tools
brew install czkawka exiftool ffmpeg tesseract

# 2. Python deps (Python 3.10+)
pip3 install -r requirements.txt

# 3. (optional) local vision model for the --ollama junk pass
#    brew install ollama && ollama serve & ; ollama pull moondream

# 4. Google Cloud / OAuth (needed only for the re-upload step)
#    Follow docs/GOOGLE_CLOUD_SETUP.md  (project in Testing mode, Desktop OAuth client,
#    photoslibrary.appendonly scope)

# 5. (optional) tune thresholds
cp config.example.yaml config.yaml   # config.yaml is gitignored
```

---

## The per-batch workflow (one calendar month)

Legend: **[YOU — browser]** = you do it manually in a browser · **[SCRIPT]** = you
run a local script. Work oldest month first as a low-risk shakedown.

### 1. [YOU — browser] Export the month via Takeout
- In Google Photos, search the month, **Select all**, add everything to a temporary
  album (e.g. `declutter-2019-05`).
- Run **Google Takeout** scoped to just that album; download the zip.
- Extract it into `batches/<YYYY-MM>/takeout/` in this repo, e.g.:
  ```
  batches/2019-05/takeout/Takeout/Google Photos/...
  ```
  (Anything under `batches/` is gitignored.)

### 2. [SCRIPT] Analyze + generate the review sheet
```bash
python3 scripts/01_analyze.py batches/2019-05
# add --ollama to also run the local vision junk pass
```
Produces `batches/2019-05/review.html` and `batches/2019-05/decisions.csv`.

### 3. [YOU / Kathryn] Review and mark decisions
- Open `review.html` in a browser (fully offline; thumbnails are embedded) — great
  for reviewing together.
- It groups: **exact duplicates · similar images · bursts · blur candidates ·
  oversized videos · text-heavy/document photos · junk candidates**. **Tap a
  photo** to toggle keep (✓ green) / delete (✕ red). Duplicate/similar/burst
  groups start with the sharpest shot pre-selected to keep — tap another if
  you'd rather keep a different one (or tap more than one to keep several).
  Tap the 🔍 icon on a card for a larger view. **Anything not shown on the
  page is unflagged and stays kept automatically** — you only need to review
  what's there.
- The toolbar shows a live **MB freed** total as you mark things for delete.
- **Text-heavy/document photos** are detected with local OCR (`tesseract`) —
  no cloud, no model download. Photos with a lot of recognizable text
  (photographed book/recipe pages, screenshots of text, receipts) get flagged
  since they're often low sentimental value, but they default to **keep**
  like the other single-item flags — nothing is pre-selected for deletion
  here. Tune the sensitivity via `text_word_threshold` in `config.yaml`.
- When done, click **⬇ Download decisions.csv** in the toolbar, then move the
  downloaded file into this batch's folder, overwriting the one `01_analyze.py`
  generated:
  ```bash
  mv ~/Downloads/decisions.csv batches/2019-05/decisions.csv
  ```
- Prefer editing the CSV by hand instead? `decisions.csv` is pre-filled with the
  same suggested defaults the page starts from — set `decision` to `delete` for
  anything you want gone, leave it blank to keep.

### 4. [SCRIPT] Restore capture dates onto keepers
```bash
python3 scripts/02_restore_exif.py batches/2019-05        # add --dry-run to preview
```
Copies every keeper into `batches/2019-05/reupload/` and writes the correct capture
date (from the Takeout JSON sidecars) into each file via exiftool.

### 5. [YOU — browser] Trash the month in Google Photos
- Search the same month again, **Select all**, **move to Trash**.
- Google keeps trash for **60 days** before permanent deletion — your safety net.

### 6. [SCRIPT] Re-upload the corrected keepers
```bash
python3 scripts/03_upload.py batches/2019-05              # add --dry-run to preview
```
Uploads keepers via the Photos API. Because dates are baked in, they slot back into
the right place in your timeline. Idempotent + resumable (`upload_log.json`).

Before uploading anything for real, it asks you to confirm you've **already**
completed step 5 (trashed the originals) — uploading before trashing would create
duplicates instead of a clean re-sort. Type `yes` to proceed. This is only asked
once per batch (a marker file remembers it), so retries/resumes (e.g. after a
7-day re-auth) won't ask again. `--dry-run` skips the prompt entirely since it
makes no real changes.

> First run opens a browser for Google authorization (Testing-mode consent, ~30s).
> If it's been >7 days since your last upload, it re-prompts automatically — expected,
> not an error. See `docs/GOOGLE_CLOUD_SETUP.md`.

---

## Repo layout
```
scripts/01_analyze.py       analysis + offline review-sheet generator
scripts/02_restore_exif.py  copy keepers + bake in correct capture dates
scripts/03_upload.py        OAuth + Photos API re-upload (no browser automation)
lib/                        shared helpers (sidecar matching, HEIC media, config)
config.example.yaml         tunable thresholds (blur, video size, similarity, ...)
docs/GOOGLE_CLOUD_SETUP.md  one-time Google Cloud / OAuth click-through
batches/<YYYY-MM>/          per-batch working dir (gitignored)
```

## Notes & limitations
- Capture dates are derived from the Takeout sidecar's UTC timestamp; timeline
  placement is accurate to the day (what Google sorts on). Near-midnight items may
  shift by a timezone offset.
- The `appendonly` scope is upload-only by design; it cannot read or delete your
  existing library. Deletion is always your manual, reversible (60-day trash) step.
- Tune `config.yaml` after your first batch if blur/similarity flagging is too
  aggressive or too loose.
