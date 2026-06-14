import os
import re
import threading
import tempfile
import zipfile
import shutil
import csv
import io
import json
from typing import List, Optional

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from . import config as cfg
from . import worker
from . import scraper
from . import llm

app = FastAPI(title="TikTok Scanner")


class CategoryIn(BaseModel):
    name: str
    search_terms: List[str]
    prompt: str
    enabled: bool = True


class SettingsIn(BaseModel):
    llm_provider: Optional[str] = None
    cookies_path: Optional[str] = None
    ollama_model: Optional[str] = None
    ollama_url: Optional[str] = None
    api_base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_model: Optional[str] = None
    storage_path: Optional[str] = None
    scan_interval_minutes: Optional[int] = None
    videos_per_scan: Optional[int] = None
    cache_ttl_hours: Optional[int] = None
    delete_after_download: Optional[bool] = None
    scanning_paused: Optional[bool] = None
    llm_batch_size: Optional[int] = None
    ollama_think: Optional[bool] = None
    ollama_num_ctx: Optional[int] = None
    ollama_num_predict: Optional[int] = None
    ollama_keep_alive: Optional[str] = None
    scrape_concurrency: Optional[int] = None
    request_delay_seconds: Optional[float] = None
    discovery_retries: Optional[int] = None
    webhook_url: Optional[str] = None
    auto_update_ytdlp: Optional[bool] = None


class TestPromptIn(BaseModel):
    url: str
    prompt: str
    category_id: Optional[int] = None


class ExampleIn(BaseModel):
    url: str
    label: str  # "match" or "no_match"


@app.on_event("startup")
def startup():
    db.init_db()
    config = cfg.load_config()
    worker.sweep_incomplete_files(config.get("storage_path"))
    threading.Thread(target=worker.run_loop, daemon=True).start()
    threading.Thread(target=worker.cache_cleanup_loop, daemon=True).start()
    threading.Thread(target=worker.health_check_loop, daemon=True).start()
    threading.Thread(target=worker.yt_dlp_update_loop, daemon=True).start()


# ---------------- Categories ----------------

@app.get("/api/categories")
def list_categories():
    return db.get_categories()


@app.post("/api/categories")
def add_category(cat: CategoryIn):
    name = cat.name.strip()
    if not name:
        raise HTTPException(400, "Category name cannot be empty")
    terms = [t.strip() for t in cat.search_terms if t.strip()]
    if not terms:
        raise HTTPException(400, "At least one search term is required")
    if not cat.prompt.strip():
        raise HTTPException(400, "Prompt cannot be empty")
    existing = [c for c in db.get_categories() if c["name"] == name]
    if existing:
        raise HTTPException(400, "A category with this name already exists")
    cat_id = db.create_category(name, terms, cat.prompt.strip(), cat.enabled)
    return {"id": cat_id}


@app.put("/api/categories/{cat_id}")
def edit_category(cat_id: int, cat: CategoryIn):
    if not db.get_category(cat_id):
        raise HTTPException(404, "Category not found")
    name = cat.name.strip()
    if not name:
        raise HTTPException(400, "Category name cannot be empty")
    terms = [t.strip() for t in cat.search_terms if t.strip()]
    if not terms:
        raise HTTPException(400, "At least one search term is required")
    if not cat.prompt.strip():
        raise HTTPException(400, "Prompt cannot be empty")
    dupes = [c for c in db.get_categories() if c["name"] == name and c["id"] != cat_id]
    if dupes:
        raise HTTPException(400, "A category with this name already exists")
    db.update_category(cat_id, name, terms, cat.prompt.strip(), cat.enabled)
    return {"ok": True}


@app.delete("/api/categories/{cat_id}")
def remove_category(cat_id: int):
    if not db.get_category(cat_id):
        raise HTTPException(404, "Category not found")
    db.delete_category(cat_id)
    return {"ok": True}


@app.post("/api/categories/{cat_id}/scan")
def scan_now(cat_id: int):
    if not db.get_category(cat_id):
        raise HTTPException(404, "Category not found")
    worker.scan_category_async(cat_id)
    return {"ok": True, "message": "Scan started"}


