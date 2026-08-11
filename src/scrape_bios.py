#!/usr/bin/env python3
"""
Instagram Bio + Recent Posts Scraper
--------------------------------------
Reads the list of unique, real Instagram handles already found in the
"Hasil" sheet (via GAS `?action=bioQueue`, which also excludes handles
that already have a Bio row) and fetches, per handle:
  1. Profile bio data -> "Bio" sheet tab
  2. The ~12 most recent posts (caption, hook, hashtags, media type,
     likes/comments/views) -> "Posts" sheet tab

Both come from the SAME Oxylabs request per handle -- Instagram's
web_profile_info response embeds the account's recent posts under
`edge_owner_to_timeline_media`, so extracting posts costs no extra
request, no extra rate-limit usage, and no extra reliability risk beyond
what bio scraping already has.

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

SCOPE LIMIT (intentional): this only extracts the ~12 most recent posts
that are already embedded in the profile response. Going deeper requires
Instagram's GraphQL pagination endpoint, which needs a `doc_id` parameter
that Instagram rotates roughly every 2-4 weeks specifically to break
scrapers -- deliberately NOT implemented here because it would silently
stop working on an unpredictable schedule. If you need full post history
instead of the recent-12 snapshot, that's a materially bigger, higher-
maintenance undertaking (realistically: a paid managed Instagram scraping
API) -- ask before assuming this script covers it.

Because of that, this script NEVER guesses or fabricates a bio or a post.
Every handle gets an explicit "status" written to the Bio sheet:
  - "OK"                          -> biography (possibly empty string
                                      if the account genuinely has none)
  - "Akun privat"                 -> profile exists but is private
  - "Tidak ditemukan / diblokir"  -> no usable data back (login wall,
                                      404, deleted account, IG blocking
                                      the request, etc.)
  - "HTTP <code>"                 -> Oxylabs/network-level failure

Likes/comments that Instagram hides (increasingly common) are written as
"Disembunyikan/N-A", never as 0 -- 0 would misleadingly look like real data.

This makes gaps visible instead of silently blank, unlike the original
Deskripsi bug.
"""

import re
from datetime import datetime, timezone

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
        raise RuntimeError(
            f"Gagal parse JSON dari GAS. Respon mentah: {response.text[:300]}\n"
            "Pastikan sheet 'Bio' sudah dibuat dan gas/Code.gs versi terbaru sudah di-deploy."
        ) from exc
    if not isinstance(queue_data, list):
        raise RuntimeError("Format antrean bio tidak valid (bukan list).")
    return queue_data


