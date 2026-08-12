#!/usr/bin/env python3
"""
Post Owner Resolver
---------------------
Fixes rows in "Hasil" stuck with a placeholder Instagram Handle (@p or
@reel) by fetching the individual post's own page directly and reading
its `og:title` meta tag.

WHY THIS EXISTS
-----------------
Individual post/reel URLs (instagram.com/p/<shortcode>/) don't contain
the poster's username anywhere in the URL -- that's an Instagram
URL-structure limitation. src/auto_pipeline.py already recovers ~6-9% of
these via an @mention appearing in the search result's title/snippet
text (extract_mention_fallback), but most rows have no such mention at
all and stay as "@p"/"@reel" placeholders.

HOW THIS WORKS
----------------
Instagram populates every post page's `<meta property="og:title">` tag
with "{full name} (@{username}) - Instagram" specifically so link
previews render correctly on WhatsApp, Facebook, Slack, etc. -- services
that fetch the raw HTML without executing JavaScript or logging in. This
is a different (and hopefully more durable) technique than the
web_profile_info endpoint used for bios: Meta has an ongoing product
incentive to keep link-preview crawling working for logged-out, non-JS
clients across the entire web, unlike an internal app API.

That said, this is still not an official/documented/guaranteed API and
can be blocked or changed without notice, same caveat as everywhere else
in this project. Every attempted post gets an explicit outcome recorded
in the "Status Pemulihan Akun" column in "Hasil" -- "OK" on success,
or a specific failure reason -- so nothing is silently left blank AND
nothing gets retried forever once it's been tried once.
"""

import html
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

GAS_WEB_APP_URL = os.environ.get("GAS_WEB_APP_URL", "")
OXYLABS_USERNAME = os.environ.get("OXYLABS_USERNAME", "")
OXYLABS_PASSWORD = os.environ.get("OXYLABS_PASSWORD", "")
GAS_SHARED_TOKEN = os.environ.get("GAS_SHARED_TOKEN", "")

MAX_POST_OWNERS_PER_RUN = int(os.environ.get("MAX_POST_OWNERS_PER_RUN", "200"))
REQUEST_DELAY_SECONDS = float(os.environ.get("POST_OWNER_REQUEST_DELAY_SECONDS", "3"))

OXYLABS_URL = "https://realtime.oxylabs.io/v1/queries"

# Matches either attribute order: property before content, or content
# before property (Instagram's markup, or any HTML, isn't guaranteed to
# keep attributes in one specific order).
OG_TITLE_RE_LIST = [
    re.compile(r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']*)["\']', re.IGNORECASE),
    re.compile(r'<meta[^>]*content=["\']([^"\']*)["\'][^>]*property=["\']og:title["\']', re.IGNORECASE),
]
USERNAME_IN_TITLE_RE = re.compile(r"\(@([\w.]{2,30})\)")


def _session_with_retries() -> requests.Session:
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
    print("Mengambil antrean post @p/@reel dari Google Sheets...")
    sep = "&" if "?" in GAS_WEB_APP_URL else "?"
    url = f"{GAS_WEB_APP_URL}{sep}action=postOwnerQueue"
    try:
        response = session.get(url, timeout=30)
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
        raise RuntimeError(f"Gagal parse JSON dari GAS: {response.text[:300]}") from exc
    if not isinstance(queue_data, list):
        raise RuntimeError("Format antrean tidak valid (bukan list).")
    return queue_data


def extract_owner_from_html(page_html: str) -> Optional[str]:
    """Returns "@username" on success, None if og:title wasn't found or
    didn't contain a recognizable "(@username)" pattern.
    """
    title = None
    for pattern in OG_TITLE_RE_LIST:
        m = pattern.search(page_html)
        if m:
            title = html.unescape(m.group(1))
            break
    if not title:
        return None

    m = USERNAME_IN_TITLE_RE.search(title)
    if not m:
        return None
    return "@" + m.group(1)