@app.get("/api/categories/{cat_id}/export")
def export_videos(cat_id: int, format: str = "csv", status: Optional[str] = None):
    cat = db.get_category(cat_id)
    if not cat:
        raise HTTPException(404, "Category not found")

    vids = db.get_videos(cat_id, status)
    safe_name = worker.safe_folder_name(cat["name"])

    if format == "json":
        return Response(
            content=json.dumps(vids, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}_videos.json"'},
        )

    # CSV
    buf = io.StringIO()
    fieldnames = ["video_id", "title", "author", "tags", "status", "reasoning", "url", "scraped_at"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for v in vids:
        row = dict(v)
        row["tags"] = ", ".join(row.get("tags") or [])
        writer.writerow(row)

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_videos.csv"'},
    )


@app.get("/api/categories/{cat_id}/videos")
def videos(cat_id: int, status: Optional[str] = None):
    if not db.get_category(cat_id):
        raise HTTPException(404, "Category not found")
    return db.get_videos(cat_id, status)


@app.get("/api/categories/{cat_id}/download-all")
def download_all_videos(cat_id: int, background_tasks: BackgroundTasks):
    cat = db.get_category(cat_id)
    if not cat:
        raise HTTPException(404, "Category not found")

    available = [
        v for v in db.get_videos(cat_id, "downloaded")
        if v.get("file_path") and os.path.isfile(v["file_path"])
    ]
    if not available:
        raise HTTPException(410, "No cached videos available to download for this category")

    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for v in available:
            zf.write(v["file_path"], arcname=os.path.basename(v["file_path"]))

    background_tasks.add_task(os.remove, tmp.name)
    safe_name = worker.safe_folder_name(cat["name"])
    return FileResponse(
        path=tmp.name,
        media_type="application/zip",
        filename=f"{safe_name}_videos.zip",
        background=background_tasks,
    )


@app.get("/api/videos/{video_id}/download")
def download_video_file(video_id: int, background_tasks: BackgroundTasks):
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(404, "Video not found")
    file_path = video.get("file_path")
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(410, "Video is no longer cached on the server (cache expired or already cleaned up)")

    filename = os.path.basename(file_path)
    config = cfg.load_config()
    if config.get("delete_after_download"):
        background_tasks.add_task(worker.remove_cached_file, video_id, file_path)

    return FileResponse(
        path=file_path,
        media_type="video/mp4",
        filename=filename,
        background=background_tasks,
    )


# ---------------- Settings ----------------

@app.get("/api/settings")
def get_settings():
    s = cfg.load_config()
    s = dict(s)
    s["api_key"] = "***" if s.get("api_key") else ""
    return s


@app.post("/api/settings")
def set_settings(s: SettingsIn):
    data = s.dict(exclude_unset=True)
    if data.get("api_key") == "***":
        data.pop("api_key")

    bounds = {
        "scan_interval_minutes": (1, None),
        "videos_per_scan": (1, None),
        "cache_ttl_hours": (0, None),
        "llm_batch_size": (1, None),
        "ollama_num_ctx": (1, None),
        "ollama_num_predict": (1, None),
        "scrape_concurrency": (1, None),
        "request_delay_seconds": (0, None),
        "discovery_retries": (0, None),
    }
    for key, (lo, hi) in bounds.items():
        if key in data and data[key] is not None:
            val = data[key]
            if lo is not None and val < lo:
                raise HTTPException(400, f"{key} must be >= {lo}")
            if hi is not None and val > hi:
                raise HTTPException(400, f"{key} must be <= {hi}")

    if "storage_path" in data and data["storage_path"] is not None:
        path = data["storage_path"].strip()
        if not path:
            raise HTTPException(400, "storage_path cannot be empty")
        try:
            os.makedirs(path, exist_ok=True)
            if not os.access(path, os.W_OK):
                raise HTTPException(400, f"storage_path '{path}' is not writable")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"Invalid storage_path '{path}': {e}")
        data["storage_path"] = path

    cfg.update_config(data)
    out = cfg.load_config()
    out["api_key"] = "***" if out.get("api_key") else ""
    return out


@app.get("/api/ollama-models")
def ollama_models():
    config = cfg.load_config()
    try:
        r = requests.get(f"{config['ollama_url']}/api/tags", timeout=10)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/test-connection")
