# Troubleshooting

The handful of things people actually hit. Symptom → fix.

## Setup

**A tool isn't found** (`czkawka_cli`, `ffprobe`, `exiftool`, `tesseract`):

```bash
brew install czkawka exiftool ffmpeg tesseract
```

Two name mismatches trip people up: the Homebrew formula is `czkawka` but the
binary is `czkawka_cli`, and `ffprobe` comes from the `ffmpeg` formula, not a
package called `ffprobe`. `exiftool` is the one required tool — without it,
re-uploaded photos land at today's date instead of their original one.

**`ModuleNotFoundError` for `cv2`, `yaml`, `PIL`, `pillow_heif`, etc.:**

```bash
python3 -m pip install -r requirements.txt
```

Use `python3 -m pip` rather than bare `pip` so it installs into the interpreter
that actually runs the scripts.

**A large export won't extract, or extracts to nothing:** requesting a Takeout
file size over 2GB delivers Zip64-format zips, which some `unzip` builds can't
open. Check with `unzip -v | grep ZIP64_SUPPORT`; if that prints nothing,
`brew install p7zip` and extract with `7z x` instead. Staying at Takeout's 2GB
default avoids the question for a typical single-month batch.

## Analysis (`01_analyze.py`)

**"No photos or videos found" but the folder isn't empty:** not usually a
folder-depth issue — the search is recursive. Most likely an interrupted
download (verify with `unzip -t <zip>` before extracting) or the zip was never
actually extracted into `batches/<month>/takeout/`.

**It's taking a long time:** normal on a few thousand photos — the duplicate
and similar-image passes are the slow part. Not hung.

## Date restore (`02_restore_exif.py`)

**"No decisions file found":** you clicked **Download decisions.csv** in
`review.html`, but it's still in `~/Downloads`. Move it:

```bash
mv ~/Downloads/decisions.csv batches/2019-05/decisions.csv
```

## Google sign-in and OAuth

**"Google hasn't verified this app":** expected — the project is deliberately
in Testing mode. Click **Advanced** → **Go to `photos-declutter` (unsafe)**.

**Sign-in expires and re-prompts after about a week:** also expected — that's
the Testing-mode token lifetime. `03_upload.py` reopens a ~30-second browser
authorization automatically; nothing already uploaded is re-sent.

## Upload (`03_upload.py`)

**It errors or crashes partway through:** just re-run it. Uploads are
idempotent — `upload_log.json` tracks what already succeeded, and re-running
skips those files and retries only what failed. This covers network drops, an
expired sign-in, a sleeping laptop, and transient errors from Google's side
alike.

## After the fact

**I deleted something I wanted:** Google Photos → Trash. You have 60 days from
deletion to restore it.

**Running out of disk space:** each batch briefly holds the zip, the extracted
copy, and the staged re-upload copy at once. `scripts/04_cleanup.py` reclaims
it once a batch is verified fully uploaded.

## Tuning the flagging

Too much or too little caught? Copy `config.example.yaml` to `config.yaml`,
adjust, and re-run `01_analyze.py` — nothing is committed until you trash the
month, so re-analyzing costs only time.

- good photos called blurry → **lower** `blur_variance_threshold`
- unrelated photos grouped as similar → **lower** `czkawka_max_difference`
- obvious near-duplicates missed → **raise** `czkawka_max_difference` toward 15–20
- ordinary photos flagged as documents → **raise** `text_word_threshold`
