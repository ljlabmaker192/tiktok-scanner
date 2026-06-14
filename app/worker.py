import time
import threading
import os
import re
import shutil
import datetime
import requests
from concurrent.futures import ThreadPoolExecutor

from . import db
from . import config as cfg
from . import scraper
from . import llm


MIN_FREE_BYTES = 500 * 1024 * 1024  # 500 MB safety margin

# Shared in-memory status, read by /api/status
status = {
    "worker_last_run": None,
    "cleanup_last_run": None,
    "last_health_check": None,
    "health_ok": None,
    "health_detail": None,
}


def notify_webhook(cat_name, meta, status_str, reason):
    """Best-effort POST to the configured webhook URL when a new match is
    found. Failures are logged but never interrupt the scan."""
    config = cfg.load_config()
    url = (config.get("webhook_url") or "").strip()
    if not url:
        return
    payload = {
        "category": cat_name,
        "video_id": meta.get("id"),
        "title": meta.get("title"),
        "author": meta.get("author"),
        "url": meta.get("url"),
        "status": status_str,
        "reason": reason,
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        db.log("WARN", f"Webhook notification failed: {e}")


def scraper_health_check():
    """Probe TikTok via yt-dlp using a known-stable, high-traffic hashtag
    (#fyp). Used to distinguish 'TikTok scraping is broken in general'
    (e.g. yt-dlp out of date, IP blocked, cookies expired) from 'this
    specific category/hashtag has nothing new'."""
    try:
        results = scraper.discover_videos("fyp", limit=1)
        ok = bool(results)
        detail = "OK" if ok else "No results returned for #fyp probe — discovery may be broken globally."
    except Exception as e:
        ok, detail = False, str(e)
    status["last_health_check"] = datetime.datetime.utcnow().isoformat()
    status["health_ok"] = ok
    status["health_detail"] = detail
    if not ok:
        db.log("WARN", f"Scraper health check failed: {detail}")
    return ok, detail


def health_check_loop():
    db.log("INFO", "Scraper health check loop started")
    # Run an initial check shortly after startup, then every 6 hours.
    time.sleep(30)
    while True:
        try:
            scraper_health_check()
        except Exception as e:
            db.log("ERROR", f"Health check error: {e}")
        time.sleep(6 * 60 * 60)


def update_yt_dlp():
    """Run `pip install -U yt-dlp` using the current Python's pip."""
    import subprocess
    import sys
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
            capture_output=True, text=True, timeout=180,
        )
        ok = out.returncode == 0
        output = (out.stdout + out.stderr).strip()
        # Clear the cached binary path so it's re-resolved after update
        if hasattr(scraper._ytdlp_bin, '_cache'):
            del scraper._ytdlp_bin._cache
        if ok:
            db.log("INFO", f"yt-dlp update check completed: {output.splitlines()[-1] if output else 'done'}")
        else:
            db.log("WARN", f"yt-dlp update failed: {output[-500:]}")
        return ok, output
    except Exception as e:
        db.log("ERROR", f"yt-dlp update error: {e}")
        return False, str(e)


def yt_dlp_update_loop():
    db.log("INFO", "yt-dlp auto-update loop started")
    while True:
        try:
            config = cfg.load_config()
            if config.get("auto_update_ytdlp", True):
                update_yt_dlp()
        except Exception as e:
            db.log("ERROR", f"yt-dlp auto-update loop error: {e}")
        time.sleep(24 * 60 * 60)  # daily


def safe_folder_name(name):
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_") or "category"


def has_disk_space(path):
    os.makedirs(path, exist_ok=True)
    total, used, free = shutil.disk_usage(path)
    return free > MIN_FREE_BYTES


