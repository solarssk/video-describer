#!/bin/bash
set -e

cd "$(dirname "$0")"

VENV_DIR="venv"
PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

# Create venv if missing
if [ ! -f "$PYTHON" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# Exclude venv from iCloud sync
xattr -w com.apple.fileprovider.ignore#P 1 "$VENV_DIR" 2>/dev/null || true

# Install/update dependencies if requirements.txt changed
REQUIREMENTS_HASH_FILE="$VENV_DIR/.requirements_hash"
CURRENT_HASH=$(python3 -c "import hashlib; print(hashlib.md5(open('requirements.txt','rb').read()).hexdigest())")

if [ ! -f "$REQUIREMENTS_HASH_FILE" ] || [ "$CURRENT_HASH" != "$(cat "$REQUIREMENTS_HASH_FILE")" ]; then
    echo "Installing dependencies..."
    "$PIP" install --quiet --disable-pip-version-check -r requirements.txt
    echo "$CURRENT_HASH" > "$REQUIREMENTS_HASH_FILE"
fi

# Install Whisper backend if neither variant is present (optional — failure does not block startup)
if ! "$PYTHON" -c "import mlx_whisper" 2>/dev/null && ! "$PYTHON" -c "import faster_whisper" 2>/dev/null; then
    if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
        echo "Installing mlx-whisper (Apple Silicon)..."
        "$PIP" install --quiet --disable-pip-version-check mlx-whisper || echo "⚠  mlx-whisper install failed — speech transcription unavailable"
    else
        echo "Installing faster-whisper..."
        "$PIP" install --quiet --disable-pip-version-check faster-whisper || echo "⚠  faster-whisper install failed — speech transcription unavailable"
    fi
fi

exec "$PYTHON" web_app.py
