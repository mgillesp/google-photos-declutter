# Google Cloud / OAuth setup (one-time, ~10 minutes)

You click through this yourself in the browser. It sets up the credentials that
`scripts/03_upload.py` uses to upload keeper files back to Google Photos. None of
this touches the Photos grid — it's the Cloud Console + a standard OAuth consent.

**Do this once.** After that, uploads just need an occasional ~30-second re-auth
(see the note at the bottom).

---

## 1. Create a project
1. Go to <https://console.cloud.google.com/>.
2. Top bar → project dropdown → **New Project**.
3. Name it e.g. `photos-declutter` → **Create** → make sure it's the selected project.

## 2. Enable the Photos Library API
1. Go to **APIs & Services → Library** (<https://console.cloud.google.com/apis/library>).
2. Search **Photos Library API** → open it → **Enable**.

## 3. Configure the OAuth consent screen (keep it in *Testing*)
1. **APIs & Services → OAuth consent screen**.
2. User type: **External** → **Create**.
3. Fill required fields:
   - App name: `photos-declutter`
   - User support email: `mitchell.gillespie@gmail.com`
   - Developer contact email: `mitchell.gillespie@gmail.com`
   - (Logo/links optional — leave blank.)
4. **Scopes** step: you don't have to add the scope here; the script requests it at
   run time. You can skip adding scopes. → **Save and Continue**.
5. **Test users** step → **Add users**:
   - `mitchell.gillespie@gmail.com`
   - Kathryn's Google account email
   → **Save and Continue**.
6. Back on the OAuth consent screen, **leave Publishing status = "Testing"**.
   ⚠️ Do **NOT** click "Publish app" / do not submit for verification. Testing mode
   avoids Google's sensitive-scope review (which can be declined). The only cost is
   the 7-day refresh-token expiry, which is fine for monthly batches.

## 4. Create the OAuth client (Desktop app)
1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Application type: **Desktop app**. Name: `photos-declutter-cli` → **Create**.
3. In the dialog, click **Download JSON**.
4. Move that file to the (gitignored) credentials location the script expects:
   ```bash
   mkdir -p ~/.config/gphotos-declutter
   mv ~/Downloads/client_secret_*.json ~/.config/gphotos-declutter/client_secret.json
   ```

## 5. First authorization (happens automatically on first upload)
The first time you run `scripts/03_upload.py`, a browser window opens asking you to
sign in and grant the `photoslibrary.appendonly` scope. Sign in as the account whose
library you're restoring (Kathryn's, or whichever account owns the photos), approve,
and the script caches a token at `~/.config/gphotos-declutter/token.json`.

> You'll see a "Google hasn't verified this app" warning because the app is in
> Testing mode — that's expected. Click **Continue** (Advanced → Go to
> photos-declutter). Only listed test users can get past this screen.

---

## The 7-day re-auth (expected, not an error)
In Testing mode, refresh tokens expire after ~7 days. If more than a week has passed
since your last upload, `03_upload.py` will detect the expired token and
automatically reopen the ~30-second browser authorization. Just approve it again and
the upload resumes. Nothing is lost — the script is idempotent.

## Scope summary
- **Only** `https://www.googleapis.com/auth/photoslibrary.appendonly` is requested.
- This scope can upload new items and manage app-created items/albums. It **cannot**
  read or delete your existing library — deletion is your manual browser trash step,
  by design.
