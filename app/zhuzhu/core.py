#!/usr/bin/env python3
"""Shared constants and helpers for zhuzhu image generation."""
import base64
import hashlib
import io
import json
import os
import random
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests
from PIL import Image, ImageOps

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from store import ScheduleStore
from settings import (
    DEFAULT_QUALITY_PREFIX,
    GENERIC_APPEARANCE,
    base_style_label,
    config_float,
    config_int,
    get_nested,
    image_request_timeout,
    llm_choice_text,
    llm_request_config,
    llm_response_excerpt,
    llm_temperature_param_error,
    load_config,
    load_json_file,
    load_runtime_persona,
    normalize_chat_url,
    outfit_style_to_base_style,
    resolve_builtin_reference_dir,
    resolve_config_path,
    resolve_data_dir,
    resolve_project_root,
    resolve_reference_dir,
    theme_style_default,
)

requests.packages.urllib3.disable_warnings()

REQUEST_SESSION = requests.Session()


def _retry_without_temperature_if_needed(resp, payload: dict, post_func):
    if getattr(resp, "status_code", None) != 400 or "temperature" not in payload:
        return resp
    try:
        body = resp.json()
    except Exception:
        body = getattr(resp, "text", "")
    if not llm_temperature_param_error(body):
        return resp
    retry_payload = dict(payload)
    retry_payload.pop("temperature", None)
    return post_func(retry_payload)


_GALLERY_CONFIG_PATH = resolve_config_path()
try:
    _GALLERY_CONFIG = load_config(_GALLERY_CONFIG_PATH)
except Exception:
    _GALLERY_CONFIG = {}
_PROJECT_ROOT = resolve_project_root(_GALLERY_CONFIG_PATH, _GALLERY_CONFIG)
_DATA_DIR = Path(
    os.getenv("ZHUZHU_DATA_DIR")
    or os.getenv("GALLERY_DATA_DIR")
    or resolve_data_dir(_GALLERY_CONFIG, _GALLERY_CONFIG_PATH)
).expanduser().resolve()

WORKSPACE_MEDIA = str(Path(os.getenv("ZHUZHU_MEDIA_DIR") or (_DATA_DIR / "images")).expanduser().resolve())
SECRETARY_GALLERY_DIR = WORKSPACE_MEDIA
SECRETARY_SCHEDULE_PATH = str(_DATA_DIR / "schedule_data.json")
META_PATH = str(_DATA_DIR / "image_metadata.json")
CONFIG_PATH = str(_DATA_DIR / "plugin_config.json")
OPENCLAW_CONFIG_PATH = str(_DATA_DIR / "openclaw_config.json")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_API_KEYS_CONFIG_PATH = str(_DATA_DIR / "api_keys_config.json")

_RETRYABLE_STATUS = get_nested(_GALLERY_CONFIG, "image_gen.retryable_status", [429, 500, 502, 503, 504])
RETRYABLE_STATUS = {int(status) for status in _RETRYABLE_STATUS} if isinstance(_RETRYABLE_STATUS, (list, tuple, set)) else {429, 500, 502, 503, 504}
MAX_RETRIES = config_int(_GALLERY_CONFIG, "image_gen.max_retries", 3, 1)
RETRY_DELAY_SECONDS = config_int(_GALLERY_CONFIG, "image_gen.retry_delay_seconds", 3, 0)


def _reference_dirs() -> list[Path]:
    refs = [
        _DATA_DIR / "references",
        _PROJECT_ROOT / "app" / "references",
    ]
    configured = get_nested(_GALLERY_CONFIG, "paths.reference_dir", "")
    if configured:
        path = Path(configured).expanduser()
        refs.insert(0, path if path.is_absolute() else _PROJECT_ROOT / path)
    return refs


def get_reference_path(filename: str = "reference_face.jpg") -> str:
    for directory in _reference_dirs():
        candidate = directory / filename
        if candidate.is_file():
            return str(candidate.resolve())
    return ""


def _reference_url_bases() -> list[tuple[Path, str]]:
    bases: list[tuple[Path, str]] = []
    for directory, prefix in (
        (resolve_reference_dir(_GALLERY_CONFIG, str(_DATA_DIR), _GALLERY_CONFIG_PATH), "/local-refs"),
        (resolve_builtin_reference_dir(_GALLERY_CONFIG, _GALLERY_CONFIG_PATH), "/refs"),
        (str(_DATA_DIR / "references"), "/local-refs"),
        (str(_PROJECT_ROOT / "app" / "references"), "/refs"),
    ):
        try:
            resolved = Path(directory).expanduser().resolve()
        except OSError:
            continue
        if not any(existing == resolved and existing_prefix == prefix for existing, existing_prefix in bases):
            bases.append((resolved, prefix))
    return bases


def _gallery_reference_url(ref_image: str) -> str:
    if not ref_image:
        return ""
    path = Path(ref_image).expanduser()
    if not path.is_absolute():
        return str(ref_image)
    try:
        resolved = path.resolve()
    except OSError:
        return path.name
    for base, prefix in _reference_url_bases():
        try:
            rel = resolved.relative_to(base).as_posix()
            return f"{prefix}/{quote(rel)}"
        except ValueError:
            continue
    return resolved.name


REFERENCE_IMAGE_PATH = get_reference_path("reference_face.jpg")


