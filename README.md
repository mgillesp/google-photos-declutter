# google-photos-declutter

**Find the duplicates, bursts, blurry shots and oversized videos in your Google
Photos library — without any of it leaving your computer.**

Free and open source (MIT). Runs locally. No server, no account, no cloud AI.

<!-- SCREENSHOT: replace with an image of review.html showing grouped duplicates
     and the MB-freed counter. Use a batch you don't mind publishing, or blur faces.
     ![The offline review sheet](docs/images/review-sheet.png) -->

---

## Why this exists

I had about twelve thousand photos in Google Photos and no realistic way to clean
them up.

Google's own **Review and Delete** tool flags blurry shots, screenshots and large
videos — but not duplicates, which was most of my problem. The paid tools that
*do* handle duplicates want around $40/year, and they want read access to your
entire photo library to do it.

So I wrote something that runs on my own machine instead, and used it on my
family's library a month at a time.

## What it does

- **Exact duplicates and near-identical shots** — perceptual hashing via czkawka
- **Burst sequences** grouped, with the sharpest frame pre-selected to keep
- **Blurry photos** scored by Laplacian variance
- **Oversized videos** flagged so you can move them somewhere cheaper
- **Screenshots and photographed documents** spotted by local OCR
- **An offline review page** — tap to keep or delete, with a running MB-freed total
- **Original capture dates preserved**, so keepers land back in the right place in
  your timeline rather than all appearing as "today"

Nothing is deleted without you reviewing it first, and Google's 60-day trash is
your undo.

## The privacy part, concretely

Every analysis step runs on your machine: perceptual hashing, blur scoring,
`ffprobe`, `tesseract` OCR. The optional vision pass runs through a local model
via Ollama. **No photo is ever sent to Anthropic, OpenAI, Google, or me.**

The only network call the tool makes is uploading your own keeper files back to
your own Google Photos account, through Google's official API.

You don't have to take my word for any of that — that's why this repo is public.
Read [`scripts/01_analyze.py`](scripts/01_analyze.py); it's the code that touches
your photos.

## What it deliberately doesn't do

- **It doesn't delete anything in your library.** Google restricted the Photos
  API in March 2025 — third-party apps can no longer read or delete existing
  library items. The only scope still available is `appendonly`, which uploads
  and nothing else. Deletion is your manual browser step, by design and by
  necessity.
- **It doesn't drive the Google Photos website.** No browser automation, no
  screenshotting the grid. That approach breaks whenever Google reskins a page,
  and a script with permission to bulk-delete your photos is not something you
  want running unattended.
- **It isn't an app.** It's Python scripts you run in a terminal.

If you see a product advertising full-library cloud scanning of Google Photos,
it's worth asking how they're getting that access.

---

## Requirements

- macOS or Linux (Windows untested; WSL will probably work)
- Python 3.10+
- Comfort with a terminal
- Homebrew, for four command-line tools

## Install

```bash
brew install czkawka exiftool ffmpeg tesseract
git clone https://github.com/<your-username>/google-photos-declutter.git
cd google-photos-declutter
pip3 install -r requirements.txt
```

Then follow [`docs/GOOGLE_CLOUD_SETUP.md`](docs/GOOGLE_CLOUD_SETUP.md) for the
one-time Google Cloud / OAuth setup (~10 minutes of browser clicking, needed only
for the re-upload step).

Optional threshold tuning:

```bash
cp config.example.yaml config.yaml    # gitignored
```

---

## The per-batch workflow

One calendar month at a time. **[BROWSER]** is you; **[SCRIPT]** is a command.
Start with your oldest month — it's the low-stakes place to learn how aggressive
the flagging is.

### 1. [BROWSER] Export the month via Takeout

Search the month in Google Photos, **Select all**, add everything to a temporary
album (e.g. `declutter-2019-05`). Run **Google Takeout** scoped to just that
album, download the zip, and extract it into the batch folder:

```
batches/2019-05/takeout/Takeout/Google Photos/...
```

Everything under `batches/` is gitignored.

### 2. [SCRIPT] Analyze

```bash
python3 scripts/01_analyze.py batches/2019-05
```

Add `--ollama` for the optional local vision pass. Produces `review.html` and
`decisions.csv`.

