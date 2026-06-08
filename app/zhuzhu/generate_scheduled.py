#!/usr/bin/env python3
"""Scheduled image generation entrypoint for zhuzhu-image-gen."""
import argparse
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout

from generate_gptimage import generate as generate_with_gpt
from generate_gitee import generate as generate_with_gitee
from core import CONFIG_PATH

DAILY_THEMES = {"morning", "noon", "evening", "bedtime"}
ALL_THEMES = sorted(DAILY_THEMES | {"sexy"})
SEND_TARGET = os.getenv("ZHUZHU_SEND_TARGET", "5509078392")
SEND_CHANNEL = os.getenv("ZHUZHU_SEND_CHANNEL", "telegram")
SEND_ACCOUNT = os.getenv("ZHUZHU_SEND_ACCOUNT", "default")
FALLBACK_TEXT = "主人～猪猪的新照片来啦！"


def _gitee_fallback_enabled() -> bool:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f) or {}
        return bool(data.get("gitee_fallback_enabled", False))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False



def _run_backend(func, theme: str, caption: bool):
    captured = io.StringIO()
    with redirect_stdout(captured):
        path = func(theme, send=False, caption=caption)

    caption_text = None
    for line in captured.getvalue().splitlines():
        if line.startswith("CAPTION:"):
            caption_text = line[len("CAPTION:"):]

    return path, caption_text



def generate(theme: str, caption: bool = True):
    if theme == "sexy":
        return _run_backend(generate_with_gitee, theme, caption)

    path, caption_text = _run_backend(generate_with_gpt, theme, caption)
    if path:
        return path, caption_text

    if not _gitee_fallback_enabled():
        print(f"[scheduled] GPT Image failed; Gitee fallback is disabled for theme={theme}", file=sys.stderr)
        return None, None

    print(f"[scheduled] GPT Image failed, falling back to Gitee for theme={theme}", file=sys.stderr)
    return _run_backend(generate_with_gitee, theme, caption)


def send_photo(path: str, caption_text: str):
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if os.path.getsize(path) <= 0:
        raise ValueError(f"empty file: {path}")

    cmd = [
        "openclaw",
        "message",
        "send",
        "--channel",
        SEND_CHANNEL,
        "--account",
        SEND_ACCOUNT,
        "--target",
        SEND_TARGET,
        "--media",
        path,
        "--message",
        caption_text or FALLBACK_TEXT,
        "--json",
    ]
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="定时生图调度器")
    parser.add_argument("--theme", choices=ALL_THEMES, required=True)
    parser.add_argument("--caption", action="store_true", default=True, help="输出并发送配文")
    args = parser.parse_args()

    path, caption_text = generate(args.theme, caption=args.caption)
    if not path:
        print(f"ERROR: all engines failed for theme={args.theme}", file=sys.stderr)
        sys.exit(1)

    caption_text = caption_text or FALLBACK_TEXT
    print(f"SUCCESS:{path}")
    print(f"CAPTION:{caption_text}")

    try:
        result = send_photo(path, caption_text)
        if result.stdout:
            print(result.stdout.strip())
    except Exception as e:
        print(f"ERROR: send failed: {e}", file=sys.stderr)
        sys.exit(2)
