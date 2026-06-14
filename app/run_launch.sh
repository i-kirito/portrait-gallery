#!/bin/zsh
set -eu

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$APP_DIR/.." && pwd)"
cd "$PROJECT_DIR"
exec python3 -u "$APP_DIR/main.py"
