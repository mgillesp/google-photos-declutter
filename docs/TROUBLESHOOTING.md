# Troubleshooting & FAQ

Symptom → cause → fix. Read the relevant section rather than the whole file.

## Setup

### `czkawka_cli: command not found`

The Homebrew formula is `czkawka`; the binary it installs is `czkawka_cli`.

```bash
brew install czkawka
command -v czkawka_cli
```

If Homebrew installed it but the shell still can't find it, Homebrew's bin directory
isn't on `PATH` — `/opt/homebrew/bin` on Apple Silicon, `/usr/local/bin` on Intel.

Without it, `01_analyze.py` skips duplicate/near-duplicate detection entirely and
tells you so — the rest of the pipeline still runs, but this is most of the tool's
value, so don't skip installing it.

### `ffprobe: command not found`

Comes from `ffmpeg`, not a package called `ffprobe`: `brew install ffmpeg`. Without
it, oversized-video flagging is skipped; everything else still works.

### `exiftool not found`

`brew install exiftool`. This one is required — `02_restore_exif.py` exits with an
error rather than continuing without it, because re-uploaded photos would land at
today's date instead of their original one.

### `ModuleNotFoundError: No module named 'cv2'` (or `yaml`, `PIL`, `pillow_heif`, ...)

Python dependencies weren't installed, or were installed into a different
interpreter than the one running the scripts.

```bash
python3 -m pip install -r requirements.txt
```

Using `python3 -m pip` rather than bare `pip` installs into the interpreter that
will actually run the scripts, avoiding a confusing class of `ModuleNotFoundError`
later when the two don't match. `pillow_heif` specifically is what lets the pipeline
read HEIC files — the default format for recent iPhone photos — so don't skip it
even though its own import failure is silent (no HEIC support, no error).

### Python is too old

Needs 3.10+. Check: `python3 --version`. On macOS the system Python is often older
than the Homebrew one; `brew install python@3.12` and use `python3.12` explicitly
if needed.

---

## Analysis (`01_analyze.py`)

### "No photos or videos found" but the folder clearly isn't empty

This is **not** usually a folder-depth problem — the scripts search recursively, so
an extra or missing level of nesting under `takeout/` on its own won't cause this.
The real causes, in order of likelihood:

1. **The download was incomplete.** A multi-gigabyte Takeout export interrupted
   partway through produces a truncated zip that either fails to extract or
   extracts to almost nothing. Verify before extracting:
   ```bash
   unzip -t your-takeout-file.zip
   ```
   If that reports errors, re-download rather than trying to salvage it.
2. **The zip was never actually extracted** into the batch folder — check:
   ```bash
   find batches/2019-05/takeout -iname '*.jpg' -o -iname '*.heic' | head -5
   ```
   If that's empty, extract the zip into `batches/2019-05/takeout/` (any nesting
   depth inside there is fine).
3. **Wrong path.** A typo in the month folder name given to the script.

### Analysis is extremely slow

Normal, not a bug. The czkawka duplicate and similar-image passes are the slow
part and scale with photo/video count — several minutes for a few thousand items
is expected. If a batch has a lot of video, the similar-video pass adds more time;
`--skip-video-dedup` skips it if you don't need it for a given batch.

### `indexed 0 sidecars`

The JSON metadata files Takeout normally ships alongside each photo are missing —
usually because the photos were downloaded directly from the Google Photos web UI
instead of exported through Takeout. Analysis still runs and falls back to each
file's own embedded EXIF date, but some files (especially screenshots) may end up
undated. Re-export via Takeout if you want reliable dates.

### Everything is flagged / almost nothing is flagged

Thresholds, not a bug — see "Tuning the flagging" below. Re-running analysis is
free; nothing is committed to your library until you trash the month.

---

## Date restore (`02_restore_exif.py`)

### "No decisions file found"

You reviewed in `review.html` and clicked **Download decisions.csv**, but the file
landed in `~/Downloads` instead of the batch folder. Move it:

```bash
mv ~/Downloads/decisions.csv batches/2019-05/decisions.csv
```

Watch for `decisions (1).csv` if you downloaded more than once.

### Some files were skipped as undated

Files with no sidecar and no usable EXIF. `--fill-missing-dates` assigns the
earliest date found anywhere in the batch, which is a reasonable guess for a
tightly time-scoped batch (one month) but can place a file many months off in a
full-year batch — weigh that tradeoff before turning it on.

---

## Google sign-in and OAuth

### "Google hasn't verified this app"

Expected — the project is deliberately in Testing mode to avoid Google's
sensitive-scope review. Click **Advanced** → **Go to `photos-declutter` (unsafe)**.
It's your own app requesting access to your own photos on your own machine.

### Sign-in expired / re-prompting after about a week

Testing-mode refresh tokens last ~7 days. `03_upload.py` detects this and reopens
a ~30-second browser authorization automatically; nothing already uploaded is
re-sent. Expected, not an error.

