import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "db.sqlite3")

DEFAULT_CONFIG = {
    "llm_provider": "ollama",          # "ollama" or "api"
    "ollama_model": "qwen2.5:14b",
    "ollama_url": "http://localhost:11434",
    "api_base_url": "https://api.openai.com/v1",
    "api_key": "",
    "api_model": "gpt-4o-mini",
    "storage_path": os.path.join(DATA_DIR, "downloads"),
    "scan_interval_minutes": 30,
    "videos_per_scan": 15,
    "cookies_path": os.path.join(DATA_DIR, "cookies.txt"),
    "cache_ttl_hours": 24,
    "delete_after_download": False,
    "scanning_paused": False,
    "llm_batch_size": 10,
    # ---- Performance / resource-usage tuning ----
    "ollama_think": False,        # disable "thinking" mode on reasoning-capable models (faster)
    "ollama_num_ctx": 2048,        # context window size (tokens) — lower uses less VRAM/RAM, faster
    "ollama_num_predict": 200,     # max tokens the model can generate per response
    "ollama_keep_alive": "30m",    # how long Ollama keeps the model loaded in VRAM after a request
    "scrape_concurrency": 4,       # number of yt-dlp discovery/metadata calls to run in parallel
    # ---- TikTok request behavior ----
    "request_delay_seconds": 1.5,  # min delay between yt-dlp calls to the same TikTok endpoint (reduces rate-limiting/blocks)
    "discovery_retries": 2,        # retries for discovery/metadata yt-dlp calls on transient failure
    # ---- Notifications ----
    "webhook_url": "",             # optional URL to POST a JSON payload to on each new match
    "auto_update_ytdlp": True,     # automatically run `pip install -U yt-dlp` daily
}


def load_config():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    merged = {**DEFAULT_CONFIG, **cfg}
    return merged


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def update_config(partial: dict):
    cfg = load_config()
    for k, v in partial.items():
        if v is not None:
            cfg[k] = v
    save_config(cfg)
    return cfg
