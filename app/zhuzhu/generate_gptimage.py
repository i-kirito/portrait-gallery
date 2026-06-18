#!/usr/bin/env python3
"""GPT Image engine backend using the configured image endpoint."""
import argparse
import base64
import io
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
    build_caption_for_image,
    build_prompt,
    get_image_int,
    get_image_request_timeout,
    get_image_model,
    sync_to_gallery,
    save_image,
    send_photo,
    update_metadata,
    _API_KEYS_CONFIG_PATH,
)

GPTIMAGE_DIRECT_URL = get_image_model("gpt_base_url")


def _get_gpt_model() -> str:
    """Read GPT Image model from env/api_keys_config.json/config.yaml."""
    env_model = os.getenv("GPT_IMAGE_MODEL", "")
    if env_model:
        return env_model.strip()

    config_path = _API_KEYS_CONFIG_PATH
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            if config.get("gpt_model"):
                return str(config["gpt_model"]).strip()
        except Exception:
            pass
    return get_image_model("gpt_model")


GPTIMAGE_DIRECT_MODEL = _get_gpt_model()

TEXT2IMG_TIMEOUT = get_image_request_timeout("text2img")
IMG2IMG_TIMEOUT = get_image_request_timeout("img2img")
IMG2IMG_MAX_SIZE = get_image_int("img2img_max_size", 512, 64)
IMG2IMG_QUALITY = get_image_int("img2img_quality", 75, 1, 100)
_IMAGES_API_UNSUPPORTED_BASES: set[str] = set()