def test_connection():
    """Check connectivity to the configured LLM backend (Ollama or API),
    including across the network if it's running on a different machine."""
    return llm.test_connection()


@app.post("/api/test-prompt")
def test_prompt(body: TestPromptIn):
    """Fetch metadata for a single TikTok video URL and run it through the
    given match prompt, without affecting the database or doing a full
    scan. Works for unsaved/edited prompts too — useful for tuning prompts
    before saving a category. If category_id is given, that category's
    saved example videos are included as few-shot examples too."""
    meta, err = scraper.get_metadata(body.url)
    if not meta or not meta.get("id"):
        raise HTTPException(400, f"Could not fetch metadata for that URL: {err or 'unknown error'}")

    examples = db.get_category_examples(body.category_id) if body.category_id else None
    is_match, reason = llm.evaluate_video(body.prompt, meta, examples)
    return {
        "meta": meta,
        "match": is_match,
        "reason": reason,
    }


@app.get("/api/categories/{cat_id}/examples")
def list_examples(cat_id: int):
    if not db.get_category(cat_id):
        raise HTTPException(404, "Category not found")
    return db.get_category_examples(cat_id)


@app.post("/api/categories/{cat_id}/examples")
def add_example(cat_id: int, body: ExampleIn):
    if not db.get_category(cat_id):
        raise HTTPException(404, "Category not found")
    if body.label not in ("match", "no_match"):
        raise HTTPException(400, "label must be 'match' or 'no_match'")

    url = body.url.strip()
    if not re.search(r"tiktok\.com", url, re.IGNORECASE):
        raise HTTPException(400, "That doesn't look like a TikTok URL")

    meta, err = scraper.get_metadata(url)
    if not meta or not meta.get("id"):
        raise HTTPException(400, f"Could not fetch metadata for that URL: {err or 'unknown error'}")

    example_id = db.add_category_example(cat_id, meta, body.label)
    return {"id": example_id, **meta, "label": body.label}


@app.delete("/api/categories/{cat_id}/examples/{example_id}")
def remove_example(cat_id: int, example_id: int):
    db.delete_category_example(example_id)
    return {"ok": True}


@app.get("/api/status")
def get_status():
    config = cfg.load_config()
    storage_path = config.get("storage_path")
    disk_free = None
    disk_total = None
    try:
        os.makedirs(storage_path, exist_ok=True)
        total, used, free = shutil.disk_usage(storage_path)
        disk_free = free
        disk_total = total
    except Exception:
        pass

    ollama_reachable = None
    if config.get("llm_provider") == "ollama":
        try:
            r = requests.get(f"{config['ollama_url']}/api/tags", timeout=5)
            ollama_reachable = r.ok
        except Exception:
            ollama_reachable = False

    return {
        "scanning_paused": bool(config.get("scanning_paused")),
        "worker_last_run": worker.status.get("worker_last_run"),
        "cleanup_last_run": worker.status.get("cleanup_last_run"),
        "disk_free_bytes": disk_free,
        "disk_total_bytes": disk_total,
        "ollama_reachable": ollama_reachable,
        "scraper_health_ok": worker.status.get("health_ok"),
        "scraper_health_detail": worker.status.get("health_detail"),
        "scraper_health_checked_at": worker.status.get("last_health_check"),
    }


@app.post("/api/scraper-health-check")
def scraper_health_check_now():
    """Manually trigger the TikTok scraper health probe (normally runs
    every 6 hours automatically)."""
    ok, detail = worker.scraper_health_check()
    return {"ok": ok, "detail": detail, "checked_at": worker.status.get("last_health_check")}


