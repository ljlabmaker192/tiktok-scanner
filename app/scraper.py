import subprocess
import json
import os
import re
import sys
import shutil
import time
import threading

from . import config as cfg
from . import db


def _ytdlp_bin():
    """Return the yt-dlp command to use. Prefers the binary on PATH
    (installed system-wide via pip3), falls back to running as a Python
    module which always works as long as the package is installed."""
    cached = getattr(_ytdlp_bin, '_cache', None)
    if cached:
        return cached

    which = shutil.which('yt-dlp')
    if which:
        _ytdlp_bin._cache = [which]
    else:
        # Fallback: run as a module — works regardless of PATH
        _ytdlp_bin._cache = [sys.executable, '-m', 'yt_dlp']

    return _ytdlp_bin._cache


_rate_lock = threading.Lock()
_last_call_time = 0.0


def _rate_limit():
    """Throttle yt-dlp calls to roughly one every `request_delay_seconds`,
    across all threads. Helps avoid TikTok rate-limiting/blocking the
    server's IP when scanning many search terms."""
    global _last_call_time
    config = cfg.load_config()
    delay = float(config.get("request_delay_seconds", 1.5))
    if delay <= 0:
        return
    with _rate_lock:
        now = time.monotonic()
        wait = (_last_call_time + delay) - now
        if wait > 0:
            time.sleep(wait)
        _last_call_time = time.monotonic()


def _cookie_args():
    config = cfg.load_config()
    cp = config.get("cookies_path")
    if cp and os.path.exists(cp) and os.path.getsize(cp) > 0:
        age_days = (time.time() - os.path.getmtime(cp)) / 86400
        if age_days > 14:
            db.log(
                "WARN",
                f"Cookies file is {age_days:.0f} days old and may be expired. "
                f"If scans start returning 0 results, re-export fresh cookies to {cp}.",
            )
        return ["--cookies", cp]
    return []


def _run_ytdlp(cmd, timeout, context, fallback_cmds=None):
    """Run a yt-dlp command with rate-limiting and retries on transient
    failures. If `fallback_cmds` is given, each alternative command is tried
    (with its own retries) if the previous one fails entirely — used to work
    around expired cookies or TikTok API-hostname blocks.

    Returns (result, error_message). `result` is the completed subprocess
    result on success, or None if all attempts/fallbacks failed; in that
    case `error_message` describes the last failure."""
    config = cfg.load_config()
    retries = max(0, int(config.get("discovery_retries", 2)))

    for cmd_index, current_cmd in enumerate([cmd] + (fallback_cmds or [])):
        last_err = None
        for attempt in range(1, retries + 2):  # +1 for the initial attempt
            _rate_limit()
            try:
                out = subprocess.run(current_cmd, capture_output=True, text=True, timeout=timeout)
                if out.returncode == 0 and out.stdout.strip():
                    return out, None
                last_err = (out.stderr.strip() or "yt-dlp returned no output")[:500]
            except Exception as e:
                last_err = str(e)

            if attempt <= retries:
                db.log(
                    "WARN",
                    f"{context} attempt {attempt}/{retries + 1} failed: {last_err}. Retrying...",
                )
                time.sleep(2 * attempt)

        if cmd_index < len(fallback_cmds or []):
            db.log("WARN", f"{context}: retrying with fallback options after: {last_err}")

    db.log("ERROR", f"{context} failed after all attempts: {last_err}")
    return None, last_err


# Alternate TikTok API hostnames yt-dlp can target when the default one is
# being rate-limited/blocked. Used as a fallback, not the primary choice.
_FALLBACK_API_HOSTNAMES = [
    "api16-normal-c-useast1a.tiktokv.com",
    "api22-normal-c-useast2a.tiktokv.com",
]


def _build_fallback_cmds(base_cmd, cookie_args):
    """Build fallback yt-dlp commands: (1) same args but without cookies
    (helps if cookies are expired/invalid and causing a hard failure rather
    than just reduced access), and (2) alternate TikTok API hostnames."""
    fallbacks = []
    if cookie_args:
        no_cookie_cmd = [a for a in base_cmd if a not in cookie_args]
        fallbacks.append(no_cookie_cmd)
    for host in _FALLBACK_API_HOSTNAMES:
        fallbacks.append(base_cmd + ["--extractor-args", f"tiktok:api_hostname={host}"])
    return fallbacks


def _resolve_discovery_url(search_term):
    """Turn a configured search term into a TikTok URL to crawl. Supports:
      - "#hashtag" or plain words -> hashtag page (/tag/...)
      - "@username"               -> that creator's video feed
      - "search:some phrase" or "s:some phrase" -> general keyword search
    """
    term = search_term.strip()
    if not term:
        return None

    if term.startswith("@"):
        username = term[1:].strip()
        if not username:
            return None
        return f"https://www.tiktok.com/@{username}"

    lowered = term.lower()
    for prefix in ("search:", "s:"):
        if lowered.startswith(prefix):
            query = term[len(prefix):].strip()
            if not query:
                return None
            from urllib.parse import quote
            return f"https://www.tiktok.com/search?q={quote(query)}"

    hashtag = term.lstrip("#").strip().replace(" ", "")
    if not hashtag:
        return None
    return f"https://www.tiktok.com/tag/{hashtag}"


