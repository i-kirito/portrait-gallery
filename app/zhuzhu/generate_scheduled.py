#!/usr/bin/env python3
"""Scheduled image generation entrypoint for zhuzhu-image-gen."""
import argparse
import io
import os
import subprocess
import sys
from contextlib import redirect_stdout

from core import _personalized_caption_fallback, _runtime_persona
from generate import generate as generate_image

DAILY_THEMES = {"morning", "noon", "evening", "bedtime"}
ALL_THEMES = sorted(DAILY_THEMES | {"sexy"})
SEND_TARGET = os.getenv("ZHUZHU_SEND_TARGET", "")
SEND_CHANNEL = os.getenv("ZHUZHU_SEND_CHANNEL", "telegram")
SEND_ACCOUNT = os.getenv("ZHUZHU_SEND_ACCOUNT", "default")

def _fallback_text(theme: str = "morning") -> str:
    return _personalized_caption_fallback(theme, _runtime_persona())


def _run_backend(func, theme: str, caption: bool, **kwargs):
    captured = io.StringIO()
    with redirect_stdout(captured):
        path = func(theme, send=False, caption=caption, source="cron", **kwargs)

    caption_text = None
    for line in captured.getvalue().splitlines():
        if line.startswith("CAPTION:"):
            caption_text = line[len("CAPTION:"):]

    return path, caption_text



def generate(theme: str, caption: bool = True, *, ref_images=None,
             xiaohongshu_outfit_reference: bool = False,
             schedule_date: str = "", schedule_time: str = ""):
    """Use the same reference checks and Qwen fallback as the gallery scheduler."""
    return _run_backend(
        generate_image, theme, caption, qwen_fallback=True,
        ref_images=ref_images, xiaohongshu_outfit_reference=xiaohongshu_outfit_reference,
        schedule_date=schedule_date, schedule_time=schedule_time,
    )


def send_photo(path: str, caption_text: str):
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if os.path.getsize(path) <= 0:
        raise ValueError(f"empty file: {path}")
    if not SEND_TARGET:
        print("[scheduled] ZHUZHU_SEND_TARGET is not configured; skip sending to avoid cross-user delivery", file=sys.stderr)
        return subprocess.CompletedProcess([], 0, "", "")

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
        caption_text or _fallback_text(),
        "--json",
    ]
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="定时生图调度器")
    parser.add_argument("--theme", choices=ALL_THEMES, required=True)
    parser.add_argument("--caption", action="store_true", default=True, help="输出并发送配文")
    parser.add_argument("--ref-images", default="", help="按穿搭图、身份图顺序填写参考图路径，逗号分隔")
    parser.add_argument("--xiaohongshu-outfit-reference", action="store_true")
    parser.add_argument("--schedule-date", default="")
    parser.add_argument("--schedule-time", default="")
    args = parser.parse_args()

    path, caption_text = generate(
        args.theme, caption=args.caption,
        ref_images=[value.strip() for value in args.ref_images.split(",") if value.strip()],
        xiaohongshu_outfit_reference=args.xiaohongshu_outfit_reference,
        schedule_date=args.schedule_date, schedule_time=args.schedule_time,
    )
    if not path:
        print(f"ERROR: all engines failed for theme={args.theme}", file=sys.stderr)
        sys.exit(1)

    caption_text = caption_text or _fallback_text(args.theme)
    print(f"SUCCESS:{path}")
    print(f"CAPTION:{caption_text}")

    try:
        result = send_photo(path, caption_text)
        if result.stdout:
            print(result.stdout.strip())
    except Exception as e:
        print(f"ERROR: send failed: {e}", file=sys.stderr)
        sys.exit(2)
