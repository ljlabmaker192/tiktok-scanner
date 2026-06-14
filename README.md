# TikTok Scanner

A self-hosted tool that continuously scans TikTok for videos matching
plain-English prompts you define (based on title/description, hashtags,
and author), and automatically downloads matches (no watermark) into
per-category folders. Everything is controlled from a web UI.

Built for a local Ubuntu server with GPU(s) for running a local LLM
(e.g. dual Tesla M40 24GB = 48GB combined VRAM).

## How it works

- You create **categories** (e.g. "cars", "looksmaxxing"), each with:
  - A list of search terms / hashtags to monitor
  - A free-text **prompt** describing what counts as a match
  - Enabled/disabled toggle
- A background worker periodically scans each enabled category's search
  terms, pulls candidate video metadata via `yt-dlp`, and sends each
  candidate's title/description/hashtags/author to your chosen LLM along
  with your prompt.
- If the LLM says it's a match, the video is downloaded (clean, no
  watermark) into `storage_path/<category_name>/`.
- All of this — categories, prompts, LLM choice/model, scan interval,
  storage path — is managed from the web UI. No config files to edit by hand.

## Requirements

- Ubuntu Server 22.04 or 24.04
- For local LLM mode: NVIDIA GPU(s) + drivers installed (the installer
  sets up Ollama, which will use your GPUs automatically)

## Installation

1. Push this project to your own GitHub repo (see below).
2. On your Ubuntu server, run:

```bash
curl -fsSL https://raw.githubusercontent.com/ljlabmaker192/tiktok-scanner/main/install.sh | bash
```

The installer will:
- Install system packages (`python3`, `python3.12`, `ffmpeg`, `git`, etc.)
- Clone the repo to `/opt/tiktok-scanner`
- Set up a Python virtual environment (preferring Python 3.12 for the
  widest prebuilt-wheel availability) and install dependencies
- Ask you:
  - Local (Ollama) or remote API for the LLM?
  - If Ollama: is it running on this machine, or a separate server
    (e.g. a dedicated GPU box)? If separate, you'll be asked for its URL
    and the installer won't try to install Ollama locally.
  - Which model to use (and pull it, if Ollama is local)
  - Where to store downloaded videos
  - Web UI port
  - Scan interval and videos-per-scan
- Write `data/config.json` with your answers
- Create and enable a `systemd` service so it starts on boot
- Start everything immediately

At the end it prints the URL for the web UI (`http://<server-ip>:<port>`).

## Recommended local models (dual M40, 48GB VRAM)

This is a metadata-classification task (reading text), not video
understanding, so a mid-size instruction model is plenty:

- `qwen2.5:14b` — good balance of speed and accuracy (recommended default)
- `qwen2.5:32b` — more accurate, still fits comfortably
- `llama3.1:8b` — fastest, good if you're running many categories

You can change the model anytime from the Settings tab — it will be
pulled automatically the next time it's used (run `ollama pull <model>`
manually if needed).

## Running Ollama on a separate server

If the scanner and Ollama run on different machines (e.g. scanner on a
small box, Ollama on a GPU server), make sure on the **Ollama machine**:

- Ollama listens on all interfaces, not just `localhost`. Set
  `OLLAMA_HOST=0.0.0.0` (e.g. in `/etc/systemd/system/ollama.service.d/override.conf`
  or its environment) and restart Ollama.
- Port `11434` is reachable from the scanner machine (check firewall
  rules, e.g. `ufw allow 11434`).
- The model is pulled there: `ollama pull <model>`.

Then in this app's Settings, set the Ollama URL to
`http://<ollama-server-ip>:11434` and use **Test connection** to verify.

For the Performance settings, consider setting "Keep model loaded in
VRAM" to a longer duration (e.g. `30m` or `-1`) since reloading a model
across the network adds extra latency on top of disk load time.

## Using the web UI