def discover_videos(search_term, limit=15):
    """Discover candidate video URLs for a search term using yt-dlp's
    flat-playlist mode (no download). The term can be a hashtag, an
    "@username" to follow a creator's feed, or "search:<phrase>" /
    "s:<phrase>" for a general keyword search."""
    url = _resolve_discovery_url(search_term)
    if not url:
        return []

    cookie_args = _cookie_args()
    base_discover = _ytdlp_bin() + ["--flat-playlist", "-J", "--playlist-end", str(limit), "--no-warnings", url]
    cmd = base_discover + cookie_args
    out, err = _run_ytdlp(
        cmd, timeout=180, context=f"discover_videos('{search_term}')",
        fallback_cmds=_build_fallback_cmds(base_discover, cookie_args),
    )
    if out is None:
        return []
    try:
        data = json.loads(out.stdout)
    except Exception as e:
        db.log("ERROR", f"discover_videos('{search_term}'): could not parse yt-dlp output: {e}")
        return []

    entries = data.get("entries", []) or []
    results = []
    for e in entries:
        vid_id = e.get("id")
        vid_url = e.get("url") or e.get("webpage_url")
        if not vid_id:
            continue
        if not vid_url or not vid_url.startswith("http"):
            uploader = e.get("uploader") or "user"
            vid_url = f"https://www.tiktok.com/@{uploader}/video/{vid_id}"
        results.append({"id": str(vid_id), "url": vid_url})
    return results


def get_metadata(video_url):
    """Fetch full metadata for a single video without downloading it.
    Returns (meta_dict, error_message); meta_dict is None on failure, with
    error_message describing why (useful for surfacing in the UI when a
    user adds an example video)."""
    video_url = _normalize_video_url(video_url)
    cookie_args = _cookie_args()
    base_meta = _ytdlp_bin() + ["-J", "--no-warnings", video_url]
    cmd = base_meta + cookie_args
    out, err = _run_ytdlp(
        cmd, timeout=120, context=f"get_metadata({video_url})",
        fallback_cmds=_build_fallback_cmds(base_meta, cookie_args),
    )
    if out is None:
        return None, err or "yt-dlp failed to fetch this video"
    try:
        d = json.loads(out.stdout)
    except Exception as e:
        msg = f"could not parse yt-dlp output: {e}"
        db.log("ERROR", f"get_metadata({video_url}): {msg}")
        return None, msg

    title = d.get("description") or d.get("title") or ""
    tags = d.get("tags") or re.findall(r"#(\w+)", title)
    return {
        "id": str(d.get("id")),
        "title": title,
        "author": d.get("uploader") or d.get("creator") or "",
        "tags": tags,
        "url": d.get("webpage_url") or video_url,
        "thumbnail": d.get("thumbnail") or "",
    }, None


def _normalize_video_url(video_url):
    """Clean up user-pasted TikTok URLs: strip tracking query params and
    surrounding whitespace. yt-dlp follows short-link redirects (vm.tiktok.com,
    vt.tiktok.com, tiktok.com/t/...) natively, so those are left as-is."""
    video_url = video_url.strip()
    base = video_url.split("?", 1)[0]
    return base or video_url


def download_video(video_url, out_dir, video_id, retries=3, backoff_seconds=5):
    """Download a clean (no-watermark) copy of the video into out_dir.
    Retries a few times with backoff on transient failures, then falls back
    to no-cookies / alternate API hostnames if all retries fail."""
    video_url = _normalize_video_url(video_url)
    os.makedirs(out_dir, exist_ok=True)
    out_template = os.path.join(out_dir, f"{video_id}.%(ext)s")
    cookie_args = _cookie_args()
    base_cmd = _ytdlp_bin() + ["-f", "mp4/best", "-o", out_template, "--no-warnings", video_url]
    cmd = base_cmd + cookie_args
    fallback_cmds = _build_fallback_cmds(base_cmd, cookie_args)

    last_err = None
    for cmd_index, current_cmd in enumerate([cmd] + fallback_cmds):
        for attempt in range(1, retries + 1):
            _rate_limit()
            try:
                out = subprocess.run(current_cmd, capture_output=True, text=True, timeout=300)
                if out.returncode == 0:
                    for f in os.listdir(out_dir):
                        if f.startswith(str(video_id)):
                            return os.path.join(out_dir, f)
                    last_err = "yt-dlp succeeded but no output file was found"
                else:
                    last_err = out.stderr.strip()[:300]
            except Exception as e:
                last_err = str(e)

            if attempt < retries:
                db.log(
                    "WARN",
                    f"download_video attempt {attempt}/{retries} failed for {video_url}: {last_err}. Retrying...",
                )
                time.sleep(backoff_seconds * attempt)

        if cmd_index < len(fallback_cmds):
            db.log("WARN", f"download_video: retrying with fallback options after: {last_err}")

    db.log("ERROR", f"download_video failed for {video_url} after all attempts: {last_err}")
    return None
