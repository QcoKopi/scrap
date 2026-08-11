# Roastery IG Keyword Scraper

Pulls keywords from a Google Sheet, searches Google (`site:instagram.com <keyword>`) via Oxylabs, writes organic results back into the "Hasil" sheet, and (optionally) fetches Instagram bio data for every account found — all runnable entirely from GitHub, no terminal required. `Keyword`, `Hasil`, and `Bio` are three tabs in **one spreadsheet**, so there's only ever one `GAS_WEB_APP_URL`.

## What was broken, and what changed

**The bug:** `Deskripsi` was always empty. Oxylabs' `google_search` parser returns each organic result's snippet under the key **`desc`** — the old script checked `description` and `snippet`, neither of which exist in the response. Confirmed against [Oxylabs' official field reference](https://developers.oxylabs.io/api-targets/search-engines/google/search/search) and fixed + tested in `src/auto_pipeline.py` (see `extract_organic_results`). A fixture-based test confirmed the fix populates `deskripsi` correctly.

**Other changes, made because of things this fix surfaced:**
- **Credentials moved out of the source code** and into environment variables / GitHub Secrets. The Oxylabs credentials that were hard-coded in the script you pasted are visible in this chat now — please **rotate/change that Oxylabs password** before using this, since anything pasted into a chat (or committed to git) should be treated as exposed.
- **The Apps Script web app currently has no authentication** — anyone with the URL can read your full keyword queue *and* write arbitrary rows into your sheet via `doPost`. I added an optional shared-secret check (`gas/Code.gs`) — see step 4 below. It's backward-compatible (skipped if you don't set it), but recommended, especially once the repo lives on GitHub.
- **Results are now sent to the sheet in one batched request per keyword** instead of one request per result (was up to ~10 requests/keyword). Your Keyword sheet has **2,336 rows** — batching matters at that scale for speed and to stay well within Apps Script's execution quotas.
- **A per-run keyword limit** (`MAX_KEYWORDS_PER_RUN`, default 200) so a single click doesn't accidentally burn through your entire Oxylabs quota/budget in one run across all 2,336 keywords. You control this from the GitHub Actions "Run workflow" screen.
- Retries with backoff on network/429/5xx errors, and clearer error messages when secrets are missing.

## New: Instagram bio scraping

`src/scrape_bios.py` + the **"Run IG Bio Scraper"** workflow fetch profile bio data for every unique real Instagram handle the keyword scraper found (in `Hasil`, column K), writing results into a new **"Bio"** sheet tab. Since `Keyword`, `Hasil`, and `Bio` are all tabs in the same spreadsheet, this reuses the same single `GAS_WEB_APP_URL` — no new secret needed.

**Read this before relying on it:** Oxylabs has no dedicated Instagram parser (confirmed against their docs — Google/Amazon get structured parsing, Instagram doesn't). This script instead calls Instagram's own public-but-unofficial `web_profile_info` endpoint through Oxylabs' generic `universal` source, a technique widely used by open-source Instagram tools but **not an officially documented or guaranteed API** — Instagram can change or block it without notice. Because of that, every handle gets an explicit `Status`, never a silently-blank bio:
- `OK` — bio retrieved
- `Akun privat (bio mungkin tidak lengkap)` — account is private
- `Tidak ditemukan / diblokir (...)` — no usable data (login wall, deleted account, blocked request, etc.)
- `HTTP <code>` — network/Oxylabs-level failure

Filter/sort the `Status` column in `Bio` to see what actually succeeded — don't treat a blank `Bio` cell as "no bio," check `Status` first.

**Setup:** none needed for the sheet itself — `gas/Code.gs` now auto-creates the **`Bio`** tab (with the correct header row) the first time it's needed, so there's no manual sheet/header setup to get wrong. Just make sure you've redeployed the updated `gas/Code.gs` (step 3 below), then use **Actions → Run IG Bio Scraper → Run workflow**.

**Account ID:** every Instagram handle also gets a stable 8-character `Account ID` (a deterministic hash of the handle — same handle always produces the same ID, computed independently, no lookup table needed), written both in `Hasil` (new column L, auto-added) and `Bio` (new column B). Since one account can appear across many `Hasil` rows, this ID is what you'd use to group/join all of an account's rows together (e.g. in a pivot table or VLOOKUP), rather than matching on the raw handle text.

Rows written *before* this feature existed won't have an ID retroactively (it's only computed at write-time) — to backfill them, open `GAS_WEB_APP_URL + "?action=backfillAccountIds"` directly in a browser once. It's safe to re-run (only fills blanks, never overwrites) and returns a small JSON summary of how many rows it updated.

