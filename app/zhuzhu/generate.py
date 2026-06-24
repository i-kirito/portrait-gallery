#!/usr/bin/env python3
"""Unified chat image generation entrypoint for zhuzhu-image-gen."""
import argparse
import json
import os
import random
import re
import sys
from datetime import date
from typing import Optional

import requests

from core import (
    CONFIG_PATH,
    SECRETARY_SCHEDULE_PATH,
    _strip_hair_color_from_schedule_hair,
    build_caption_for_image,
    build_prompt,
    enhance_prompt,
    get_cpa_chat_url,
    get_cpa_key,
    get_image_model,
    get_llm_models,
    get_reference_path,
    send_photo,
)
from generate_gitee import MODEL_NAME as GITEE_MODEL_NAME
from generate_gitee import generate as generate_with_gitee
from generate_gptimage import GPTIMAGE_DIRECT_MODEL
from generate_gptimage import generate as generate_with_gptimage
from settings import llm_choice_text, llm_temperature_param_error, outfit_style_to_prompt_hint, style_reference_filename

# Gemini image generation always uses the CPA Base URL config.
_GEMINI_CPA_MODEL = get_image_model("gemini_model", "gemini-3.1-flash-image")

DAILY_THEMES = {"morning", "noon", "evening", "bedtime"}
ALL_THEMES = sorted(DAILY_THEMES | {"sexy", "custom"})

STYLE_REF_MAP = {}
for _style in ("cool", "girly", "sweet"):
    _fname = style_reference_filename(_style)
    _path = get_reference_path(_fname)
    if _path:
        STYLE_REF_MAP[_style] = _path


def _extract_style_hint(outfit_info: str) -> str:
    if not outfit_info:
        return ""
    m = re.search(r'风格[：:]\s*(\S+)', outfit_info)
    if not m:
        return ""
    raw_style = m.group(1).strip()
    return outfit_style_to_prompt_hint(raw_style)


def _extract_outfit_style_name(outfit_info: str) -> str:
    if not outfit_info:
        return ""
    m = re.search(r'风格[：:]\s*([^\s\n，,。；;]+)', outfit_info)
    return m.group(1).strip() if m else ""


