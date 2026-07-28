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
    schedule_filename_theme,
    sync_to_gallery,
    save_image,
    send_photo,
    update_metadata,
    CONFIG_PATH,
    _API_KEYS_CONFIG_PATH,
)
from characters import NATURAL_FACE_SHAPE_GUARD

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
IMG2IMG_MAX_SIZE = get_image_int("img2img_max_size", 1024, 64)
IMG2IMG_QUALITY = get_image_int("img2img_quality", 92, 1, 100)
_IMAGES_API_UNSUPPORTED_BASES: set[str] = set()
_LAST_TERMINAL_IMAGE_FAILURE = ""
_LAST_IMAGE_FAILURE_KIND = ""
_LAST_SUCCESSFUL_IMAGE_ENDPOINT = ""


def _configured_image_base_url(url: str) -> str:
    base = str(url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/images/generations", "/images/edits"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _normalize_gpt_image_endpoint(item: dict) -> dict:
    base_url = _configured_image_base_url(str(item.get("base_url", "") or "").strip())
    api_key = str(item.get("api_key", "") or "").strip()
    label = str(item.get("label", "") or "").strip()
    if not base_url or not api_key:
        return {}
    endpoint = {"base_url": base_url, "api_key": api_key}
    if label:
        endpoint["label"] = label
    return endpoint


def _load_gpt_image_endpoints() -> list[dict]:
    """Load complete GPT Image endpoints from env or local key config."""
    env_json = os.getenv("GPT_IMAGE_ENDPOINTS", "").strip()
    if env_json:
        try:
            data = json.loads(env_json)
            if isinstance(data, list):
                endpoints = [
                    endpoint
                    for item in data
                    if isinstance(item, dict)
                    for endpoint in (_normalize_gpt_image_endpoint(item),)
                    if endpoint
                ]
                if endpoints:
                    return endpoints
        except (json.JSONDecodeError, TypeError) as e:
            print(f"Failed to parse GPT_IMAGE_ENDPOINTS env var: {e}", file=sys.stderr)

    config_path = _API_KEYS_CONFIG_PATH
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            raw = config.get("gpt_image_endpoints")
            if isinstance(raw, list):
                return [
                    endpoint
                    for item in raw
                    if isinstance(item, dict)
                    for endpoint in (_normalize_gpt_image_endpoint(item),)
                    if endpoint
                ]
        except Exception as e:
            print(f"Failed to read gpt_image_endpoints from api_keys_config.json: {e}", file=sys.stderr)
    return []


def _get_gpt_key() -> str:
    """Read GPT key from environment variable or api_keys_config.json."""
    endpoints = _load_gpt_image_endpoints()
    if endpoints:
        return endpoints[0]["api_key"]

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
    endpoints = _load_gpt_image_endpoints()
    if endpoints:
        return endpoints[0]["base_url"]

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


def _gpt_chat_fallback_enabled() -> bool:
    """Return whether Images API failures may fall back to /chat/completions."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f) or {}
        return bool(data.get("gpt_chat_fallback_enabled", False))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False


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


def _terminal_image_failure_reason(status_code: int, body: str) -> str:
    """Return a user-facing reason for failures that retries cannot repair."""
    lower = str(body or "").lower()
    if "insufficient_quota" in lower or "额度已用完" in lower:
        return "GPT Image 图片账号额度已用完"
    if "deactivated_workspace" in lower or "workspace" in lower and "deactivat" in lower:
        return "GPT Image 工作区已停用"
    if "no available compatible accounts" in lower:
        return "GPT Image 没有可用的兼容账号"
    # AxonHub/CPA often returns temporary distributor routing failures as
    # model_not_found + "no available channel". These recover after a short
    # wait, so do not treat them as terminal.
    if "model_not_found" in lower and not (
        "no available channel" in lower
        or "无可用渠道" in lower
        or "under group" in lower
        or "分组" in str(body or "")
    ):
        return "GPT Image 模型不可用"
    if status_code in {401, 403}:
        return "GPT Image 凭据无效或无权限"
    if (
        "moderation_blocked" in lower
        or "safety_violations" in lower
        or "content_policy_violation" in lower
        or "content policy" in lower
        or "safety system" in lower
    ):
        return "GPT Image 内容安全拦截（moderation）"
    return ""


def _set_terminal_image_failure(status_code: int, body: str) -> str:
    global _LAST_TERMINAL_IMAGE_FAILURE
    reason = _terminal_image_failure_reason(status_code, body)
    if reason:
        _LAST_TERMINAL_IMAGE_FAILURE = reason
    return reason


def _note_image_failure_kind(kind: str) -> str:
    """Remember the latest images-api failure class for dual-ref fallback reason."""
    global _LAST_IMAGE_FAILURE_KIND
    value = str(kind or "").strip()
    if value:
        _LAST_IMAGE_FAILURE_KIND = value
    return value


def _images_api_failure_kind(
    status_code: Optional[int] = None,
    body: str = "",
    exc: Optional[BaseException] = None,
) -> str:
    """Classify timeout / Codex EOF / moderation for explicit operator logs."""
    text_blob = f"{body or ''} {exc or ''}".lower()
    if exc is not None and isinstance(exc, (requests.Timeout, TimeoutError)):
        return "timeout"
    if (
        exc is not None
        and isinstance(exc, (FileNotFoundError, PermissionError))
    ) or "reference image is not readable:" in text_blob:
        return "reference_unavailable"
    if exc is not None and isinstance(exc, (requests.ConnectionError, ConnectionError)):
        return "connection"
    if "timed out" in text_blob or "timeout" in text_blob or "read timed out" in text_blob:
        return "timeout"
    if (
        "unexpected eof" in text_blob
        or ("codex/images/edits" in text_blob and "eof" in text_blob)
        or ("internal_server_error" in text_blob and "images/edits" in text_blob)
    ):
        return "codex_edits_eof"
    if (
        "moderation_blocked" in text_blob
        or "safety_violations" in text_blob
        or "content_policy_violation" in text_blob
    ):
        return "moderation"
    if (
        "no available channel" in text_blob
        or "no available compatible accounts" in text_blob
        or "无可用渠道" in text_blob
        or "没有可用的兼容账号" in text_blob
    ):
        return "unavailable_channel"
    if status_code in {401, 403}:
        return "auth"
    if status_code:
        return f"http_{status_code}"
    return "error"


def _face_only_fallback_instruction() -> str:
    return (
        "\n[CRITICAL] Dual-reference upstream failed; face-only fallback mode. "
        "Use the reference image ONLY for facial identity and requested hair color/style "
        "(e.g. dusty rose pink hair / wispy air bangs). "
        "Pose, body posture, hands, outfit, props, scene, background, lighting, camera angle, "
        "framing, and composition must follow the text description strictly. "
        "One subject only — never put a second person in frame."
    )


def _is_agnes_model(model: str) -> bool:
    return (model or "").strip().lower().startswith("agnes-image-")


def _image_engine_label(model: str = "") -> str:
    name = (model or GPTIMAGE_DIRECT_MODEL or "").strip()
    lower = name.lower()
    if _is_agnes_model(name):
        return "Agnes"
    if "gpt-image" in lower or lower == "gpt image":
        return "GPT Image"
    return name or "GPT Image"


def _gpt_headers(content_type: bool = False, api_key: Optional[str] = None) -> dict:
    headers = {}
    if content_type:
        headers["Content-Type"] = "application/json"
    effective_key = _get_gpt_key() if api_key is None else str(api_key).strip()
    if effective_key:
        headers["Authorization"] = f"Bearer {effective_key}"
    return headers


def _direct_gpt_image_endpoints() -> list[dict]:
    """Return ordered endpoints, retaining the legacy single-endpoint config."""
    endpoints = _load_gpt_image_endpoints()
    if endpoints:
        return endpoints
    base_url = _get_gpt_raw_base_url()
    if not base_url:
        return []
    return [{"base_url": base_url, "api_key": _get_gpt_key()}]


def _endpoint_failure_allows_failover(kind: str) -> bool:
    value = str(kind or "").strip().lower()
    return value in {
        "timeout",
        "connection",
        "codex_edits_eof",
        "unavailable_channel",
        "unsupported_api",
    } or bool(re.fullmatch(r"http_5\d\d", value))


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

    _preflight_reference_image(image_path)
    img = Image.open(image_path)
    img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    if img.mode in ('RGBA', 'P'):
        img = img.convert('RGB')

    buffer = io.BytesIO()
    img.save(buffer, format='JPEG', quality=quality, optimize=True)
    b64 = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/jpeg;base64,{b64}"


def _image_bytes_for_edit(image_path: str, max_size: int = IMG2IMG_MAX_SIZE) -> bytes:
    """Resize a reference only when it exceeds the configured identity limit."""
    from PIL import Image

    _preflight_reference_image(image_path)
    img = Image.open(image_path)
    img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _preflight_reference_image(image_path: str) -> str:
    """Raise an explicit filesystem error before Pillow parses a reference."""
    path = str(image_path or "").strip()
    if not path:
        raise FileNotFoundError("reference image path is empty")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"reference image does not exist: {path}")
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError as exc:
        raise OSError(f"reference image is not readable: {path}: {exc}") from exc
    return path


def _request_quality_fields(model: str) -> dict:
    """Request the highest supported source quality from GPT Image endpoints."""
    if "gpt-image" not in str(model or "").strip().lower():
        return {}
    return {"quality": "high", "output_format": "png"}


def _gpt_endpoint_label(url: str = "") -> str:
    parsed = urlparse(url or _get_gpt_raw_base_url() or _get_gpt_base_url())
    return parsed.netloc or (url or "GPT Image")


def _is_wardrobe_reference(ref_image: Optional[str]) -> bool:
    raw = str(ref_image or "").replace("\\", "/").lower()
    return "/wardrobe/" in raw or raw.startswith("wardrobe_") or "/references/wardrobe/" in raw


def _reference_expression_guard() -> str:
    return (
        "\n[IMPORTANT] Facial expression guard: the facial expression must be newly generated "
        "from the current text description, schedule, action, scene, and emotional tone. "
        "Do NOT copy the reference image's facial expression, mouth shape, smile, grin, "
        "tongue, gaze emotion, or facial mood; ignore any strong expression in the reference image. "
        "If the reference shows pouty lips, duck face, pursed kissy mouth, 嘟嘴, or exaggerated lip push, "
        "treat that as reference-only noise and do not carry it into every generated frame. "
        "Mouth and expression may still be pouty, playful, or sweet when the schedule/text itself asks for it; "
        "just never default to the reference photo's mouth shape."
    )


def _reference_edit_instruction(
    ref_image: Optional[str],
    precise_edit: bool = False,
    include_face_shape_guard: bool = True,
) -> str:
    if precise_edit:
        return (
            "\n[CRITICAL] Precision edit mode: the attached image is the immutable source image, not "
            "a face or style reference. Apply only the explicitly requested local edit. Preserve every "
            "unnamed person, object, facial feature, garment, pose, hand, camera property, composition, "
            "crop, and stylistic attribute from the source. Do not reinterpret or regenerate unchanged "
            "areas, and do not apply the normal face-reference or wardrobe-reference rules."
        )
    if _is_wardrobe_reference(ref_image):
        return (
            "\n[IMPORTANT] Use the reference image ONLY as an outfit and styling reference. "
            "Copy the clothing combination, garment structure, fabric layering, colors, accessories, footwear, displayed wig hairstyle, and overall outfit styling mood from the reference image. "
            "Do NOT copy any human figure layout from the reference image. The person's face, hairstyle, pose, body posture, hand gestures, gaze direction, camera angle, framing, background, lighting, and expression must follow the text description."
            + _reference_expression_guard()
        )
    face_shape_guard = f"{NATURAL_FACE_SHAPE_GUARD} " if include_face_shape_guard else ""
    return (
        "\n[IMPORTANT] Use the reference image ONLY as a facial/style reference. "
        "Use stable identity cues such as the eyes, brows, nose, lips, and their relative spacing to keep the person recognizable. "
        "Treat the reference image's observed facial width, cheek volume, jaw contour, and chin proportions as the neutral baseline rather than an invitation to beautify or exaggerate them. "
        f"{face_shape_guard}"
        "Do NOT copy or reference the hairstyle, hair color, hair accessories, clothing, outfit, pose, body posture, hand gestures, gaze direction, camera angle, framing, background, lighting, mouth expression, or any other non-facial elements from the reference image. "
        "Do NOT transfer the reference photo mouth shape (including pout / duck-face / 嘟嘴 if present); rebuild expression from the text/schedule only. "
        "All non-facial elements must strictly follow the text description. "
        "If the text says the hair must be a specific color or hairstyle, that text is absolute and overrides the reference image completely, even when the reference image shows pink, red, light, or otherwise different hair."
        + _reference_expression_guard()
    )


def _normalize_ref_images(
    ref_image: Optional[str] = None,
    ref_images: Optional[list] = None,
) -> list[str]:
    """Normalize single/multi reference paths into an ordered unique list."""
    values: list[str] = []
    if isinstance(ref_images, (list, tuple)):
        values.extend(str(v or "").strip() for v in ref_images)
    if ref_image:
        values.insert(0, str(ref_image).strip())
    ordered: list[str] = []
    seen = set()
    for raw in values:
        path = str(raw or "").strip()
        if not path:
            continue
        key = os.path.abspath(os.path.expanduser(path))
        if key in seen:
            continue
        if not os.path.isfile(path) and not os.path.isfile(key):
            # keep original token; caller may resolve later
            if path in seen:
                continue
            seen.add(path)
            ordered.append(path)
            continue
        resolved = key if os.path.isfile(key) else path
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(resolved)
    return ordered


def _multi_reference_edit_instruction(
    ref_images: list[str],
    precise_edit: bool = False,
    include_face_shape_guard: bool = True,
) -> str:
    """Build dual/multi-ref instructions.

    Convention:
    - image 1 = base / pose / outfit / scene lock
    - image 2+ = face/identity (and optional hair color when requested in text)
    """
    refs = [str(x or "").strip() for x in (ref_images or []) if str(x or "").strip()]
    if not refs:
        return ""
    if len(refs) == 1:
        return _reference_edit_instruction(
            refs[0],
            precise_edit=precise_edit,
            include_face_shape_guard=include_face_shape_guard,
        )

    face_shape_guard = f"{NATURAL_FACE_SHAPE_GUARD} " if include_face_shape_guard else ""
    parts = [
        "[CRITICAL] Multi-reference edit mode with ordered images:",
        "Image 1 = immutable base photo. Strictly lock pose, body posture, hand gestures, outfit/clothing layers, accessories already worn, props, scene, background, lighting, camera angle, framing, crop, and composition from Image 1. Do not invent a new pose.",
        "Image 2 (and later face references) = identity/face source only. Transfer facial identity (eyes, brows, nose, lips, facial proportions) onto the person in Image 1.",
        f"{face_shape_guard}If the text requests a specific hair color or bangs (e.g. dusty rose pink hair / wispy air bangs), apply that hair change while still keeping Image 1 pose and outfit locked.".strip(),
        "Do NOT copy clothing, pose, hands, camera, background, body layout, or facial expression/mouth shape from Image 2 (including any pout/duck-face/嘟嘴 only present in the face reference). Expression follows text/schedule, not Image 2. Do NOT put a second person in frame. Keep one subject only.",
        "Change only: face identity + requested hair color/style from text/face refs. Everything else must match Image 1.",
    ]
    return chr(10) + chr(10).join(parts) + _reference_expression_guard()


def _generate_via_images_api(
    prompt: str,
    ref_image: Optional[str],
    size: Optional[str],
    raw_base_url: str,
    precise_edit: bool = False,
    ref_images: Optional[list] = None,
    api_key: Optional[str] = None,
    max_attempts: Optional[int] = None,
) -> Optional[tuple]:
    """Call OpenAI-compatible /v1/images/generations or /v1/images/edits."""
    global _LAST_TERMINAL_IMAGE_FAILURE

    images_base = _normalize_gpt_images_base_url(raw_base_url)
    engine_label = _image_engine_label()
    if not images_base:
        print("ERROR: image_gen.gpt_base_url is required", file=sys.stderr)
        return None

    refs = _normalize_ref_images(ref_image, ref_images)
    primary_ref = refs[0] if refs else ""
    multi_ref = len(refs) > 1
    agnes_img2img = bool(refs and _is_agnes_model(GPTIMAGE_DIRECT_MODEL))
    endpoint = f"{images_base}/images/generations" if (not refs or agnes_img2img) else f"{images_base}/images/edits"
    endpoint_label = _gpt_endpoint_label(endpoint)
    headers = _gpt_headers(api_key=api_key)
    timeout = IMG2IMG_TIMEOUT if refs else TEXT2IMG_TIMEOUT
    # Dual/multi-ref on Codex edits is often slow-fail (EOF/timeout). Do not burn full retries
    # before face-only fallback; single-ref / text2img keep normal retry budget.
    attempt_limit = max(1, int(max_attempts)) if max_attempts is not None else (1 if multi_ref else MAX_RETRIES)
    if multi_ref:
        attempt_limit = 1
    start = time.time()

    edit_prompt = prompt
    if refs:
        edit_prompt += _multi_reference_edit_instruction(
            refs,
            precise_edit=precise_edit,
            include_face_shape_guard=NATURAL_FACE_SHAPE_GUARD not in prompt,
        )

    for attempt in range(attempt_limit):
        try:
            if refs and agnes_img2img:
                payload = {
                    "model": GPTIMAGE_DIRECT_MODEL,
                    "prompt": edit_prompt,
                    "n": 1,
                    "extra_body": {
                        "image": [_compress_image_for_img2img(path) for path in refs],
                        "response_format": "url",
                    },
                }
                if size:
                    payload["size"] = size
                payload.update(_request_quality_fields(GPTIMAGE_DIRECT_MODEL))
                resp = REQUEST_SESSION.post(
                    endpoint,
                    headers={**headers, "Content-Type": "application/json"},
                    json=payload,
                    timeout=timeout,
                )
            elif refs:
                data = {
                    "model": GPTIMAGE_DIRECT_MODEL,
                    "prompt": edit_prompt,
                    "n": "1",
                }
                if size:
                    data["size"] = size
                data.update(_request_quality_fields(GPTIMAGE_DIRECT_MODEL))
                # OpenAI-compatible multi-image edits: image[] fields in order.
                files = []
                for index, path in enumerate(refs):
                    image_bytes = _image_bytes_for_edit(path)
                    files.append(
                        (
                            "image[]" if len(refs) > 1 else "image",
                            (f"reference_{index + 1}.png", image_bytes, "image/png"),
                        )
                    )
                resp = REQUEST_SESSION.post(
                    endpoint,
                    headers=headers,
                    data=data,
                    files=files,
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
                payload.update(_request_quality_fields(GPTIMAGE_DIRECT_MODEL))
                resp = REQUEST_SESSION.post(
                    endpoint,
                    headers={**headers, "Content-Type": "application/json"},
                    json=payload,
                    timeout=timeout,
                )

            if resp.status_code != 200:
                if _looks_like_images_api_unsupported(resp.status_code, resp.text):
                    _mark_images_api_unsupported(raw_base_url)
                    _note_image_failure_kind("unsupported_api")
                    print(
                        f"{engine_label} Images API unsupported [{endpoint_label}]",
                        file=sys.stderr,
                    )
                    return None
                failure_kind = _note_image_failure_kind(
                    _images_api_failure_kind(resp.status_code, resp.text)
                )
                terminal_reason = _set_terminal_image_failure(resp.status_code, resp.text)
                print(
                    f"{engine_label} Images API error {resp.status_code} kind={failure_kind} "
                    f"[{endpoint_label}] refs={len(refs)} "
                    f"(attempt {attempt + 1}/{attempt_limit}): {resp.text[:240]}",
                    file=sys.stderr,
                )
                if terminal_reason:
                    print(
                        f"{engine_label} terminal failure [{endpoint_label}]: {terminal_reason}",
                        file=sys.stderr,
                    )
                    return None
                if (
                    multi_ref
                    and failure_kind in {"timeout", "codex_edits_eof"}
                ):
                    print(
                        f"{engine_label} multi-ref fast-fail kind={failure_kind}; "
                        f"skip remaining edits retries",
                        file=sys.stderr,
                    )
                    return None
                if resp.status_code in RETRYABLE_STATUS and attempt < attempt_limit - 1:
                    time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                    continue
                return None

            payload_json = resp.json()
            if isinstance(payload_json, dict) and payload_json.get("error"):
                err_body = json.dumps(payload_json.get("error"), ensure_ascii=False)
                failure_kind = _note_image_failure_kind(
                    _images_api_failure_kind(resp.status_code, err_body)
                )
                terminal_reason = _set_terminal_image_failure(resp.status_code, err_body)
                print(
                    f"{engine_label} Images API business error kind={failure_kind} "
                    f"[{endpoint_label}] refs={len(refs)} "
                    f"(attempt {attempt + 1}/{attempt_limit}): {err_body[:240]}",
                    file=sys.stderr,
                )
                if terminal_reason:
                    print(
                        f"{engine_label} terminal failure [{endpoint_label}]: {terminal_reason}",
                        file=sys.stderr,
                    )
                    return None
                # Codex dual edits often returns HTTP 200 + unexpected EOF. Fail fast for multi-ref.
                if multi_ref and failure_kind in {"timeout", "codex_edits_eof"}:
                    print(
                        f"{engine_label} multi-ref fast-fail kind={failure_kind}; "
                        f"skip remaining edits retries",
                        file=sys.stderr,
                    )
                    return None
                if attempt < attempt_limit - 1:
                    time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                    continue
                return None
            img_data = _image_response_bytes(payload_json)
            if not img_data:
                print(
                    f"{engine_label} Images API: no image in response [{endpoint_label}] "
                    f"refs={len(refs)} (attempt {attempt + 1}/{attempt_limit}): {resp.text[:240]}",
                    file=sys.stderr,
                )
                if attempt < attempt_limit - 1:
                    time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                    continue
                return None
            return img_data, round(time.time() - start, 2)

        except Exception as e:
            failure_kind = _note_image_failure_kind(_images_api_failure_kind(exc=e))
            print(
                f"{engine_label} Images API failed kind={failure_kind} [{endpoint_label}] "
                f"refs={len(refs)} (attempt {attempt + 1}/{attempt_limit}): {e}",
                file=sys.stderr,
            )
            if failure_kind == "reference_unavailable":
                _LAST_TERMINAL_IMAGE_FAILURE = f"GPT Image 参考图不可用: {e}"
                return None
            if multi_ref and failure_kind in {"timeout", "codex_edits_eof"}:
                print(
                    f"{engine_label} multi-ref fast-fail kind={failure_kind}; "
                    f"skip remaining edits retries",
                    file=sys.stderr,
                )
                return None
            if attempt < attempt_limit - 1:
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            return None


def _generate_via_chat_gpt(
    prompt: str,
    ref_image: Optional[str] = None,
    size: Optional[str] = None,
    precise_edit: bool = False,
    ref_images: Optional[list] = None,
    raw_base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    max_attempts: Optional[int] = None,
) -> Optional[tuple]:
    """Call the legacy chat-compatible GPT Image endpoint."""
    global _LAST_TERMINAL_IMAGE_FAILURE

    base_url = _normalize_gpt_chat_url(raw_base_url) if raw_base_url else _get_gpt_base_url()
    if not base_url:
        print("ERROR: image_gen.gpt_base_url is required", file=sys.stderr)
        return None

    if not GPTIMAGE_DIRECT_MODEL:
        print("ERROR: image_gen.gpt_model is required", file=sys.stderr)
        return None

    headers = _gpt_headers(content_type=True, api_key=api_key)
    refs = _normalize_ref_images(ref_image, ref_images)

    if refs:
        try:
            face_instruction = _multi_reference_edit_instruction(
                refs,
                precise_edit=precise_edit,
                include_face_shape_guard=NATURAL_FACE_SHAPE_GUARD not in prompt,
            )
            content = []
            for path in refs:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": _compress_image_for_img2img(path)},
                    }
                )
            content.append({"type": "text", "text": prompt + face_instruction})
        except Exception as e:
            failure_kind = _note_image_failure_kind(_images_api_failure_kind(exc=e))
            if failure_kind == "reference_unavailable":
                _LAST_TERMINAL_IMAGE_FAILURE = f"GPT Image 参考图不可用: {e}"
            print(
                f"Failed to compress reference image kind={failure_kind}: {e}",
                file=sys.stderr,
            )
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

    timeout = IMG2IMG_TIMEOUT if refs else TEXT2IMG_TIMEOUT
    start = time.time()

    endpoint_label = _gpt_endpoint_label(base_url)
    attempt_limit = max(1, int(max_attempts)) if max_attempts is not None else MAX_RETRIES
    for attempt in range(attempt_limit):
        try:
            resp = REQUEST_SESSION.post(
                base_url,
                headers=headers,
                json=payload,
                timeout=timeout,
            )

            if resp.status_code != 200:
                terminal_reason = _set_terminal_image_failure(resp.status_code, resp.text)
                print(
                    f"Direct GPT API error {resp.status_code} [{endpoint_label}] "
                    f"(attempt {attempt + 1}/{attempt_limit}): {resp.text[:200]}",
                    file=sys.stderr,
                )
                if terminal_reason:
                    print(
                        f"Direct GPT API terminal failure [{endpoint_label}]: {terminal_reason}",
                        file=sys.stderr,
                    )
                    return None
                _note_image_failure_kind(_images_api_failure_kind(resp.status_code, resp.text))
                if resp.status_code in RETRYABLE_STATUS and attempt < attempt_limit - 1:
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
                        f"(attempt {attempt + 1}/{attempt_limit}): {str(img_url)[:100]}",
                        file=sys.stderr,
                    )
                    if attempt < attempt_limit - 1:
                        time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                        continue
                    return None
            else:
                response_content = msg.get("content", "") or ""
                b64_match = re.search(r'!\[[^\]]*\]\(data:image/[^;]+;base64,([^)]+)\)', response_content)
                if not b64_match:
                    terminal_reason = _set_terminal_image_failure(resp.status_code, response_content)
                    print(
                        f"Direct GPT API: no base64 image in response "
                        f"(attempt {attempt + 1}/{attempt_limit}): {response_content[:300]}",
                        file=sys.stderr,
                    )
                    if terminal_reason:
                        print(
                            f"Direct GPT API terminal failure [{endpoint_label}]: {terminal_reason}",
                            file=sys.stderr,
                        )
                        return None
                    if attempt < attempt_limit - 1:
                        time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                        continue
                    return None
                img_data = base64.b64decode(b64_match.group(1))
            elapsed = round(time.time() - start, 2)

            return img_data, elapsed

        except Exception as e:
            failure_kind = _note_image_failure_kind(_images_api_failure_kind(exc=e))
            print(f"Direct GPT API failed kind={failure_kind} [{endpoint_label}] (attempt {attempt + 1}/{attempt_limit}): {e}", file=sys.stderr)
            if failure_kind == "reference_unavailable":
                _LAST_TERMINAL_IMAGE_FAILURE = f"GPT Image 参考图不可用: {e}"
                return None
            if attempt < attempt_limit - 1:
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            return None


def _generate_via_direct_gpt(
    prompt: str,
    ref_image: Optional[str] = None,
    size: Optional[str] = None,
    precise_edit: bool = False,
    ref_images: Optional[list] = None,
) -> Optional[tuple]:
    """Call the configured GPT Image endpoint (text2img + img2img).

    Args:
        prompt: Generation prompt
        ref_image: Optional reference image path for img2img mode
        size: Optional output image size

    Returns:
        (img_data, elapsed_time) tuple or None on failure
    """
    global _LAST_IMAGE_FAILURE_KIND, _LAST_SUCCESSFUL_IMAGE_ENDPOINT

    endpoints = _direct_gpt_image_endpoints()
    if not endpoints:
        print("ERROR: image_gen.gpt_base_url is required", file=sys.stderr)
        return None
    if not GPTIMAGE_DIRECT_MODEL:
        print("ERROR: image_gen.gpt_model is required", file=sys.stderr)
        return None

    engine_label = _image_engine_label()
    is_agnes = _is_agnes_model(GPTIMAGE_DIRECT_MODEL)
    per_endpoint_attempts = 1 if len(endpoints) > 1 else None

    for endpoint_index, endpoint_config in enumerate(endpoints):
        raw_base_url = str(endpoint_config.get("base_url") or "").strip()
        api_key = str(endpoint_config.get("api_key") or "").strip()
        endpoint_label = endpoint_config.get("label") or _gpt_endpoint_label(raw_base_url)
        has_next_endpoint = endpoint_index < len(endpoints) - 1
        _LAST_IMAGE_FAILURE_KIND = ""

        if _is_explicit_chat_url(raw_base_url):
            result = _generate_via_chat_gpt(
                prompt,
                ref_image,
                size,
                precise_edit=precise_edit,
                ref_images=ref_images,
                raw_base_url=raw_base_url,
                api_key=api_key,
                max_attempts=per_endpoint_attempts,
            )
            if result:
                _LAST_SUCCESSFUL_IMAGE_ENDPOINT = str(endpoint_label)
                return result
            if _LAST_TERMINAL_IMAGE_FAILURE:
                return None
            if has_next_endpoint and _endpoint_failure_allows_failover(_LAST_IMAGE_FAILURE_KIND):
                print(
                    f"{engine_label} endpoint [{endpoint_label}] failed kind={_LAST_IMAGE_FAILURE_KIND}; "
                    "trying next configured endpoint",
                    file=sys.stderr,
                )
                continue
            return None

        images_api_unsupported = _images_api_known_unsupported(raw_base_url)
        if images_api_unsupported:
            _note_image_failure_kind("unsupported_api")
            result = None
        else:
            result = _generate_via_images_api(
                prompt,
                ref_image,
                size,
                raw_base_url,
                precise_edit=precise_edit,
                ref_images=ref_images,
                api_key=api_key,
                max_attempts=per_endpoint_attempts,
            )
        if result:
            _LAST_SUCCESSFUL_IMAGE_ENDPOINT = str(endpoint_label)
            return result
        if _LAST_TERMINAL_IMAGE_FAILURE:
            return None

        images_api_unsupported = _images_api_known_unsupported(raw_base_url)
        if has_next_endpoint and _endpoint_failure_allows_failover(_LAST_IMAGE_FAILURE_KIND):
            print(
                f"{engine_label} endpoint [{endpoint_label}] failed kind={_LAST_IMAGE_FAILURE_KIND}; "
                "trying next configured endpoint",
                file=sys.stderr,
            )
            continue
        if has_next_endpoint:
            print(
                f"{engine_label} endpoint [{endpoint_label}] failed kind={_LAST_IMAGE_FAILURE_KIND}; "
                "failure is not eligible for endpoint failover",
                file=sys.stderr,
            )
            return None

        reason = "unsupported" if images_api_unsupported else "failed"
        if _is_explicit_images_url(raw_base_url):
            return None
        if is_agnes:
            print(
                f"{engine_label} Images API {reason}; not retrying chat-compatible GPT Image endpoint",
                file=sys.stderr,
            )
            return None
        if not _gpt_chat_fallback_enabled():
            print(
                f"Images API {reason}; chat-compatible GPT Image fallback is disabled",
                file=sys.stderr,
            )
            return None

        if images_api_unsupported:
            print("Images API unsupported; using chat-compatible GPT Image endpoint", file=sys.stderr)
        else:
            print("Images API failed; retrying chat-compatible GPT Image endpoint", file=sys.stderr)
        result = _generate_via_chat_gpt(
            prompt,
            ref_image,
            size,
            precise_edit=precise_edit,
            ref_images=ref_images,
            raw_base_url=raw_base_url,
            api_key=api_key,
        )
        if result:
            _LAST_SUCCESSFUL_IMAGE_ENDPOINT = str(endpoint_label)
        return result

    return None


def generate(theme: str, send: bool = False, caption: bool = False,
             prompt_override: Optional[str] = None, ref_image: Optional[str] = None,
             style: Optional[str] = None, size: Optional[str] = None,
             prompt_is_final: bool = False, source: str = "chat",
             sync_gallery: bool = True, schedule_time: str = "",
             precise_edit: bool = False, ref_images: Optional[list] = None):
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
    refs = _normalize_ref_images(ref_image, ref_images)
    if refs and not ref_image:
        ref_image = refs[0]
    requested_mode = "img2img" if refs else "text2img"
    final_mode = requested_mode
    requested_ref_image = ref_image or ""
    used_ref_image = requested_ref_image
    fallback_used = False
    endpoint_label = _gpt_endpoint_label()
    engine_label = _image_engine_label()
    global _LAST_TERMINAL_IMAGE_FAILURE, _LAST_IMAGE_FAILURE_KIND
    _LAST_TERMINAL_IMAGE_FAILURE = ""
    _LAST_IMAGE_FAILURE_KIND = ""
    print(f"🎨 {engine_label} via {endpoint_label} ({requested_mode})...", file=sys.stderr)
    if len(refs) > 1:
        print(
            f"{engine_label} multi-ref ordered count={len(refs)} "
            f"(base={os.path.basename(refs[0])}, face={os.path.basename(refs[1])})",
            file=sys.stderr,
        )

    result = _generate_via_direct_gpt(
        prompt, ref_image, size, precise_edit=precise_edit, ref_images=refs
    )
    dual_fallback_used = False
    dual_fallback_reason = ""
    # A+B: dual/multi-ref failed (timeout/EOF/unstable edits) -> face-only single ref.
    # Keep pose lock in text instruction; lock quality may drift but still out-images.
    _skip_dual_face_fallback = bool(
        _LAST_TERMINAL_IMAGE_FAILURE
        and any(
            marker in _LAST_TERMINAL_IMAGE_FAILURE
            for marker in (
                "额度已用完",
                "凭据无效",
                "工作区已停用",
                "没有可用的兼容账号",
                "模型不可用",
            )
        )
    )
    if not result and len(refs) > 1 and not precise_edit and not _skip_dual_face_fallback:
        dual_fallback_reason = (
            _LAST_IMAGE_FAILURE_KIND
            or _LAST_TERMINAL_IMAGE_FAILURE
            or "dual_ref_upstream_failed"
        )
        # Convention: refs[0]=base lock, refs[1]=face/identity.
        face_ref = refs[1] if len(refs) > 1 else refs[-1]
        print(
            f"{engine_label} dual/multi-ref failed (kind={dual_fallback_reason}); "
            f"falling back to face-only ref={os.path.basename(face_ref)} "
            f"(pose lock via text; action may drift)",
            file=sys.stderr,
        )
        # Allow non-terminal dual failures (timeout/EOF) to retry with single face ref.
        # Also retry after dual moderation: single face ref often avoids base-image sexual block.
        _LAST_TERMINAL_IMAGE_FAILURE = ""
        # Keep kind for metadata; do not wipe so face-only path can still log separately.
        face_prompt = prompt + _face_only_fallback_instruction()
        result = _generate_via_direct_gpt(
            face_prompt,
            face_ref,
            size,
            precise_edit=False,
            ref_images=[face_ref],
        )
        if result:
            dual_fallback_used = True
            fallback_used = True
            final_mode = "img2img_face_only_fallback"
            used_ref_image = face_ref
            print(
                f"{engine_label} face-only fallback succeeded via {endpoint_label}",
                file=sys.stderr,
            )
        else:
            print(
                f"{engine_label} face-only fallback also failed"
                + (f": {_LAST_TERMINAL_IMAGE_FAILURE}" if _LAST_TERMINAL_IMAGE_FAILURE else ""),
                file=sys.stderr,
            )

    if not result and ref_image and not precise_edit and not _LAST_TERMINAL_IMAGE_FAILURE:
        print(
            f"{engine_label} img2img failed via {endpoint_label}; "
            f"retrying text2img without reference image",
            file=sys.stderr,
        )
        fallback_used = True
        final_mode = "text2img"
        used_ref_image = ""
        result = _generate_via_direct_gpt(prompt, None, size)
    elif not result and _LAST_TERMINAL_IMAGE_FAILURE:
        print(
            f"{engine_label} stopped without more retries: {_LAST_TERMINAL_IMAGE_FAILURE}",
            file=sys.stderr,
        )

    if not result:
        print(f"ERROR: {engine_label} endpoint failed: {endpoint_label}", file=sys.stderr)
        return None

    img_data, gen_time = result
    filename_theme = schedule_filename_theme(theme, schedule_time)
    path, filename, ts = save_image(
        img_data,
        theme,
        GPTIMAGE_DIRECT_MODEL,
        style=style,
        target_size=size,
        filename_theme=filename_theme,
    )
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
            "requested_size": size or "",
            "requested_generation_mode": requested_mode,
            "generation_mode": final_mode,
            "ref_image": os.path.basename(used_ref_image) if used_ref_image else "",
            "ref_image_path": used_ref_image,
            "requested_ref_image": os.path.basename(requested_ref_image) if requested_ref_image else "",
            "requested_ref_image_path": requested_ref_image,
            "fallback_used": fallback_used,
            "fallback_from": (
                "multi_ref_img2img"
                if dual_fallback_used
                else ("img2img" if fallback_used else "")
            ),
            "fallback_to": (
                "img2img_face_only"
                if dual_fallback_used
                else ("text2img" if fallback_used and final_mode == "text2img" else "")
            ),
            "dual_ref_fallback_used": dual_fallback_used,
            "dual_ref_fallback_reason": dual_fallback_reason if dual_fallback_used else "",
            "requested_ref_images": [os.path.basename(x) for x in refs],
            "precise_edit": bool(precise_edit),
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
    parser.add_argument("--ref-images", type=str, default=None, help="多参考图，逗号分隔")
    parser.add_argument("--size", type=str, default=None, help="图片尺寸")
    parser.add_argument("--source", choices=["cron", "web", "chat", "custom", "hermes_api"], default="chat", help="来源标识")
    parser.add_argument("--schedule-time", type=str, default="", help="对应的日程时间和活动，如 '11:00 做奶茶'")
    parser.add_argument("--precise-edit", action="store_true", help="严格局部编辑，禁止无参考图降级")
    args = parser.parse_args()
    path = generate(args.theme, args.send, args.caption, args.prompt, args.ref_image,
                    size=args.size, source=args.source, schedule_time=args.schedule_time,
                    precise_edit=args.precise_edit,
                    ref_images=[p.strip() for p in str(args.ref_images or "").split(",") if p.strip()] or None)
    if not path:
        print("ERROR: GPT Image generation failed", file=sys.stderr)
        sys.exit(1)
