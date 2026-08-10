#!/usr/bin/env python3
"""
Roastery Instagram Keyword Scraper
-----------------------------------
Pulls unprocessed keywords from a Google Sheet (via a Google Apps Script
Web App), searches Google (via Oxylabs SERP API) for
`site:instagram.com <keyword>`, and writes organic results back to the
"Hasil" sheet.

Fix (2026-08-10): Oxylabs' `google_search` parser returns the snippet text
under the key "desc" (not "description" or "snippet"). The old script only
checked "description"/"snippet", which don't exist in the response, so
Deskripsi was always written as an empty string. See:
https://developers.oxylabs.io/api-targets/search-engines/google/search/search
(section "Organic", output data dictionary).

Credentials are now read from environment variables instead of being
hard-coded, so this can run safely in GitHub Actions (via repo secrets)
without committing secrets to the repository.
"""

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------
# Configuration (env vars -- see .env.example / README.md)
# --------------------------------------------------------------------------
GAS_WEB_APP_URL = os.environ.get("GAS_WEB_APP_URL", "")
OXYLABS_USERNAME = os.environ.get("OXYLABS_USERNAME", "")
OXYLABS_PASSWORD = os.environ.get("OXYLABS_PASSWORD", "")
GEO_LOCATION = os.environ.get("OXYLABS_GEO_LOCATION", "Indonesia")

# Optional shared-secret token forwarded to the GAS web app (see gas/Code.gs).
# Leave unset if you haven't added token-checking to the Apps Script yet.
GAS_SHARED_TOKEN = os.environ.get("GAS_SHARED_TOKEN", "")

# Safety valve: how many keywords to process in a single run. Oxylabs
# charges per request and a single run processing all ~2,300+ keywords
# could take hours and cost real money. Override with MAX_KEYWORDS_PER_RUN.
MAX_KEYWORDS_PER_RUN = int(os.environ.get("MAX_KEYWORDS_PER_RUN", "200"))

# Delay between Oxylabs requests (seconds), be polite / avoid throttling.
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "2"))

OXYLABS_URL = "https://realtime.oxylabs.io/v1/queries"

IG_RESERVED_PATHS = {"p", "reel", "tv", "explore", "stories"}


