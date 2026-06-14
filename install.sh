#!/bin/bash
set -e

INSTALL_DIR="/opt/tiktok-scanner"
SERVICE_NAME="tiktok-scanner"

echo "================================================"
echo "          TikTok Scanner - Installer"
echo "================================================"
echo ""

if [ "$(id -u)" -eq 0 ]; then
  RUN_USER="${SUDO_USER:-root}"
else
  RUN_USER="$(whoami)"
fi

# ---------------------------------------------------
# 1. System dependencies
# ---------------------------------------------------
echo "[1/6] Installing system dependencies..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip ffmpeg git curl

# ---------------------------------------------------
# 2. Fetch the project
# ---------------------------------------------------
echo ""
echo "[2/6] Fetching project files..."
if [ -d "$INSTALL_DIR/.git" ]; then
  echo "Existing install found at $INSTALL_DIR — pulling latest changes..."
  sudo git -C "$INSTALL_DIR" pull
else
  sudo mkdir -p "$INSTALL_DIR"
  sudo git clone "$REPO_URL" "$INSTALL_DIR"
fi
sudo chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR"
cd "$INSTALL_DIR"
mkdir -p data

# ---------------------------------------------------
# 3. Install Python packages system-wide
# ---------------------------------------------------
echo ""
echo "[3/6] Installing Python packages..."
pip3 install --break-system-packages --upgrade pip
pip3 install --break-system-packages -r requirements.txt

echo "Installed packages:"
pip3 show yt-dlp uvicorn fastapi | grep -E "^(Name|Version):"

# ---------------------------------------------------
# 4. Interactive configuration
# ---------------------------------------------------
echo ""
echo "[4/6] Configuration"
echo ""
echo "Which LLM should evaluate scraped videos against your prompts?"
echo "  1) Local model via Ollama (uses your GPUs)"
echo "  2) Remote API (OpenAI-compatible endpoint)"
read -rp "Choose [1]: " llm_choice
llm_choice=${llm_choice:-1}

OLLAMA_MODEL="qwen2.5:14b"
OLLAMA_URL="http://localhost:11434"
API_BASE_URL="https://api.openai.com/v1"
API_KEY=""
API_MODEL="gpt-4o-mini"