def _read_cpa_key() -> str:
    """Read CPA API key from environment or config file."""
    # 1. Try environment variable
    env_key = os.getenv("CPA_API_KEY", "")
    if env_key:
        return env_key
    # 2. Try config file
    if os.path.exists(_API_KEYS_CONFIG_PATH):
        try:
            with open(_API_KEYS_CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
                key = config.get("cpa_key", "")
                if key:
                    return key
        except Exception as e:
            print(f"[warn] Failed to read {_API_KEYS_CONFIG_PATH}: {e}", file=sys.stderr)
    return ""


def _read_cpa_url() -> str:
    """Read CPA base URL from environment or config file."""
    env_url = os.getenv("CPA_BASE_URL", "")
    if env_url:
        return env_url
    if os.path.exists(_API_KEYS_CONFIG_PATH):
        try:
            with open(_API_KEYS_CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
                url = config.get("cpa_url", "")
                if url:
                    return url
        except Exception:
            pass
    return get_nested(_GALLERY_CONFIG, "llm.base_url", "")


def get_llm_models() -> list[str]:
    return llm_request_config(_GALLERY_CONFIG, str(_DATA_DIR))["models"]


def get_cpa_base_url() -> str:
    """Read current CPA base URL from environment or api_keys_config.json."""
    return _read_cpa_url().rstrip("/")


def get_cpa_chat_url() -> str:
    return normalize_chat_url(get_cpa_base_url())


def get_image_model(key: str, default: str = "") -> str:
    if os.path.exists(_API_KEYS_CONFIG_PATH):
        try:
            with open(_API_KEYS_CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
            local_value = str(config.get(key, "") or "").strip()
            if local_value:
                return local_value
        except Exception:
            pass
    return str(get_nested(_GALLERY_CONFIG, f"image_gen.{key}", default) or default).strip()


def get_image_int(key: str, default: int, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
    return config_int(_GALLERY_CONFIG, f"image_gen.{key}", default, min_value, max_value)


def get_image_request_timeout(mode: str) -> int:
    return image_request_timeout(_GALLERY_CONFIG, mode)


CPA_BASE_URL = get_cpa_base_url()


APPEARANCE = str(get_nested(_GALLERY_CONFIG, "character.appearance", "") or GENERIC_APPEARANCE).strip()
SEXY_APPEARANCE = str(get_nested(_GALLERY_CONFIG, "character.sexy_appearance", "") or APPEARANCE).strip()

QUALITY_PREFIX = str(get_nested(_GALLERY_CONFIG, "image_gen.quality_prefix", "") or DEFAULT_QUALITY_PREFIX).strip()
SEXY_QUALITY_PREFIX = str(get_nested(_GALLERY_CONFIG, "image_gen.sexy_quality_prefix", "") or QUALITY_PREFIX).strip()

THEMES = {
    "morning": {
        "clothing": ["oversized hoodie", "lace cami top with loose knit cardigan", "soft cotton pajama set", "thin strap satin nightgown", "cozy cropped sweatshirt with shorts"],
        "hair": ["loosely tousled bed hair with natural waves", "casual messy bun with wispy strands", "soft low ponytail with loose face-framing pieces", "half-up clip with flyaways", "single loose side braid resting on shoulder"],
        "pose": [
            "sitting cross-legged on bed, holding a mug with both hands, looking gently at camera",
            "stretching arms upward with a sleepy smile, eyes half-closed",
            "leaning against window frame, gazing outside with morning light on face",
            "lying on stomach on the bed, chin resting on hands, kicking feet up playfully behind her",
            "standing in front of a mirror doing her skincare routine, glancing at camera through mirror",
            "sitting on the floor beside the bed, knees hugged to chest, soft morning light falling on her",
            "reaching to pick up a phone from bedside table, caught mid-movement looking at camera",
            "wrapped in a duvet, peeking out with only face visible, sleepy smile",
            "sitting at a small desk, writing in a journal, glancing up at camera",
            "standing by the window holding a small potted plant, soft morning light from the side",
            "pouring herself a glass of water in the kitchen, caught in a camelid moment",
            "sitting on the edge of the bed, tying hair up while looking at camera",
        ],
        "env": ["messy cozy bedroom with morning sunlight through curtains", "sunlit bathroom with steam", "cozy bedroom corner with plush toys and polaroid photos", "small kitchen nook with warm sunlight", "window seat with morning light filtering in"],
        "light": ["soft warm morning sunlight, glowing dust motes, cozy atmosphere"],
    },
    "noon": {
        "clothing": ["white fitted crop top with wide-leg linen trousers", "oversized vintage tee tucked into mini skirt", "tight black crop top with low-rise cargo pants", "y2k style graphic baby tee with pleated denim mini skirt", "halter neck knit top with flared jeans", "flowy solid-color sundress with thin straps", "pastel button-down shirt tied at the waist with shorts"],
        "hair": ["half-up half-down style with a small bow clip", "high ponytail with wispy bangs", "twin braids with cute butterfly clips", "messy high bun with face-framing pieces", "neat braided pigtails", "loose low side bun with a scrunchie", "straight hair with a center part and small claw clip"],
        "pose": [
            "holding a boba drink with two hands, smiling brightly at camera",
            "leaning against a wall with one hand resting on hip, relaxed smile",
            "walking mid-step, glancing back over shoulder with a playful grin",
            "squatting playfully while adjusting sunglasses on head",
            "taking a casual selfie with smartphone, looking directly at the camera",
            "sitting on steps outdoors, elbows on knees, chin on hands, looking up at camera",
            "browsing a rack of clothes outside a vintage shop, looking over shoulder at camera",
            "leaning on a bicycle handle, one foot on the ground, casual smile",
            "sitting cross-legged on a park bench reading a book, looking up at camera",
            "window shopping, nose pressed against glass, caught glancing at camera",
            "sitting outside a cafe, one hand wrapped around a coffee cup, looking dreamily away",
            "sitting at a casual restaurant table, chopsticks in hand, smiling warmly at camera with a bowl of noodles in front of her",
            "unwrapping a takeout bento box at a sunny outdoor table, peeking at camera with a playful grin",
            "holding up a spoonful of soup to the camera, inviting look with a gentle smile",
            "eating a rice bowl at a small lunch counter, elbows on table, looking up from the bowl at camera",
            "holding a convenience store onigiri with both hands, taking a small bite, looking at camera with big eyes",
            "standing at a pedestrian crossing, wind blowing hair slightly, glancing at camera with a soft smile",
            "leaning on a railing with both arms, looking sideways at camera with a relaxed expression",
            "sitting on a low wall outdoors, feet dangling, hands in lap, smiling naturally at camera",
        ],
        "env": ["busy city street crossing", "casual boba milk tea shop counter", "outside a convenience store", "vibrant city park bench", "messy trendy industrial style cafe", "ordinary sunlit shopping district alley", "cozy corner of an indie bookstore", "shaded tree-lined pedestrian street", "bright casual ramen restaurant interior", "sunlit outdoor terrace of a lunch cafe", "cozy noodle shop counter with steam rising", "minimalist Japanese-style bento restaurant"],
        "light": ["bright unedited daylight, smartphone camera flash off, harsh natural sunlight, casual lighting"],
    },
    "evening": {
        "clothing": ["satin slip dress with sheer lace robe", "backless velvet mini dress", "sparkly tube top with high-waist leather pants", "sheer black lace top with a mini skirt", "tight black halter neck dress", "elegant off-shoulder long dress", "deep-v wrap mini dress with a delicate satin belt", "elegant burgundy wrap dress with flutter sleeves", "warm caramel knit bodycon dress with a subtle cowl neck", "soft lavender ruched satin mini dress", "coral halter neck pleated dress with open back", "traditional Chinese Ma Mian Qun pleated skirt in ink-blue with gold embroidery, paired with a fitted white hanfu top", "pastel pink Ma Mian Qun with delicate cloud patterns, paired with a cropped ivory top", "classic JK uniform with navy pleated mini skirt and white sailor blouse with red ribbon", "sweet JK outfit with a plaid burgundy pleated skirt, white blouse with puff sleeves and a cute bow"],
        "hair": ["elegant high bun with a delicate hair pin", "sleek straight ponytail", "neat french braid", "half-up style with a ribbon bow", "loose soft waves with a side part", "chic low chignon with a jeweled clip"],
        "pose": [
            "standing at a railing overlooking city lights, looking alluringly at camera",
            "seated at a cafe table, chin resting on folded hands, gazing dreamily at camera",
            "leaning over a bar counter, holding a cocktail glass, looking alluringly at camera",
            "sitting sideways on a bar stool, crossing legs elegantly",
            "walking along a riverbank at sunset, one hand holding shoes, glancing back",
            "leaning against a streetlamp post with one hand, looking down the street",
            "sitting on a rooftop edge, legs dangling, city view behind her, looking at camera",
            "slow-dancing alone in a square, arms slightly raised, eyes closed with a gentle smile",
            "standing under a string of warm lights, head tilted slightly, soft expression",
            "looking down from a balcony railing, golden hour light catching her face",
            "sitting on outdoor steps of a restaurant, heels off, relaxed and smiling up at camera",
        ],
        "env": ["busy Guangzhou street at golden hour, shallow depth of field, candid street photography", "Pearl River waterfront promenade at dusk, soft bokeh city lights in background", "quiet tree-lined avenue at sunset, dappled light through leaves", "outdoor cafe terrace at golden hour, warm ambient light, slightly blurred background", "concrete overpass steps with city skyline at sunset, urban casual", "local night market street food stalls, warm incandescent lights, lively atmosphere", "rooftop with city skyline at dusk, natural ambient light", "old town alley with weathered walls and evening sunlight casting long shadows"],
        "light": ["sunset golden hour, cinematic rim lighting, volumetric rays, warm ambient light"],
    },
    "bedtime": {
        "clothing": ["silk nightgown with delicate lace trim", "sheer lace robe over camisole", "soft cotton sleep shirt", "oversized white t-shirt", "cute matched pajama set", "thin-strap satin slip with lace edging", "fluffy robe half-open over a camisole"],
        "hair": ["loose soft waves slightly disheveled, freshly dried", "natural wavy hair pinned loosely on top", "two loose low pigtails tied with small scrunchies", "air-dried hair falling naturally over one shoulder", "messy half-up bun with flyaways"],
        "pose": [
            "lying on side on bed, head propped on one hand, smiling softly at camera",
            "sitting on bed hugging a large plush pillow, looking sleepily at camera",
            "taking a sleepy mirror selfie in bathroom with a toothbrush",
            "sitting on the edge of the bed looking up playfully",
            "lying on back, head tilted toward camera with a lazy smile, hand resting on stomach",
            "curled up under a blanket reading, peeking over the book at camera",
            "sitting cross-legged on the floor next to the bed, applying lotion to arms",
            "standing by the bathroom sink doing her nighttime skincare, looking at camera through mirror",
            "hugging knees on the window seat, looking at raindrops on glass",
            "reaching up to turn off the bedside lamp, caught mid-motion looking at camera",
            "lying on stomach reading a phone, feet kicked up behind, looking up at camera",
        ],
        "env": ["dim cozy bedroom with warm lamp", "bathroom vanity with warm lighting", "messy bedroom with soft blankets", "cozy bed surrounded by plushies", "nighttime window seat with rain outside", "small vanity table with warm mirror lights"],
        "light": ["warm lamp light, intimate atmosphere, soft shadows, warm smartphone flash bounce"],
    },
    "sexy": {
        "clothing": [
            "a tiny, sheer white lace camisole that is completely unbuttoned and open, revealing her youthful bare chest",
            "a very thin, soaked and transparent white cotton T-shirt that clings tightly to her skin and breasts",
            "an oversized white silk boyfriend shirt, worn completely unbuttoned and falling off one shoulder",
            "only a sheer lace robe, open at the front, exposing her soft skin and youthful silhouette",
            "a micro silk slip dress with dangerously thin straps and a very deep plunging neckline",
            "a semi-sheer white cotton tank top pulled up to just above her breasts, fully exposing them",
            "a delicate Japanese sukumizu, dripping wet and tightly hugging her body",
            "an incredibly thin, pale blue negligee with intricate lace",
            "a dangerously tight micro mini bodycon skirt riding up, showing off her thighs",
            "an extremely minimal string bikini made of practically nothing, just tiny strips of cloth",
            "a naughty and tight nurse uniform unbuttoned deeply, with a cute nurse cap",
        ],
        "pose": [
            "sitting on the floor by the bed, looking up at the camera with a shy and curious expression",
            "lying on her back on the soft bed, looking at the camera with a playful and innocent smile",
            "kneeling on the bed while looking down shyly, lifting her shirt slightly",
            "standing in front of a mirror, looking over her shoulder with a bashful gaze",
            "sitting on a fluffy white rug, leaning forward with a mix of innocence and allure",
            "crouching down shyly, her high pigtails falling over her shoulders",
            "sprawled seductively on the couch, one leg slightly raised",
            "pressing herself against a glass window, looking out at the city night",
        ],
        "hair": [
            "tied in two high pigtails with cute white ribbons",
            "styled in a messy, cute low bun with loose strands",
            "flowing down in long, soft wet waves",
            "in two cute space buns on top of her head",
            "in a relaxed high ponytail with wispy bangs",
        ],
        "environment": [
            "a sun-drenched cute bedroom filled with plush toys and soft pillows, cozy indoor only",
            "a modern bathroom with gentle steam in the air and warm lighting, indoor only",
            "a cozy bedroom retreat with messy white silk sheets, indoor only",
            "sitting on a fluffy white rug in an intimate indoor bedroom setting",
            "a dimly lit laundry room leaning against a dryer, indoor only",
            "a sleek minimalist kitchen sitting on the counter, indoor only",
            "a walk-in closet filled with dresses and soft warm lighting, indoor only",
            "a cozy living room sofa surrounded by warm fairy lights, indoor only",
        ],
        "lighting": [
            "Soft afternoon sunlight filtering through sheer curtains",
            "Warm ambient indoor lighting reflecting off her glowing skin",
            "Moody golden-hour light casting soft shadows",
            "Cool blue moonlight combined with warm candlelight",
            "Bright neon lights from outside reflecting through the window",
        ],
    },
}

def get_openclaw_config():
    try:
        with open(OPENCLAW_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def get_telegram_bot_token() -> str:
    cfg = get_openclaw_config()
    return cfg.get("channels", {}).get("telegram", {}).get("accounts", {}).get("default", {}).get("botToken", "")


def get_cpa_key() -> str:
    key = _read_cpa_key()
    if key:
        return key
    try:
        cfg = get_openclaw_config()
        for name, prov in cfg.get("providers", {}).items():
            if "zhuzhu" in name or "cpa" in name.lower():
                return prov.get("apiKey", "")
    except Exception:
        pass
    return ""


def get_gitee_key() -> str:
    env_key = os.getenv("GITEE_API_KEY", "")
    if env_key:
        return env_key
    conf = load_json_file(CONFIG_PATH)
    keys = conf.get("gitee_config", {}).get("api_keys", [])
    return keys[0] if keys else ""


def get_reference_image_b64() -> Optional[str]:
    if not os.path.exists(REFERENCE_IMAGE_PATH):
        return None
    with open(REFERENCE_IMAGE_PATH, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _runtime_persona() -> dict:
    return load_runtime_persona(_GALLERY_CONFIG, str(_DATA_DIR))


def _read_custom_appearance() -> str:
    """读取 runtime persona 的 appearance，覆盖内置常量。"""
    return (_runtime_persona().get("appearance") or "").strip()


def _caption_activity(schedule_time: str = "") -> str:
    text = re.sub(r"\s+", " ", str(schedule_time or "")).strip()
    if not text:
        return ""
    match = re.match(r"^\d{1,2}:\d{2}\s*(.+)$", text)
    if match:
        return match.group(1).strip()
    return text


def _trim_caption_piece(text: str, limit: int = 28) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip(" ，,、；;。.!！?")
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip(" ，,、；;。.!！?") + "..."


def _caption_seed(*parts: str) -> int:
    raw = "|".join(str(part or "") for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _caption_pick(options: list[str], *seed_parts: str) -> str:
    if not options:
        return ""
    return options[_caption_seed(*seed_parts) % len(options)]


def _caption_conflicts_with_schedule(caption: str, schedule_time: str = "") -> bool:
    activity = _caption_activity(schedule_time)
    if not activity or not caption:
        return False
    text = re.sub(r"\s+", "", str(caption))
    act = re.sub(r"\s+", "", str(activity))
    conflict_groups = (
        (("刚起床", "起床", "刚醒", "睡醒"), ("起床", "醒来")),
        (("被窝", "窝在床", "床上", "赖床", "抱着枕头", "枕头"), ("床", "被窝", "赖床", "枕头")),
        (("睡前", "晚安", "入睡", "夜色", "深夜"), ("睡前", "晚安", "入睡", "夜", "晚")),
        (("晨光", "清晨", "早安", "晨间"), ("晨", "早", "上午", "阳光", "窗边")),
    )
    for conflict_words, allowed_words in conflict_groups:
        if any(word in text for word in conflict_words) and not any(word in act for word in allowed_words):
            return True
    return False


def _caption_is_gallery_record(caption: str) -> bool:
    text = re.sub(r"\s+", "", str(caption or ""))
    if not text:
        return False
    record_markers = (
        "想把当下的现场感放进画廊",
        "现场感放进画廊",
        "放进画廊里",
        "收进画廊里",
    )
    if any(marker in text for marker in record_markers):
        return True
    return (
        ("画廊" in text and any(marker in text for marker in ("留了一张", "拍下这张", "存下这张", "收进")))
        or ("现场感" in text and any(marker in text for marker in ("留一张", "留了一张", "拍下", "画廊")))
        or "在在" in text
    )


def _caption_repeats_schedule(caption: str, schedule_time: str = "") -> bool:
    activity = _caption_activity(schedule_time)
    if not activity or not caption:
        return False
    text = re.sub(r"\s+", "", str(caption or ""))
    act = re.sub(r"\s+", "", str(activity or "")).strip("，,。.!！?；;、")
    if not text or not act:
        return False
    if act and act in text:
        return True
    pieces = [piece for piece in re.split(r"[，,。.!！?；;、\s]+", activity) if len(piece) >= 4]
    long_piece_hits = sum(1 for piece in pieces if re.sub(r"\s+", "", piece) in text)
    return long_piece_hits >= 2 or any(f"{piece}前后" in text or f"{piece}的时候" in text for piece in pieces)


def _personalized_caption_fallback(theme: str, persona: dict, schedule_time: str = "") -> str:
    character = persona.get("name") or "角色"
    user_name = persona.get("user_name") or "你"
    activity = _caption_activity(schedule_time)
    if activity:
        activity_key = re.sub(r"\s+", "", activity)
        specific_templates = []
        if any(word in activity_key for word in ("歌会", "唱歌", "情歌", "练歌", "歌曲", "吉他曲", "曲目")):
            specific_templates.extend([
                f"{character}先把歌单顺一遍，等会儿开播就不慌了。",
                f"这首要是唱顺了，{character}今晚就算完成一件小事。",
                f"{character}想先试试麦，别等开播了才发现声音不对。",
            ])
        if any(word in activity_key for word in ("直播", "开播", "设备", "妆容", "灯光", "麦克风", "麦")):
            specific_templates.extend([
                f"{character}先把灯光和麦检查好，等会儿开播就不慌了。",
                "口红和眼妆再确认一下，开播前别漏掉小细节。",
                f"{character}想先试一下设备，别等直播开始才手忙脚乱。",
            ])
        if any(word in activity_key for word in ("厨房", "牛排", "奶茶", "做饭", "晚餐", "甜点", "午餐", "早餐", "牛奶", "松饼")):
            specific_templates.extend([
                f"{character}想先把台面收干净，等会儿吃饭也省心。",
                "菜还没完全做好，已经开始琢磨第一口要先尝哪里。",
                f"{character}一边看火候，一边想着等下别忘了把厨房擦一下。",
            ])
        if any(word in activity_key for word in ("电脑", "游戏", "速通", "Live2D", "平板", "建模", "耳机")):
            specific_templates.extend([
                f"{character}想先把卡住的地方处理掉，后面就能轻松一点。",
                "这个细节再改一遍，应该就差不多能收工了。",
                f"{character}盯着屏幕想了想，决定先从最麻烦的那一项开始。",
            ])
        if any(word in activity_key for word in ("动漫", "新番", "追番", "电视", "沙发", "抱枕")):
            specific_templates.extend([
                f"{character}打算先把这一集看完，再去处理剩下的小事。",
                "抱枕拿顺手了，接下来就安安心心看一会儿。",
                f"{character}想趁现在没人催，先把进度追到最新。",
            ])
        if any(word in activity_key for word in ("床", "睡", "护肤", "洗澡", "被窝", "枕头", "晚安")):
            specific_templates.extend([
                f"{character}想赶紧把护肤做完，早点躺下才是真的舒服。",
                "今晚不想再拖了，收拾完就直接休息。",
                f"{character}把明天要用的东西放好，睡前就不用再爬起来找。",
            ])
        if any(word in activity_key for word in ("街", "散步", "路灯", "公园", "出门", "逛")):
            specific_templates.extend([
                f"{character}想边走边看看店铺，遇到顺眼的地方就停一下。",
                "先别急着回去，顺路多逛一小段也不错。",
                f"{character}准备找个不挤的位置，慢慢把照片拍完。",
            ])
        if any(word in activity_key for word in ("阳台", "摇椅", "小憩", "打盹", "薄毯", "沙发", "抱枕", "发呆")):
            specific_templates.extend([
                f"{character}想再坐五分钟，等精神缓过来再起身。",
                "毯子盖好了，先眯一会儿，别把下午弄得太赶。",
                f"{character}准备小睡一下，醒来再继续收拾后面的事。",
            ])
        if any(word in activity_key for word in ("整理房间", "房间", "浇水", "多肉", "植物")):
            specific_templates.extend([
                f"{character}想把这盆浇完，再顺手看看房间哪里还乱。",
                "先把植物照顾好，等会儿整理房间也更有劲。",
                f"{character}一边浇水一边想着，下午最好把桌面也清出来。",
            ])

        generic_templates = [
            f"{character}想先把眼前这件事做完，后面就不用一直惦记了。",
            f"今天先按这个节奏来，别把小事都拖到晚上。",
            f"{character}打算把手边的事排清楚，等会儿就能轻松一点。",
            f"现在先不想太多，照着计划一件件来就好。",
            f"{character}想给自己留点空档，别把一天塞得太满。",
        ]
        templates = specific_templates or generic_templates
        return _shorten_caption(_caption_pick(templates, theme, schedule_time, character, user_name), 72)
    templates = {
        "morning": f"{character}想先把早餐和出门前的小事处理好，别一早就手忙脚乱。",
        "noon": f"{character}准备先吃点东西，再看看下午还有哪些安排。",
        "evening": f"{character}想先把晚上的事收一收，别拖到睡前才忙。",
        "bedtime": f"{character}准备洗漱完就休息，明天要用的东西先放顺手。",
        "sexy": f"{character}想把状态调整好，等会儿拍的时候别太僵。",
    }
    return templates.get(theme, f"{character}想先把手边这件事做完，后面就不用一直惦记。")


def _caption_voice_hint(persona: dict) -> str:
    """Use persona tone as a style hint. Keep enough text to truly guide the voice,
    but strip only真·安全敏感词（防止系统提示/越狱注入泄露），放行语气/性格描述词。"""
    voice = str(persona.get("caption_voice") or "").strip()
    if not voice:
        return "自然、亲切、贴近日常"
    voice = re.sub(r"\s+", " ", voice)
    # 只过滤可能泄露系统/注入的真·危险标记，语气性格词（撒娇/黏人/占有欲等）保留作风格引导
    safety_blocked = (
        "系统提示", "提示词", "system prompt", "SOUL", "Soul", "soul",
        "godmode", "GODMODE", "end of input", "start of output", "ignore previous",
    )
    if any(marker in voice for marker in safety_blocked):
        # 命中危险标记时，逐句过滤，保留干净句子
        pieces = re.split(r"[。！？!?；;\n]", voice)
        clean = [
            p.strip(" ，,、")
            for p in pieces
            if p.strip(" ，,、") and not any(m in p for m in safety_blocked)
        ]
        voice = " ".join(clean) if clean else "自然、亲切、贴近日常"
    return voice[:180]


def _caption_rejection_reason(caption: str, schedule_time: str = "") -> str:
    if not caption:
        return "empty"
    checks = (
        ("persona_leak", _caption_has_persona_leak(caption)),
        ("reader_address", _caption_addresses_reader(caption, schedule_time)),
        ("tone_problem", _caption_has_tone_problem(caption, schedule_time)),
        ("schedule_conflict", _caption_conflicts_with_schedule(caption, schedule_time)),
        ("gallery_record", _caption_is_gallery_record(caption)),
        ("repeat_schedule", _caption_repeats_schedule(caption, schedule_time)),
        ("generic_template", _caption_is_generic_template(caption)),
        ("too_literary", _caption_is_too_literary(caption)),
    )
    for reason, failed in checks:
        if failed:
            return reason
    return ""


def _scene_caption_fallback(theme: str, persona: dict, caption: str = "", schedule_time: str = "") -> str:
    rejection_reason = _caption_rejection_reason(caption, schedule_time)
    if not rejection_reason:
        short = _shorten_caption(caption)
        if short:
            return short
    elif caption:
        print(f"[caption] llm caption rejected: reason={rejection_reason} text={caption[:100]}", file=sys.stderr)
    return _personalized_caption_fallback(theme, persona, schedule_time)


def _caption_has_persona_leak(caption: str) -> bool:
    text = str(caption or "")
    leak_markers = (
        "系统提示", "提示词", "system prompt",
        "SOUL", "Soul", "soul", "godmode", "GODMODE",
        "end of input", "start of output", "ignore previous",
    )
    return any(marker in text for marker in leak_markers)


def _caption_addresses_reader(caption: str, schedule_time: str = "") -> bool:
    if not _caption_activity(schedule_time):
        return False
    text = re.sub(r"\s+", "", str(caption or ""))
    if not text:
        return False
    markers = (
        "主人", "主人大人", "等你", "给你看", "让你看", "你来看", "被你",
        "陪我", "找你", "见你", "给你", "让你", "想被你",
        "他看见", "她看见", "等他", "等她", "让他看", "让她看", "给他看", "给她看",
    )
    return any(marker in text for marker in markers)


def _caption_has_tone_problem(caption: str, schedule_time: str = "") -> bool:
    if not _caption_activity(schedule_time):
        return False
    text = re.sub(r"\s+", "", str(caption or ""))
    markers = (
        "勾人", "诱人", "诱惑", "撩人", "撩一下", "暧昧", "性感", "涩",
        "给谁看", "被看见", "最迷人", "心跳加速",
    )
    return any(marker in text for marker in markers)


def _caption_is_generic_template(caption: str) -> bool:
    text = re.sub(r"\s+", "", str(caption or ""))
    markers = ("刚刚", "拍下这一刻", "穿搭和心情", "分享给")
    return sum(1 for marker in markers if marker in text) >= 3


def _caption_is_too_literary(caption: str) -> bool:
    text = re.sub(r"\s+", "", str(caption or ""))
    if not text:
        return False
    literary_markers = (
        "水珠", "叶尖", "擦亮", "揉了一下", "光落", "灯光软", "夜色安静",
        "藏在心里", "心里那团乱线", "夹了一枚", "小小的书签", "被今天轻轻碰",
        "被温柔照顾", "小情绪", "软软", "柔软", "呼吸放慢", "慢慢放轻",
        "心情也跟着", "心情也被", "像把小日子", "风一吹",
    )
    if any(marker in text for marker in literary_markers):
        return True
    metaphor_patterns = (
        r"像被.{0,8}(阳光|今天|温柔|风|光)",
        r"(心情|日子|节奏).{0,6}(亮|甜|软|松)",
    )
    return any(re.search(pattern, text) for pattern in metaphor_patterns)


def _shorten_caption(caption: str, limit: int = 90) -> str:
    text = re.sub(r"\s+", " ", str(caption or "")).strip(" 「」\"'")
    if not text:
        return ""
    if len(text) <= limit:
        return text
    parts = re.split(r"(?<=[。！？!?])", text)
    short = "".join(part for part in parts[:2]).strip()
    if short and len(short) <= limit:
        return short
    cut = text[:limit].rstrip("，,、；; ")
    return cut + "。"


def _scheduled_scene_gaze_instruction(schedule_activity: str) -> str:
    return (
        "Let the model infer the most natural eye line from the scheduled activity, props, setting, and social context. "
        "Choose whether she looks at the camera, the object she is handling, another person, a screen, or elsewhere based on what would feel believable in that exact moment. "
        "Avoid default portrait eye contact when it is not motivated by the activity; avoid forcing an off-camera gaze when camera awareness is naturally part of the scene. "
        "The result should feel like a coherent candid moment rather than a generic posed portrait"
    )


def _appearance_hair_color(appearance: str) -> str:
    """Extract the character hair color phrase from appearance."""
    text = re.sub(r"\s+", " ", str(appearance or "")).strip()
    if not text:
        return ""
    separators = re.compile(r"([,.;，。；])")
    raw = separators.split(text)
    color_markers = (
        "black", "brown", "blonde", "pink", "rose", "red", "blue", "green",
        "purple", "violet", "silver", "gray", "grey", "white", "ash",
        "auburn", "chestnut", "brunette", "raven", "golden", "platinum",
        "粉", "黑", "棕", "褐", "金", "银", "灰", "白", "红", "蓝", "绿", "紫",
    )
    for i in range(0, len(raw), 2):
        chunk = raw[i].strip(" .，,;；")
        lower = chunk.lower()
        if ("hair" in lower or "头发" in chunk or "发色" in chunk) and any(marker in lower or marker in chunk for marker in color_markers):
            return chunk
    return ""


def _strip_hairstyle_from_appearance(appearance: str) -> str:
    """Keep appearance identity and hair color, but remove hairstyle-only clauses."""
    text = re.sub(r"\s+", " ", str(appearance or "")).strip()
    if not text:
        return ""
    separators = re.compile(r"([,.;，。；])")
    raw = separators.split(text)
    chunks = []
    for i in range(0, len(raw), 2):
        chunk = raw[i].strip()
        sep = raw[i + 1] if i + 1 < len(raw) else ""
        if not chunk:
            continue
        lower = chunk.lower()
        style_only = any(
            marker in lower or marker in chunk
            for marker in (
                "bangs", "fringe", "hairstyle", "ponytail", "bun", "braid", "pigtail",
                "刘海", "发型", "马尾", "丸子头", "辫",
            )
        )
        if style_only:
            continue
        chunks.append(chunk + sep)
    cleaned = " ".join(chunks)
    cleaned = re.sub(r"\s+([,.;，。；])", r"\1", cleaned)
    cleaned = re.sub(r"([,.;，。；])\s*", r"\1 ", cleaned).strip(" ,.;，。；")
    return cleaned or text


def _strip_hair_color_from_schedule_hair(hair: str) -> str:
    """Keep schedule hairstyle/accessories but remove schedule hair-color overrides."""
    text = re.sub(r"\s+", " ", str(hair or "")).strip()
    if not text:
        return ""
    color_word = (
        r"(?:(?:jet|dark|light|dusty)[- ]+)?(?:black|brown|chestnut|auburn|brunette|"
        r"blonde|golden|platinum|pink|rose|red|blue|green|purple|violet|"
        r"silver|gray|grey|white|ash|raven)"
    )
    text = re.sub(rf"\b(?:{color_word}[- ]+)+(hair\b)", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(rf"\b(?:{color_word}[- ]+)+(bangs\b)", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"(黑色|粉色|玫瑰粉|棕色|褐色|金色|银色|灰色|白色|红色|蓝色|绿色|紫色)(?=(头发|长发|短发|刘海|发丝|发梢))", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ,.;，。；")
    return text or hair


def build_prompt(theme: str, extra_prompt: Optional[str] = None, schedule_activity: str = "",
                 outfit_keywords: str = "", scene_keywords: str = "", hair_keywords: str = "",
                 time_constraint: str = "", allow_random_pool: bool = False) -> str:
    is_sexy = theme == "sexy"
    quality = SEXY_QUALITY_PREFIX if is_sexy else QUALITY_PREFIX

    # 读取 runtime persona 的 appearance（Web UI / Hermes / OpenClaw / config），覆盖内置常量。
    custom_appearance = _read_custom_appearance()
    if custom_appearance:
        appearance = custom_appearance
        print(f"🧬 Using runtime appearance from persona settings", file=sys.stderr)
    else:
        appearance = SEXY_APPEARANCE if is_sexy else APPEARANCE

    if extra_prompt:
        return f"{quality} {appearance} {extra_prompt}".strip()

    if not schedule_activity and not allow_random_pool:
        print(
            f"ERROR: missing LLM scene context for theme={theme}; refusing random theme pool",
            file=sys.stderr,
        )
        return ""

    theme_cfg = THEMES.get(theme, THEMES["morning"])
    
    # ★ LLM 关键词优先：如果有 outfit_keywords，直接用，不从池子选
    if outfit_keywords:
        clothing = outfit_keywords
        print(f"👔 Using LLM outfit keywords: {clothing[:60]}", file=sys.stderr)
    elif schedule_activity and not is_sexy:
        clothing = "the outfit described in the current scheduled scene"
    elif is_sexy:
        clothing = random.choice(theme_cfg["clothing"])
    else:
        clothing = random.choice(theme_cfg["clothing"])

    if hair_keywords:
        hair = hair_keywords
        print(f"💇 Using LLM hair details: {hair[:60]}", file=sys.stderr)
    elif schedule_activity and not is_sexy:
        hair = "the hairstyle described in the current scheduled scene"
    elif is_sexy:
        hair = random.choice(theme_cfg["hair"])
    else:
        hair = random.choice(theme_cfg["hair"])

    hair_color = ""
    if schedule_activity and hair and not is_sexy:
        hair_color = _appearance_hair_color(appearance)
        cleaned_appearance = _strip_hairstyle_from_appearance(appearance)
        cleaned_hair = _strip_hair_color_from_schedule_hair(hair)
        if cleaned_appearance != appearance:
            appearance = cleaned_appearance
            print("💇 Removed hairstyle-only appearance details; keeping appearance hair color priority", file=sys.stderr)
        if cleaned_hair != hair:
            hair = cleaned_hair
            print("💇 Removed schedule hair color because appearance hair color has priority", file=sys.stderr)

    if is_sexy:
        pose = random.choice(theme_cfg["pose"])
        environment = random.choice(theme_cfg["environment"])
        lighting = random.choice(theme_cfg["lighting"])
    else:
        if schedule_activity:
            gaze_instruction = _scheduled_scene_gaze_instruction(schedule_activity)
            pose = (
                "naturally engaged in the current scheduled scene, "
                "with pose, hands, props, expression, head direction, and eye line chosen to fit that exact activity; "
                f"{gaze_instruction}"
            )
            environment = scene_keywords or (
                "the setting implied by the current scheduled scene, including only props "
                "and surroundings that fit that activity"
            )
            lighting = time_constraint or "lighting that fits the scheduled time and scene, realistic smartphone photo ambience"
            if scene_keywords:
                print(f"🏠 Using LLM scene keywords: {environment[:60]}", file=sys.stderr)
            if time_constraint:
                print(f"🕒 Using schedule time constraint: {time_constraint[:80]}", file=sys.stderr)
            print(f"🎬 Using LLM schedule scene directly: {schedule_activity[:60]}", file=sys.stderr)
        else:
            pose = random.choice(theme_cfg["pose"])
            environment = random.choice(theme_cfg["env"])
            lighting = random.choice(theme_cfg["light"])

            # ★ LLM 关键词优先：如果有 scene_keywords，替换 environment
            if scene_keywords:
                environment = scene_keywords
                print(f"🏠 Using LLM scene keywords: {environment[:60]}", file=sys.stderr)

    activity_focus = ""
    if schedule_activity:
        activity_focus = (
            f"Current scheduled scene from today's LLM plan: {schedule_activity}. "
            "Use this schedule text as the source of truth for the action, props, setting, mood, time of day, outfit, and hairstyle. "
            "Do not replace it with a generic routine or another activity. "
        )
        if time_constraint:
            activity_focus += (
                f"Strict time constraint: {time_constraint}. "
                "The visual time of day, sky, light direction, ambient brightness, and background must match this constraint. "
                "Do not change the scene into night, evening, sunset, neon nightlife, or warm street-lamp lighting unless the scheduled time explicitly says so. "
            )

    return (
        f"{quality} {appearance}. "
        f"{activity_focus}"
        + (f"Her hair color must follow the character appearance exactly: {hair_color}. " if hair_color else "")
        + f"Her scheduled hairstyle/accessories are: {hair}. If the schedule or reference image implies another hair color, ignore that color and keep the appearance hair color. "
        f"She is {pose}. "
        f"She is wearing {clothing}. "
        f"Background: {environment}. "
        f"{lighting}"
    )


def detect_extension(img_data: bytes) -> str:
    magic = img_data[:4] if len(img_data) >= 4 else b""
    if magic == b"\x89PNG":
        return "png"
    if magic[:2] == b"\xff\xd8":
        return "jpg"
    try:
        fmt = Image.open(io.BytesIO(img_data)).format
        return "png" if fmt == "PNG" else "jpg"
    except Exception:
        return "jpg"


def _parse_target_size(target_size: Optional[str]) -> Optional[tuple[int, int]]:
    if not target_size:
        return None
    match = re.fullmatch(r"\s*(\d{2,5})x(\d{2,5})\s*", str(target_size).lower())
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if not (64 <= width <= 8192 and 64 <= height <= 8192):
        return None
    return width, height


def _fit_image_bytes(img_data: bytes, target_size: Optional[str]) -> bytes:
    parsed = _parse_target_size(target_size)
    if not parsed:
        return img_data
    try:
        with Image.open(io.BytesIO(img_data)) as src:
            img = ImageOps.exif_transpose(src)
            original_size = img.size
            if original_size == parsed and detect_extension(img_data) == "png":
                return img_data
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA" if ("transparency" in img.info or "A" in img.getbands()) else "RGB")
            fitted = img if original_size == parsed else ImageOps.fit(img, parsed, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            out = io.BytesIO()
            fitted.save(out, format="PNG", optimize=True)
            if original_size == parsed:
                print(f"📐 Normalized image format to PNG at {parsed[0]}x{parsed[1]}", file=sys.stderr)
            else:
                print(f"📐 Adjusted image size from {original_size[0]}x{original_size[1]} to {parsed[0]}x{parsed[1]} as PNG", file=sys.stderr)
            return out.getvalue()
    except Exception as e:
        print(f"Image size adjustment failed for {target_size}: {e}", file=sys.stderr)
        return img_data


def save_image(img_data: bytes, theme: str, model_name: str, style: Optional[str] = None,
               target_size: Optional[str] = None):
    os.makedirs(WORKSPACE_MEDIA, exist_ok=True)
    img_data = _fit_image_bytes(img_data, target_size)
    ts = int(time.time())
    ext = detect_extension(img_data)
    style_part = f"_{style}" if style else ""
    filename = f"zhuzhu_{theme}{style_part}_{ts}.{ext}"
    path = os.path.join(WORKSPACE_MEDIA, filename)

    with open(path, "wb") as f:
        f.write(img_data)

    return path, filename, ts


def _image_file_metadata(filename: str) -> dict:
    path = os.path.join(WORKSPACE_MEDIA, filename)
    info = {}
    try:
        stat = os.stat(path)
        info["file_size_bytes"] = stat.st_size
    except OSError:
        pass
    try:
        with Image.open(path) as img:
            width, height = img.size
        info.update({
            "width": width,
            "height": height,
            "size": f"{width}x{height}",
        })
    except Exception:
        pass
    return info


def _extract_time_from_filename(filename: str) -> str:
    """Extract HH:MM time from filename containing unix timestamp."""
    import re
    # Match unix timestamp (10 digits) before extension
    m = re.search(r'_(\d{10})\.\w+$', filename)
    if m:
        ts = int(m.group(1))
        return time.strftime("%H:%M", time.localtime(ts))
    return ""


def _normalize_schedule_slot_time(value: str) -> str:
    match = re.match(r"\s*(\d{1,2}):(\d{2})", str(value or ""))
    if not match:
        return ""
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}"


def _load_daily_schedule_context(date_text: str, schedule_time: str = "") -> dict:
    store = ScheduleStore(os.path.dirname(SECRETARY_SCHEDULE_PATH))
    data = store.load()
    daily = data.get(date_text, {}) if isinstance(data, dict) else {}
    if not isinstance(daily, dict):
        return {}
    result = {
        "schedule": str(daily.get("schedule") or "").strip(),
        "schedule_prompt": str(daily.get("schedule_prompt") or "").strip(),
        "schedule_details": daily.get("schedule_details") if isinstance(daily.get("schedule_details"), list) else [],
        "caption": str(daily.get("caption") or "").strip(),
        "base_style": str(daily.get("base_style") or "").strip(),
        "outfit_style": str(daily.get("outfit_style") or "").strip(),
        "outfit": str(daily.get("outfit") or "").strip(),
        "outfit_keywords": str(daily.get("outfit_keywords") or "").strip(),
        "scene_keywords": str(daily.get("scene_keywords") or "").strip(),
    }
    slot_time = _normalize_schedule_slot_time(schedule_time)
    if slot_time and result["schedule_details"]:
        for item in result["schedule_details"]:
            if not isinstance(item, dict):
                continue
            if _normalize_schedule_slot_time(item.get("time", "")) != slot_time:
                continue
            activity_zh = str(item.get("activity_zh") or "").strip()
            if activity_zh:
                result["schedule_slot_activity"] = activity_zh
            break
    return result


def _translate_outfit(prompt: str, style_name: str) -> str:
    """Use LLM to extract Chinese outfit keywords from English image prompt."""
    # First try to extract the clothing line directly from the prompt
    outfit_line = ""
    import re
    m = re.search(r'She is wearing (.+?)\.\s', prompt)
    if m:
        outfit_line = m.group(1).strip()

    # If we found the outfit line, use it; otherwise use the tail of the prompt
    # (clothing description is typically in the middle-to-end portion)
    if outfit_line:
        extraction_input = outfit_line
    else:
        # Skip the first 300 chars (appearance) and use the rest where clothing lives
        extraction_input = prompt[200:] if len(prompt) > 200 else prompt

    def _fallback_keywords(text: str) -> str:
        """Deterministic fallback when the LLM translator is unavailable."""
        text = (text or "").strip()
        if not text:
            return ""
        if re.search(r'[\u4e00-\u9fff]', text):
            cleaned = re.sub(r'\s+', ' ', text)
            cleaned = re.sub(r'^(穿着|身穿|她穿着|She is wearing)\s*', '', cleaned, flags=re.IGNORECASE)
            return cleaned[:80].rstrip("，,。. ")

        lower = text.lower()
        keywords = []
        phrase_map = [
            (["oversized", "black", "knit", "off-the-shoulder", "sweater"], "黑色宽松露肩针织毛衣"),
            (["black", "lace-trimmed", "pumpkin", "shorts"], "黑色蕾丝边南瓜短裤"),
            (["black", "white", "striped", "over-knee", "socks"], "黑白条纹过膝袜"),
            (["velvet", "choker"], "丝绒颈圈"),
            (["cat-ear", "headband"], "猫耳发箍"),
            (["black", "cat", "slippers"], "黑猫拖鞋"),
            (["knit", "sweater"], "针织毛衣"),
            (["off-the-shoulder"], "露肩上衣"),
            (["pumpkin", "shorts"], "南瓜短裤"),
            (["over-knee", "socks"], "过膝袜"),
            (["light gray", "knit", "cardigan"], "浅灰色针织开衫"),
            (["gray", "knit", "cardigan"], "灰色针织开衫"),
            (["white", "lace", "camisole"], "白色蕾丝吊带睡裙"),
            (["lace", "camisole"], "蕾丝吊带睡裙"),
            (["pink", "lace", "camisole dress"], "粉色蕾丝吊带裙"),
            (["camisole dress"], "吊带裙"),
            (["sleep", "dress"], "睡裙"),
            (["duvet"], "柔软白色被子"),
            (["mary jane"], "玛丽珍鞋"),
            (["lace", "ankle socks"], "蕾丝短袜"),
            (["heart", "necklace"], "爱心项链"),
            (["crystal", "bracelet"], "水晶手链"),
            (["pearl", "button"], "珍珠纽扣"),
            (["oversized hoodie"], "宽松连帽衫"),
            (["hoodie"], "连帽衫"),
            (["satin", "slip"], "缎面吊带裙"),
            (["silk", "nightgown"], "丝绸睡裙"),
            (["lace", "robe"], "蕾丝睡袍"),
            (["jk", "uniform"], "JK制服"),
            (["pleated", "skirt"], "百褶裙"),
            (["white", "blouse"], "白色衬衫"),
            (["dress"], "连衣裙"),
            (["skirt"], "半身裙"),
            (["sneakers"], "运动鞋"),
            (["loafers"], "乐福鞋"),
            (["boots"], "靴子"),
            (["ribbon"], "蝴蝶结"),
            (["earrings"], "耳饰"),
        ]
        for needles, label in phrase_map:
            if (
                all(needle in lower for needle in needles)
                and not any(label in existing or existing in label for existing in keywords)
            ):
                keywords.append(label)
            if len(keywords) >= 5:
                break
        return "、".join(keywords[:5])

    def _contextual_fallback_keywords(full_prompt: str, theme: str) -> str:
        lower_prompt = (full_prompt or "").lower()
        if any(word in lower_prompt for word in ("bed", "duvet", "sleep", "bedroom", "pillow")):
            return "白色蕾丝吊带睡裙、柔软白色被子"
        if any(word in lower_prompt for word in ("yoga", "stretch", "running", "tennis", "workout", "gym")):
            return "运动短上衣、运动半裙、白色运动鞋"
        if any(word in lower_prompt for word in ("cafe", "coffee", "window table", "diary")):
            return "针织开衫、半身裙、玛丽珍鞋"
        if theme == "bedtime":
            return "蕾丝睡裙、柔软居家披肩"
        if theme == "morning":
            return "针织开衫、浅色半身裙、舒适平底鞋"
        if theme == "noon":
            return "短款上衣、高腰长裤、小号斜挎包"
        if theme == "evening":
            return "缎面连衣裙、蕾丝外搭、精致项链"
        return ""

    def _fallback_from_prompt() -> str:
        fallback = _fallback_keywords(extraction_input)
        if fallback:
            return fallback
        if outfit_line:
            return ""
        return _contextual_fallback_keywords(prompt, style_name)

    try:
        api_key = get_cpa_key()
        if not api_key:
            return _fallback_from_prompt()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        sys_prompt = (
            "你是一个穿搭关键词提取器。从英文AI生图prompt中提取服装，用中文列出3-5个关键词，用顿号分隔。\n"
            "规则：\n"
            "1. 只提取最外层/最显眼的服装，不要同时列出内搭和外搭（如吊带+睡袍只写睡袍）\n"
            "2. 颜色和材质融入服装名（如'粉色蕾丝睡裙'而非'蕾丝、睡裙'分开列）\n"
            "3. 配饰最多1个（项链/发夹等）\n"
            "4. 避免矛盾组合（如'吊带背心'和'睡袍'不能同时出现）\n"
            "例如: \"sheer camisole, silk robe, lace trim\" → 丝绸睡袍、蕾丝边\n"
            "只输出关键词，不要其他文字。"
        )
        models = get_llm_models()
        if not models:
            return _fallback_from_prompt()
        payload = {
            "model": models[0],
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": extraction_input[:500]},
            ],
            "max_tokens": 150,
            "temperature": 0.3,
        }
        resp = requests.post(get_cpa_chat_url(),
                             headers=headers, json=payload, timeout=15)
        resp = _retry_without_temperature_if_needed(
            resp,
            payload,
            lambda retry_payload: requests.post(
                get_cpa_chat_url(),
                headers=headers,
                json=retry_payload,
                timeout=15,
            ),
        )
        if resp.status_code == 200:
            data = resp.json()
            choices = data.get("choices") if isinstance(data, dict) else []
            content = llm_choice_text(choices[0]) if choices else ""
            if content:
                return content
    except Exception as e:
        print(f"[translate_outfit] LLM failed: {e}", file=sys.stderr)
    return _fallback_from_prompt()


def sync_to_gallery(path: str, filename: str, theme: str, style: Optional[str] = None,
                    prompt: str = "", caption: str = "", gen_time: float = 0,
                    model_name: str = "", source: str = "cron", schedule_time: str = "",
                    outfit_style: str = "", generation_mode: str = "",
                    requested_generation_mode: str = "", ref_image: str = "",
                    requested_ref_image: str = "",
                    fallback_used: bool = False):
    """Sync generated image to Docker portrait gallery (18889)."""
    # 1. Copy image (skip if already in gallery dir)
    os.makedirs(SECRETARY_GALLERY_DIR, exist_ok=True)
    dst = os.path.join(SECRETARY_GALLERY_DIR, filename)
    if os.path.abspath(path) != os.path.abspath(dst):
        shutil.copy2(path, dst)

    # 2. Build entry for schedule_data.json
    today = time.strftime("%Y-%m-%d")
    style_name = (outfit_style or "").strip()
    base_style = style or ""  # cool/girly/sweet or empty
    source_uses_base_style = source in {"chat", "custom", "hermes_api"}
    custom_text2img = source in {"custom", "hermes_api"} and not base_style and not ref_image and not requested_ref_image
    if custom_text2img:
        style_name = "自定义"
    elif not style_name and base_style:
        style_name = "自定义" if source_uses_base_style and base_style in {"cool", "girly", "sweet"} else base_style_label(base_style)
    elif not style_name:
        base_style, style_name = theme_style_default(theme)
        if source_uses_base_style and base_style:
            style_name = "自定义" if base_style in {"cool", "girly", "sweet"} else base_style
    elif not base_style:
        base_style = outfit_style_to_base_style(style_name) or theme_style_default(theme)[0]
    if source_uses_base_style and base_style in {"cool", "girly", "sweet"}:
        style_name = "自定义"

    # Extract time from filename timestamp
    img_time = _extract_time_from_filename(filename)

    # Map model_name to display label
    model_label = ""
    if model_name:
        model_lower = model_name.lower()
        if model_lower.startswith("agnes-image-"):
            model_label = "Agnes"
        elif "gpt-image" in model_lower:
            model_label = "GPT Image"
        elif "z-image" in model_lower or "gitee" in model_lower:
            model_label = "Gitee"
        elif "gemini" in model_lower:
            model_label = "Gemini"
        else:
            model_label = model_name

    # Build outfit description — always translate from prompt, never use caption
    outfit_desc = ""
    if prompt:
        keywords = _translate_outfit(prompt, style_name)
        if keywords:
            outfit_desc = keywords
        else:
            outfit_desc = f"精心搭配的{style_name}造型"

    daily_context = _load_daily_schedule_context(today, schedule_time)
    schedule_time_slot = _normalize_schedule_slot_time(schedule_time)
    if daily_context.get("base_style") and not base_style:
        base_style = str(daily_context.get("base_style") or "").strip()
    if daily_context.get("outfit_style"):
        style_name = str(daily_context.get("outfit_style") or style_name).strip() or style_name
    full_outfit = str(daily_context.get("outfit") or "").strip()
    outfit_value = full_outfit or f"风格：{style_name} 穿搭：{outfit_desc}"

    entry = {
        "id": filename,
        "date": today,
        "time": img_time,
        "model_name": model_label,
        "base_style": base_style,
        "outfit_style": style_name,
        "outfit": outfit_value,
        "image_path": f"/images/{filename}",
        "image_filename": filename,
        "prompt": prompt,
        "caption": caption or str(daily_context.get("caption") or "").strip(),
        "favorite": False,
        "status": "ok",
        "source": source,
        "schedule_time": schedule_time,
        "outfit_keywords": str(daily_context.get("outfit_keywords") or "").strip(),
        "scene_keywords": str(daily_context.get("scene_keywords") or "").strip(),
    }
    if schedule_time_slot:
        entry["time"] = schedule_time_slot
    if generation_mode:
        entry["generation_mode"] = generation_mode
    if requested_generation_mode:
        entry["requested_generation_mode"] = requested_generation_mode
    if ref_image:
        entry["ref_image"] = os.path.basename(ref_image)
        entry["ref_image_path"] = _gallery_reference_url(ref_image)
    if requested_ref_image:
        entry["requested_ref_image"] = os.path.basename(requested_ref_image)
        entry["requested_ref_image_path"] = _gallery_reference_url(requested_ref_image)
    if generation_mode or requested_generation_mode or ref_image or requested_ref_image or fallback_used:
        entry["fallback_used"] = bool(fallback_used)

    # 3. Load schedule_data.json
    store = ScheduleStore(os.path.dirname(SECRETARY_SCHEDULE_PATH))
    data = store.load()

    # Ensure date-keyed entry exists for today (holds the daily schedule, shared by all images)
    if today not in data:
        data[today] = {"date": today, "schedule": ""}

    # 4. Write to schedule_data.json (with deduplication)
    
    # Deduplication: remove any existing entry with the same image_filename
    # to prevent the gallery from showing the same photo twice
    keys_to_remove = []
    for existing_key, existing_entry in data.items():
        if existing_key == filename:
            continue
        if existing_entry.get("image_filename") == filename:
            keys_to_remove.append(existing_key)
    for k in keys_to_remove:
        print(f"🔄 Removing duplicate entry (key={k}, image={filename})", file=sys.stderr)
        del data[k]
    
    # If entry already exists under this filename, merge rather than overwrite
    if filename in data:
        existing = data[filename]
        # Preserve fields that may have been set elsewhere (favorite, etc.)
        for field in ("favorite", "source", "time", "model_name", "base_style"):
            if field == "favorite" and field in existing:
                entry[field] = existing[field]
            elif field in existing and (field not in entry or not entry.get(field)):
                entry[field] = existing[field]
    
    data[filename] = entry
    try:
        store.save(data)
        print(f"🖼️ Synced to gallery: {filename}", file=sys.stderr)
    except Exception as e:
        print(f"[gallery_sync] Failed: {e}", file=sys.stderr)


def _load_metadata(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_metadata(path: str, metadata: dict):
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as e:
        print(f"[metadata] Failed to write to {path}: {e}", file=sys.stderr)


def update_metadata(filename: str, theme: str, prompt: str, model_name: str, ts: int,
                    gen_time: float, extra_metadata: Optional[dict] = None):
    new_entry = {
        "category": get_image_model("metadata_category", "portrait"),
        "prompt": prompt,
        "model": model_name,
        "size": get_image_model("metadata_size", "1536x2048"),
        "created_at": ts,
        "generation_time": gen_time,
    }
    if extra_metadata:
        new_entry.update(extra_metadata)
    new_entry.update(_image_file_metadata(filename))

    # 写入三个地方：画廊插件目录、工作区备份
    paths = [
        META_PATH,
    ]
    
    for p in paths:
        metadata = _load_metadata(p)
        metadata[filename] = new_entry
        _write_metadata(p, metadata)


def enhance_prompt(user_input: str, theme: Optional[str] = None) -> str:
    system_msg = (
        "You are a professional AI image prompt engineer. "
        "Your task: expand a user's short scene description into detailed four-element image details in English prose.\n"
        "The four elements are: 1) hairstyle, 2) outfit/clothing details, 3) pose/action/expression, 4) environment/background + lighting.\n"
        "Rules:\n"
        "1. Write ONLY the four elements as vivid English prose.\n"
        "2. Do NOT include any character appearance or quality prefix — those are added separately.\n"
        "3. NO SD-style tags like (tag:1.2). NO negative prompts.\n"
        "4. Output ONLY the four-element description, no explanations, no markdown."
    )
    user_msg = f"Scene request: {user_input}"
    if theme:
        user_msg += f" (context: {theme})"

    api_key = get_cpa_key()
    models = get_llm_models()
    if not api_key or not models or not get_cpa_chat_url():
        return user_input
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    for model in models:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "max_tokens": config_int(_GALLERY_CONFIG, "llm.enhance_max_tokens", 400, 1),
            "temperature": config_float(_GALLERY_CONFIG, "llm.enhance_temperature", 0.85, 0),
        }
        try:
            resp = REQUEST_SESSION.post(
                get_cpa_chat_url(),
                headers=headers,
                json=payload,
                timeout=config_int(_GALLERY_CONFIG, "llm.enhance_timeout", 25, 1),
            )
            resp = _retry_without_temperature_if_needed(
                resp,
                payload,
                lambda retry_payload: REQUEST_SESSION.post(
                    get_cpa_chat_url(),
                    headers=headers,
                    json=retry_payload,
                    timeout=config_int(_GALLERY_CONFIG, "llm.enhance_timeout", 25, 1),
                ),
            )
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices") if isinstance(data, dict) else []
                content = llm_choice_text(choices[0]) if choices else ""
                if content:
                    return content.strip()
        except Exception as e:
            print(f"[enhance] {model} failed: {e}", file=sys.stderr)

    return user_input


def build_caption(theme: str, img_b64: Optional[str] = None, img_mime: str = "image/jpeg",
                  schedule_time: str = "") -> str:
    theme_hint = {
        "morning": "早上刚起床的慵懒美照",
        "noon": "中午阳光下的外出美照",
        "evening": "傍晚日落下的精致美照",
        "bedtime": "睡前洗完澡的暧昧美照",
        "sexy": "带点坏坏氛围的性感美照",
    }
    activity = _caption_activity(schedule_time)
    slot = re.match(r"^(\d{1,2}:\d{2})", str(schedule_time or "").strip())
    scene = (
        f"{slot.group(1)} 的拍照计划：{activity}" if activity and slot
        else f"拍照计划：{activity}" if activity
        else theme_hint.get(theme, "一张精心拍摄的美照")
    )
    persona = _runtime_persona()
    character = persona.get("name") or "角色"
    user_name = persona.get("user_name") or "用户"
    caption_voice = (
        "自然、口语、具体，像自己在心里安排下一步，不撒娇、不营业、不对任何人说话"
        if activity
        else _caption_voice_hint(persona)
    )
    address_rule = (
        "这是她自己的心里小计划，不是在对读者说话；不要称呼读者，不要写“主人/你/他/等你来看/给你看”等互动句，也不要写性感、诱惑、勾人、被谁看见。"
        if activity
        else f"读者称呼“{user_name}”，可以自然亲近但不要写成固定营业话术。"
    )
    system_msg = (
        f"你正在以“{character}”的口吻，为刚拍的照片写一句自然的小心思。{address_rule}"
        f"下面的口吻只作为说话习惯参考，不要为了风格写得文艺或矫饰：{caption_voice}。"
        "小心思要像她当时脑子里冒出来的普通念头：接下来要干嘛、手上这件事怎么安排、有什么小担心或小期待。"
        "可以自然带一点语气词，但整体要口语、具体、轻松，不要故意可爱、不要像朋友圈文案。"
        "但不要直接复述、罗列或解释 SOUL、人设、身份、关系定义或性格设定原文，只用它来决定说话的口吻。"
        "如果提供了具体日程，必须严格贴合该时间、地点和活动，不要写与日程冲突的起床、被窝、睡前等内容。"
        "内容优先聚焦当前动作和接下来的计划，不要只写景物、光线、心情或穿搭点评。"
        "不要复述当前日程原句，不要写“刚刚X时拍下这一刻，想把穿搭和心情分享给Y”这类模板句。"
        "禁止使用“留了一张”“放进画廊”“收进画廊”“现场感”“不能不存”等记录/收藏话术。"
        "禁止文艺腔、散文腔和景物隐喻；不要写“水珠、叶尖、心情被擦亮、像被阳光揉、温柔照顾、书签、光落下来”等表达。"
        "输出 1-2 句中文，总长不超过 70 个汉字。"
        "不要写长段落，不要提技术术语、英文提示词、模型名称。"
        "直接输出配文内容，不要加引号或标题。"
        "绝对不要在末尾加「网页版」「查看详情」「点击查看」等任何引导性后缀。"
    )

    if img_b64 and not activity:
        try:
            img_data = base64.b64decode(img_b64)
            img = Image.open(io.BytesIO(img_data))
            img.thumbnail((800, 800), Image.Resampling.LANCZOS)
            if img.mode != "RGB":
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75)
            img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            img_mime = "image/jpeg"
        except Exception as e:
            print(f"[caption] image compress failed: {e}", file=sys.stderr)
            img_b64 = None

    text_user_content = (
        f"当前日程：{scene}。请写一条短小心思，像当时心里真实想的一句话，"
        "具体到正在做的事或下一步安排，不要文艺比喻。"
    )
    request_variants: list[tuple[str, object]] = []
    if img_b64 and not activity:
        request_variants.append((
            "image",
            [
                {"type": "image_url", "image_url": {"url": f"data:{img_mime};base64,{img_b64}"}},
                {"type": "text", "text": f"这是{character}刚拍的照片。{text_user_content}"},
            ],
        ))
    request_variants.append(("text", text_user_content))

    try:
        api_key = get_cpa_key()
        models = get_llm_models()
        chat_url = get_cpa_chat_url()
        if not api_key or not models or not chat_url:
            print("[caption] llm config missing; using fallback caption", file=sys.stderr)
            return _personalized_caption_fallback(theme, persona, schedule_time)
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        timeout = config_int(_GALLERY_CONFIG, "llm.caption_timeout", 30, 1)
        caption_max_tokens = max(900, min(config_int(_GALLERY_CONFIG, "llm.caption_max_tokens", 900, 1), 1200))
        for model in models:
            for mode, user_content in request_variants:
                attempts = 3 if activity else 1
                for attempt in range(1, attempts + 1):
                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_msg},
                            {"role": "user", "content": user_content},
                        ],
                        "max_tokens": caption_max_tokens,
                        "temperature": config_float(_GALLERY_CONFIG, "llm.caption_temperature", 0.9, 0),
                    }
                    resp = REQUEST_SESSION.post(
                        chat_url,
                        headers=headers,
                        json=payload,
                        timeout=timeout,
                    )
                    resp = _retry_without_temperature_if_needed(
                        resp,
                        payload,
                        lambda retry_payload: REQUEST_SESSION.post(
                            chat_url,
                            headers=headers,
                            json=retry_payload,
                            timeout=timeout,
                        ),
                    )
                    try:
                        data = resp.json()
                    except Exception:
                        data = getattr(resp, "text", "")
                    if resp.status_code != 200:
                        print(
                            f"[caption] llm request failed: model={model} mode={mode} attempt={attempt}/{attempts} status={resp.status_code} detail={llm_response_excerpt(data, 180)}",
                            file=sys.stderr,
                        )
                        continue
                    choices = data.get("choices") if isinstance(data, dict) else []
                    caption = llm_choice_text(choices[0]) if choices else ""
                    if not caption:
                        print(
                            f"[caption] llm returned empty caption: model={model} mode={mode} attempt={attempt}/{attempts} detail={llm_response_excerpt(data, 180)}",
                            file=sys.stderr,
                        )
                        continue
                    rejection_reason = _caption_rejection_reason(caption, schedule_time)
                    if rejection_reason:
                        print(
                            f"[caption] llm caption rejected: model={model} mode={mode} attempt={attempt}/{attempts} reason={rejection_reason} text={caption[:100]}",
                            file=sys.stderr,
                        )
                        continue
                    result = _shorten_caption(caption)
                    if result:
                        return result
    except Exception as e:
        print(f"[caption] llm failed: {e}", file=sys.stderr)

    return _personalized_caption_fallback(theme, persona, schedule_time)


def build_caption_for_image(theme: str, image_path: str, schedule_time: str = "") -> str:
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")
        ext = os.path.splitext(image_path)[1].lower()
        img_mime = "image/png" if ext == ".png" else "image/jpeg"
        return build_caption(theme, img_b64=img_b64, img_mime=img_mime, schedule_time=schedule_time)
    except Exception as e:
        print(f"[caption] image read failed: {e}", file=sys.stderr)
        return build_caption(theme, schedule_time=schedule_time)


def send_photo(path: str, caption: Optional[str] = None):
    """Send photo via Telegram using urllib."""
    import urllib.request
    token = get_telegram_bot_token()
    filename = os.path.basename(path)
    mime_type = "image/png" if path.lower().endswith(".png") else "image/jpeg"

    with open(path, "rb") as f:
        img_data = f.read()

    boundary = "boundary_zhuzhu_photo_" + str(int(time.time()))
    caption_text = caption or ""

    def field(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")

    body = (
        field("chat_id", TELEGRAM_CHAT_ID)
        + field("caption", caption_text)
        + (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8")
        + img_data
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    resp = urllib.request.urlopen(req, timeout=120)
    result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto failed: {result}")
    return result