def _session_with_retries() -> requests.Session:
    """A requests session that retries on transient network/5xx/429 errors."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_queue(session: requests.Session) -> List[Dict[str, Any]]:
    print("Mengambil antrean keyword dari Google Sheets...")
    try:
        response = session.get(GAS_WEB_APP_URL, timeout=30)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Gagal terhubung ke GAS (gangguan jaringan): {exc}\n"
            "Ini biasanya sementara -- coba jalankan workflow lagi."
        ) from exc

    if response.status_code != 200:
        raise RuntimeError(f"Gagal terhubung ke GAS. Status: {response.status_code}")

    try:
        queue_data = response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Gagal parse JSON dari GAS. Respon mentah: {response.text[:300]}"
        ) from exc

    if not isinstance(queue_data, list):
        raise RuntimeError("Format antrean tidak valid (bukan list).")

    return queue_data


def extract_ig_handle(link: str) -> str:
    if "instagram.com/" not in link:
        return ""
    parts = link.split("instagram.com/", 1)
    subparts = parts[1].split("/")
    handle = subparts[0] if subparts else ""
    if handle and handle not in IG_RESERVED_PATHS:
        return f"@{handle}"
    if "/reel/" in link:
        return "@reel"
    if "/p/" in link:
        return "@p"
    return "@instagram"


def extract_organic_results(oxy_data: Dict[str, Any], keyword: str) -> List[Dict[str, Any]]:
    """Turn a raw Oxylabs response into the row dicts the GAS web app expects.

    The description/snippet lives under the "desc" key in Oxylabs' parsed
    `results.organic[]` items -- NOT "description" or "snippet". This was
    the root cause of the empty "Deskripsi" column.
    """
    organics: List[Dict[str, Any]] = []
    res_raw = oxy_data.get("results", [])
    if isinstance(res_raw, list) and res_raw:
        content = res_raw[0].get("content", {})
        if isinstance(content, dict):
            results_dict = content.get("results", {})
            if isinstance(results_dict, dict):
                organics = results_dict.get("organic", []) or []

    rows: List[Dict[str, Any]] = []
    for idx, org in enumerate(organics, start=1):
        link = org.get("url", "")
        title = org.get("title", "")

        # FIX: "desc" is the correct key (Oxylabs docs: results.organic[].desc)
        snippet = org.get("desc", "")

        url_shown = org.get("url_shown", "")
        ig_handle = extract_ig_handle(link)
        favicon_source = (
            "Video" if ("/reel/" in link or "/p/" in link) else f"Instagram · {keyword}"
        )

        rows.append(
            {
                "account": keyword,
                "pos": idx,
                "posOverall": org.get("pos_overall", idx),
                "judul": title,
                "url": link,
                "urlShown": url_shown,
                "deskripsi": snippet,
                "faviconSource": favicon_source,
                "urutanHasil": str(idx),
                "instagramHandle": ig_handle,
            }
        )
    return rows


def query_oxylabs(session: requests.Session, keyword: str) -> Optional[Dict[str, Any]]:
    payload = {
        "source": "google_search",
        "query": f"site:instagram.com {keyword}",
        "geo_location": GEO_LOCATION,
        "parse": True,
    }
    try:
        resp = session.post(
            OXYLABS_URL, auth=(OXYLABS_USERNAME, OXYLABS_PASSWORD), json=payload, timeout=30
        )
    except requests.RequestException as exc:
        print(f"  -> [GANGGUAN JARINGAN KE OXYLABS]: {exc}")
        return None
    if resp.status_code != 200:
        print(f"  -> [GAGAL OXYLABS] Status Code: {resp.status_code} | {resp.text[:200]}")
        return None
    return resp.json()


def send_results(session: requests.Session, keyword: str, row: int, rows: List[Dict[str, Any]]) -> bool:
    """Send ALL organic results for one keyword in a single POST call.

    Batching avoids hammering the Apps Script web app with one HTTP request
    per organic result (previously up to ~10 requests/keyword). At 2,300+
    keywords in the queue that adds up fast and is unnecessarily slow/fragile.
    """
    payload: Dict[str, Any] = {"row": row, "account": keyword, "items": rows}
    if GAS_SHARED_TOKEN:
        payload["token"] = GAS_SHARED_TOKEN

    try:
        resp = session.post(GAS_WEB_APP_URL, json=payload, timeout=20)
    except requests.RequestException as exc:
        print(f"  -> [GANGGUAN JARINGAN KE SHEET]: {exc}")
        return False
    if resp.status_code != 200:
        print(f"  -> [GAGAL KIRIM KE SHEET] Status Code: {resp.status_code}")
        return False
    try:
        result = resp.json()
    except json.JSONDecodeError:
        print(f"  -> [GAGAL PARSE RESPON GAS]: {resp.text[:200]}")
        return False
    if result.get("status") != "success":
        print(f"  -> [GAS MENOLAK]: {result}")
        return False
    return True


def main() -> None:
    missing = [
        name
        for name, val in (
            ("GAS_WEB_APP_URL", GAS_WEB_APP_URL),
            ("OXYLABS_USERNAME", OXYLABS_USERNAME),
            ("OXYLABS_PASSWORD", OXYLABS_PASSWORD),
        )
        if not val
    ]
    if missing:
        print(f"ERROR: variabel lingkungan berikut belum diset: {', '.join(missing)}")
        print("Lihat .env.example / README.md untuk cara mengatur GitHub Actions secrets.")
        sys.exit(1)

    # TEMPORARY DEBUG (safe to print -- never prints the actual password,
    # only lengths/partial values, to diagnose a persistent 401 that isn't
    # explained by a wrong Oxylabs password). Remove once resolved.
    print(
        f"[DEBUG] OXYLABS_USERNAME = {OXYLABS_USERNAME!r} (len={len(OXYLABS_USERNAME)})"
    )
    print(f"[DEBUG] OXYLABS_PASSWORD length = {len(OXYLABS_PASSWORD)} characters")
    print(
        f"[DEBUG] OXYLABS_PASSWORD starts/ends with whitespace? "
        f"{OXYLABS_PASSWORD != OXYLABS_PASSWORD.strip()}"
    )
    print(f"[DEBUG] GAS_WEB_APP_URL = {GAS_WEB_APP_URL!r}")

    session = _session_with_retries()

    try:
        queue_data = fetch_queue(session)
    except RuntimeError as exc:
        print(str(exc))
        sys.exit(1)

    if not queue_data:
        print("Antrean kosong atau semua keyword sudah selesai diproses.")
        return

    total_in_queue = len(queue_data)
    batch = queue_data[:MAX_KEYWORDS_PER_RUN]
    print(
        f"Total keyword tersisa di antrean: {total_in_queue}. "
        f"Memproses {len(batch)} keyword pada run ini "
        f"(MAX_KEYWORDS_PER_RUN={MAX_KEYWORDS_PER_RUN})."
    )

    processed, failed, empty = 0, 0, 0

    for item in batch:
        row = item.get("row")
        keyword = item.get("account")
        if not keyword:
            continue

        print(f"\n[PROSES] Baris {row} - Keyword: '{keyword}'...")

        oxy_data = query_oxylabs(session, keyword)
        if oxy_data is None:
            failed += 1
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        rows = extract_organic_results(oxy_data, keyword)

        if not rows:
            print(f"  -> Tidak ada hasil organik ditemukan untuk '{keyword}'.")
            empty += 1
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        print(f"  -> Ditemukan {len(rows)} hasil organik. Mengirim ke sheet Hasil (1 request)...")
        if send_results(session, keyword, row, rows):
            print(f"  -> [SUKSES] Keyword '{keyword}' selesai dikirim ({len(rows)} baris).")
            processed += 1
        else:
            failed += 1

        time.sleep(REQUEST_DELAY_SECONDS)

    remaining = total_in_queue - len(batch)
    print(
        f"\nSelesai. Berhasil: {processed} | Kosong: {empty} | Gagal: {failed} | "
        f"Sisa di antrean setelah run ini: {remaining}"
    )
    if remaining > 0:
        print(
            "Masih ada keyword tersisa. Jalankan workflow lagi (manual atau tunggu "
            "jadwal berikutnya) untuk melanjutkan."
        )


if __name__ == "__main__":
    main()
