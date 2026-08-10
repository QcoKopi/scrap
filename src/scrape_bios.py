#!/usr/bin/env python3
"""
Instagram Bio Scraper
----------------------
Reads the list of unique, real Instagram handles already found in the
"Hasil" sheet (via GAS `?action=bioQueue`, which also excludes handles
that already have a Bio row) and fetches profile bio data for each,
writing results to the "Bio" sheet tab.

HOW IT WORKS / IMPORTANT CAVEATS
---------------------------------
Oxylabs does not have a dedicated, structured Instagram parser (unlike
Google/Amazon) -- see https://developers.oxylabs.io/api-targets. This
script instead uses Oxylabs' generic `universal` source to call
Instagram's own public (but undocumented/unofficial) profile-info
endpoint:

    https://i.instagram.com/api/v1/users/web_profile_info/?username=...

with a spoofed `x-ig-app-id` header, forwarded via Oxylabs' documented
`force_headers` context option
(https://developers.oxylabs.io/scraper-apis/web-scraper-api/features/http-context-and-job-management/headers-cookies-method).
This is a widely used technique (e.g. by the open-source `instaloader`
project), NOT an official/guaranteed API. Instagram can change or block
it at any time without notice, especially at volume, for private
accounts, or if Instagram serves a login wall instead of data.

Because of that, this script NEVER guesses or fabricates a bio. Every
handle gets an explicit "status" written to the sheet:
  - "OK"                          -> biography (possibly empty string
                                      if the account genuinely has none)
  - "Akun privat"                 -> profile exists but is private
  - "Tidak ditemukan / diblokir"  -> no usable data back (login wall,
                                      404, deleted account, IG blocking
                                      the request, etc.)
  - "HTTP <code>"                 -> Oxylabs/network-level failure

This makes gaps visible instead of silently blank, unlike the original
Deskripsi bug.
"""

import json
import os
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

MAX_BIOS_PER_RUN = int(os.environ.get("MAX_BIOS_PER_RUN", "200"))
REQUEST_DELAY_SECONDS = float(os.environ.get("BIO_REQUEST_DELAY_SECONDS", "3"))

OXYLABS_URL = "https://realtime.oxylabs.io/v1/queries"

# Well-known public web app id used by Instagram's own web client; sending
# it lets unauthenticated requests reach web_profile_info. Not a secret,
# but not officially documented/guaranteed either -- see module docstring.
IG_APP_ID = "936619743392459"


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


def fetch_bio_queue(session: requests.Session) -> List[Dict[str, Any]]:
    print("Mengambil antrean handle Instagram dari Google Sheets...")
    sep = "&" if "?" in GAS_WEB_APP_URL else "?"
    url = f"{GAS_WEB_APP_URL}{sep}action=bioQueue"
    response = session.get(url, timeout=15)
    if response.status_code != 200:
        raise RuntimeError(f"Gagal terhubung ke GAS. Status: {response.status_code}")
    try:
        queue_data = response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Gagal parse JSON dari GAS. Respon mentah: {response.text[:300]}\n"
            "Pastikan sheet 'Bio' sudah dibuat dan gas/Code.gs versi terbaru sudah di-deploy."
        ) from exc
    if not isinstance(queue_data, list):
        raise RuntimeError("Format antrean bio tidak valid (bukan list).")
    return queue_data


def fetch_profile(session: requests.Session, username: str) -> Dict[str, Any]:
    """Returns a dict with either parsed profile data or a "status" explaining failure.

    Never raises for expected failure modes (blocked/private/not found) --
    those are reported back as data, not exceptions, so one bad handle
    doesn't stop the whole run.
    """
    payload = {
        "source": "universal",
        "url": f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}",
        "context": [
            {"key": "force_headers", "value": True},
            {"key": "headers", "value": {"x-ig-app-id": IG_APP_ID, "Accept": "*/*"}},
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

    # `universal` source returns the raw page/response body. Instagram's
    # endpoint returns JSON text, but if IG served an HTML login-wall
    # page instead, `content` won't parse as JSON -- that's a real,
    # expected failure mode, not a bug.
    ig_data: Optional[Dict[str, Any]] = None
    if isinstance(content, dict):
        ig_data = content
    elif isinstance(content, str):
        try:
            ig_data = json.loads(content)
        except json.JSONDecodeError:
            return {"status": "Tidak ditemukan / diblokir (bukan JSON, kemungkinan login wall)"}
    else:
        return {"status": "Tidak ditemukan / diblokir (format respon tak dikenal)"}

    user = (ig_data or {}).get("data", {}).get("user")
    if not user:
        return {"status": "Tidak ditemukan / diblokir (akun tidak ada / dihapus)"}

    is_private = bool(user.get("is_private"))

    return {
        "namaLengkap": user.get("full_name", ""),
        "bio": user.get("biography", ""),
        "followers": (user.get("edge_followed_by") or {}).get("count", ""),
        "following": (user.get("edge_follow") or {}).get("count", ""),
        "posts": (user.get("edge_owner_to_timeline_media") or {}).get("count", ""),
        "website": user.get("external_url", ""),
        "isPrivate": is_private,
        "isVerified": bool(user.get("is_verified")),
        "status": "Akun privat (bio mungkin tidak lengkap)" if is_private else "OK",
    }


def send_bio_result(session: requests.Session, handle: str, data: Dict[str, Any]) -> bool:
    payload: Dict[str, Any] = {"type": "bio", "items": [{**data, "instagramHandle": handle}]}
    if GAS_SHARED_TOKEN:
        payload["token"] = GAS_SHARED_TOKEN

    resp = session.post(GAS_WEB_APP_URL, json=payload, timeout=20)
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
        sys.exit(1)

    session = _session_with_retries()

    try:
        queue_data = fetch_bio_queue(session)
    except RuntimeError as exc:
        print(str(exc))
        sys.exit(1)

    if not queue_data:
        print("Tidak ada handle Instagram baru yang perlu di-scrape bio-nya.")
        return

    total_in_queue = len(queue_data)
    batch = queue_data[:MAX_BIOS_PER_RUN]
    print(
        f"Total handle tersisa di antrean bio: {total_in_queue}. "
        f"Memproses {len(batch)} handle pada run ini (MAX_BIOS_PER_RUN={MAX_BIOS_PER_RUN})."
    )

    ok, private, failed = 0, 0, 0

    for item in batch:
        handle = item.get("handle", "")
        username = handle.lstrip("@")
        if not username:
            continue

        print(f"\n[PROSES] {handle}...")
        data = fetch_profile(session, username)
        status = data.get("status", "")

        if status == "OK":
            ok += 1
            print(f"  -> [OK] Bio ditemukan ({len(data.get('bio', ''))} karakter).")
        elif status.startswith("Akun privat"):
            private += 1
            print(f"  -> [PRIVAT] {status}")
        else:
            failed += 1
            print(f"  -> [GAGAL] {status}")

        send_bio_result(session, handle, data)
        time.sleep(REQUEST_DELAY_SECONDS)

    remaining = total_in_queue - len(batch)
    print(
        f"\nSelesai. OK: {ok} | Privat: {private} | Gagal: {failed} | "
        f"Sisa di antrean setelah run ini: {remaining}"
    )
    if remaining > 0:
        print("Masih ada handle tersisa. Jalankan workflow lagi untuk melanjutkan.")


if __name__ == "__main__":
    main()
