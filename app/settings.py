"""Runtime configuration and path helpers for portrait gallery."""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import yaml

APP_DIR = Path(__file__).resolve().parent
DEFAULT_PROJECT_ROOT = APP_DIR.parent

DEFAULT_GITEE_IMAGE_URL = "https://ai.gitee.com/v1/images/generations"

DEFAULT_OUTFIT_STYLES = [
    "冷御风", "甜美风", "元气风", "温柔风", "优雅风",
    "休闲风", "酷飒风", "清新风", "性感风", "复古风",
]

GENERIC_APPEARANCE = (
    "adult woman with natural facial features, realistic body proportions, everyday styling, "
    "clear face, expressive eyes, and a coherent personal look — photographed as a real person, not a rendered model"
)

DEFAULT_PHOTO_REALISM_FLOOR = (
    "Real photographed person. "
    "Natural skin texture. "
    "No plastic skin. "
    "No heavy retouching. "
    "No AI artifacts."
)

DEFAULT_QUALITY_PREFIX = (
    "Candid real-life smartphone photograph.\n"
    "Natural ambient light only.\n"
    "True-to-life color.\n"
    "Mild optical softness.\n"
    "Subtle sensor noise.\n"
    "No HDR glow.\n"
    "No cinematic color grade.\n\n"
    "Casual everyday snapshot.\n"
    "Natural handheld phone camera.\n"
    "Relaxed candid moment.\n"
    "Slightly imperfect but natural framing.\n"
    "No heavy retouching.\n"
    "No AI artifacts.\n"
    "No plastic skin.\n"
    "Natural skin texture."
)

DEFAULT_STYLE_REFERENCE_FILES = {
    "cool": "reference_face.jpg",
    "girly": "ref_style_girly.jpg",
    "sweet": "ref_style_sweet.jpg",
}

DEFAULT_STYLE_REFERENCE_PROMPTS = {
    "cool": (
        "Cool elegant portrait reference: mature, confident, polished look, "
        "sleek fashion styling, cool-toned or dark outfit mood, natural facial proportions, "
        "calm aloof expression, high-fashion city vibe, suitable for 酷飒风、冷御风、优雅风、复古风 and other chic or edgy schedules."
    ),
    "girly": (
        "Bright girly portrait reference: youthful, lively, cheerful and playful look, "
        "energetic styling, cute expressive face, fresh casual or sporty mood, "
        "sunny upbeat atmosphere, suitable for 元气风、少女风、休闲风 and active daily scenes."
    ),
    "sweet": (
        "Soft sweet portrait reference: gentle, warm, delicate and romantic look, "
        "soft styling, natural relaxed facial mood with a soft closed or lightly parted mouth "
        "(never pouty lips, duck face, or 嘟嘴), cozy pastel or fresh outfit atmosphere, "
        "dreamy approachable vibe, suitable for 甜美风、温柔风、清新风 and relaxed cozy schedules."
    ),
}

RUNTIME_CONFIG_MUTABLE_FIELDS = {
    "llm": {"model", "models", "fallback_model"},
    "integrations": {"hermes_cli", "openclaw_cli"},
}


def configured_timezone(config: dict | None = None) -> ZoneInfo:
    timezone_name = str(get_nested(config or {}, "config.timezone", "") or "").strip()
    timezone_name = timezone_name or os.getenv("TZ", "") or "Asia/Shanghai"
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def service_now(config: dict | None = None) -> datetime:
    return datetime.now(configured_timezone(config))


def service_today(config: dict | None = None) -> date:
    return service_now(config).date()


