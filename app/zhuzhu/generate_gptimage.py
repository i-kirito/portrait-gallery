#!/usr/bin/env python3
"""GPT Image engine backend using the configured chat-compatible image endpoint."""
import argparse
import base64
import json
import os
import re
import sys
import time
from typing import Optional
from urllib.parse import urlparse

import requests

from core import (
    MAX_RETRIES,
    REQUEST_SESSION,
    RETRYABLE_STATUS,
    RETRY_DELAY_SECONDS,
    build_caption,
    build_prompt,
    get_image_int,
    get_image_model,
    sync_to_gallery,
    save_image,
    send_photo,
    update_metadata,
    _API_KEYS_CONFIG_PATH,
)

GPTIMAGE_DIRECT_URL = get_image_model("gpt_base_url")
GPTIMAGE_DIRECT_MODEL = get_image_model("gpt_model")

TEXT2IMG_TIMEOUT = get_image_int("text2img_timeout", 180, 1)
IMG2IMG_TIMEOUT = get_image_int("img2img_timeout", 300, 1)
IMG2IMG_MAX_SIZE = get_image_int("img2img_max_size", 512, 64)
IMG2IMG_QUALITY = get_image_int("img2img_quality", 75, 1, 100)


def _get_gpt_key() -> str:
    """Read GPT key from environment variable or api_keys_config.json."""
    env_key = os.getenv("GPT_IMAGE_API_KEY", "")
    if env_key:
        return env_key

    config_path = _API_KEYS_CONFIG_PATH
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            return config.get("gpt_key", "")
        except Exception as e:
            print(f"Failed to read api_keys_config.json: {e}", file=sys.stderr)
    return ""


def _get_gpt_base_url() -> str:
    """Read GPT Image base URL from environment or api_keys_config.json."""
    env_url = os.getenv("GPT_IMAGE_BASE_URL", "")
    if env_url:
        return _normalize_gpt_base_url(env_url)

    config_path = _API_KEYS_CONFIG_PATH
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            if config.get("gpt_base_url"):
                return _normalize_gpt_base_url(config["gpt_base_url"])
        except Exception:
            pass
    return _normalize_gpt_base_url(GPTIMAGE_DIRECT_URL)


