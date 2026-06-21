#!/bin/zsh
set -eu

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$APP_DIR/.." && pwd)"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi

export HERMES_PORTRAIT_GALLERY_HOME="$PROJECT_DIR"
export CONFIG_PATH="$PROJECT_DIR/config/config.yaml"
export GALLERY_DATA_DIR="$PROJECT_DIR/data"
export ZHUZHU_DATA_DIR="$PROJECT_DIR/data"
export ZHUZHU_PROJECT_DIR="$PROJECT_DIR"
export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

cd "$PROJECT_DIR"
exec "$PYTHON_BIN" -u "$APP_DIR/main.py"