@app.get("/api/yt-dlp-version")
def yt_dlp_version():
    import subprocess
    try:
        cmd = scraper._ytdlp_bin() + ["--version"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return {"version": r.stdout.strip() or r.stderr.strip()}
    except Exception as e:
        return {"version": None, "error": str(e)}


@app.post("/api/yt-dlp-update")
def yt_dlp_update():
    """Manually trigger `pip install -U yt-dlp`. This is also run
    automatically once a day if `auto_update_ytdlp` is enabled."""
    ok, output = worker.update_yt_dlp()
    version = yt_dlp_version()
    return {"ok": ok, "output": output[-1000:], "version": version.get("version")}


@app.get("/api/boot-status")
def boot_status():
    """Check whether the tiktok-scanner systemd service is enabled to start
    on boot. Returns ok=False with an error if systemctl/sudo isn't usable
    (e.g. not running under systemd, or no passwordless sudo configured)."""
    import subprocess
    try:
        r = subprocess.run(
            ["systemctl", "is-enabled", "tiktok-scanner"],
            capture_output=True, text=True, timeout=10,
        )
        state = r.stdout.strip() or r.stderr.strip()
        return {"ok": True, "enabled": state == "enabled", "state": state}
    except FileNotFoundError:
        return {"ok": False, "error": "systemctl not found (not a systemd system?)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/boot-toggle")
def boot_toggle(body: dict):
    """Enable or disable starting the tiktok-scanner service on boot.
    Requires the user running the app to have passwordless sudo for
    `systemctl enable/disable tiktok-scanner` (the installer sets this up
    automatically; see README for manual setup)."""
    import subprocess
    enabled = bool(body.get("enabled"))
    action = "enable" if enabled else "disable"
    try:
        r = subprocess.run(
            ["sudo", "-n", "systemctl", action, "tiktok-scanner"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            err = r.stderr.strip() or r.stdout.strip()
            db.log("ERROR", f"Failed to {action} start-on-boot: {err}")
            raise HTTPException(
                500,
                f"Could not {action} the service: {err or 'unknown error'}. "
                f"Make sure passwordless sudo is configured for systemctl "
                f"(see README: 'Start on boot toggle').",
            )
        db.log("INFO", f"Start-on-boot {'enabled' if enabled else 'disabled'} via web UI")
        return {"ok": True, "enabled": enabled}
    except FileNotFoundError:
        raise HTTPException(500, "systemctl/sudo not found (not a systemd system?)")


# ---------------- Logs ----------------

@app.get("/api/logs")
def logs(limit: int = 200):
    return db.get_logs(limit)


# ---------------- Frontend ----------------

FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


# ---------------- Stats ----------------

@app.get("/api/stats")
def get_stats():
    return db.get_stats()


# ---------------- Video search ----------------

@app.get("/api/videos/search")
def search_videos_endpoint(q: str, cat_id: Optional[int] = None, status: Optional[str] = None, limit: int = 100):
    if not q or not q.strip():
        raise HTTPException(400, "q is required")
    return db.search_videos(q.strip(), cat_id=cat_id, status=status, limit=limit)


# ---------------- Bulk scan ----------------

@app.post("/api/categories/scan-all")
def scan_all():
    cats = db.get_categories()
    started = []
    for cat in cats:
        if cat["enabled"]:
            worker.scan_category_async(cat["id"])
            started.append(cat["id"])
    return {"ok": True, "started": started}


# ---------------- SSE live log stream ----------------

import queue as _queue
from fastapi.responses import StreamingResponse as _StreamingResponse

_log_subscribers: list = []
_log_sub_lock = threading.Lock()

_original_db_log = db.log

def _patched_log(level, message):
    _original_db_log(level, message)
    import datetime as _dt
    entry = json.dumps({"ts": _dt.datetime.utcnow().isoformat(), "level": level, "message": str(message)})
    with _log_sub_lock:
        dead = []
        for q in _log_subscribers:
            try:
                q.put_nowait(entry)
            except Exception:
                dead.append(q)
        for q in dead:
            _log_subscribers.remove(q)

# Monkey-patch so all modules share the same broadcaster
db.log = _patched_log
worker.db.log = _patched_log
scraper.db.log = _patched_log
llm.db.log = _patched_log


@app.get("/api/logs/stream")
def stream_logs():
    q: _queue.Queue = _queue.Queue(maxsize=200)
    with _log_sub_lock:
        _log_subscribers.append(q)

    def event_gen():
        try:
            while True:
                try:
                    data = q.get(timeout=20)
                    yield f"data: {data}\n\n"
                except _queue.Empty:
                    yield ": ping\n\n"
        except GeneratorExit:
            pass
        finally:
            with _log_sub_lock:
                if q in _log_subscribers:
                    _log_subscribers.remove(q)

    return _StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