def _normalize_gpt_base_url(url: str) -> str:
    """Accept either a /v1 base URL or the full chat completions endpoint."""
    base = (url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _compress_image_for_img2img(
    image_path: str,
    max_size: int = IMG2IMG_MAX_SIZE,
    quality: int = IMG2IMG_QUALITY,
) -> str:
    """Compress image to base64 for img2img."""
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


def _gpt_endpoint_label(url: str = "") -> str:
    parsed = urlparse(url or _get_gpt_base_url())
    return parsed.netloc or (url or "GPT Image")


def _generate_via_direct_gpt(prompt: str, ref_image: Optional[str] = None, size: Optional[str] = None) -> Optional[tuple]:
    """Call the configured GPT Image endpoint (text2img + img2img)

    Args:
        prompt: Generation prompt
        ref_image: Optional reference image path for img2img mode
        size: Optional output image size

    Returns:
        (img_data, elapsed_time) tuple or None on failure
    """
    api_key = _get_gpt_key()
    if not api_key:
        print("ERROR: GPT_IMAGE_API_KEY or gpt_key is required", file=sys.stderr)
        return None
    base_url = _get_gpt_base_url()
    if not base_url:
        print("ERROR: image_gen.gpt_base_url is required", file=sys.stderr)
        return None
    if not GPTIMAGE_DIRECT_MODEL:
        print("ERROR: image_gen.gpt_model is required", file=sys.stderr)
        return None

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    if ref_image:
        try:
            compressed_img = _compress_image_for_img2img(ref_image)
            content = [
                {"type": "image_url", "image_url": {"url": compressed_img}},
                {"type": "text", "text": prompt},
            ]
            # Inject face-only reference instruction so model doesn't copy ref hairstyle or expression
            face_instruction = "\n[IMPORTANT] Use the reference image ONLY as a facial reference. Focus on matching the face shape, facial structure, and overall facial features to achieve high similarity with the person in the reference image. Do NOT copy or reference the hairstyle, hair color, hair accessories, clothing, outfit, pose, body posture, hand gestures, background, lighting, or any other non-facial elements from the reference image. All of these must strictly follow the text description above. Do NOT copy the facial expression, mouth shape, tongue, or grin from the reference image — the expression must also strictly follow the text description."
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

    endpoint_label = _gpt_endpoint_label(base_url)
    for attempt in range(MAX_RETRIES):
        try:
            resp = REQUEST_SESSION.post(
                base_url,
                headers=headers,
                json=payload,
                timeout=timeout,
            )

            if resp.status_code != 200:
                print(
                    f"Direct GPT API error {resp.status_code} [{endpoint_label}] "
                    f"(attempt {attempt + 1}/{MAX_RETRIES}): {resp.text[:200]}",
                    file=sys.stderr,
                )
                if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                    continue
                return None

            data = resp.json()
            msg = data["choices"][0]["message"]

            # CPA-compatible format: message.images[0].image_url.url
            images = msg.get("images", [])
            if images and isinstance(images, list):
                img_url = images[0].get("image_url", {}).get("url", "")
                if img_url.startswith("data:image/"):
                    img_data = base64.b64decode(img_url.split(",", 1)[1])
                else:
                    print(
                        f"Direct GPT API: unexpected image_url format "
                        f"(attempt {attempt + 1}/{MAX_RETRIES}): {str(img_url)[:100]}",
                        file=sys.stderr,
                    )
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                        continue
                    return None
            else:
                # Some chat-compatible endpoints return markdown image data.
                response_content = msg.get("content", "") or ""
                b64_match = re.search(r'!\[[^\]]*\]\(data:image/[^;]+;base64,([^)]+)\)', response_content)
                if not b64_match:
                    print(
                        f"Direct GPT API: no base64 image in response "
                        f"(attempt {attempt + 1}/{MAX_RETRIES}): {response_content[:300]}",
                        file=sys.stderr,
                    )
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                        continue
                    return None
                img_data = base64.b64decode(b64_match.group(1))
            elapsed = round(time.time() - start, 2)

            return img_data, elapsed

        except Exception as e:
            print(f"Direct GPT API failed [{endpoint_label}] (attempt {attempt + 1}/{MAX_RETRIES}): {e}", file=sys.stderr)
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            return None


def generate(theme: str, send: bool = False, caption: bool = False,
             prompt_override: Optional[str] = None, ref_image: Optional[str] = None,
             style: Optional[str] = None, size: Optional[str] = None,
             prompt_is_final: bool = False, source: str = "chat",
             sync_gallery: bool = True):
    """GPT Image 生成入口 — 使用当前配置的 GPT Image Base URL

    Args:
        theme: 时段主题 (morning/noon/evening/bedtime/sexy/custom)
        send: 是否直接发送 Telegram
        caption: 是否生成配文
        prompt_override: 自定义提示词（自动注入画质前缀+外貌）
        ref_image: 参考图本地路径，传入则启用图生图模式（img2img）
        style: 风格名 (cool/girly/sweet)，用于文件名标注
        size: 图片尺寸
        prompt_is_final: prompt_override 已包含画质、外貌、日程等注入内容时设为 True
        source: 来源标识，直连后端默认视为聊天通道生成
        sync_gallery: 是否直接写入画廊索引；统一入口会自行同步一次
    """
    prompt = prompt_override if prompt_is_final and prompt_override else build_prompt(theme, prompt_override)
    mode = "img2img" if ref_image else "text2img"
    endpoint_label = _gpt_endpoint_label()
    print(f"🎨 GPT Image via {endpoint_label} ({mode})...", file=sys.stderr)

    result = _generate_via_direct_gpt(prompt, ref_image, size)
    if not result and ref_image:
        print(f"GPT Image img2img failed via {endpoint_label}; retrying text2img without reference image", file=sys.stderr)
        result = _generate_via_direct_gpt(prompt, None, size)

    if not result:
        print(f"ERROR: GPT Image endpoint failed: {endpoint_label}", file=sys.stderr)
        return None

    img_data, gen_time = result
    path, filename, ts = save_image(img_data, theme, GPTIMAGE_DIRECT_MODEL, style=style)
    update_metadata(filename, theme, prompt, GPTIMAGE_DIRECT_MODEL, ts, gen_time)

    cap_text = None
    if caption:
        cap_text = build_caption(theme)
    if send:
        send_photo(path, cap_text)

    if sync_gallery:
        sync_to_gallery(
            path,
            filename,
            theme,
            style,
            prompt=prompt,
            caption=cap_text or "",
            gen_time=gen_time,
            model_name=GPTIMAGE_DIRECT_MODEL,
            source=source,
        )

    print(f"SUCCESS:{path}")
    if cap_text:
        print(f"CAPTION:{cap_text}")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GPT Image 生图（使用配置的 Base URL）")
    parser.add_argument("--theme", choices=["morning", "noon", "evening", "bedtime", "sexy", "custom"], default="sexy")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--caption", action="store_true")
    parser.add_argument("--prompt", type=str, default=None, help="自定义 prompt")
    parser.add_argument("--ref-image", type=str, default=None, help="参考图本地路径（图生图/img2img 模式）")
    parser.add_argument("--size", type=str, default=None, help="图片尺寸")
    parser.add_argument("--source", choices=["cron", "web", "chat", "custom"], default="chat", help="来源标识")
    args = parser.parse_args()
    path = generate(args.theme, args.send, args.caption, args.prompt, args.ref_image, size=args.size, source=args.source)
    if not path:
        print("ERROR: GPT Image generation failed", file=sys.stderr)
        sys.exit(1)
