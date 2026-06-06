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

# Install/update dependencies if requirements.txt changed
REQUIREMENTS_HASH_FILE="$VENV_DIR/.requirements_hash"
CURRENT_HASH=$(python3 -c "import hashlib; print(hashlib.md5(open('requirements.txt','rb').read()).hexdigest())")

if [ ! -f "$REQUIREMENTS_HASH_FILE" ] || [ "$CURRENT_HASH" != "$(cat "$REQUIREMENTS_HASH_FILE")" ]; then
    echo "Installing dependencies..."
    "$PIP" install --quiet -r requirements.txt
    echo "$CURRENT_HASH" > "$REQUIREMENTS_HASH_FILE"
fi

exec "$PYTHON" web_app.py