def _configured_image_base_url(url: str) -> str:
    base = str(url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/images/generations", "/images/edits"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _get_gpt_key() -> str:
    """Read GPT key from environment variable or api_keys_config.json."""
    env_key = os.getenv("GPT_IMAGE_API_KEY", "")
    if env_key:
        return env_key
    cpa_env_key = os.getenv("CPA_API_KEY", "")
    if cpa_env_key:
        return cpa_env_key

    config_path = _API_KEYS_CONFIG_PATH
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            return config.get("gpt_key", "") or config.get("cpa_key", "")
        except Exception as e:
            print(f"Failed to read api_keys_config.json: {e}", file=sys.stderr)
    return ""


def _get_gpt_raw_base_url() -> str:
    """Read GPT Image base URL from environment or api_keys_config.json."""
    env_url = os.getenv("GPT_IMAGE_BASE_URL", "")
    if env_url:
        return env_url.strip().rstrip("/")

    config_path = _API_KEYS_CONFIG_PATH
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            if config.get("gpt_base_url"):
                local_url = str(config["gpt_base_url"]).strip().rstrip("/")
                configured_url = str(GPTIMAGE_DIRECT_URL or "").strip().rstrip("/")
                if local_url == configured_url:
                    return _configured_image_base_url(local_url)
                return local_url
        except Exception:
            pass
    return _configured_image_base_url(GPTIMAGE_DIRECT_URL)


def _get_gpt_base_url() -> str:
    """Return the effective request URL used by the legacy chat image mode."""
    return _normalize_gpt_chat_url(_get_gpt_raw_base_url())


def _normalize_gpt_chat_url(url: str) -> str:
    """Accept either a /v1 base URL or the full chat completions endpoint."""
    base = (url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    for suffix in ("/images/generations", "/images/edits"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}/chat/completions"


def _normalize_gpt_images_base_url(url: str) -> str:
    """Accept /v1 or a full image/chat endpoint and return the /v1 base."""
    base = (url or "").strip().rstrip("/")
    if not base:
        return ""
    for suffix in ("/chat/completions", "/images/generations", "/images/edits"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _is_explicit_chat_url(url: str) -> bool:
    return (url or "").strip().rstrip("/").endswith("/chat/completions")


def _is_explicit_images_url(url: str) -> bool:
    return (url or "").strip().rstrip("/").endswith(("/images/generations", "/images/edits"))


def _mark_images_api_unsupported(base_url: str):
    base = _normalize_gpt_images_base_url(base_url)
    if base:
        _IMAGES_API_UNSUPPORTED_BASES.add(base)


def _images_api_known_unsupported(base_url: str) -> bool:
    base = _normalize_gpt_images_base_url(base_url)
    return bool(base and base in _IMAGES_API_UNSUPPORTED_BASES)


def _looks_like_images_api_unsupported(status_code: int, body: str) -> bool:
    if status_code not in {404, 405}:
        return False
    low = (body or "").lower()
    return (
        "path not found" in low
        or "not found" in low
        or "method not allowed" in low
        or "unsupported" in low
    )


def _is_agnes_model(model: str) -> bool:
    return (model or "").strip().lower().startswith("agnes-image-")


def _gpt_headers(content_type: bool = False) -> dict:
    headers = {}
    if content_type:
        headers["Content-Type"] = "application/json"
    api_key = _get_gpt_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _image_response_bytes(data: dict) -> Optional[bytes]:
    images = data.get("data", [])
    if not isinstance(images, list) or not images:
        return None

    first = images[0] or {}
    b64_data = first.get("b64_json") or first.get("base64")
    if b64_data:
        return base64.b64decode(b64_data)

    url = first.get("url") or first.get("image_url") or ""
    if not url and isinstance(first.get("image"), dict):
        url = first["image"].get("url", "")
    if isinstance(url, dict):
        url = url.get("url", "")
    if isinstance(url, str) and url.startswith("data:image/"):
        return base64.b64decode(url.split(",", 1)[1])
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        resp = REQUEST_SESSION.get(url, timeout=60)
        resp.raise_for_status()
        return resp.content
    return None


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


def _image_bytes_for_edit(image_path: str, max_size: int = IMG2IMG_MAX_SIZE) -> bytes:
    """Resize reference image to a compact PNG for /v1/images/edits."""
    from PIL import Image

    img = Image.open(image_path)
    img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _gpt_endpoint_label(url: str = "") -> str:
    parsed = urlparse(url or _get_gpt_raw_base_url() or _get_gpt_base_url())
    return parsed.netloc or (url or "GPT Image")


def _is_wardrobe_reference(ref_image: Optional[str]) -> bool:
    raw = str(ref_image or "").replace("\\", "/").lower()
    return "/wardrobe/" in raw or raw.startswith("wardrobe_") or "/references/wardrobe/" in raw


def _generate_via_images_api(prompt: str, ref_image: Optional[str], size: Optional[str], raw_base_url: str) -> Optional[tuple]:
    """Call OpenAI-compatible /v1/images/generations or /v1/images/edits."""
    images_base = _normalize_gpt_images_base_url(raw_base_url)
    if not images_base:
        print("ERROR: image_gen.gpt_base_url is required", file=sys.stderr)
        return None

    agnes_img2img = bool(ref_image and _is_agnes_model(GPTIMAGE_DIRECT_MODEL))
    endpoint = f"{images_base}/images/generations" if (not ref_image or agnes_img2img) else f"{images_base}/images/edits"
    endpoint_label = _gpt_endpoint_label(endpoint)
    headers = _gpt_headers()
    timeout = IMG2IMG_TIMEOUT if ref_image else TEXT2IMG_TIMEOUT
    start = time.time()

    edit_prompt = prompt
    if ref_image:
        if _is_wardrobe_reference(ref_image):
            edit_prompt += (
                "\n[IMPORTANT] Use the reference image ONLY as an outfit and styling reference. "
                "Copy the clothing combination, garment structure, fabric layering, colors, accessories, footwear, displayed wig hairstyle, and overall outfit styling mood from the reference image. "
                "Do NOT copy any human figure layout from the reference image. The person's face, hairstyle, pose, body posture, hand gestures, gaze direction, camera angle, framing, background, lighting, and expression must follow the text description."
            )
        else:
            edit_prompt += (
                "\n[IMPORTANT] Use the reference image ONLY as a facial/style reference. "
                "Do NOT copy the hairstyle, clothing, pose, body posture, hand gestures, gaze direction, camera angle, framing, background, lighting, or expression "
                "unless the text description explicitly asks for them."
            )

    for attempt in range(MAX_RETRIES):
        try:
            if ref_image and agnes_img2img:
                payload = {
                    "model": GPTIMAGE_DIRECT_MODEL,
                    "prompt": edit_prompt,
                    "n": 1,
                    "extra_body": {
                        "image": [_compress_image_for_img2img(ref_image)],
                        "response_format": "url",
                    },
                }
                if size:
                    payload["size"] = size
                resp = REQUEST_SESSION.post(
                    endpoint,
                    headers={**headers, "Content-Type": "application/json"},
                    json=payload,
                    timeout=timeout,
                )
            elif ref_image:
                image_bytes = _image_bytes_for_edit(ref_image)
                data = {
                    "model": GPTIMAGE_DIRECT_MODEL,
                    "prompt": edit_prompt,
                    "n": "1",
                }
                if size:
                    data["size"] = size
                resp = REQUEST_SESSION.post(
                    endpoint,
                    headers=headers,
                    data=data,
                    files={"image": ("reference.png", image_bytes, "image/png")},
                    timeout=timeout,
                )
            else:
                payload = {
                    "model": GPTIMAGE_DIRECT_MODEL,
                    "prompt": prompt,
                    "n": 1,
                }
                if size:
                    payload["size"] = size
                resp = REQUEST_SESSION.post(
                    endpoint,
                    headers={**headers, "Content-Type": "application/json"},
                    json=payload,
                    timeout=timeout,
                )

            if resp.status_code != 200:
                if _looks_like_images_api_unsupported(resp.status_code, resp.text):
                    _mark_images_api_unsupported(raw_base_url)
                    print(
                        f"Images API unsupported [{endpoint_label}]; "
                        "using chat-compatible GPT Image endpoint",
                        file=sys.stderr,
                    )
                    return None
                print(
                    f"Images API error {resp.status_code} [{endpoint_label}] "
                    f"(attempt {attempt + 1}/{MAX_RETRIES}): {resp.text[:240]}",
                    file=sys.stderr,
                )
                if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                    continue
                return None

            img_data = _image_response_bytes(resp.json())
            if not img_data:
                print(
                    f"Images API: no image in response [{endpoint_label}] "
                    f"(attempt {attempt + 1}/{MAX_RETRIES}): {resp.text[:240]}",
                    file=sys.stderr,
                )
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                    continue
                return None
            return img_data, round(time.time() - start, 2)

        except Exception as e:
            print(f"Images API failed [{endpoint_label}] (attempt {attempt + 1}/{MAX_RETRIES}): {e}", file=sys.stderr)
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            return None


def _generate_via_chat_gpt(prompt: str, ref_image: Optional[str] = None, size: Optional[str] = None) -> Optional[tuple]:
    """Call the legacy chat-compatible GPT Image endpoint."""
    base_url = _get_gpt_base_url()
    if not base_url:
        print("ERROR: image_gen.gpt_base_url is required", file=sys.stderr)
        return None

    if not GPTIMAGE_DIRECT_MODEL:
        print("ERROR: image_gen.gpt_model is required", file=sys.stderr)
        return None

    headers = _gpt_headers(content_type=True)

    if ref_image:
        try:
            compressed_img = _compress_image_for_img2img(ref_image)
            if _is_wardrobe_reference(ref_image):
                face_instruction = "\n[IMPORTANT] Use the reference image ONLY as an outfit reference. Recreate the clothing pieces, layering, silhouette, colors, materials, accessories, footwear, and displayed wig hairstyle from the reference image on the person described in the text. Do NOT treat the reference image as a face reference, and do NOT copy any pose, body posture, gaze direction, camera angle, framing, background, lighting, or expression from it."
            else:
                face_instruction = "\n[IMPORTANT] Use the reference image ONLY as a facial reference. Focus on matching the face shape, facial structure, and overall facial features to achieve high similarity with the person in the reference image. Do NOT copy or reference the hairstyle, hair color, hair accessories, clothing, outfit, pose, body posture, hand gestures, gaze direction, camera angle, framing, background, lighting, or any other non-facial elements from the reference image. All of these must strictly follow the text description above. Do NOT copy the facial expression, mouth shape, tongue, or grin from the reference image — the expression must also strictly follow the text description."
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


def _generate_via_direct_gpt(prompt: str, ref_image: Optional[str] = None, size: Optional[str] = None) -> Optional[tuple]:
    """Call the configured GPT Image endpoint (text2img + img2img).

    Args:
        prompt: Generation prompt
        ref_image: Optional reference image path for img2img mode
        size: Optional output image size

    Returns:
        (img_data, elapsed_time) tuple or None on failure
    """
    raw_base_url = _get_gpt_raw_base_url()
    if not raw_base_url:
        print("ERROR: image_gen.gpt_base_url is required", file=sys.stderr)
        return None
    if not GPTIMAGE_DIRECT_MODEL:
        print("ERROR: image_gen.gpt_model is required", file=sys.stderr)
        return None

    if not _is_explicit_chat_url(raw_base_url) and not _images_api_known_unsupported(raw_base_url):
        result = _generate_via_images_api(prompt, ref_image, size, raw_base_url)
        if result or _is_explicit_images_url(raw_base_url):
            return result
        if _images_api_known_unsupported(raw_base_url):
            print("Images API unsupported; using chat-compatible GPT Image endpoint", file=sys.stderr)
        else:
            print("Images API failed; retrying chat-compatible GPT Image endpoint", file=sys.stderr)
    return _generate_via_chat_gpt(prompt, ref_image, size)


def generate(theme: str, send: bool = False, caption: bool = False,
             prompt_override: Optional[str] = None, ref_image: Optional[str] = None,
             style: Optional[str] = None, size: Optional[str] = None,
             prompt_is_final: bool = False, source: str = "chat",
             sync_gallery: bool = True, schedule_time: str = ""):
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
    prompt = (
        prompt_override
        if prompt_is_final and prompt_override
        else build_prompt(
            theme,
            prompt_override,
            allow_random_pool=(theme == "custom" and not prompt_override),
        )
    )
    if not prompt:
        print(f"ERROR: prompt is empty for theme={theme}; generation aborted", file=sys.stderr)
        return None
    requested_mode = "img2img" if ref_image else "text2img"
    final_mode = requested_mode
    requested_ref_image = ref_image or ""
    used_ref_image = requested_ref_image
    fallback_used = False
    endpoint_label = _gpt_endpoint_label()
    print(f"🎨 GPT Image via {endpoint_label} ({requested_mode})...", file=sys.stderr)

    result = _generate_via_direct_gpt(prompt, ref_image, size)
    if not result and ref_image:
        print(f"GPT Image img2img failed via {endpoint_label}; retrying text2img without reference image", file=sys.stderr)
        fallback_used = True
        final_mode = "text2img"
        used_ref_image = ""
        result = _generate_via_direct_gpt(prompt, None, size)

    if not result:
        print(f"ERROR: GPT Image endpoint failed: {endpoint_label}", file=sys.stderr)
        return None

    img_data, gen_time = result
    path, filename, ts = save_image(img_data, theme, GPTIMAGE_DIRECT_MODEL, style=style, target_size=size)
    update_metadata(
        filename,
        theme,
        prompt,
        GPTIMAGE_DIRECT_MODEL,
        ts,
        gen_time,
        {
            "source": source,
            "base_style": style or "",
            "requested_generation_mode": requested_mode,
            "generation_mode": final_mode,
            "ref_image": os.path.basename(used_ref_image) if used_ref_image else "",
            "ref_image_path": used_ref_image,
            "requested_ref_image": os.path.basename(requested_ref_image) if requested_ref_image else "",
            "requested_ref_image_path": requested_ref_image,
            "fallback_used": fallback_used,
            "fallback_from": "img2img" if fallback_used else "",
            "fallback_to": "text2img" if fallback_used else "",
        },
    )

    cap_text = None
    if caption:
        cap_text = build_caption_for_image(theme, path, schedule_time=schedule_time)
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
            schedule_time=schedule_time,
            generation_mode=final_mode,
            requested_generation_mode=requested_mode,
            ref_image=used_ref_image,
            requested_ref_image=requested_ref_image,
            fallback_used=fallback_used,
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
    parser.add_argument("--source", choices=["cron", "web", "chat", "custom", "hermes_api"], default="chat", help="来源标识")
    parser.add_argument("--schedule-time", type=str, default="", help="对应的日程时间和活动，如 '11:00 做奶茶'")
    args = parser.parse_args()
    path = generate(args.theme, args.send, args.caption, args.prompt, args.ref_image,
                    size=args.size, source=args.source, schedule_time=args.schedule_time)
    if not path:
        print("ERROR: GPT Image generation failed", file=sys.stderr)
        sys.exit(1)