**Recovering handles from `/p/` and `/reel/` links:** an individual post/reel URL (`instagram.com/p/<shortcode>/`) doesn't contain the poster's username at all — that's an Instagram URL-structure limitation, not something extractable from the link. `src/auto_pipeline.py` now falls back to scanning the post's title/snippet text for an `@mention` when the URL alone doesn't reveal the handle (e.g. "Dari @kopikita.jkt di Jakarta..."). Real-data check across the 2,336-keyword run: about **8.6%** of otherwise-placeholder rows had a recoverable mention this way — a modest but free win, applied automatically on every future run (existing rows aren't retroactively fixed, same reasoning as Account ID above).

## New: recent posts per account (caption, hashtags, engagement)

Every time the Bio Scraper fetches a profile, it also pulls that account's **~12 most recent posts** — media type (Foto/Video/Reel/Carousel), post URL, the first line of the caption ("Hook"), full caption, hashtags, post date, likes, comments, and views (Reels/video) — into an auto-created **`Posts`** sheet. This costs **zero extra requests**: Instagram's profile endpoint already embeds recent posts in the same response used for bio.

**Scope limit, on purpose:** this is a recent-12 snapshot, not full post history. Going further requires Instagram's paginated GraphQL endpoint, which needs a `doc_id` parameter Instagram rotates roughly every 2-4 weeks specifically to break scrapers. I deliberately didn't build that — it would work today and silently stop working on an unpredictable schedule, which is worse than not having it. If you need deep post history (not just the recent snapshot), that's a bigger undertaking — realistically a paid managed Instagram scraping API (e.g. Apify, Bright Data — roughly $1-2 per 1,000 results at the time of writing) rather than something to bolt onto this Oxylabs-based setup. Ask me if/when you want to go there.

**Hidden likes:** Instagram increasingly lets accounts hide their like counts. When that happens, the `Likes` cell shows `Disembunyikan/N-A`, never `0` — a real zero and "hidden" are different things, and showing 0 for hidden data would be misleading in exactly the way the original Deskripsi bug was.

## Running it — no terminal needed