if [ "$llm_choice" = "1" ]; then
  LLM_PROVIDER="ollama"

  echo ""
  echo "Is Ollama running on this machine, or on a separate server (e.g. your GPU box)?"
  echo "  1) This machine"
  echo "  2) A different server on the network"
  read -rp "Choose [1]: " ollama_location
  ollama_location=${ollama_location:-1}

  echo ""
  echo "Recommended models for dual Tesla M40 (48GB combined VRAM):"
  echo "  - qwen2.5:14b   (fast, great for metadata classification)"
  echo "  - qwen2.5:32b   (more accurate, fits with quantization)"
  echo "  - llama3.1:8b   (fastest, lightest)"
  read -rp "Ollama model to use [qwen2.5:14b]: " input_model
  OLLAMA_MODEL=${input_model:-qwen2.5:14b}

  if [ "$ollama_location" = "2" ]; then
    echo ""
    read -rp "Ollama URL on that server (e.g. http://192.168.1.50:11434): " input_ollama_url
    OLLAMA_URL=${input_ollama_url:-http://localhost:11434}
    echo ""
    echo "NOTE: Make sure Ollama on that machine is listening on 0.0.0.0"
    echo "  (set OLLAMA_HOST=0.0.0.0 and restart ollama) and the model is pulled:"
    echo "  ollama pull $OLLAMA_MODEL"
  else
    OLLAMA_URL="http://localhost:11434"
    if ! command -v ollama &> /dev/null; then
      echo ""
      echo "Ollama not found — installing it now..."
      curl -fsSL https://ollama.com/install.sh | sh
    else
      echo "Ollama is already installed."
    fi
    echo ""
    echo "Pulling model '$OLLAMA_MODEL'..."
    ollama pull "$OLLAMA_MODEL" || echo "WARNING: model pull failed — run 'ollama pull $OLLAMA_MODEL' manually later."
  fi
else
  LLM_PROVIDER="api"
  read -rp "API base URL [https://api.openai.com/v1]: " input_url
  API_BASE_URL=${input_url:-https://api.openai.com/v1}
  read -rp "API key: " API_KEY
  read -rp "Model name [gpt-4o-mini]: " input_model
  API_MODEL=${input_model:-gpt-4o-mini}
fi

echo ""
read -rp "Where should downloaded videos be stored? [$INSTALL_DIR/data/downloads]: " STORAGE_PATH
STORAGE_PATH=${STORAGE_PATH:-$INSTALL_DIR/data/downloads}
mkdir -p "$STORAGE_PATH"

echo ""
read -rp "Web UI port [8080]: " WEB_PORT
WEB_PORT=${WEB_PORT:-8080}

echo ""
read -rp "How often should it scan for new videos, in minutes? [30]: " SCAN_INTERVAL
SCAN_INTERVAL=${SCAN_INTERVAL:-30}

echo ""
read -rp "How many videos to check per search term, per scan? [15]: " VIDEOS_PER_SCAN
VIDEOS_PER_SCAN=${VIDEOS_PER_SCAN:-15}

# ---------------------------------------------------
# 5. Write config.json
# ---------------------------------------------------
echo ""
echo "[5/6] Writing configuration..."
cat > data/config.json <<EOF
{
  "llm_provider": "$LLM_PROVIDER",
  "ollama_model": "$OLLAMA_MODEL",
  "ollama_url": "$OLLAMA_URL",
  "api_base_url": "$API_BASE_URL",
  "api_key": "$API_KEY",
  "api_model": "$API_MODEL",
  "storage_path": "$STORAGE_PATH",
  "scan_interval_minutes": $SCAN_INTERVAL,
  "videos_per_scan": $VIDEOS_PER_SCAN,
  "cookies_path": "$INSTALL_DIR/data/cookies.txt"
}
EOF

touch data/cookies.txt

# ---------------------------------------------------
# 6. systemd service
# ---------------------------------------------------
echo ""
echo "[6/6] Creating systemd service..."

# Find uvicorn — pip installs it to /usr/local/bin
UVICORN_BIN=$(which uvicorn || echo /usr/local/bin/uvicorn)

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=TikTok Scanner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$UVICORN_BIN app.main:app --host 0.0.0.0 --port $WEB_PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl restart ${SERVICE_NAME}

# Allow web UI boot-toggle to run systemctl without password
SUDOERS_FILE="/etc/sudoers.d/tiktok-scanner-boot-toggle"
sudo tee "$SUDOERS_FILE" > /dev/null <<EOF
$RUN_USER ALL=(root) NOPASSWD: /usr/bin/systemctl enable ${SERVICE_NAME}, /usr/bin/systemctl disable ${SERVICE_NAME}, /usr/bin/systemctl is-enabled ${SERVICE_NAME}
EOF
sudo chmod 440 "$SUDOERS_FILE"

IP=$(hostname -I | awk '{print $1}')

echo ""
echo "================================================"
echo "  Installation complete!"
echo "================================================"
echo ""
echo "  Web UI:   http://$IP:$WEB_PORT"
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status ${SERVICE_NAME}"
echo "    sudo systemctl restart ${SERVICE_NAME}"
echo "    journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "  To update yt-dlp manually:"
echo "    pip3 install --break-system-packages -U yt-dlp"
echo ""
echo "  To update the app:"
echo "    sudo git -C $INSTALL_DIR pull && sudo systemctl restart ${SERVICE_NAME}"
echo ""
if [ "$LLM_PROVIDER" = "ollama" ]; then
echo "  NOTE: if TikTok requires login to browse certain hashtags,"
echo "  export cookies to: $INSTALL_DIR/data/cookies.txt (Netscape format)"
echo ""
fi
echo "================================================"