def _get_today_outfit_style_name() -> str:
    today_str = date.today().isoformat()
    if not os.path.exists(_SCHEDULE_PATH):
        return ""
    try:
        with open(_SCHEDULE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return ""

    daily = data.get(today_str)
    if isinstance(daily, dict):
        style_name = (daily.get("outfit_style") or "").strip()
        if style_name:
            return style_name
        style_name = _extract_outfit_style_name(daily.get("outfit", ""))
        if style_name:
            return style_name

    for entry in data.values():
        if not isinstance(entry, dict) or entry.get("date") != today_str:
            continue
        style_name = (entry.get("outfit_style") or "").strip()
        if style_name:
            return style_name
        style_name = _extract_outfit_style_name(entry.get("outfit", ""))
        if style_name:
            return style_name
    return ""


def _gallery_outfit_style_for_source(source: str, actual_style: Optional[str]) -> str:
    """Return the gallery style label without leaking the daily schedule style into chat/custom images."""
    if (source or "").strip() in {"chat", "custom", "hermes_api"}:
        style = (actual_style or "").strip()
        if style in {"cool", "girly", "sweet"}:
            return "自定义"
        return style
    return _get_today_outfit_style_name()


def _gitee_fallback_enabled() -> bool:
    """Return whether automatic GPT/Gemini -> Gitee fallback is enabled."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f) or {}
        return bool(data.get("gitee_fallback_enabled", False))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False


def _chat_llm(messages: list[dict], max_tokens: int, temperature: float) -> str:
    api_key = get_cpa_key()
    chat_url = get_cpa_chat_url()
    models = get_llm_models()
    if not chat_url or not models:
        return ""

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for model in models:
        try:
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            resp = requests.post(chat_url, headers=headers, json=payload, timeout=30)
            if resp.status_code == 400:
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                if llm_temperature_param_error(body):
                    payload.pop("temperature", None)
                    resp = requests.post(chat_url, headers=headers, json=payload, timeout=30)
            if resp.status_code != 200:
                continue
            data = resp.json()
            choices = data.get("choices") if isinstance(data, dict) else []
            if not choices:
                continue
            content = llm_choice_text(choices[0])
            if content:
                return content
        except Exception as e:
            print(f"[llm] {model} failed: {e}", file=sys.stderr)
    return ""


def _classify_style(prompt_text: str) -> str:
    """Use LLM to classify prompt into cool/girly/sweet based on vibe."""
    system = (
        "You are a style classifier for character portrait generation. "
        "Given an image description, classify the overall vibe into exactly one of three styles:\n"
        "- cool: 冷御风 — mature, elegant, sophisticated, chic, edgy, aloof, mysterious, confident, high-fashion vibe. "
        "Keywords: 冷艳, 御姐, 高冷, 气质, 成熟, dark, serious\n"
        "- girly: 少女风 — cute, playful, youthful, cheerful, bubbly, energetic, sporty, lively. "
        "Keywords: 活泼, 元气, 可爱, 俏皮, 运动, 校园, fun\n"
        "- sweet: 甜妹风 — sweet, gentle, warm, soft, delicate, romantic, cozy, dreamy, tender, innocent. "
        "Keywords: 甜美, 温柔, 软萌, 治愈, 粉色, 暖光, 可爱, 居家, 睡衣, 双马尾\n\n"
        "Examples:\n"
        "- '穿着黑色皮衣站在街头' → cool\n"
        "- 'JK制服在学校操场跑步' → girly\n"
        "- '穿着粉色睡衣坐在床边' → sweet\n"
        "- '晚宴红毯礼服' → cool\n"
        "- '猫咪自拍比心' → girly\n"
        "- '慵懒居家暖光' → sweet\n\n"
        "Output ONLY the single word: cool, girly, or sweet. No explanation, no punctuation."
    )

    raw = _chat_llm(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt_text[:500]},
        ],
        max_tokens=50,
        temperature=0.1,
    ).lower()
    for word in ("cool", "girly", "sweet"):
        if word in raw:
            return word

    return random.choice(["cool", "girly", "sweet"])


# 发型池 — LLM 从这个池子里根据场景选最搭的发型
_HAIRSTYLE_POOL = [
    "high ponytail", "low ponytail", "side ponytail",
    "twin tails", "messy bun", "double buns",
    "french braid", "double dutch braids", "side braid",
    "half-up half-down", "braided crown", "pigtail braids",
]


def _decide_hairstyle(prompt_text: str) -> Optional[str]:
    """Use LLM to pick the most fitting hairstyle for the scene."""
    pool_str = ", ".join(_HAIRSTYLE_POOL)
    system = (
        "You are a hairstyle selector for character portrait generation. "
        "Given a scene description, pick the SINGLE most fitting hairstyle from the pool below.\n\n"
        f"Hairstyle pool: {pool_str}\n\n"
        "Scene-to-hairstyle guidelines:\n"
        "- JK uniform/school/active/sporty → high ponytail, double dutch braids, side braid, half-up half-down\n"
        "- Cute/playful/douyin/kawaii → twin tails, double buns, pigtail braids, messy bun\n"
        "- Elegant/date/evening/dinner/formal → low ponytail, braided crown, half-up half-down\n"
        "- Loungewear/pajamas/bedtime/home/relaxed → messy bun, low ponytail, side braid\n"
        "- Street/city/cool/edgy → high ponytail, side ponytail, low ponytail\n"
        "- Sweet/romantic/cozy/warm → french braid, side braid, half-up half-down, low ponytail\n"
        "- Waiting/gentle/melancholy → side braid, low ponytail, half-up half-down\n\n"
        "IMPORTANT: Vary your selection! Do NOT always pick the same hairstyle. "
        "Consider the scene's mood, outfit, and setting carefully.\n\n"
        "Output ONLY the hairstyle name from the pool, e.g. 'high ponytail'. No explanation."
    )

    raw = _chat_llm(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt_text[:500]},
        ],
        max_tokens=200,
        temperature=0.2,
    ).lower()
    best_idx = len(raw)
    best_h = None
    for h in _HAIRSTYLE_POOL:
        idx = raw.find(h)
        if idx != -1 and idx < best_idx:
            best_idx = idx
            best_h = h
    if best_h:
        return best_h

    return None


def resolve_prompt(
    theme: str,
    prompt_override: Optional[str] = None,
    enhance: bool = False,
    schedule_activity: str = "",
    allow_random_pool: bool = False,
) -> str:
    if not prompt_override:
        return build_prompt(
            theme,
            schedule_activity=schedule_activity,
            allow_random_pool=allow_random_pool,
        )
    
    if enhance:
        enhanced_parts = enhance_prompt(prompt_override, theme=theme)
        return build_prompt(theme, enhanced_parts)
    
    return build_prompt(theme, prompt_override)


def _generate_with_gemini_cpa(theme: str, prompt: str):
    """Call CPA gemini-3.1-flash-image model, return image path or None."""
    import base64
    import re
    import time
    import requests
    from core import get_cpa_chat_url, get_cpa_key, save_image, update_metadata, sync_to_gallery

    api_key = get_cpa_key()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": _GEMINI_CPA_MODEL,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
    }
    chat_url = get_cpa_chat_url()
    start = time.time()
    try:
        resp = requests.post(chat_url, headers=headers, json=payload, timeout=180)
        if resp.status_code != 200:
            print(f"Gemini CPA error {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
            return None
        data = resp.json()
        content = data["choices"][0]["message"]["content"]

        # Try to extract image URL from markdown/html/direct link
        img_data = None
        url_match = re.search(r'!\[[^\]]*\]\(([^)\s]+)\)', content)
        if not url_match:
            url_match = re.search(r'src=[\"\']([^\"\'>\s]+)[\"\']', content, re.IGNORECASE)
        if not url_match:
            url_match = re.search(r'https?://[^\s<>\"\']+\.(?:png|jpg|jpeg|gif|webp)', content, re.IGNORECASE)

        if url_match:
            img_url = url_match.group(1) if 'group' in dir(url_match) else url_match.group(0)
            img_resp = requests.get(img_url, timeout=60)
            if img_resp.status_code == 200:
                img_data = img_resp.content

        # Fallback: try base64 in response
        if not img_data:
            b64_match = re.search(r'base64,([A-Za-z0-9+/=]+)', content)
            if b64_match:
                img_data = base64.b64decode(b64_match.group(1))

        if not img_data:
            print(f"Gemini CPA: no image found in response: {content[:300]}", file=sys.stderr)
            return None

        elapsed = round(time.time() - start, 2)
        path, filename, ts = save_image(img_data, theme, _GEMINI_CPA_MODEL)
        update_metadata(filename, theme, prompt, _GEMINI_CPA_MODEL, ts, elapsed)
        # sync_to_gallery 由 generate.py 统一处理，此处不重复
        return path

    except Exception as e:
        print(f"Gemini CPA failed: {e}", file=sys.stderr)
        return None


_SCHEDULE_PATH = SECRETARY_SCHEDULE_PATH

# Theme → schedule period keywords
_THEME_PERIODS = {
    "morning": ["上午", "早上", "清晨"],
    "noon": ["中午", "下午", "上午"],
    "evening": ["晚上", "傍晚", "下午"],
    "bedtime": ["晚上", "深夜"],
    "sexy": ["晚上", "深夜"],
    "custom": [],
}


def _normalize_schedule_slot(value: str) -> tuple:
    """Return (HH:mm activity, activity) for a schedule override."""
    m = re.match(r'\s*(\d{1,2}):(\d{2})\s*(.*)', value or "")
    if not m:
        activity = (value or "").strip()
        return activity, activity
    hour = int(m.group(1))
    minute = int(m.group(2))
    activity = m.group(3).strip()
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return activity, activity
    return f"{hour:02d}:{minute:02d} {activity}".strip(), activity


def _find_schedule_activity(schedule: str, time_slot: str, max_distance: int = 60) -> str:
    """Find the activity near HH:mm in a schedule block."""
    _time_text, activity = _find_schedule_slot(schedule, time_slot, max_distance=max_distance)
    return activity


def _find_schedule_slot(schedule: str, time_slot: str, max_distance: int = 60) -> tuple[str, str]:
    """Find the nearest schedule slot near HH:mm, returning (HH:mm, activity)."""
    m = re.match(r'\s*(\d{1,2}):(\d{2})', time_slot or "")
    if not m or not schedule:
        return "", ""
    target_min = int(m.group(1)) * 60 + int(m.group(2))
    times = re.findall(r'(\d{1,2}):(\d{2})', schedule)
    parts = re.split(r'\d{1,2}:\d{2}\s*', schedule)
    best_time, best, best_dist = "", "", 9999
    for (hs, ms), activity in zip(times, parts[1:]):
        slot_min = int(hs) * 60 + int(ms)
        dist = abs(slot_min - target_min)
        if dist < best_dist:
            best_dist = dist
            best_time = f"{int(hs):02d}:{int(ms):02d}"
            best = activity.strip().rstrip('～').strip()
    if best and best_dist <= max_distance:
        return best_time, best
    return "", ""


def _normalize_schedule_detail_time(value: str) -> str:
    m = re.match(r'\s*(\d{1,2}):(\d{2})(?:\s+.*)?$', str(value or ""))
    if not m:
        return ""
    hour = int(m.group(1))
    minute = int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}"


def _schedule_time_constraint(value: str) -> str:
    time_text = _normalize_schedule_detail_time(value)
    if not time_text:
        return ""
    hour, minute = [int(part) for part in time_text.split(":")]
    clock = f"{hour:02d}:{minute:02d}"
    if 5 <= hour < 8:
        label = "early morning"
        lighting = "soft early-morning natural daylight"
        forbid = "night, evening, sunset, neon nightlife, or street-lamp-dominated lighting"
    elif 8 <= hour < 12:
        label = "late morning" if hour >= 10 else "morning"
        lighting = "clear morning natural daylight with a bright daytime street or indoor ambience"
        forbid = "night, evening, sunset, neon signs as the main light source, or street-lamp-dominated lighting"
    elif 12 <= hour < 14:
        label = "midday"
        lighting = "bright midday natural daylight"
        forbid = "night, evening, sunset, neon nightlife, or street-lamp-dominated lighting"
    elif 14 <= hour < 17:
        label = "afternoon"
        lighting = "afternoon natural daylight"
        forbid = "night, evening, neon nightlife, or street-lamp-dominated lighting"
    elif 17 <= hour < 19:
        label = "early evening"
        lighting = "early-evening dusk or golden-hour light only if it fits the exact clock time"
        forbid = "deep night or neon nightlife unless explicitly described"
    elif 19 <= hour < 22:
        label = "evening"
        lighting = "realistic evening ambient light matching the exact clock time"
        forbid = "midday sunlight or unrelated time-of-day changes"
    else:
        label = "late night"
        lighting = "realistic late-night low light matching the exact clock time"
        forbid = "daylight or unrelated time-of-day changes"
    return (
        f"The scheduled clock time is {clock}, {label}. "
        f"Use {lighting}. "
        f"Forbidden time mismatch: {forbid}."
    )


def _detail_time_is_daylight(value: str) -> bool:
    time_text = _normalize_schedule_detail_time(value)
    if not time_text:
        return False
    hour = int(time_text.split(":", 1)[0])
    return 5 <= hour < 17


def _strip_daylight_conflicts(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    replacements = (
        r"\bat night\b",
        r"\bat nighttime\b",
        r"\bin the evening\b",
        r"\bduring the evening\b",
        r"\bafter dark\b",
        r"\bat dusk\b",
        r"\bat sunset\b",
        r"\bnighttime\b",
        r"\bneon-lit\b",
        r"\bstreet-lamp-lit\b",
        r"\bstreet lamps?\b",
        r"\bstreetlights?\b",
        r"\bnightlife\b",
    )
    for pattern in replacements:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bglowing shop signs?\b", "daytime storefront signboards", text, flags=re.IGNORECASE)
    text = re.sub(r"\bglowing signs?\b", "daytime signboards", text, flags=re.IGNORECASE)
    text = re.sub(r"\blit signs?\b", "daytime signboards", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"(?:,\s*){2,}", ", ", text)
    return text.strip(" ,.;:")


def _schedule_detail_for_time(detail: dict, raw_time: str) -> dict:
    if not isinstance(detail, dict) or not _detail_time_is_daylight(raw_time):
        return detail if isinstance(detail, dict) else {}
    cleaned = dict(detail)
    conflict_words = re.compile(
        r"\b(night|nighttime|evening|sunset|dusk|neon-lit|street-lamp|street lamp|streetlight|nightlife)\b",
        re.IGNORECASE,
    )
    for field in ("activity_en", "action_en", "scene_en", "props_en"):
        if cleaned.get(field):
            cleaned[field] = _strip_daylight_conflicts(cleaned[field])
    if conflict_words.search(str(cleaned.get("lighting_en", ""))):
        cleaned["lighting_en"] = "clear daytime natural light matching the scheduled clock time"
    return cleaned


def _schedule_detail_map(details) -> dict:
    if not isinstance(details, list):
        return {}
    mapped = {}
    for item in details:
        if not isinstance(item, dict):
            continue
        time_text = _normalize_schedule_detail_time(item.get("time", ""))
        if time_text:
            mapped[time_text] = item
    return mapped


def _nearest_schedule_detail(details, time_slot: str, max_distance: int = 60) -> tuple[str, dict]:
    if not isinstance(details, list):
        return "", {}
    target = _normalize_schedule_detail_time(time_slot)
    if not target:
        return "", {}
    target_hour, target_minute = [int(part) for part in target.split(":")]
    target_minutes = target_hour * 60 + target_minute
    best_time, best_detail, best_dist = "", {}, 9999
    for item in details:
        if not isinstance(item, dict):
            continue
        item_time = _normalize_schedule_detail_time(item.get("time", ""))
        if not item_time:
            continue
        hour, minute = [int(part) for part in item_time.split(":")]
        dist = abs(hour * 60 + minute - target_minutes)
        if dist < best_dist:
            best_time, best_detail, best_dist = item_time, item, dist
    if best_detail and best_dist <= max_distance:
        return best_time, best_detail
    return "", {}


def _schedule_detail_text(detail: dict) -> str:
    if not isinstance(detail, dict):
        return ""
    parts = []
    labels = (
        ("activity_en", "Activity"),
        ("action_en", "Action"),
        ("scene_en", "Scene"),
        ("outfit_en", "Outfit"),
        ("hair_en", "Hair"),
        ("props_en", "Props"),
        ("lighting_en", "Lighting"),
    )
    for field, label in labels:
        value = re.sub(r"\s+", " ", str(detail.get(field, ""))).strip(" .")
        if field == "hair_en":
            value = _strip_hair_color_from_schedule_hair(value)
        if value:
            parts.append(f"{label}: {value}")
    return ". ".join(parts)


def _schedule_detail_keywords(detail: dict, fallback_outfit: str = "") -> tuple[str, str, str]:
    if not isinstance(detail, dict):
        return fallback_outfit, "", ""
    outfit = re.sub(r"\s+", " ", str(detail.get("outfit_en", "") or fallback_outfit)).strip()
    hair = re.sub(r"\s+", " ", str(detail.get("hair_en", ""))).strip()
    hair = _strip_hair_color_from_schedule_hair(hair)
    scene_parts = []
    for field in ("scene_en", "props_en", "lighting_en"):
        value = re.sub(r"\s+", " ", str(detail.get(field, ""))).strip(" .")
        if value:
            scene_parts.append(value)
    return outfit, ". ".join(scene_parts), hair


def _append_time_constraint(scene_kw: str, time_constraint: str) -> str:
    scene = re.sub(r"\s+", " ", str(scene_kw or "")).strip(" .")
    constraint = re.sub(r"\s+", " ", str(time_constraint or "")).strip(" .")
    if not constraint:
        return scene
    if not scene:
        return constraint
    return f"{scene}. {constraint}"


def _clean_schedule_detail_override(raw_value: str) -> dict:
    if not raw_value:
        return {}
    try:
        data = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned = {}
    for field in (
        "time",
        "activity_zh",
        "activity_en",
        "action_en",
        "scene_en",
        "outfit_en",
        "hair_en",
        "props_en",
        "lighting_en",
    ):
        value = re.sub(r"\s+", " ", str(data.get(field, ""))).strip()
        if value:
            cleaned[field] = value
    return cleaned


def _get_schedule_context(theme: str, schedule_time_override: str = "", schedule_detail_override: Optional[dict] = None) -> tuple:
    """Read daily schedule and return context, display slot, outfit, scene, and hair details."""
    if theme not in _THEME_PERIODS or not _THEME_PERIODS[theme]:
        return "", "", "", "", ""
    today_str = date.today().isoformat()
    data = {}
    if os.path.exists(_SCHEDULE_PATH):
        try:
            with open(_SCHEDULE_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}

    def _usable_daily_schedule(entry: dict) -> bool:
        return (
            isinstance(entry, dict)
            and entry.get("source") != "fallback"
            and entry.get("status", "ok") == "ok"
            and bool(str(entry.get("schedule") or "").strip())
            and entry.get("schedule") != "生成失败"
        )
    
    # Search for schedule: first try date key, then scan all entries for today
    schedule = ""
    schedule_prompt = ""
    outfit_info = ""
    outfit_kw = ""
    scene_kw = ""
    schedule_details = []
    daily = data.get(today_str)
    if _usable_daily_schedule(daily):
        schedule = daily["schedule"]
        schedule_prompt = daily.get("schedule_prompt", "") or daily["schedule"]
        outfit_info = daily.get("outfit", "")
        outfit_kw = daily.get("outfit_keywords", "")
        scene_kw = daily.get("scene_keywords", "")
        schedule_details = daily.get("schedule_details", []) if isinstance(daily.get("schedule_details"), list) else []
    
    # If date-keyed entry has no schedule, scan ALL entries for today
    if not schedule:
        for key, entry in data.items():
            if not isinstance(entry, dict):
                continue
            if (
                not entry.get("image_filename")
                and entry.get("date") == today_str
                and _usable_daily_schedule(entry)
            ):
                schedule = entry["schedule"]
                schedule_prompt = entry.get("schedule_prompt", "") or entry["schedule"]
                outfit_info = entry.get("outfit", "")
                outfit_kw = entry.get("outfit_keywords", "")
                scene_kw = entry.get("scene_keywords", "")
                schedule_details = entry.get("schedule_details", []) if isinstance(entry.get("schedule_details"), list) else []
                break

    detail_by_time = _schedule_detail_map(schedule_details)

    if schedule_time_override and not schedule and not schedule_detail_override:
        print("📋 No usable daily LLM schedule found for schedule override; skipping schedule action injection", file=sys.stderr)
        return "", "", "", "", ""

    if schedule_time_override:
        raw_slot, activity = _normalize_schedule_slot(schedule_time_override)
        if activity:
            raw_time = _normalize_schedule_detail_time(raw_slot)
            time_constraint = _schedule_time_constraint(raw_time)
            detail = schedule_detail_override if isinstance(schedule_detail_override, dict) else {}
            if not detail:
                detail = detail_by_time.get(raw_time, {})
            detail = _schedule_detail_for_time(detail, raw_time)
            prompt_time, prompt_match = _find_schedule_slot(schedule_prompt, raw_slot, max_distance=0)
            display_time, display_match = _find_schedule_slot(schedule, raw_slot, max_distance=0)
            prompt_activity = _schedule_detail_text(detail) or prompt_match or activity
            detail_outfit_kw, detail_scene_kw, detail_hair_kw = _schedule_detail_keywords(detail, outfit_kw)
            detail_scene_kw = _append_time_constraint(detail_scene_kw, time_constraint)
            style_hint = _extract_style_hint(outfit_info)
            ctx = f"Today's plan: {prompt_activity}"
            if time_constraint:
                ctx += f". Time: {time_constraint}"
            if style_hint:
                ctx += f". Style: {style_hint}"
            print(f"📋 Schedule override: {ctx}", file=sys.stderr)
            raw_time = raw_time or display_time or prompt_time
            raw_activity = activity or display_match or prompt_match
            return ctx, f"{raw_time} {raw_activity}".strip(), detail_outfit_kw, detail_scene_kw, detail_hair_kw
        # 只传了 HH:MM 没有活动文字 → 在日程中精确匹配该时间
        if (schedule_prompt or schedule) and raw_slot:
            h_match = re.match(r'(\d{1,2}):(\d{2})', raw_slot)
            if h_match:
                raw_time = _normalize_schedule_detail_time(raw_slot)
                time_constraint = _schedule_time_constraint(raw_time)
                detail = _schedule_detail_for_time(detail_by_time.get(raw_time), raw_time)
                best = _schedule_detail_text(detail) or _find_schedule_activity(schedule_prompt or schedule, raw_slot)
                display_best = _find_schedule_activity(schedule, raw_slot) or best
                if best:
                    detail_outfit_kw, detail_scene_kw, detail_hair_kw = _schedule_detail_keywords(detail, outfit_kw)
                    detail_scene_kw = _append_time_constraint(detail_scene_kw, time_constraint)
                    ctx = f"Today's plan: {best}"
                    if time_constraint:
                        ctx += f". Time: {time_constraint}"
                    print(f"📋 Schedule time-match ({raw_slot}): {ctx}", file=sys.stderr)
                    return ctx, f"{raw_slot} {display_best}".strip(), detail_outfit_kw, detail_scene_kw, detail_hair_kw
    
    if not schedule:
        print("📋 No daily LLM schedule found; skipping schedule action injection", file=sys.stderr)
        return "", "", "", "", ""
    
    # Time-based schedule format: "HH:MM activity" or "period：activity"
    # Try period-based matching first (上午/中午/下午/晚上/深夜)
    periods = _THEME_PERIODS[theme]
    parts = re.split(
        r'(?=[\U0001F000-\U0001FFFF\U00002600-\U000027BF\U00002700-\U000027BF]|☕|🖼|🎹|🎵|🎶|💃|🎭|📚|🎨|🛒|🍰|☀️|🌙|🌅|🌇|🌆|🌃|🌤️)',
        schedule_prompt or schedule
    )
    for p in periods:
        for part in parts:
            part = part.strip()
            if not part:
                continue
            cleaned = re.sub(r'^[\U0001F000-\U0001FFFF\U00002600-\U000027BF\U0000FE0F\s]+', '', part).strip()
            if cleaned.startswith(p + "：") or cleaned.startswith(p + ":"):
                activity = re.sub(r'^[^：:]*[：:]\s*', '', cleaned).strip()
                if activity:
                    style_hint = _extract_style_hint(outfit_info)
                    ctx = f"Today's plan: {activity}"
                    if style_hint:
                        ctx += f". Style: {style_hint}"
                    print(f"📋 Schedule context: {ctx}", file=sys.stderr)
                    return ctx, activity, outfit_kw, "", ""
    
    # Time-based matching: "HH:MM activity" format
    # Theme → hour ranges
    _THEME_HOURS = {
        "morning": (6, 11),
        "noon": (12, 17),
        "evening": (18, 20),
        "bedtime": (21, 23),  # 21-23 晚上
        "bedtime_late": (0, 5),  # 0-5 凌晨
    }
    hour_min, hour_max = _THEME_HOURS.get(theme, (0, 0))
    # bedtime 包含凌晨 0-5 点
    from datetime import datetime
    now = datetime.now()
    # 优先用 schedule_time_override 的精确时间
    if schedule_time_override:
        _tm = re.match(r'(\d{1,2}):(\d{2})', schedule_time_override.strip())
        if _tm:
            now = now.replace(hour=int(_tm.group(1)), minute=int(_tm.group(2)))
    if theme == "bedtime":
        hour_min, hour_max = 21, 23  # 晚上
        # 凌晨 0-5 点单独处理
        if 0 <= now.hour < 6:
            hour_min, hour_max = 0, 5
    
    # Split schedule into time slots: "HH:MM activity" → [(hour, min, activity), ...]
    prompt_schedule = schedule_prompt or schedule
    times = re.findall(r'(\d{1,2}):(\d{2})', prompt_schedule)
    parts = re.split(r'\d{1,2}:\d{2}\s*', prompt_schedule)
    display_times = re.findall(r'(\d{1,2}):(\d{2})', schedule)
    display_parts = re.split(r'\d{1,2}:\d{2}\s*', schedule)
    candidates = []
    now_minutes = now.hour * 60 + now.minute

    # parts[0] is empty (before first time), parts[1:] are activities
    for (h_str, m_str), activity in zip(times, parts[1:]):
        hour = int(h_str)
        minute = int(m_str)
        if hour_min <= hour <= hour_max:
            activity = activity.strip().rstrip('～').strip()
            if activity:
                slot_minutes = hour * 60 + minute
                candidates.append((abs(slot_minutes - now_minutes), slot_minutes, h_str, m_str, activity))

    if candidates:
        _, _, h_str, m_str, activity = min(candidates, key=lambda item: (item[0], item[1]))
        raw_time = f"{int(h_str):02d}:{int(m_str):02d}"
        time_constraint = _schedule_time_constraint(raw_time)
        detail = _schedule_detail_for_time(detail_by_time.get(raw_time), raw_time)
        prompt_activity = _schedule_detail_text(detail) or activity
        detail_outfit_kw, detail_scene_kw, detail_hair_kw = _schedule_detail_keywords(detail, outfit_kw)
        detail_scene_kw = _append_time_constraint(detail_scene_kw, time_constraint)
        style_hint = _extract_style_hint(outfit_info)
        display_activity = activity
        for (dh, dm), d_activity in zip(display_times, display_parts[1:]):
            if int(dh) == int(h_str) and int(dm) == int(m_str):
                display_activity = d_activity.strip().rstrip('～').strip() or activity
                break
        ctx = f"Today's plan: {prompt_activity}"
        if time_constraint:
            ctx += f". Time: {time_constraint}"
        if style_hint:
            ctx += f". Style: {style_hint}"
        print(f"📋 Schedule context: {ctx}", file=sys.stderr)
        # Return both context (for prompt) and raw time slot (for display)
        return ctx, f"{raw_time} {display_activity}", detail_outfit_kw, detail_scene_kw, detail_hair_kw
    # If we get here, no time slot matched - return empty
    return "", "", "", "", ""


def generate(
    theme: str,
    engine: str = "gptimage",
    caption: bool = False,
    prompt_override: Optional[str] = None,
    enhance: bool = False,
    send: bool = False,
    style: Optional[str] = None,
    source: str = "chat",
    ref_image: Optional[str] = None,
    size: Optional[str] = None,
    schedule_time: str = "",
    schedule_detail_json: str = "",
    prompt_final: bool = False,
    no_auto_style: bool = False,
):
    # If user didn't specify a hairstyle, let LLM pick one
    if prompt_override and not prompt_final and engine == "gptimage" and theme != "sexy":
        hair_keywords = {"马尾", "辫", "丸子头", "双马尾", "编发", "披肩", "散发", "盘发",
                         "ponytail", "braid", "bun", "tails", "updo", "half-up"}
        if not any(kw in prompt_override.lower() for kw in hair_keywords):
            llm_hair = _decide_hairstyle(prompt_override)
            if llm_hair:
                prompt_override = f"{llm_hair}, {prompt_override}"
                print(f"💇 LLM chose hairstyle: {llm_hair}", file=sys.stderr)

    allow_random_pool = theme == "custom" and not prompt_override and not prompt_final

    if prompt_final and prompt_override:
        resolved_prompt = prompt_override
    else:
        resolved_prompt = resolve_prompt(
            theme,
            prompt_override,
            enhance,
            allow_random_pool=allow_random_pool,
        )

    # Inject daily schedule context for timed photos (not custom/sexy)
    schedule_detail_override = _clean_schedule_detail_override(schedule_detail_json)
    schedule_time_constraint = _schedule_time_constraint(schedule_time)
    schedule_ctx, schedule_raw, outfit_kw, scene_kw, hair_kw = _get_schedule_context(
        theme,
        schedule_time,
        schedule_detail_override,
    )
    schedule_activity = ""
    if schedule_ctx and theme in DAILY_THEMES and not prompt_final:
        # Extract activity text for schedule-aware prompt building
        import re
        m = re.search(r"Today's plan:\s*(.+?)(?:\.\s*(?:Time|Style):|$)", schedule_ctx)
        if m:
            schedule_activity = m.group(1).strip()
        resolved_prompt = f"{resolved_prompt}. {schedule_ctx}"
        print(f"📋 Injected schedule into prompt (activity: {schedule_activity})", file=sys.stderr)
    
    # Re-build prompt with schedule-aware element selection if we have activity
    if schedule_activity and theme in DAILY_THEMES and not prompt_final:
        if scene_kw:
            print(f"🏠 Using LLM slot scene details: {scene_kw[:60]}", file=sys.stderr)
        if hair_kw:
            print(f"💇 Using LLM slot hairstyle: {hair_kw[:60]}", file=sys.stderr)
        resolved_prompt = build_prompt(theme, schedule_activity=schedule_activity,
                                       outfit_keywords=outfit_kw,
                                       scene_keywords=scene_kw,
                                       hair_keywords=hair_kw,
                                       time_constraint=schedule_time_constraint)
        resolved_prompt = f"{resolved_prompt}. {schedule_ctx}"
        print(f"🎨 Rebuilt prompt from LLM schedule line (outfit_kw={outfit_kw[:40]})", file=sys.stderr)
    elif theme in DAILY_THEMES and not prompt_final and not prompt_override:
        print(
            f"ERROR: missing LLM schedule context for daily theme={theme}; refusing random prompt pool",
            file=sys.stderr,
        )
        return None

    if not resolved_prompt:
        print(f"ERROR: prompt is empty for theme={theme}; generation aborted", file=sys.stderr)
        return None

    # Resolve style to ref_image path (only supported by gptimage engine)
    requested_ref_image = ref_image
    auto_style = None
    explicit_style = style  # remember if user explicitly set --style
    if style:
        if engine != "gptimage":
            print(f"ERROR: --style 需要 --engine gptimage (当前引擎: {engine})", file=sys.stderr)
            return None
        ref_image = requested_ref_image or STYLE_REF_MAP.get(style)
        if not ref_image:
            print(f"⚠️ style '{style}' 参考图不存在，将使用纯文生图", file=sys.stderr)

    # Auto style classification is label-only. The app-level reference profile
    # selector is responsible for choosing actual image-to-image references.
    custom_like_source = source in {"custom", "hermes_api"}
    if engine == "gptimage" and not no_auto_style and not explicit_style and not requested_ref_image and theme != "sexy":
        classify_input = prompt_override or resolved_prompt
        auto_style = _classify_style(classify_input)
        ref_image = None
        if auto_style:
            label_only = " label-only" if custom_like_source else " label-only; no hardcoded ref"
            print(f"🧠 LLM selected style{label_only}: {auto_style}", file=sys.stderr)
    elif requested_ref_image:
        ref_image = requested_ref_image

    # Track the actual style used (explicit or auto) for filename and metadata
    actual_style = explicit_style or auto_style

    used_model = ""
    if theme == "sexy":
        path = generate_with_gitee(
            theme,
            send=False,
            caption=False,
            prompt_override=resolved_prompt,
            prompt_is_final=True,
            source=source,
            sync_gallery=False,
            schedule_time=schedule_raw,
        )
        if path:
            used_model = GITEE_MODEL_NAME
    elif engine == "gptimage":
        path = generate_with_gptimage(
            theme,
            send=False,
            caption=False,
            prompt_override=resolved_prompt,
            ref_image=ref_image,
            size=size,
            style=actual_style,
            prompt_is_final=True,
            source=source,
            sync_gallery=False,
            schedule_time=schedule_raw,
        )
        if path:
            used_model = GPTIMAGE_DIRECT_MODEL
        if not path:
            if _gitee_fallback_enabled():
                print("GPT Image failed, falling back to Gitee", file=sys.stderr)
                path = generate_with_gitee(
                    theme,
                    send=False,
                    caption=False,
                    prompt_override=resolved_prompt,
                    prompt_is_final=True,
                    source=source,
                    sync_gallery=False,
                    schedule_time=schedule_raw,
                )
                if path:
                    used_model = GITEE_MODEL_NAME
            else:
                print("GPT Image failed; Gitee fallback is disabled", file=sys.stderr)
    elif engine == "gemini":
        path = _generate_with_gemini_cpa(theme, resolved_prompt)
        if path:
            used_model = _GEMINI_CPA_MODEL
        if not path:
            if _gitee_fallback_enabled():
                print("Gemini CPA failed, falling back to Gitee", file=sys.stderr)
                path = generate_with_gitee(
                    theme,
                    send=False,
                    caption=False,
                    prompt_override=resolved_prompt,
                    prompt_is_final=True,
                    source=source,
                    sync_gallery=False,
                    schedule_time=schedule_raw,
                )
                if path:
                    used_model = GITEE_MODEL_NAME
            else:
                print("Gemini CPA failed; Gitee fallback is disabled", file=sys.stderr)
    else:  # engine == "gitee"
        path = generate_with_gitee(
            theme,
            send=False,
            caption=False,
            prompt_override=resolved_prompt,
            prompt_is_final=True,
            source=source,
            sync_gallery=False,
            schedule_time=schedule_raw,
        )
        if path:
            used_model = GITEE_MODEL_NAME
        if not path:
            print("Gitee failed, falling back to GPT Image", file=sys.stderr)
            path = generate_with_gptimage(
                theme,
                send=False,
                caption=False,
                prompt_override=resolved_prompt,
                ref_image=ref_image,
                size=size,
                style=actual_style,
                prompt_is_final=True,
                source=source,
                sync_gallery=False,
                schedule_time=schedule_raw,
            )
            if path:
                used_model = GPTIMAGE_DIRECT_MODEL

    caption_text = None
    if path and caption:
        caption_text = build_caption_for_image(theme, path, schedule_time=schedule_raw)
        if caption_text:
            if send:
                send_photo(path, caption_text)
            print(f"CAPTION:{caption_text}")

    # Sync to Docker portrait gallery
    if path:
        from core import sync_to_gallery
        sync_to_gallery(path, os.path.basename(path), theme, actual_style,
                        prompt=prompt_override or resolved_prompt,
                        caption=caption_text or "",
                        model_name=used_model,
                        source=source,
                        schedule_time=schedule_raw,
                        outfit_style=_gallery_outfit_style_for_source(source, actual_style))

    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="聊天生图主入口")
    parser.add_argument("--theme", choices=ALL_THEMES, default=None)
    parser.add_argument("--engine", choices=["gitee", "gemini", "gptimage"], default="gptimage", help="默认 GPT Image，失败自动降级")
    parser.add_argument("--caption", action="store_true")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--prompt", type=str, default=None, help="自定义描述（自动注入前缀+外貌）")
    parser.add_argument("--enhance", action="store_true", help="用 LLM 扩写描述后再自动注入")
    parser.add_argument("--style", choices=["cool", "girly", "sweet"], default=None, help="风格底模: cool(冷御风)/girly(少女风)/sweet(甜妹风), 仅 gptimage 引擎支持")
    parser.add_argument("--source", choices=["cron", "web", "chat", "custom", "hermes_api"], default="chat", help="来源标识: cron(定时)/web(现在在干嘛)/chat(聊天生图)/custom(自定义)/hermes_api(Hermes API)")
    parser.add_argument("--ref-image", type=str, default=None, help="参考图本地路径（图生图/img2img 模式）")
    parser.add_argument("--size", type=str, default=None, help="图片尺寸")
    parser.add_argument("--schedule-time", type=str, default="", help="定时任务对应的日程时间和活动，如 '20:30 晚间直播'")
    parser.add_argument("--schedule-detail-json", type=str, default="", help="当前日程推断明细 JSON，用于即时生图")
    parser.add_argument("--prompt-final", action="store_true", help="prompt 已是完整生图提示词，不再注入画质/人设/发型")
    parser.add_argument("--no-auto-style", action="store_true", help="不自动选择底模参考图，用于纯文/纯图生图")
    args = parser.parse_args()

    effective_theme = args.theme or ("custom" if args.prompt else "morning")

    path = generate(
        effective_theme,
        args.engine,
        args.caption,
        args.prompt,
        args.enhance,
        args.send,
        args.style,
        source=args.source,
        ref_image=args.ref_image,
        size=args.size,
        schedule_time=args.schedule_time,
        schedule_detail_json=args.schedule_detail_json,
        prompt_final=args.prompt_final,
        no_auto_style=args.no_auto_style,
    )
    if not path:
        print("ERROR: generation failed", file=sys.stderr)
        sys.exit(1)
    print(f"SUCCESS:{path}")