def resolve_post_owner(session: requests.Session, post_url: str) -> Dict[str, Any]:
    """Returns {"instagramHandle": "@x", "status": "OK"} on success, or
    {"status": "<reason>"} on any failure -- never raises for expected
    failure modes, and never guesses a handle it isn't confident about.
    """
    payload = {
        "source": "universal",
        "url": post_url,
        "context": [
            {"key": "force_headers", "value": True},
            # Spoofing a known link-preview crawler's User-Agent. Sites
            # commonly serve full server-rendered HTML (with og:meta
            # intact) specifically to recognized crawlers like this one --
            # since that's what makes their own links preview correctly on
            # Facebook/WhatsApp -- while showing a login wall to
            # unrecognized/generic traffic. Worth trying before concluding
            # the technique doesn't work at all; not guaranteed to help.
            {
                "key": "headers",
                "value": {
                    "Accept": "text/html",
                    "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
                },
            },
        ],
    }

    try:
        resp = session.post(
            OXYLABS_URL, auth=(OXYLABS_USERNAME, OXYLABS_PASSWORD), json=payload, timeout=30
        )
    except requests.RequestException as exc:
        return {"status": f"Error jaringan: {exc}"}

    if resp.status_code != 200:
        return {"status": f"HTTP {resp.status_code}"}

    try:
        oxy_data = resp.json()
    except json.JSONDecodeError:
        return {"status": "Gagal parse respon Oxylabs"}

    results = oxy_data.get("results", [])
    if not results:
        return {"status": "Tidak ditemukan / diblokir (respon kosong)"}

    content = results[0].get("content")
    if not isinstance(content, str):
        return {"status": "Tidak ditemukan / diblokir (format respon tak dikenal)"}

    handle = extract_owner_from_html(content)
    if not handle:
        return {"status": "Tidak ditemukan / diblokir (og:title tidak ada atau formatnya beda, kemungkinan login wall)"}

    return {"instagramHandle": handle, "status": "OK"}


def send_result(session: requests.Session, row: int, result: Dict[str, Any]) -> bool:
    payload: Dict[str, Any] = {"type": "postOwner", "items": [{**result, "row": row}]}
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
        parsed = resp.json()
    except json.JSONDecodeError:
        print(f"  -> [GAGAL PARSE RESPON GAS]: {resp.text[:200]}")
        return False
    if parsed.get("status") != "success":
        print(f"  -> [GAS MENOLAK]: {parsed}")
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
        sys.exit(1)

    session = _session_with_retries()

    try:
        queue_data = fetch_queue(session)
    except RuntimeError as exc:
        print(str(exc))
        sys.exit(1)

    if not queue_data:
        print("Tidak ada post @p/@reel baru yang perlu dipulihkan namanya.")
        return

    total_in_queue = len(queue_data)
    batch = queue_data[:MAX_POST_OWNERS_PER_RUN]
    print(
        f"Total post tersisa di antrean: {total_in_queue}. "
        f"Memproses {len(batch)} post pada run ini (MAX_POST_OWNERS_PER_RUN={MAX_POST_OWNERS_PER_RUN})."
    )

    resolved, failed = 0, 0

    for item in batch:
        row = item.get("row")
        url = item.get("url", "")
        if not row or not url:
            continue

        print(f"\n[PROSES] Baris {row} - {url}...")
        result = resolve_post_owner(session, url)

        if result.get("status") == "OK":
            resolved += 1
            print(f"  -> [OK] Ditemukan: {result['instagramHandle']}")
        else:
            failed += 1
            print(f"  -> [GAGAL] {result.get('status')}")

        send_result(session, row, result)
        time.sleep(REQUEST_DELAY_SECONDS)

    remaining = total_in_queue - len(batch)
    print(
        f"\nSelesai. Berhasil dipulihkan: {resolved} | Gagal: {failed} | "
        f"Sisa di antrean setelah run ini: {remaining}"
    )
    if remaining > 0:
        print("Masih ada post tersisa. Jalankan workflow lagi untuk melanjutkan.")


if __name__ == "__main__":
    main()