def fetch_profile(session: requests.Session, username: str) -> tuple:
    """Returns (bio_data: dict, raw_user: dict | None).

    raw_user is Instagram's full user JSON on success (used afterwards to
    also extract recent posts, at no extra request cost), or None on any
    failure -- bio_data["status"] explains why.

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
        return {"status": f"Error jaringan: {exc}"}, None

    if resp.status_code != 200:
        return {"status": f"HTTP {resp.status_code}"}, None

    try:
        oxy_data = resp.json()
    except json.JSONDecodeError:
        return {"status": "Gagal parse respon Oxylabs"}, None

    results = oxy_data.get("results", [])
    if not results:
        return {"status": "Tidak ditemukan / diblokir (respon kosong)"}, None

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
            return {"status": "Tidak ditemukan / diblokir (bukan JSON, kemungkinan login wall)"}, None
    else:
        return {"status": "Tidak ditemukan / diblokir (format respon tak dikenal)"}, None

    user = (ig_data or {}).get("data", {}).get("user")
    if not user:
        return {"status": "Tidak ditemukan / diblokir (akun tidak ada / dihapus)"}, None

    is_private = bool(user.get("is_private"))

    bio_data = {
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
    return bio_data, user


def _extract_caption(node: Dict[str, Any]) -> str:
    edges = ((node.get("edge_media_to_caption") or {}).get("edges")) or []
    if edges:
        return edges[0].get("node", {}).get("text", "") or ""
    return ""


def _media_type(node: Dict[str, Any]) -> str:
    if node.get("product_type") == "clips":
        return "Reel"
    if node.get("__typename") == "GraphSidecar" or node.get("edge_sidecar_to_children"):
        return "Carousel"
    if node.get("is_video"):
        return "Video"
    return "Foto"


def _engagement_count(node: Dict[str, Any], keys: List[str]) -> Optional[int]:
    """Returns None (not 0) when Instagram doesn't expose the count at all,
    e.g. an account that has hidden its like counts -- 0 would misleadingly
    look like a real, verified value of zero.
    """
    for key in keys:
        block = node.get(key)
        if isinstance(block, dict) and block.get("count") is not None:
            return block["count"]
    return None


def extract_posts(user: Dict[str, Any], handle: str) -> List[Dict[str, Any]]:
    """Pulls the recent-posts snapshot already embedded in the profile
    response (up to ~12 posts). See module docstring for why this doesn't
    paginate further.
    """
    timeline = user.get("edge_owner_to_timeline_media") or {}
    edges = timeline.get("edges") or []

    posts: List[Dict[str, Any]] = []
    for edge in edges:
        node = edge.get("node") or {}
        shortcode = node.get("shortcode", "")
        if not shortcode:
            continue

        media_type = _media_type(node)
        caption = _extract_caption(node)
        hook = caption.split("\n")[0][:150] if caption else ""
        hashtags = ", ".join(re.findall(r"#\w+", caption)) if caption else ""

        posted_at = ""
        taken_at = node.get("taken_at_timestamp")
        if taken_at:
            try:
                posted_at = datetime.fromtimestamp(int(taken_at), tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
            except (ValueError, OSError, OverflowError):
                posted_at = ""

        likes = _engagement_count(node, ["edge_media_preview_like", "edge_liked_by"])
        comments = _engagement_count(node, ["edge_media_to_comment", "edge_media_to_parent_comment"])
        views = node.get("video_view_count", node.get("video_play_count"))

        url_path = "reel" if media_type == "Reel" else "p"

        posts.append(
            {
                "instagramHandle": handle,
                "mediaType": media_type,
                "postUrl": f"https://www.instagram.com/{url_path}/{shortcode}/",
                "hook": hook,
                "caption": caption,
                "hashtags": hashtags,
                "postedAt": posted_at,
                "likes": likes if likes is not None else "Disembunyikan/N-A",
                "comments": comments if comments is not None else "Disembunyikan/N-A",
                "views": views if views is not None else "",
            }
        )
    return posts


def send_bio_result(session: requests.Session, handle: str, data: Dict[str, Any]) -> bool:
    payload: Dict[str, Any] = {"type": "bio", "items": [{**data, "instagramHandle": handle}]}
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


def send_post_results(session: requests.Session, posts: List[Dict[str, Any]]) -> bool:
    if not posts:
        return True
    payload: Dict[str, Any] = {"type": "posts", "items": posts}
    if GAS_SHARED_TOKEN:
        payload["token"] = GAS_SHARED_TOKEN

    try:
        resp = session.post(GAS_WEB_APP_URL, json=payload, timeout=20)
    except requests.RequestException as exc:
        print(f"  -> [GANGGUAN JARINGAN KE SHEET - POSTS]: {exc}")
        return False
    if resp.status_code != 200:
        print(f"  -> [GAGAL KIRIM POSTS KE SHEET] Status Code: {resp.status_code}")
        return False
    try:
        result = resp.json()
    except json.JSONDecodeError:
        print(f"  -> [GAGAL PARSE RESPON GAS - POSTS]: {resp.text[:200]}")
        return False
    if result.get("status") != "success":
        print(f"  -> [GAS MENOLAK - POSTS]: {result}")
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
    total_posts = 0

    for item in batch:
        handle = item.get("handle", "")
        username = handle.lstrip("@")
        if not username:
            continue

        print(f"\n[PROSES] {handle}...")
        data, raw_user = fetch_profile(session, username)
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

        if raw_user is not None:
            posts = extract_posts(raw_user, handle)
            if posts:
                send_post_results(session, posts)
                total_posts += len(posts)
                print(f"  -> [POSTS] {len(posts)} postingan terbaru diekstrak & dikirim.")
            else:
                print("  -> [POSTS] Tidak ada postingan ditemukan di respon (akun kosong/privat).")

        time.sleep(REQUEST_DELAY_SECONDS)

    remaining = total_in_queue - len(batch)
    print(
        f"\nSelesai. OK: {ok} | Privat: {private} | Gagal: {failed} | "
        f"Total postingan terekam: {total_posts} | "
        f"Sisa di antrean setelah run ini: {remaining}"
    )
    if remaining > 0:
        print("Masih ada handle tersisa. Jalankan workflow lagi untuk melanjutkan.")


if __name__ == "__main__":
    main()