This is the actual "frontend": **GitHub Actions' built-in "Run workflow" button**. It's secure (your credentials never leave GitHub's encrypted secrets store) and requires no custom app of my own to build or host.

Once set up (steps below), running the scraper is:
1. Go to your repo on GitHub → **Actions** tab
2. Click **"Run IG Keyword Scraper"** in the left sidebar
3. Click **"Run workflow"**, optionally adjust "how many keywords to process," click the green **Run workflow** button
4. Watch progress live in the log, or check the "Hasil" sheet directly

It also runs automatically every day at 02:00 UTC to keep chewing through the queue — remove the `schedule:` block in `.github/workflows/run-scraper.yml` if you only want manual runs.

## Setup (one-time)

### 1. Create the repo
Push this folder to a **new GitHub repository**. Simplest path if you don't already use git:
- Go to github.com → New repository → create it (private is recommended, see security note above)
- On your computer: download/unzip this project, then:
  ```bash
  cd roastery-ig-scraper
  git init
  git add .
  git commit -m "Initial commit: fixed scraper + Actions workflow"
  git branch -M main
  git remote add origin https://github.com/<your-username>/<your-repo>.git
  git push -u origin main
  ```
  (Or use GitHub Desktop / the "upload files" button on github.com if you'd rather not use the terminal at all — this is the only one-time step that benefits from it.)

### 2. Add your secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**. Add:

| Secret name | Value |
|---|---|
| `GAS_WEB_APP_URL` | Your Apps Script web app `/exec` URL |
| `OXYLABS_USERNAME` | Your Oxylabs username |
| `OXYLABS_PASSWORD` | Your Oxylabs password (**rotate it first**, see above) |
| `GAS_SHARED_TOKEN` | Only if you did step 4 below |

Secrets are encrypted, never shown in logs, and not visible to anyone browsing the repo.

### 3. Update the Apps Script
In your Google Sheet: **Extensions → Apps Script**, replace the code with `gas/Code.gs` from this repo, then **Deploy → Manage deployments → Edit → New version → Deploy** (keep the same `/exec` URL).

### 4. (Recommended) Lock down the Apps Script endpoint
In the Apps Script editor: **Project Settings → Script Properties → Add script property**, name it `SHARED_TOKEN`, value = any long random string you make up. Then add that same string as the `GAS_SHARED_TOKEN` repo secret. Now requests without the matching token are rejected.

### 5. Run it
Go to **Actions → Run IG Keyword Scraper → Run workflow** (and, once `Hasil` has data, **Run IG Bio Scraper → Run workflow**).

## Optional: read-only status dashboard

`dashboard/index.html` is a static page you can host for free on **GitHub Pages** (Settings → Pages → deploy from branch → `/dashboard` folder) to check progress from a phone or browser without opening the sheet. It only *reads* data — it can't trigger scraping (that stays on the Actions tab, which requires GitHub login).

To use it: the simplest way to get a CSV link is `https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/export?format=csv&gid=SHEET_GID` — find `SPREADSHEET_ID` in the sheet's normal edit URL, and `SHEET_GID` by clicking each tab and reading `#gid=...` at the end of the address bar. Since each tab has a different `gid`, this avoids a common mix-up where publishing "Hasil" and "Keyword" separately via **File → Share → Publish to web** produces the *same* link for both (easy to do if you forget to switch the sheet dropdown before republishing). Either method works, as long as the sheet's sharing is set to "Anyone with the link — Viewer" (Publish to web isn't required for the `/export` link).

## Local testing (optional)

```bash
cp .env.example .env   # fill in real values, never commit this file
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)   # or use a tool like direnv/python-dotenv
python src/auto_pipeline.py
python src/scrape_bios.py   # after Hasil has data and the Bio tab exists
```

## Cost/scale notes

Your Keyword sheet has 2,336 rows; each keyword = one Oxylabs `google_search` request without JS rendering (the script doesn't set `render`), which on the plan shown to me is **$1.00 per 1,000 results (Google)**. Processing the entire queue once costs roughly **$2.34** — and your plan's quota (up to 98,000 results) has plenty of headroom above that, so cost isn't really a constraint here.

The real constraint is the GitHub Actions job timeout (set to 90 minutes). At the default `MAX_KEYWORDS_PER_RUN=200`, clearing the whole queue takes ~12 runs (12 manual clicks, or ~12 days on the daily schedule). You can safely push `max_keywords` higher from the "Run workflow" screen if you want fewer, longer runs — just keep an eye on whether a run finishes within the timeout (visible in the Actions log), and raise `timeout-minutes` in `run-scraper.yml` if you increase the batch size a lot.

Bio scraping goes through the `universal` source (not `google_search`), which falls under the **"Other" tier at $1.15/1,000 results** on the plan shown to me. The number of unique Instagram handles depends on how many distinct accounts turn up across your keyword results — likely far fewer than 2,336 (many keywords will surface the same popular accounts) — so cost stays small, but unlike the keyword search this one hits Instagram directly, which is more failure-prone (see the Status caveats above) than it is expensive.