- **Categories tab**: add/edit/delete categories. Each category has a
  name (used as its download folder), comma-separated search terms, and
  a prompt describing what you want. Click "Scan now" to trigger an
  immediate scan, or "Show videos" to view matches and links.
  - Search terms can be:
    - a **hashtag**, e.g. `carsoftiktok` or `#carsoftiktok`
    - an **account to watch**, e.g. `@somecreator` (its video feed)
    - a **keyword search**, e.g. `search:rare jdm cars` or `s:rare jdm cars`
  - **Test this prompt**: paste a TikTok video URL to check whether your
    current prompt would match it, without running a full scan or saving
    the category — useful for tuning prompts.
  - **Example videos**: for a saved category, add a few example TikTok
    videos labeled "this IS a match" / "this is NOT a match". These are
    included as few-shot examples in every prompt sent to the LLM for
    that category, which can noticeably improve accuracy on nuanced
    prompts.
  - If a category goes 3+ consecutive scans with no new candidates, a
    warning badge appears (discovery may be broken — TikTok layout
    change, expired cookies, etc.)
- **Settings tab**:
  - **System: "Start automatically on server boot"** — toggles whether the
    `tiktok-scanner` systemd service is enabled (survives a reboot). Saved
    immediately when switched. Requires the installer's passwordless-sudo
    rule for `systemctl enable/disable/is-enabled tiktok-scanner` (set up
    automatically by `install.sh`). If you installed manually, add this to
    `/etc/sudoers.d/tiktok-scanner-boot-toggle`:
    ```
    youruser ALL=(root) NOPASSWD: /usr/bin/systemctl enable tiktok-scanner, /usr/bin/systemctl disable tiktok-scanner, /usr/bin/systemctl is-enabled tiktok-scanner
    ```
  - Switch between local Ollama and a remote OpenAI-compatible API,
    change the model, storage path, scan interval, and how many
    candidates to check per term per scan.
  - **Test connection**: checks that the configured LLM backend
    (local or remote) is reachable, lists available Ollama models, and
    flags if your configured model isn't pulled yet.
  - **Performance / Resource Usage**: tune things like disabling model
    "thinking" mode, context window size, max response length, how long
    Ollama keeps the model loaded in VRAM, and how many parallel `yt-dlp`
    lookups run during discovery. A "low-resource preset" button applies
    a conservative combination in one click — useful on older GPUs (e.g.
    Tesla M40s) or when running the scanner and Ollama on different
    machines.
- **Logs tab**: live activity log — scan progress, matches, and errors.

## Managing the service

```bash
sudo systemctl status tiktok-scanner
sudo systemctl restart tiktok-scanner
sudo systemctl stop tiktok-scanner
journalctl -u tiktok-scanner -f
```

## TikTok cookies (optional, recommended)

TikTok sometimes requires a logged-in session to browse hashtag pages or
to avoid rate limiting. If discovery/downloads start failing:

1. Use a browser extension (e.g. "Get cookies.txt") to export your
   tiktok.com cookies in Netscape format while logged in.
2. Save the file to `/opt/tiktok-scanner/data/cookies.txt`.

`yt-dlp` will automatically use it for future requests.

## Keeping it up to date

TikTok changes frequently, and `yt-dlp` is updated often to keep up.
Update it periodically:

```bash
cd /opt/tiktok-scanner
source venv/bin/activate
pip install -U yt-dlp
sudo systemctl restart tiktok-scanner
```

## Updating to a new version

```bash
cd /opt/tiktok-scanner
git pull
source venv/bin/activate
pip install --only-binary=:all: -r requirements.txt
sudo systemctl restart tiktok-scanner
```


## Notes & limitations

- Discovery relies on `yt-dlp`'s TikTok hashtag-page support. TikTok
  changes its site frequently — if discovery stops finding new videos,
  update `yt-dlp` first (see above) and add cookies if needed.
- Downloaded files remain on disk if you delete a category from the UI;
  only the database records are removed.
- Be mindful of TikTok's terms of service and copyright when archiving
  or reusing other people's content — this tool is intended for personal
  research/archiving use.