### 3. [YOU] Review

Open `batches/2019-05/review.html` — fully offline, thumbnails embedded.

Grouped into: exact duplicates · similar images · bursts · video bursts ·
similar videos · blur candidates · oversized videos · text-heavy/document
photos · junk candidates.

Video bursts use a wider, duration-aware window than photo bursts (a clip's
own length shouldn't count against it — see `video_burst_window_seconds` in
`config.yaml`). Videos already caught by a video burst are skipped in the
similar-videos pass so the same clip doesn't show up flagged twice.

Tap a photo to toggle keep (✓) / delete (✕). Duplicate, similar and burst groups
start with the sharpest shot pre-selected — tap another if you'd rather keep a
different one, or several to keep more than one. Single-item flags default to
keep. **Anything not shown on the page is kept automatically**, so you only
review what's flagged.

When done, click **⬇ Download decisions.csv**, then move it into the batch
folder:

```bash
mv ~/Downloads/decisions.csv batches/2019-05/decisions.csv
```

Prefer editing by hand? `decisions.csv` is pre-filled with the same defaults —
set `decision` to `delete` for anything you want gone.

### 4. [SCRIPT] Restore capture dates on the keepers

```bash
python3 scripts/02_restore_exif.py batches/2019-05 --dry-run
python3 scripts/02_restore_exif.py batches/2019-05
```

Copies keepers into `reupload/` and writes the correct capture date into each
file from the Takeout JSON sidecars.

### 5. [BROWSER] Trash the month

Search the month again, **Select all**, **move to Trash**. All of it — you have
clean dated copies staged locally. Google holds trash **60 days**.

Do this *before* step 6. Uploading first leaves you with duplicates.

### 6. [SCRIPT] Re-upload the keepers

```bash
python3 scripts/03_upload.py batches/2019-05 --dry-run
python3 scripts/03_upload.py batches/2019-05
```

Idempotent and resumable — if it fails partway, just run it again. It asks you to
confirm step 5 is done before uploading anything for real.

> First run opens a browser for Google authorization (~30s). If it's been more
> than 7 days since the last upload, it re-prompts automatically. That's the
> Testing-mode token expiry — expected, not an error.

### 7. Clean up

```bash
python3 scripts/04_cleanup.py batches/2019-05
```

Reclaims local disk space once you've verified the month looks right.

---

## Guided setup — $14

The code is free and always will be. If you'd rather not fumble through the
Google Cloud Console part, there's a paid bundle with the walkthrough I wish I'd
had:

- Annotated Google Cloud setup, with the two mistakes that cost me an evening
  called out inline
- A pre-flight checklist that catches problems in three minutes
- Full workflow guide with realistic timings
- Troubleshooting guide covering every error I actually hit
- **A Claude skill** that runs the pipeline with you, reads your error output,
  and tells you what went wrong — plus a paste-in prompt pack for ChatGPT

**[Get the guided setup →](https://gumroad.com/l/<your-product>)**

Entirely optional. Everything you need to use this tool is in this repo.

---

## Repo layout

```
scripts/01_analyze.py       analysis + offline review-sheet generator
scripts/02_restore_exif.py  copy keepers + bake in correct capture dates
scripts/03_upload.py        OAuth + Photos API re-upload
scripts/04_cleanup.py       reclaim local disk space after a verified batch
lib/                        sidecar matching, HEIC media, config, preflight checks
config.example.yaml         tunable thresholds
docs/GOOGLE_CLOUD_SETUP.md  one-time Google Cloud / OAuth click-through
batches/<YYYY-MM>/          per-batch working dir (gitignored)
```

## Notes & limitations

- Capture dates come from the Takeout sidecar's UTC timestamp; timeline placement
  is accurate to the day (what Google sorts on). Near-midnight items may shift by
  a timezone offset.
- The `appendonly` scope cannot read or delete your existing library. Deletion is
  always your manual, reversible step.
- Tune `config.yaml` after your first batch if flagging is too aggressive or too
  loose. Re-running analysis costs nothing — nothing is committed until step 5.
- **Keep an independent backup before you start.** 60-day trash is a safety net,
  not a backup.

## License

MIT — see [LICENSE](LICENSE). No warranty; you're responsible for your own photos
and your own Google account.
