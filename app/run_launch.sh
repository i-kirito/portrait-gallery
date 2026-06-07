#!/bin/zsh
set -eu

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
exec python3 -u "$HOME/.hermes/portrait_gallery_launcher.py"