def scan_category(cat):
    config = cfg.load_config()
    limit = config.get("videos_per_scan", 15)
    db.log("INFO", f"Scanning category '{cat['name']}'")

    out_dir_name = safe_folder_name(cat["name"])
    out_dir = f"{config['storage_path'].rstrip('/')}/{out_dir_name}"

    disk_ok = has_disk_space(config["storage_path"])
    if not disk_ok:
        db.log(
            "WARN",
            f"Low disk space at '{config['storage_path']}' — skipping video downloads for "
            f"'{cat['name']}' this scan (matches will still be recorded and retried later).",
        )

    # Retry previously-matched videos whose download failed
    if disk_ok:
        pending = db.get_videos_needing_download(cat["id"])
        for v in pending:
            file_path = scraper.download_video(v["url"], out_dir, v["video_id"])
            if file_path:
                db.update_video_download(v["id"], file_path)
                db.log("INFO", f"[{cat['name']}] Retried download succeeded: {v['video_id']}")
            if not has_disk_space(config["storage_path"]):
                disk_ok = False
                break

    concurrency = max(1, int(config.get("scrape_concurrency", 4)))

    candidates = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for result in pool.map(
            lambda term: scraper.discover_videos(term, limit=limit), cat["search_terms"]
        ):
            candidates.extend(result)

    seen = set()
    match_count = 0

    # Dedup before fetching full metadata (saves yt-dlp calls)
    to_fetch = []
    for c in candidates:
        vid_id = c["id"]
        if vid_id in seen or db.video_exists(cat["id"], vid_id):
            continue
        seen.add(vid_id)
        to_fetch.append(c)

    # First pass: gather metadata for all new candidates (no LLM calls yet),
    # fetched concurrently since these are network/CPU-bound yt-dlp calls.
    metas = []
    if to_fetch:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for meta, err in pool.map(lambda c: scraper.get_metadata(c["url"]), to_fetch):
                if meta and meta.get("id"):
                    metas.append(meta)

    new_count = len(metas)

    # Evaluate all candidates in one batched LLM call (falls back to
    # per-video calls automatically if the batch response is malformed)
    examples = db.get_category_examples(cat["id"])
    results = llm.evaluate_videos_batch(
        cat["prompt"], metas, chunk_size=max(1, int(config.get("llm_batch_size", 10))),
        examples=examples,
    )

    if results is None:
        # LLM backend unreachable — skip this scan without recording any
        # videos, so they're picked up again (not permanently "rejected").
        db.log(
            "WARN",
            f"[{cat['name']}] Skipping scan: LLM backend unreachable. "
            f"{new_count} candidate(s) will be re-checked next scan.",
        )
        db.set_last_scanned(cat["id"])
        return

    for meta, (is_match, reason) in zip(metas, results):
        if is_match:
            existing = db.find_existing_file(meta["id"])
            if existing:
                file_path = existing
                db.log(
                    "INFO",
                    f"[{cat['name']}] Reusing already-downloaded file for video {meta['id']} "
                    f"(matched in another category)",
                )
            elif disk_ok:
                file_path = scraper.download_video(meta["url"], out_dir, meta["id"])
                if not has_disk_space(config["storage_path"]):
                    disk_ok = False
            else:
                file_path = None
            status = "downloaded" if file_path else "matched"
            db.insert_video(
                cat["id"], meta["id"], meta["url"], meta["title"], meta["author"],
                meta["tags"], status, reason, file_path, meta.get("thumbnail"),
            )
            match_count += 1
            db.log("INFO", f"[{cat['name']}] MATCH ({status}): {meta['id']} - {reason}")
            notify_webhook(cat["name"], meta, status, reason)
        else:
            db.insert_video(
                cat["id"], meta["id"], meta["url"], meta["title"], meta["author"],
                meta["tags"], "rejected", reason, None, meta.get("thumbnail"),
            )

    db.set_last_scanned(cat["id"], new_candidates_count=new_count)
    if new_count == 0:
        streak = db.get_category(cat["id"]).get("empty_scan_streak", 0)
        if streak >= 3:
            db.log(
                "WARN",
                f"[{cat['name']}] No new candidates found for {streak} consecutive scans. "
                f"Discovery may be broken (TikTok layout change, expired cookies, etc.) — "
                f"check 'yt-dlp -U' and cookies.txt.",
            )
    db.log(
        "INFO",
        f"Finished scanning '{cat['name']}': {new_count} new videos checked, {match_count} matched",
    )


def scan_category_async(cat_id):
    cat = db.get_category(cat_id)
    if not cat:
        return
    t = threading.Thread(target=scan_category, args=(cat,), daemon=True)
    t.start()


def remove_cached_file(video_row_id, file_path):
    """Remove a single cached video file after it has been sent to the client.
    Skips actual deletion if another category's record still points at the
    same file (the same TikTok video matched multiple categories)."""
    try:
        if os.path.isfile(file_path) and not db.file_path_in_use(file_path, exclude_video_row_id=video_row_id):
            os.remove(file_path)
            db.log("INFO", f"Removed cached file after download: {file_path}")
    except Exception as e:
        db.log("ERROR", f"Failed to remove cached file {file_path}: {e}")
    db.clear_video_cache(video_row_id)


def sweep_incomplete_files(storage_path):
    """Remove orphaned yt-dlp partial-download artifacts (.part, .ytdl, .tmp)
    left behind by interrupted downloads from a previous run."""
    if not storage_path or not os.path.isdir(storage_path):
        return
    removed = 0
    for root, _dirs, files in os.walk(storage_path):
        for f in files:
            if f.endswith((".part", ".ytdl", ".tmp")) or ".part-Frag" in f:
                try:
                    os.remove(os.path.join(root, f))
                    removed += 1
                except Exception:
                    pass
    if removed:
        db.log("INFO", f"Startup cleanup: removed {removed} incomplete download artifact(s)")


def cleanup_cache():
    """Delete cached video files on disk that have exceeded the TTL,
    freeing server storage once the client has had time to download them.
    A TTL of 0 disables time-based cleanup entirely (files only removed
    via delete-after-download, if enabled)."""
    config = cfg.load_config()
    ttl_hours = float(config.get("cache_ttl_hours", 24))
    if ttl_hours <= 0:
        return
    for v in db.get_expired_cached_videos(ttl_hours):
        file_path = v.get("file_path")
        if file_path and os.path.isfile(file_path) and not db.file_path_in_use(file_path, exclude_video_row_id=v["id"]):
            try:
                os.remove(file_path)
                db.log("INFO", f"Removed cached file (TTL expired): {file_path}")
            except Exception as e:
                db.log("ERROR", f"Failed to remove cached file {file_path}: {e}")
        db.clear_video_cache(v["id"])


def cache_cleanup_loop():
    db.log("INFO", "Cache cleanup loop started")
    while True:
        try:
            cleanup_cache()
            status["cleanup_last_run"] = datetime.datetime.utcnow().isoformat()
        except Exception as e:
            db.log("ERROR", f"Cache cleanup error: {e}")
        time.sleep(15 * 60)


def run_loop():
    db.log("INFO", "Worker loop started")
    while True:
        interval = 30
        try:
            config = cfg.load_config()
            interval = max(1, int(config.get("scan_interval_minutes", 30)))
            if config.get("scanning_paused"):
                db.log("INFO", "Scanning is paused; skipping this cycle")
            else:
                for cat in db.get_categories():
                    if cat["enabled"]:
                        try:
                            scan_category(cat)
                        except Exception as e:
                            db.log("ERROR", f"Error scanning category '{cat['name']}': {e}")
            status["worker_last_run"] = datetime.datetime.utcnow().isoformat()
        except Exception as e:
            db.log("ERROR", f"Worker loop error: {e}")
        time.sleep(interval * 60)
