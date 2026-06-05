#!/usr/bin/env python3
"""GPT Image engine backend — 只走 jiuuij.de5.net 直连，支持文生图和图生图。"""
import argparse
import base64
import json
import os
import re
import sys
import time
from typing import Optional

import requests

from core import (
    REQUEST_SESSION,
    build_caption,
    build_prompt,
    sync_to_gallery,
    save_image,
    send_photo,
    update_metadata,
)

# jiuuij.de5.net 直连（文生图 + 图生图）— 默认值，可被 api_keys_config.json 的 gpt_base_url 覆盖
GPTIMAGE_DIRECT_URL = "https://jiuuij.de5.net/v1/chat/completions"
GPTIMAGE_DIRECT_MODEL = "gpt-image-2"

# 文生图超时 180s（jiuuij 偶尔慢），图生图 300s
TEXT2IMG_TIMEOUT = 180
IMG2IMG_TIMEOUT = 300


def _get_gpt_key() -> str:
    """Read GPT key from environment variable or api_keys_config.json"""
    # 1. Try environment variable
    env_key = os.getenv("GPT_IMAGE_API_KEY", "")
    if env_key:
        return env_key
    # 2. Try config file
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "api_keys_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                key = config.get("gpt_key", "")
                if key:
                    return key
        except Exception as e:
            print(f"Failed to read api_keys_config.json: {e}", file=sys.stderr)
    return ""


def _get_gpt_base_url() -> str:
    """Read GPT Image base URL from api_keys_config.json, fallback to default"""
    env_url = os.getenv("GPT_IMAGE_BASE_URL", "")
    if env_url:
        return env_url
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "api_keys_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                url = config.get("gpt_base_url", "")
                if url:
                    return url
        except Exception:
            pass
    return GPTIMAGE_DIRECT_URL


def _compress_image_for_img2img(image_path: str, max_size: int = 512, quality: int = 75) -> str:
    """Compress image to base64 for img2img (512px, quality 75 to avoid timeout)"""
    from PIL import Image
    import io

    img = Image.open(image_path)
    img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    if img.mode in ('RGBA', 'P'):
        img = img.convert('RGB')

    buffer = io.BytesIO()
    img.save(buffer, format='JPEG', quality=quality, optimize=True)
    b64 = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/jpeg;base64,{b64}"


def _generate_via_direct_gpt(prompt: str, ref_image: Optional[str] = None, size: Optional[str] = None) -> Optional[tuple]:
    """Call jiuuij.de5.net directly for GPT Image generation (text2img + img2img)

    Args:
        prompt: Generation prompt
        ref_image: Optional reference image path for img2img mode
        size: Optional output image size

    Returns:
        (img_data, elapsed_time) tuple or None on failure
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_get_gpt_key()}",
    }

    if ref_image:
        try:
            compressed_img = _compress_image_for_img2img(ref_image)
            content = [
                {"type": "image_url", "image_url": {"url": compressed_img}},
                {"type": "text", "text": prompt},
            ]
            # Inject face-only reference instruction so model doesn't copy ref hairstyle or expression
            face_instruction = "\n[IMPORTANT] Use the reference image ONLY for facial features and face structure as a base. Do NOT copy the hairstyle from the reference image. The hairstyle must strictly follow the text description above. Do NOT copy the facial expression, mouth shape, tongue, or grin from the reference image. The expression must strictly follow the text description."
            content = [
                {"type": "image_url", "image_url": {"url": compressed_img}},
                {"type": "text", "text": prompt + face_instruction},
            ]
        except Exception as e:
            print(f"Failed to compress reference image: {e}", file=sys.stderr)
            return None
    else:
        content = prompt

    payload = {
        "model": GPTIMAGE_DIRECT_MODEL,
        "stream": False,
        "messages": [{"role": "user", "content": content}],
    }
    if size:
        payload["size"] = size

    timeout = IMG2IMG_TIMEOUT if ref_image else TEXT2IMG_TIMEOUT
    start = time.time()

    try:
        resp = REQUEST_SESSION.post(
            _get_gpt_base_url(),
            headers=headers,
            json=payload,
            timeout=timeout,
        )

        if resp.status_code != 200:
            print(f"Direct GPT API error {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
            return None

        data = resp.json()
        response_content = data["choices"][0]["message"]["content"]

        # Extract base64 image from markdown: ![image](data:image/png;base64,...)
        b64_match = re.search(r'!\[[^\]]*\]\(data:image/[^;]+;base64,([^)]+)\)', response_content)
        if not b64_match:
            print(f"Direct GPT API: no base64 image in response: {response_content[:300]}", file=sys.stderr)
            return None

        img_data = base64.b64decode(b64_match.group(1))
        elapsed = round(time.time() - start, 2)

        return img_data, elapsed

    except Exception as e:
        print(f"Direct GPT API failed: {e}", file=sys.stderr)
        return None


def generate(theme: str, send: bool = False, caption: bool = False,
             prompt_override: Optional[str] = None, ref_image: Optional[str] = None,
             style: Optional[str] = None, size: Optional[str] = None):
    """GPT Image 生成入口 — 统一走 jiuuij.de5.net 直连

    Args:
        theme: 时段主题 (morning/noon/evening/bedtime/sexy/custom)
        send: 是否直接发送 Telegram
        caption: 是否生成配文
        prompt_override: 自定义提示词（自动注入画质前缀+外貌）
        ref_image: 参考图本地路径，传入则启用图生图模式（img2img）
        style: 风格名 (cool/girly/sweet)，用于文件名标注
        size: 图片尺寸
    """
    prompt = build_prompt(theme, prompt_override)
    mode = "img2img" if ref_image else "text2img"
    print(f"🎨 GPT Image via jiuuij.de5.net ({mode})...", file=sys.stderr)

    result = _generate_via_direct_gpt(prompt, ref_image, size)

    if not result:
        print("ERROR: jiuuij.de5.net failed", file=sys.stderr)
        return None

    img_data, gen_time = result
    path, filename, ts = save_image(img_data, theme, GPTIMAGE_DIRECT_MODEL, style=style)
    update_metadata(filename, theme, prompt, GPTIMAGE_DIRECT_MODEL, ts, gen_time)
    sync_to_gallery(path, filename, theme, style=style, prompt=prompt, model_name=GPTIMAGE_DIRECT_MODEL, source="cron")

    cap_text = None
    if caption:
        cap_text = build_caption(theme)
    if send:
        send_photo(path, cap_text)

    print(f"SUCCESS:{path}")
    if cap_text:
        print(f"CAPTION:{cap_text}")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GPT Image gpt-image-2 生图（jiuuij.de5.net 直连）")
    parser.add_argument("--theme", choices=["morning", "noon", "evening", "bedtime", "sexy", "custom"], default="sexy")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--caption", action="store_true")
    parser.add_argument("--prompt", type=str, default=None, help="自定义 prompt")
    parser.add_argument("--ref-image", type=str, default=None, help="参考图本地路径（图生图/img2img 模式）")
    args = parser.parse_args()
    path = generate(args.theme, args.send, args.caption, args.prompt, args.ref_image)
    if not path:
        print("ERROR: GPT Image generation failed", file=sys.stderr)
        sys.exit(1)