### Browser opens, then the flow fails or hangs

Check in this order:

1. Is the signed-in account on the **Test users** list in the OAuth consent
   screen? Only listed accounts can pass the unverified-app warning.
2. Was the OAuth client created as **Desktop app**? A Web application client
   fails here with `redirect_uri_mismatch`.
3. Did the browser tab reach "The authentication flow has completed"? Closing it
   early cancels the flow.

### `redirect_uri_mismatch`

Wrong OAuth client type. Create a new **Desktop app** client, download it, and
replace `~/.config/gphotos-declutter/client_secret.json`.

### Forcing a completely fresh sign-in

```bash
rm ~/.config/gphotos-declutter/token.json
```

Safe — clears only the cached sign-in, not the client credentials.

---

## Upload (`03_upload.py`)

### HTTP 401 or 403

Usually an expired sign-in mid-run. Re-running re-authorizes and skips completed
uploads. If it persists, force a fresh sign-in (see above) — the token may carry
the wrong scope.

### HTTP 429

Google's daily rate limit. Wait a few hours and re-run; completed uploads are
skipped, so no progress is lost.

### HTTP 500 / 502 / 503 / 504

Google's side, usually transient. Re-run in a few minutes.

### Upload crashed partway (network drop, laptop sleep, etc.)

Just re-run. `upload_log.json` tracks what succeeded, and completed files are
skipped — this is true after a crash, a sleep, or an expired sign-in alike.

### Some items report `create_failed`

The bytes uploaded but Google declined to create the library item. Re-running
retries only the failures; a failure that persists on the same file usually means
that file is unsupported or corrupt.

---

## After the fact

### Photos came back at the wrong date

Dates come from the Takeout sidecar's UTC timestamp, so placement is accurate to
the day (what Google sorts on) — items near midnight can shift by a timezone
offset. If a whole batch looks wrong, sidecars were probably missing during date
restore; see `indexed 0 sidecars` above.

### The library now has duplicates

Upload ran before the month was trashed. Recoverable: trash the month again in
Google Photos, then re-upload from the still-staged `reupload/` folder.

### I deleted something I wanted

Google Photos → Trash. 60 days from deletion. If it's past 60 days and the file
was a keeper, check the batch's local `reupload/` folder before assuming it's
gone — it's there until `04_cleanup.py` runs for that batch.

### Running out of disk space

Each batch holds the zip, the extracted copy, and the staged re-upload copy —
roughly 2–3x the batch's size while all three exist at once. `scripts/04_cleanup.py`
reclaims it once a batch is verified fully uploaded (it refuses to run otherwise).

---

## Tuning the flagging

Too much or too little caught? Copy `config.example.yaml` to `config.yaml`,
adjust, and re-run `01_analyze.py` — nothing is committed until you trash the
month, so re-analyzing costs only time.

- good photos called blurry → **lower** `blur_variance_threshold`
- unrelated photos grouped as similar → **lower** `czkawka_max_difference`
- obvious near-duplicates missed → **raise** `czkawka_max_difference` toward 15–20
- ordinary photos flagged as documents → **raise** `text_word_threshold`

A high percentage of a batch getting flagged isn't inherently a sign the
thresholds are wrong — a genuinely duplicate-heavy or blurry period in your
library will legitimately flag a lot. Use these knobs when specific flagged
items look wrong, not because the overall percentage feels high.

---

## FAQ

### Why local-only? Isn't cloud AI better at spotting bad photos?

Maybe marginally, and it isn't worth it. Every analysis step runs on your
machine: perceptual hashing for duplicates, Laplacian variance for blur, ffprobe
for video, tesseract for OCR. The optional vision pass runs through Ollama, also
on your machine. No photo is ever sent to any cloud service. The code is MIT
licensed specifically so you don't have to take that on faith — read
`scripts/01_analyze.py` yourself.

### Why do I have to do the browser steps myself? Can't this be automated?

Google locked down the Photos Library API in March 2025. Third-party apps can no
longer read a pre-existing library — the only scope that still works is
`photoslibrary.appendonly`, which can upload new items but cannot read or delete
what's already there. So automated export/deletion isn't technically possible
through the official API, for anyone. Browser automation is the alternative, and
it's deliberately not used here: it breaks whenever Google adjusts the page, and
a script capable of bulk-deleting your library unattended is exactly the kind of
thing you don't want running without a human watching. Keeping deletion manual
means you see what's being deleted, with Google's 60-day trash as the undo.

### Can I use this on a library that isn't mine?

You need the owner's Google account on the OAuth consent screen's Test users
list, and you sign in as them during authorization — with their knowledge and
permission.

### Does it work on Windows?

Untested. The Python is portable but the setup instructions assume Homebrew. WSL
is the most likely path if you want to try it.