def normalize_runtime_config(value: Any) -> dict:
    """Keep Web-mutated overrides inside the intentionally writable surface."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for section, allowed_fields in RUNTIME_CONFIG_MUTABLE_FIELDS.items():
        section_value = value.get(section)
        if not isinstance(section_value, dict):
            continue
        cleaned = {
            key: field_value
            for key, field_value in section_value.items()
            if key in allowed_fields
        }
        if cleaned:
            result[section] = cleaned
    return result

DEFAULT_BASE_STYLE_LABELS = {
    "cool": "冷御风",
    "girly": "少女风",
    "sweet": "甜妹风",
}

DEFAULT_OUTFIT_STYLE_MAP = {
    "冷御风": {"base_style": "cool", "prompt_hint": "cool elegant style"},
    "甜美风": {"base_style": "sweet", "prompt_hint": "sweet style"},
    "元气风": {"base_style": "girly", "prompt_hint": "energetic girly style"},
    "温柔风": {"base_style": "sweet", "prompt_hint": "gentle soft style"},
    "优雅风": {"base_style": "cool", "prompt_hint": "elegant style"},
    "休闲风": {"base_style": "girly", "prompt_hint": "casual style"},
    "酷飒风": {"base_style": "cool", "prompt_hint": "chic cool style"},
    "清新风": {"base_style": "sweet", "prompt_hint": "fresh style"},
    "性感风": {"base_style": "cool", "prompt_hint": "glamorous style"},
    "复古风": {"base_style": "cool", "prompt_hint": "retro style"},
    "少女风": {"base_style": "girly", "prompt_hint": "youthful girly style"},
    "甜妹风": {"base_style": "sweet", "prompt_hint": "sweet soft style"},
}

DEFAULT_THEME_STYLE_MAP = {
    "morning": ("sweet", "甜妹风"),
    "noon": ("girly", "少女风"),
    "evening": ("cool", "冷御风"),
    "bedtime": ("sweet", "甜妹风"),
    "sexy": ("cool", "冷御风"),
    "custom": ("sweet", "甜妹风"),
}

DEFAULT_CUSTOM_IMAGE_ASPECT = "1:1"
DEFAULT_CUSTOM_IMAGE_RESOLUTION = "1k"
DEFAULT_CUSTOM_IMAGE_SIZE = "1024x1024"
DEFAULT_SCHEDULE_IMAGE_SIZE = "1536x2048"
DEFAULT_CUSTOM_SHOT_TYPE = "selfie"

LEGACY_SCHEDULE_IMAGE_FRAMING_RULE = (
    "Mandatory 3:4 full-body environmental framing: pull the camera back and show the entire person "
    "from the complete top of the hair to both shoes, adapted naturally to the scheduled action. "
    "Keep clear breathing room above the hair and below the feet, with the head safely inside the upper "
    "80 percent of the canvas and all limbs, hands, footwear, and important props inside the frame. "
    "For seated or leaning actions, show the complete head, full seated posture, legs, feet, and surrounding "
    "activity area. Use an eye-level camera at a comfortable distance; no close-up, medium crop, high-angle "
    "crop, oversized person, cut-off forehead, missing hair, cropped knees, or cropped shoes. "
    "Fill the entire 3:4 canvas edge to edge with the photographed scene; no black bars, blurred side panels, "
    "letterboxing, pillarboxing, frames, borders, or blank margins. "
    "This complete-subject framing rule overrides any casual or slightly imperfect framing instruction."
)

SCHEDULE_IMAGE_FRAMING_MARKER = "Professional 3:4 lifestyle photography composition:"

SCHEDULE_IMAGE_FRAMING_RULE = (
    f"{SCHEDULE_IMAGE_FRAMING_MARKER} FRAMING MODE: ORDINARY DAILY PORTRAIT (mandatory). "
    "Use an eye-level medium or medium-long portrait cropped from the complete head to the waist, hips, or mid-thigh; "
    "never use a head-to-toe view. Keep knees, lower legs, feet, shoes, and most of the floor outside the frame; they "
    "do not need to be visible even when the outfit description lists them or the subject is standing or walking. "
    "For a desk, table, cooking, craft, plant-care, or handheld-object activity, prioritize the face, both hands, the "
    "tool or prop, and the immediate work surface. The subject should fill roughly 70 to 90 percent of the frame height "
    "without cutting the hair, face, hands, or important props. Place the camera at the subject's eye or upper-torso "
    "height with a level horizon and a natural 50-85mm-equivalent portrait perspective. No high-angle, overhead, "
    "downward-looking, wide-angle, ultra-wide, distant-camera, or surveillance-like view. Do not foreshorten the body, "
    "enlarge the head relative to the body, shorten the legs, or make the subject look small or squat. Preserve elegant, "
    "natural human proportions and use the immediate environment only to support the action. Do not copy a reference "
    "image's camera distance or crop. Fill the entire 3:4 canvas edge to edge with the photographed scene; no black "
    "bars, blurred side panels, letterboxing, pillarboxing, frames, borders, or blank margins. This mandatory framing "
    "mode overrides any outfit details, visible-shoes request, generic full-body wording, or slightly imperfect framing."
)

SCHEDULE_IMAGE_OOTD_FRAMING_RULE = (
    f"{SCHEDULE_IMAGE_FRAMING_MARKER} FRAMING MODE: EXPLICIT OOTD OR CLOTHING SHOWCASE. "
    "Use a level, eye-height full-body fashion photograph only because the scheduled Activity or Action explicitly "
    "centers on showing the complete outfit. Keep the complete hair and both shoes inside the frame with restrained "
    "breathing room, use a natural 50-70mm-equivalent perspective, and preserve long, realistic body proportions. "
    "No high-angle, overhead, downward-looking, ultra-wide, distorted, or distant catalog view. The subject must remain "
    "the clear visual focus. Fill the entire 3:4 canvas edge to edge; no black bars, blurred panels, frames, borders, or "
    "blank margins. Do not copy the reference image's camera distance or crop."
)

SCHEDULE_IMAGE_SCENERY_FRAMING_RULE = (
    f"{SCHEDULE_IMAGE_FRAMING_MARKER} FRAMING MODE: EXPLICIT SCENERY OR SENSE-OF-PLACE PHOTO. "
    "Use a wider environmental composition because the scheduled Activity or Action explicitly centers on sharing or "
    "photographing the scenery, landscape, architecture, skyline, or view. Keep a level natural camera, undistorted "
    "human proportions, and a readable relationship between the person and place; full-body visibility is optional, "
    "not mandatory. Avoid high-angle, overhead, ultra-wide distortion, excessive empty floor or ceiling, and a tiny "
    "incidental subject. Fill the entire 3:4 canvas edge to edge; no black bars, blurred panels, frames, borders, or "
    "blank margins. Do not copy the reference image's camera distance or crop."
)

SCHEDULE_OOTD_INTENT_RE = re.compile(
    r"(?:\bootd\b|(?:showcase|show|check|style|photograph|share)\b.{0,40}\b(?:outfit|look|clothing)\b|"
    r"(?:outfit|look|clothing)\b.{0,40}\b(?:showcase|check|photo)|"
    r"(?:展示|分享|拍摄|检查|试穿|搭配).{0,12}(?:穿搭|造型|服装)|"
    r"(?:穿搭|造型|服装).{0,12}(?:展示|分享|拍摄|检查|试穿))",
    re.IGNORECASE | re.DOTALL,
)

SCHEDULE_SCENERY_INTENT_RE = re.compile(
    r"(?:\b(?:share|photograph|capture|admire|show)\b.{0,50}\b"
    r"(?:scenery|landscape|architecture|skyline|panorama|seascape|mountain view|city view)\b|"
    r"(?:分享|拍摄|记录|欣赏).{0,12}(?:风景|景色|建筑|天际线|全景|山景|海景|夜景))",
    re.IGNORECASE | re.DOTALL,
)

SCHEDULE_ACTIVITY_ACTION_RE = re.compile(
    r"\bActivity:\s*(?P<activity>.*?)\s+Action:\s*(?P<action>.*?)"
    r"(?=\s+(?:Scene|Outfit|Hair|Props|Lighting|Time):|(?:\.\s*)?Style:|$)",
    re.IGNORECASE | re.DOTALL,
)

CUSTOM_IMAGE_FRAMING_RULE = (
    "Keep the requested camera distance and main subject fully inside the frame; "
    "do not crop the head, important hands, held props, or the described action."
)

# Keep shot guidance short. Long pose menus and weighted tags tend to over-constrain
# custom generations and make the model ignore the user's scene description.
CUSTOM_SHOT_TYPE_PROMPTS = {
    "selfie": (
        "camera view: authentic smartphone selfie, natural arm-length selfie POV, "
        "head to upper torso visible, soft candid expression"
    ),
    "half_body": (
        "camera view: waist-up candid portrait photographed by someone else, "
        "head to waist visible, natural everyday framing"
    ),
    "full_body": (
        "camera view: full-body candid photo, head to toe visible, "
        "camera pulled back enough to show the complete outfit and pose"
    ),
}

CUSTOM_SHOT_LANDSCAPE_PROMPTS = {
    "selfie": "use the extra width for background while remaining a clear selfie",
    "half_body": "use the extra width for environment while remaining a third-person waist-up portrait",
    "full_body": "use the extra width for environment while keeping head-to-toe visibility",
}

CUSTOM_SHOT_TYPE_LABELS = {
    "selfie": "自拍",
    "half_body": "半身照",
    "full_body": "全身照",
}

CUSTOM_SHOT_TYPE_ALIASES = {
    "selfie": "selfie",
    "自拍": "selfie",
    "对镜": "selfie",
    "mirror": "selfie",
    "mirror_selfie": "selfie",
    "closeup": "selfie",
    "close_up": "selfie",
    "default": "half_body",
    "默认": "half_body",
    "upper_body": "half_body",
    "half": "half_body",
    "half_body": "half_body",
    "halfbody": "half_body",
    "半身": "half_body",
    "半身照": "half_body",
    "远景": "full_body",
    "ootd": "full_body",
    "full": "full_body",
    "full_body": "full_body",
    "fullbody": "full_body",
    "全身": "full_body",
    "全身照": "full_body",
}

CUSTOM_IMAGE_SIZE_MAP = {
    "1:1": {
        "1k": "1024x1024",
        "2k": "2048x2048",
        "4k": "4096x4096",
    },
    "3:2": {
        "1k": "1536x1024",
        "2k": "2048x1365",
        "4k": "4096x2731",
    },
    "2:3": {
        "1k": "1024x1536",
        "2k": "1365x2048",
        "4k": "2731x4096",
    },
    "16:9": {
        "1k": "1366x768",
        "2k": "2048x1152",
        "4k": "4096x2304",
    },
    "9:16": {
        "1k": "768x1366",
        "2k": "1152x2048",
        "4k": "2304x4096",
    },
    "3:4": {
        "1k": "768x1024",
        "2k": "1536x2048",
        "4k": "3072x4096",
    },
    "4:3": {
        "1k": "1024x768",
        "2k": "2048x1536",
        "4k": "4096x3072",
    },
    "21:9": {
        "1k": "1792x768",
        "2k": "3584x1536",
        "4k": "4096x1755",
    },
}

CUSTOM_IMAGE_MIN_DIMENSION = 256
CUSTOM_IMAGE_MAX_DIMENSION = 4096
CUSTOM_IMAGE_MAX_PIXELS = CUSTOM_IMAGE_MAX_DIMENSION * CUSTOM_IMAGE_MAX_DIMENSION

CUSTOM_IMAGE_ALLOWED_SIZES = {
    size
    for by_resolution in CUSTOM_IMAGE_SIZE_MAP.values()
    for size in by_resolution.values()
}

SCHEDULE_IMAGE_ALLOWED_SIZES = set(CUSTOM_IMAGE_SIZE_MAP["3:4"].values())

DEFAULT_PERSONA_SOURCE = "custom"
PERSONA_SOURCE_ALIASES = {
    "custom": "custom",
    "local": "custom",
    "manual": "custom",
    "自定义": "custom",
    "hermes": "hermes",
    "Hermes": "hermes",
    "openclaw": "openclaw",
    "OpenClaw": "openclaw",
    "open_claw": "openclaw",
}

DEFAULT_PUSH_CHANNEL = "wechat"
PUSH_CHANNEL_ALIASES = {
    "tg": "telegram",
    "TG": "telegram",
    "telegram": "telegram",
    "Telegram": "telegram",
    "微信": "wechat",
    "weixin": "wechat",
    "wechat": "wechat",
    "WeChat": "wechat",
}

DEFAULT_NO_PROXY_HOSTS = (
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "host.docker.internal",
)


def _non_empty(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_persona_source(value: Any) -> str:
    text = _non_empty(value)
    if not text:
        return DEFAULT_PERSONA_SOURCE
    return PERSONA_SOURCE_ALIASES.get(text, PERSONA_SOURCE_ALIASES.get(text.lower(), DEFAULT_PERSONA_SOURCE))


def normalize_push_channel(value: Any) -> str:
    text = _non_empty(value)
    if not text:
        return DEFAULT_PUSH_CHANNEL
    return PUSH_CHANNEL_ALIASES.get(text, PUSH_CHANNEL_ALIASES.get(text.lower(), DEFAULT_PUSH_CHANNEL))


def normalize_custom_image_aspect(value: Any) -> str:
    text = _non_empty(value).replace("：", ":")
    return text if text in CUSTOM_IMAGE_SIZE_MAP else DEFAULT_CUSTOM_IMAGE_ASPECT


def normalize_custom_image_resolution(value: Any) -> str:
    text = _non_empty(value).lower()
    if text in {"1", "1k", "默认", "default"}:
        return "1k"
    if text in {"2", "2k"}:
        return "2k"
    if text in {"4", "4k"}:
        return "4k"
    return DEFAULT_CUSTOM_IMAGE_RESOLUTION


def _normalize_custom_image_dimensions(value: Any) -> str:
    text = _non_empty(value).lower().replace("×", "x")
    match = re.fullmatch(r"\s*(\d{2,5})\s*x\s*(\d{2,5})\s*", text)
    if not match:
        return ""
    width, height = int(match.group(1)), int(match.group(2))
    if not (
        CUSTOM_IMAGE_MIN_DIMENSION <= width <= CUSTOM_IMAGE_MAX_DIMENSION
        and CUSTOM_IMAGE_MIN_DIMENSION <= height <= CUSTOM_IMAGE_MAX_DIMENSION
        and width * height <= CUSTOM_IMAGE_MAX_PIXELS
    ):
        return ""
    return f"{width}x{height}"


def normalize_custom_image_size(size: Any = "", aspect: Any = "", resolution: Any = "") -> str:
    aspect_text = _non_empty(aspect).replace("：", ":")
    resolution_text = _non_empty(resolution).lower()
    if aspect_text in CUSTOM_IMAGE_SIZE_MAP and resolution_text in CUSTOM_IMAGE_SIZE_MAP[aspect_text]:
        return CUSTOM_IMAGE_SIZE_MAP[aspect_text][resolution_text]

    size_text = _non_empty(size).lower()
    if size_text in CUSTOM_IMAGE_ALLOWED_SIZES:
        return size_text
    if "auto" in {size_text, aspect_text.lower(), resolution_text} or "自动" in {
        size_text,
        aspect_text,
        resolution_text,
    }:
        return ""
    custom_size = _normalize_custom_image_dimensions(size_text)
    if custom_size:
        return custom_size

    safe_aspect = normalize_custom_image_aspect(aspect)
    safe_resolution = normalize_custom_image_resolution(resolution)
    return CUSTOM_IMAGE_SIZE_MAP.get(safe_aspect, {}).get(safe_resolution, DEFAULT_CUSTOM_IMAGE_SIZE)


def schedule_image_size(config: Any) -> str:
    """Return the stable 3:4 output size used by gallery schedule photos."""
    image_config = config.get("image_gen", {}) if isinstance(config, dict) else {}
    if not isinstance(image_config, dict):
        image_config = {}
    configured = image_config.get("schedule_size") or image_config.get("metadata_size")
    size_text = _non_empty(configured).lower()
    return size_text if size_text in SCHEDULE_IMAGE_ALLOWED_SIZES else DEFAULT_SCHEDULE_IMAGE_SIZE


def _schedule_activity_action_text(prompt: str) -> str:
    matches = list(SCHEDULE_ACTIVITY_ACTION_RE.finditer(prompt or ""))
    if not matches:
        return ""
    match = matches[-1]
    return " ".join(
        part.strip()
        for part in (match.group("activity"), match.group("action"))
        if part and part.strip()
    )


def schedule_image_framing_rule(prompt: Any) -> str:
    activity_action = _schedule_activity_action_text(_non_empty(prompt))
    if activity_action and SCHEDULE_OOTD_INTENT_RE.search(activity_action):
        return SCHEDULE_IMAGE_OOTD_FRAMING_RULE
    if activity_action and SCHEDULE_SCENERY_INTENT_RE.search(activity_action):
        return SCHEDULE_IMAGE_SCENERY_FRAMING_RULE
    return SCHEDULE_IMAGE_FRAMING_RULE


def apply_schedule_image_framing(prompt: Any) -> str:
    raw_text = _non_empty(prompt)
    if not raw_text:
        return raw_text
    framing_rule = schedule_image_framing_rule(raw_text)
    if raw_text.endswith(framing_rule):
        return raw_text
    text = raw_text.replace(LEGACY_SCHEDULE_IMAGE_FRAMING_RULE, "").rstrip(" .")
    marker_index = text.find(SCHEDULE_IMAGE_FRAMING_MARKER)
    if marker_index >= 0:
        text = text[:marker_index].rstrip(" .")
    return f"{text}. {framing_rule}"


def normalize_custom_shot_type(value: Any) -> str:
    text = _non_empty(value).lower()
    if not text:
        return DEFAULT_CUSTOM_SHOT_TYPE
    return CUSTOM_SHOT_TYPE_ALIASES.get(text, CUSTOM_SHOT_TYPE_ALIASES.get(_non_empty(value), DEFAULT_CUSTOM_SHOT_TYPE))


def _custom_size_orientation(size: Any) -> str:
    text = _non_empty(size).lower().replace("×", "x")
    match = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", text)
    if not match:
        return ""
    width = int(match.group(1))
    height = int(match.group(2))
    if width > height:
        return "landscape"
    if height > width:
        return "portrait"
    return "square"


def custom_shot_prompt(value: Any, size: Any = "") -> str:
    shot_type = normalize_custom_shot_type(value)
    shot_prompt = CUSTOM_SHOT_TYPE_PROMPTS.get(shot_type, CUSTOM_SHOT_TYPE_PROMPTS[DEFAULT_CUSTOM_SHOT_TYPE])
    if _custom_size_orientation(size) == "landscape":
        landscape_prompt = CUSTOM_SHOT_LANDSCAPE_PROMPTS.get(shot_type, "")
        if landscape_prompt:
            shot_prompt = f"{shot_prompt}, {landscape_prompt}"
    return f"{shot_prompt}, {CUSTOM_IMAGE_FRAMING_RULE}"


def custom_shot_label(value: Any) -> str:
    shot_type = normalize_custom_shot_type(value)
    return CUSTOM_SHOT_TYPE_LABELS.get(shot_type, CUSTOM_SHOT_TYPE_LABELS[DEFAULT_CUSTOM_SHOT_TYPE])


def auto_push_agent(persona_source: Any, push_channel: Any = "") -> str:
    source = normalize_persona_source(persona_source)
    if source == "openclaw":
        return "openclaw"
    return "hermes"


def normalize_outfit_styles(value: Any, allowed: list[str] | None = None) -> list[str]:
    allowed_styles = allowed or DEFAULT_OUTFIT_STYLES
    allowed_lookup = {style.strip(): style for style in allowed_styles if _non_empty(style)}
    if isinstance(value, str):
        raw_items = re.split(r"[\n,，、|/]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = []

    result: list[str] = []
    for item in raw_items:
        text = _non_empty(item)
        style = allowed_lookup.get(text)
        if style and style not in result:
            result.append(style)
    return result


def base_style_label(base_style: str) -> str:
    return DEFAULT_BASE_STYLE_LABELS.get(_non_empty(base_style), _non_empty(base_style))


def style_reference_filename(base_style: str) -> str:
    return DEFAULT_STYLE_REFERENCE_FILES.get(_non_empty(base_style), "")


def reference_filename_to_style(filename: str) -> str:
    name = os.path.basename(_non_empty(filename))
    for style, ref_filename in DEFAULT_STYLE_REFERENCE_FILES.items():
        if name == ref_filename:
            return style
    return ""


def builtin_reference_map() -> dict[str, dict[str, str]]:
    return {
        filename: {
            "style": style,
            "label": base_style_label(style),
            "prompt": DEFAULT_STYLE_REFERENCE_PROMPTS.get(style, ""),
        }
        for style, filename in DEFAULT_STYLE_REFERENCE_FILES.items()
    }


def outfit_style_to_base_style(style_name: str) -> str:
    text = _non_empty(style_name)
    if text in DEFAULT_BASE_STYLE_LABELS:
        return text
    return DEFAULT_OUTFIT_STYLE_MAP.get(text, {}).get("base_style", "")


def outfit_style_to_prompt_hint(style_name: str) -> str:
    text = _non_empty(style_name)
    mapped = DEFAULT_OUTFIT_STYLE_MAP.get(text, {})
    if mapped.get("prompt_hint"):
        return mapped["prompt_hint"]
    if text and not re.search(r"[\u4e00-\u9fff]", text):
        return text
    return ""


def theme_style_default(theme: str) -> tuple[str, str]:
    return DEFAULT_THEME_STYLE_MAP.get(_non_empty(theme), DEFAULT_THEME_STYLE_MAP["custom"])


def load_enabled_outfit_styles(config: dict, data_dir: str) -> list[str]:
    keys = load_json_file(api_keys_path(data_dir))
    local_styles = normalize_outfit_styles(keys.get("enabled_outfit_styles"))
    if local_styles:
        return local_styles

    schedule_cfg = config.get("schedule", {}) if isinstance(config.get("schedule"), dict) else {}
    configured_styles = normalize_outfit_styles(schedule_cfg.get("enabled_outfit_styles"))
    return configured_styles or list(DEFAULT_OUTFIT_STYLES)


def normalize_schedule_forbidden_keywords(value: Any) -> list[str]:
    """Normalize user-provided schedule ban words while preserving order."""
    raw_items: list[Any]
    if isinstance(value, str):
        raw_items = re.split(r"[\n,，;；、]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = []

    result: list[str] = []
    for item in raw_items:
        text = re.sub(r"\s+", " ", str(item or "")).strip(" ，,。.!！?；;、")
        if not text or text in result:
            continue
        result.append(text[:40])
        if len(result) >= 30:
            break
    return result


def load_schedule_forbidden_keywords(config: dict, data_dir: str) -> list[str]:
    keys = load_json_file(api_keys_path(data_dir))
    local_keywords = normalize_schedule_forbidden_keywords(keys.get("schedule_forbidden_keywords"))
    if local_keywords:
        return local_keywords

    schedule_cfg = config.get("schedule", {}) if isinstance(config.get("schedule"), dict) else {}
    return normalize_schedule_forbidden_keywords(schedule_cfg.get("forbidden_keywords"))


def config_int(config: dict, path: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        value = int(get_nested(config, path, default))
    except (TypeError, ValueError):
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def image_process_timeout(config: dict, with_reference_fallback: bool = True) -> int:
    """Return a safe outer timeout for the image-generation child process."""
    max_retries = config_int(config, "image_gen.max_retries", 3, 1)
    retry_delay = config_int(config, "image_gen.retry_delay_seconds", 3, 0)
    text_timeout = config_int(config, "image_gen.text2img_timeout", 180, 1)
    img_timeout = config_int(config, "image_gen.img2img_timeout", 300, 1)
    enhance_timeout = config_int(config, "llm.enhance_timeout", 25, 1)
    caption_timeout = config_int(config, "llm.caption_timeout", 30, 1)
    retry_delay_window = retry_delay * sum(range(1, max_retries))
    text_window = text_timeout * max_retries + retry_delay_window
    image_window = text_window
    if with_reference_fallback:
        image_window += img_timeout * max_retries + retry_delay_window
    total = image_window + enhance_timeout + caption_timeout + 120
    computed = max(900, ((total + 59) // 60) * 60)

    explicit = config_int(config, "image_gen.process_timeout", 0, 0)
    if not explicit:
        explicit = config_int(config, "image_gen.timeout", 0, 0)
    return max(explicit, computed)


def image_request_timeout(config: dict, mode: str) -> int:
    """Return the effective per-request timeout for text2img/img2img calls."""
    normalized_mode = _non_empty(mode).lower()
    if normalized_mode in {"img2img", "image", "reference"}:
        key = "image_gen.img2img_timeout"
        default = 300
        safe_minimum = 900
    else:
        key = "image_gen.text2img_timeout"
        default = 180
        safe_minimum = 600

    configured = config_int(config, key, default, 1)
    legacy_global = config_int(config, "image_gen.timeout", 0, 0)
    return max(configured, legacy_global, safe_minimum)


def get_image_request_timeout(config_or_mode, mode: str = "") -> int:
    """Compatibility wrapper for zhuzhu modules imported from the web server."""
    if isinstance(config_or_mode, dict):
        return image_request_timeout(config_or_mode, mode)
    return image_request_timeout({}, str(config_or_mode or mode))


def config_float(config: dict, path: str, default: float, min_value: float | None = None, max_value: float | None = None) -> float:
    try:
        value = float(get_nested(config, path, default))
    except (TypeError, ValueError):
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def get_nested(data: dict, path: str, default: Any = None) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def unique_values(*values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _non_empty(value)
        if text and text not in result:
            result.append(text)
    return result


def normalize_llm_models(*values: Any) -> list[str]:
    """Return a stable, de-duplicated ordered LLM model chain."""
    result: list[str] = []

    def add(value: Any):
        if isinstance(value, (list, tuple)):
            for item in value:
                add(item)
            return
        if isinstance(value, dict):
            return
        text = _non_empty(value)
        if text and text not in result:
            result.append(text)

    for value in values:
        add(value)
    return result


def configured_llm_models(llm_config: Any) -> list[str]:
    """Resolve the configured LLM chain while keeping legacy scalar edits effective."""
    if not isinstance(llm_config, dict):
        return []
    configured_value = llm_config.get("models", [])
    configured = (
        normalize_llm_models(configured_value)
        if isinstance(configured_value, (list, tuple))
        else []
    )
    legacy = normalize_llm_models(
        llm_config.get("model", ""),
        llm_config.get("fallback_model", ""),
    )
    if not configured:
        return legacy
    if not legacy:
        return configured
    if configured[:len(legacy)] == legacy:
        return configured

    tail_start = 2 if "fallback_model" in llm_config else 1
    return normalize_llm_models(legacy, configured[tail_start:])


def deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_project_root(config_path: str = "", config: dict | None = None) -> Path:
    env_root = _non_empty(os.getenv("HERMES_PORTRAIT_GALLERY_HOME") or os.getenv("PORTRAIT_GALLERY_HOME"))
    if env_root:
        return Path(env_root).expanduser().resolve()

    configured = _non_empty(get_nested(config or {}, "paths.project_root", ""))
    if configured:
        root = Path(configured).expanduser()
        return (DEFAULT_PROJECT_ROOT / root).resolve() if not root.is_absolute() else root.resolve()

    if config_path:
        path = Path(config_path).expanduser().resolve()
        if path.name:
            return path.parent.parent.resolve()

    return DEFAULT_PROJECT_ROOT.resolve()


def resolve_config_path(config_path: str = "") -> str:
    explicit = _non_empty(config_path or os.getenv("CONFIG_PATH"))
    if explicit:
        return str(Path(explicit).expanduser().resolve())

    env_root = _non_empty(os.getenv("HERMES_PORTRAIT_GALLERY_HOME") or os.getenv("PORTRAIT_GALLERY_HOME"))
    candidates: list[Path] = []
    if env_root:
        candidates.append(Path(env_root).expanduser() / "config" / "config.yaml")

    candidates.extend([
        DEFAULT_PROJECT_ROOT / "config" / "config.yaml",
        Path.home() / "hermes-portrait-gallery-external" / "config" / "config.yaml",
        Path.home() / "Docker" / "hermes-portrait-gallery" / "config" / "config.yaml",
        Path.home() / "docker" / "hermes-portrait-gallery" / "config" / "config.yaml",
    ])
    for volume in Path("/Volumes").glob("*"):
        candidates.append(volume / "hermes-portrait-gallery" / "config" / "config.yaml")

    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())

    return str((DEFAULT_PROJECT_ROOT / "config" / "config.yaml").resolve())


def load_config(config_path: str = "") -> dict:
    path = resolve_config_path(config_path)
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    local_path = Path(path).with_name("local.yaml")
    if local_path.exists():
        with open(local_path, encoding="utf-8") as f:
            local_config = yaml.safe_load(f) or {}
        if isinstance(local_config, dict):
            config = deep_merge(config, local_config)

    # Mutable Web settings live with deployment data so the base config can
    # remain read-only in Docker images.
    data_dir = resolve_data_dir(config, path)
    runtime_config = normalize_runtime_config(load_json_file(runtime_config_path(data_dir)))
    if runtime_config:
        config = deep_merge(config, runtime_config)
    return config


def resolve_path(value: Any, root: Path, default: str | Path = "") -> str:
    raw = _non_empty(value)
    if not raw and default:
        raw = str(default)
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    return str(path.resolve())


def resolve_data_dir(config: dict, config_path: str = "") -> str:
    root = resolve_project_root(config_path, config)
    value = get_nested(config, "paths.data_dir", "") or config.get("data_dir", "data")
    return resolve_path(value, root, "data")


def default_image_dir(data_dir: str) -> str:
    return str((Path(data_dir).expanduser() / "images").resolve())


def normalize_image_dir(value: Any, data_dir: str) -> str:
    raw = _non_empty(value)
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(data_dir).expanduser() / path
    return str(path.resolve())


def resolve_image_dir(config: dict, data_dir: str) -> str:
    keys = load_json_file(api_keys_path(data_dir))
    local_dir = normalize_image_dir(keys.get("image_dir"), data_dir)
    if local_dir:
        return local_dir

    value = get_nested(config, "paths.image_dir", "") or get_nested(config, "gallery.image_dir", "")
    configured = normalize_image_dir(value, data_dir)
    return configured or default_image_dir(data_dir)


def resolve_script_dir(config: dict, config_path: str = "") -> str:
    root = resolve_project_root(config_path, config)
    value = get_nested(config, "image_gen.script_dir", "")
    return resolve_path(value, root, "app/zhuzhu")


def resolve_builtin_reference_dir(config: dict, config_path: str = "") -> str:
    root = resolve_project_root(config_path, config)
    value = get_nested(config, "paths.builtin_reference_dir", "")
    return resolve_path(value, root, "app/references")


def resolve_reference_dir(config: dict, data_dir: str, config_path: str = "") -> str:
    root = resolve_project_root(config_path, config)
    value = get_nested(config, "paths.reference_dir", "")
    return resolve_path(value, root, Path(data_dir) / "references")


def load_json_file(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


PERSONA_FIELD_ALIASES = {
    "name": (
        "character_name",
        "characterName",
        "self_name",
        "selfName",
        "assistant_name",
        "bot_name",
        "display_name",
        "displayName",
        "nickname",
        "name",
    ),
    "user_name": (
        "user_name",
        "userName",
        "audience_name",
        "audienceName",
        "owner_name",
        "ownerName",
        "master_name",
        "masterName",
        "user_call",
        "User_Call",
        "address_user_as",
        "target_name",
    ),
    "persona": (
        "persona",
        "character_persona",
        "role_persona",
        "identity",
        "Identity",
        "description",
        "bio",
        "profile",
    ),
    "caption_voice": (
        "caption_voice",
        "captionVoice",
        "caption_style",
        "captionStyle",
        "voice",
        "tone",
        "vibe",
        "personality",
        "Tone_Guidance",
        "小心思口吻",
    ),
    "appearance": (
        "appearance",
        "character_appearance",
        "characterAppearance",
        "visual",
        "look",
        "image_prompt",
        "visual_prompt",
    ),
}


def _clean_persona_text(value: Any, limit: int = 1800) -> str:
    text = _non_empty(value)
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip(" -_`*：:")
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return text


def _split_persona_name(value: str) -> str:
    text = _clean_persona_text(value, 80)
    if not text:
        return ""
    text = re.split(r"[/／,，|、（(]", text, 1)[0].strip()
    return text[:40].strip()


def _short_persona_source(source: str) -> str:
    home = str(Path.home())
    return source.replace(home, "~")


def _persona_candidate_dicts(data: dict) -> list[dict]:
    candidates: list[dict] = []
    if isinstance(data, dict):
        candidates.append(data)
        for key in ("character", "persona", "profile", "identity", "agent", "assistant", "display", "gallery"):
            child = data.get(key)
            if isinstance(child, dict):
                candidates.append(child)
        for key in ("personas", "personalities", "profiles"):
            group = data.get(key)
            if isinstance(group, dict):
                candidates.extend(v for v in group.values() if isinstance(v, dict))
    return candidates


def _extract_persona_fields(data: dict, source: str) -> dict:
    fields: dict[str, str] = {}
    for candidate in _persona_candidate_dicts(data):
        for target, aliases in PERSONA_FIELD_ALIASES.items():
            if fields.get(target):
                continue
            for alias in aliases:
                if alias not in candidate:
                    continue
                value = candidate.get(alias)
                if isinstance(value, (dict, list)):
                    continue
                text = _clean_persona_text(value, 2400 if target == "persona" else 900)
                if text:
                    fields[target] = text
                    break
    if fields:
        fields["_source"] = _short_persona_source(source)
    return fields


def _read_persona_json(path: Path) -> dict:
    data = load_json_file(str(path))
    return _extract_persona_fields(data, str(path)) if data else {}


def _read_persona_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return _extract_persona_fields(data, str(path)) if isinstance(data, dict) else {}


def _looks_like_prompt_dump(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "start of input",
        "start of output",
        "end of input",
        "ignore previous",
        "godmode",
        "tool call",
    )
    return any(marker in lowered for marker in markers)


def _read_keyed_persona_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    if not text.strip():
        return {}

    fields: dict[str, str] = {}
    key_map = {
        "name": (r"\bSelf_Call\b", r"\bName\b", r"自称", r"名字", r"角色名称"),
        "user_name": (r"\bUser_Call\b", r"用户称呼", r"称呼用户", r"Owner_Call"),
        "persona": (r"\bIdentity\b", r"\bPersona\b", r"身份卡", r"身份", r"核心人格"),
        "caption_voice": (r"\bTone_Guidance\b", r"\bVibe\b", r"语气", r"口吻", r"性格底色"),
        "appearance": (r"\bAppearance\b", r"\bVisual\b", r"\bLook\b", r"外貌", r"人物外貌", r"视觉"),
    }
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("- ")
        if not line or ":" not in line and "：" not in line:
            continue
        cleaned = re.sub(r"[*`#>]", "", line).strip()
        parts = re.split(r"[:：]", cleaned, 1)
        if len(parts) != 2:
            continue
        label_text = parts[0].strip()
        value = parts[1].strip()
        for target, labels in key_map.items():
            if fields.get(target):
                continue
            if any(re.search(label, label_text, re.IGNORECASE) for label in labels):
                value = _clean_persona_text(value, 1200 if target in ("persona", "caption_voice") else 120)
                if value and not _looks_like_prompt_dump(value):
                    fields[target] = _split_persona_name(value) if target in ("name", "user_name") else value
                break

    if fields:
        fields["_source"] = _short_persona_source(str(path))
    return fields


def _load_project_persona_files(data_dir: str) -> list[dict]:
    base = Path(data_dir)
    files = [
        base / "persona.json",
        base / "hermes_persona.json",
        base / "openclaw_persona.json",
        base / "openclaw_config.json",
    ]
    return [_read_persona_json(path) for path in files]


def _load_hermes_persona() -> list[dict]:
    base = Path.home() / ".hermes"
    files = [
        base / "persona.json",
        base / "hermes_persona.json",
        base / "profile.json",
        base / "SOUL.md",
        base / "config.yaml",
    ]
    result: list[dict] = []
    for path in files:
        if path.suffix.lower() in (".yaml", ".yml"):
            result.append(_read_persona_yaml(path))
        elif path.suffix.lower() == ".json":
            result.append(_read_persona_json(path))
        else:
            result.append(_read_keyed_persona_file(path))
    return result


def _load_openclaw_persona() -> list[dict]:
    base = Path.home() / ".openclaw"
    files = [
        base / "persona.json",
        base / "openclaw_persona.json",
        base / "profile.json",
        base / "openclaw.json",
    ]
    result = [_read_persona_json(path) for path in files]
    for pattern in ("agents/main/SOUL.md", "agents/main/IDENTITY.md", "agents/*/SOUL.md", "agents/*/IDENTITY.md"):
        for path in sorted(base.glob(pattern)):
            result.append(_read_keyed_persona_file(path))
    return result


def load_runtime_persona(config: dict, data_dir: str) -> dict:
    """Resolve local character/persona settings without treating agent prompts as commands."""
    keys_config = load_json_file(api_keys_path(data_dir))
    persona_source = normalize_persona_source(keys_config.get("persona_source"))
    resolved = {
        "name": "",
        "user_name": "",
        "persona": "",
        "caption_voice": "",
        "appearance": "",
        "source": "",
        "persona_source": persona_source,
        "sources": {},
    }
    sources: dict[str, str] = {}

    def apply(fields: dict, default_source: str = "", keys: tuple[str, ...] = ("name", "user_name", "persona", "caption_voice", "appearance")):
        source = fields.get("_source") or default_source
        for key in keys:
            if resolved.get(key) or not fields.get(key):
                continue
            value = _clean_persona_text(fields[key], 2400 if key == "persona" else 1200)
            if key in ("name", "user_name"):
                value = _split_persona_name(value)
            if not value or _looks_like_prompt_dump(value):
                continue
            resolved[key] = value
            sources[key] = source

    local_fields = _extract_persona_fields(keys_config, "data/api_keys_config.json")
    local_appearance = _non_empty(keys_config.get("appearance") or keys_config.get("character_appearance"))
    if local_appearance:
        local_fields["appearance"] = local_appearance
        local_fields["_source"] = "data/api_keys_config.json"
    apply(local_fields, keys=("appearance",))

    if persona_source == "hermes":
        for fields in _load_hermes_persona():
            apply(fields, keys=("name", "user_name", "persona", "caption_voice", "appearance"))
    elif persona_source == "openclaw":
        for fields in _load_openclaw_persona():
            apply(fields, keys=("name", "user_name", "persona", "caption_voice", "appearance"))
    else:
        apply(local_fields, keys=("persona",))
        for fields in _load_project_persona_files(data_dir):
            apply(fields, keys=("name", "user_name", "persona", "caption_voice", "appearance"))

    character = config.get("character", {}) if isinstance(config.get("character"), dict) else {}
    apply(_extract_persona_fields({"character": character}, "config.character"))

    if not resolved["name"]:
        resolved["name"] = "角色"
        sources["name"] = "default"
    if not resolved["user_name"]:
        resolved["user_name"] = "你"
        sources["user_name"] = "default"
    if not resolved["caption_voice"]:
        resolved["caption_voice"] = "自然、亲切、贴近日常，带一点轻松的分享感。"
        sources["caption_voice"] = "default"

    resolved["sources"] = sources
    persona_sources = [sources.get(k, "") for k in ("persona", "caption_voice", "name", "user_name", "appearance") if sources.get(k)]
    resolved["source"] = persona_sources[0] if persona_sources else "default"
    return resolved


def api_keys_path(data_dir: str) -> str:
    return os.path.join(data_dir, "api_keys_config.json")


def plugin_config_path(data_dir: str) -> str:
    return os.path.join(data_dir, "plugin_config.json")


def runtime_config_path(data_dir: str) -> str:
    return os.path.join(data_dir, "runtime_config.json")


def normalize_chat_url(base_url: str) -> str:
    base = _non_empty(base_url).rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    for suffix in ("/images/generations", "/images/edits"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}/chat/completions"


def _split_no_proxy(value: Any) -> list[str]:
    text = _non_empty(value)
    if not text:
        return []
    return [item for item in re.split(r"[\s,]+", text) if item]


def _host_from_url(value: Any) -> str:
    text = _non_empty(value)
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"http://{text}")
    return parsed.hostname or ""


def merge_no_proxy(*values: Any) -> str:
    merged: list[str] = []
    for value in values:
        for item in _split_no_proxy(value):
            if item not in merged:
                merged.append(item)
    return ",".join(merged)


def llm_temperature_param_error(value: Any) -> bool:
    """Return True when an OpenAI-compatible endpoint rejects temperature."""
    if isinstance(value, dict):
        chunks: list[str] = []
        error = value.get("error")
        if isinstance(error, dict):
            chunks.extend(str(error.get(key) or "") for key in ("message", "code", "type"))
        for key in ("message", "msg", "detail", "error"):
            item = value.get(key)
            if item:
                chunks.append(str(item))
        text = " ".join(chunks) or json.dumps(value, ensure_ascii=False)
    else:
        text = str(value or "")
    lower = text.lower()
    if "temperature" not in lower:
        return False
    return any(
        marker in lower
        for marker in (
            "deprecated",
            "not supported",
            "unsupported",
            "not allowed",
            "unrecognized",
            "unknown parameter",
            "invalid parameter",
        )
    )


def llm_text_from_value(value: Any) -> str:
    """Extract readable text from common OpenAI-compatible content shapes."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value).strip()
    if isinstance(value, list):
        parts = [llm_text_from_value(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "content", "value", "output_text", "reasoning_content"):
            text = llm_text_from_value(value.get(key))
            if text:
                return text
    return ""


def llm_choice_text(choice: Any) -> str:
    """Extract text from one chat completion choice."""
    if not isinstance(choice, dict):
        return llm_text_from_value(choice)
    message = choice.get("message")
    if isinstance(message, dict):
        text = (
            llm_text_from_value(message.get("content"))
            or llm_text_from_value(message.get("reasoning_content"))
            or llm_text_from_value(message.get("text"))
        )
        if text:
            return text
    delta = choice.get("delta")
    if isinstance(delta, dict):
        text = (
            llm_text_from_value(delta.get("content"))
            or llm_text_from_value(delta.get("reasoning_content"))
            or llm_text_from_value(delta.get("text"))
        )
        if text:
            return text
    return (
        llm_text_from_value(choice.get("text"))
        or llm_text_from_value(choice.get("content"))
        or llm_text_from_value(choice.get("reasoning_content"))
    )


def llm_response_excerpt(value: Any, limit: int = 500) -> str:
    """Return a compact raw-response excerpt for diagnostics."""
    if isinstance(value, str):
        text = value.strip()
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except Exception:
            text = str(value or "")
    return text[:max(80, limit)].strip()


def llm_request_config(config: dict, data_dir: str) -> dict:
    keys = load_json_file(api_keys_path(data_dir))
    base_url = (
        _non_empty(keys.get("cpa_url"))
        or _non_empty(os.getenv("CPA_BASE_URL"))
        or _non_empty(get_nested(config, "llm.base_url", ""))
    )
    api_key = (
        _non_empty(keys.get("cpa_key"))
        or _non_empty(os.getenv("CPA_API_KEY"))
        or _non_empty(get_nested(config, "llm.api_key", ""))
    )
    models = configured_llm_models(config.get("llm", {}) if isinstance(config, dict) else {})
    return {"base_url": base_url.rstrip("/"), "chat_url": normalize_chat_url(base_url), "api_key": api_key, "models": models}


def apply_network_env(config: dict, env: dict[str, str] | None = None, data_dir: str = "") -> dict[str, str]:
    target = env if env is not None else os.environ
    network = config.get("network", {}) if isinstance(config.get("network"), dict) else {}
    mapping = {
        "http_proxy": ("HTTP_PROXY", "http_proxy"),
        "https_proxy": ("HTTPS_PROXY", "https_proxy"),
    }
    for key, env_names in mapping.items():
        value = _non_empty(network.get(key))
        if not value:
            continue
        for env_name in env_names:
            target[env_name] = value

    keys = load_json_file(api_keys_path(data_dir)) if data_dir else {}
    dynamic_no_proxy_hosts = [
        _host_from_url(keys.get("gpt_base_url")),
        _host_from_url(keys.get("cpa_url")),
        _host_from_url(get_nested(config, "image_gen.gpt_base_url", "")),
        _host_from_url(get_nested(config, "llm.base_url", "")),
    ]
    no_proxy = merge_no_proxy(
        target.get("NO_PROXY", ""),
        target.get("no_proxy", ""),
        network.get("no_proxy", ""),
        ",".join(DEFAULT_NO_PROXY_HOSTS),
        ",".join(host for host in dynamic_no_proxy_hosts if host),
    )
    if no_proxy:
        target["NO_PROXY"] = no_proxy
        target["no_proxy"] = no_proxy
    return target


def build_child_env(config: dict, config_path: str, data_dir: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    root = resolve_project_root(config_path, config)
    image_dir = resolve_image_dir(config, data_dir)
    env = dict(os.environ)
    env["HERMES_PORTRAIT_GALLERY_HOME"] = str(root)
    env["CONFIG_PATH"] = str(Path(config_path).expanduser().resolve())
    env["GALLERY_DATA_DIR"] = data_dir
    env["ZHUZHU_DATA_DIR"] = data_dir
    env["ZHUZHU_PROJECT_DIR"] = str(root)
    env["ZHUZHU_MEDIA_DIR"] = image_dir
    app_path = str(root / "app")
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = app_path if not current_pythonpath else app_path + os.pathsep + current_pythonpath
    apply_network_env(config, env, data_dir=data_dir)
    if extra:
        env.update({k: v for k, v in extra.items() if v})
    return env


def configured_python(config: dict) -> str:
    return _non_empty(get_nested(config, "runtime.python", "")) or _non_empty(get_nested(config, "image_gen.python", ""))
