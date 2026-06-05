#!/usr/bin/env python3
"""Unified chat image generation entrypoint for zhuzhu-image-gen."""
import argparse
import json
import os
import random
import sys
from datetime import date
from typing import Optional

import requests

from core import build_caption, build_prompt, enhance_prompt, send_photo
from generate_gitee import generate as generate_with_gitee
from generate_gptimage import generate as generate_with_gptimage

# CPA endpoint for Gemini image generation
_GEMINI_CPA_URL = "http://127.0.0.1:8327/v1/chat/completions"
_GEMINI_CPA_MODEL = "gemini-3.1-flash-image"

DAILY_THEMES = {"morning", "noon", "evening", "bedtime"}
ALL_THEMES = sorted(DAILY_THEMES | {"sexy", "custom"})

# 风格底模参考图路径映射（用户可自行放入参考图到 references/ 目录）
_REF_DIR = os.path.join(os.path.dirname(__file__), "..", "references")
STYLE_REF_MAP = {}
for _style, _fname in [
    ("cool", "reference_face.jpg"),
    ("girly", "ref_style_girly.jpg"),
    ("sweet", "ref_style_sweet.jpg"),
]:
    _path = os.path.join(_REF_DIR, _fname)
    if os.path.isfile(_path):
        STYLE_REF_MAP[_style] = _path

_OPENCODE_API = "https://opencode.ai/zen/go/v1/chat/completions"


def _classify_style(prompt_text: str) -> str:
    """Use LLM to classify prompt into cool/girly/sweet based on vibe."""
    api_key = os.getenv("OPENCODE_API_KEY")
    if not api_key:
        from core import get_cpa_key
        api_key = get_cpa_key()
        if not api_key:
            return random.choice(["cool", "girly", "sweet"])

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

    try:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        payload = {
            "model": "deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt_text[:500]},
            ],
            "max_tokens": 50,
            "temperature": 0.1,
        }
        resp = requests.post(_OPENCODE_API, headers=headers, json=payload, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            msg = data["choices"][0]["message"]
            # deepseek 推理模型有时把输出放 reasoning_content 而非 content
            raw = (msg.get("content") or "").strip().lower()
            if not raw:
                raw = (msg.get("reasoning_content") or "").strip().lower()
            # Extract the style word from the response
            for word in ("cool", "girly", "sweet"):
                if word in raw:
                    return word
    except Exception as e:
        print(f"[style_classify] LLM failed: {e}", file=sys.stderr)

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
    api_key = os.getenv("OPENCODE_API_KEY")
    if not api_key:
        from core import get_cpa_key
        api_key = get_cpa_key()
        if not api_key:
            return None

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

    try:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        payload = {
            "model": "mimo-v2.5",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt_text[:500]},
            ],
            "max_tokens": 200,
            "temperature": 0.2,
        }
        resp = requests.post(_OPENCODE_API, headers=headers, json=payload, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            msg = data["choices"][0]["message"]
            # Check content first, then fall back to reasoning_content
            raw = (msg.get("content") or msg.get("reasoning_content") or "").strip().lower()
            # Find which hairstyle appears first in the response
            best_idx = len(raw)
            best_h = None
            for h in _HAIRSTYLE_POOL:
                idx = raw.find(h)
                if idx != -1 and idx < best_idx:
                    best_idx = idx
                    best_h = h
            if best_h:
                return best_h
    except Exception as e:
        print(f"[hairstyle] LLM failed: {e}", file=sys.stderr)

    return None


def resolve_prompt(theme: str, prompt_override: Optional[str] = None, enhance: bool = False, schedule_activity: str = "") -> str:
    if not prompt_override:
        return build_prompt(theme, schedule_activity=schedule_activity)
    
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
    from core import get_cpa_key, save_image, update_metadata, sync_to_gallery

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {get_cpa_key()}",
    }
    payload = {
        "model": _GEMINI_CPA_MODEL,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
    }
    start = time.time()
    try:
        resp = requests.post(_GEMINI_CPA_URL, headers=headers, json=payload, timeout=180)
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
        sync_to_gallery(path, filename, theme, prompt=prompt, model_name=_GEMINI_CPA_MODEL, source="cron")
        return path

    except Exception as e:
        print(f"Gemini CPA failed: {e}", file=sys.stderr)
        return None


_SCHEDULE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "schedule_data.json")

# Theme → schedule period keywords
_THEME_PERIODS = {
    "morning": ["上午", "早上", "清晨"],
    "noon": ["中午", "下午", "上午"],
    "evening": ["晚上", "傍晚", "下午"],
    "bedtime": ["晚上", "深夜"],
    "sexy": ["晚上", "深夜"],
    "custom": [],
}


def _get_schedule_context(theme: str) -> tuple:
    """Read daily schedule and return (context_string, raw_time_slot, outfit_keywords, scene_keywords)."""
    if theme not in _THEME_PERIODS or not _THEME_PERIODS[theme]:
        return "", "", "", ""
    today_str = date.today().isoformat()
    if not os.path.exists(_SCHEDULE_PATH):
        return "", "", "", ""
    try:
        with open(_SCHEDULE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return "", "", "", ""
    
    # Search for schedule: first try date key, then scan all entries for today
    schedule = ""
    outfit_info = ""
    outfit_kw = ""
    scene_kw = ""
    daily = data.get(today_str)
    if daily and daily.get("schedule") and daily["schedule"] not in ("生成失败", ""):
        schedule = daily["schedule"]
        outfit_info = daily.get("outfit", "")
        outfit_kw = daily.get("outfit_keywords", "")
        scene_kw = daily.get("scene_keywords", "")
    
    # If date-keyed entry has no schedule, scan ALL entries for today
    if not schedule:
        for key, entry in data.items():
            if entry.get("date") == today_str and entry.get("schedule") and entry["schedule"] not in ("生成失败", ""):
                schedule = entry["schedule"]
                outfit_info = entry.get("outfit", "")
                outfit_kw = entry.get("outfit_keywords", "")
                scene_kw = entry.get("scene_keywords", "")
                break
    
    if not schedule:
        # Fallback: no schedule found, generate context from current time + theme
        from datetime import datetime
        now = datetime.now()
        time_str = f"{now.hour:02d}:{now.minute:02d}"
        
        _FALLBACK_ACTIVITIES = {
            "morning": ["晨间护肤routine", "喝咖啡看日出", "晨跑后拉伸放松", "做早餐中", "阳台看书晒太阳", "整理穿搭出门"],
            "noon": ["午后小憩", "咖啡厅办公", "和闺蜜约饭", "逛街shopping", "公园散步拍照", "喝下午茶吃甜点"],
            "evening": ["下班后放松时刻", "健身房运动", "弹琴唱歌", "做饭时间", "夜晚城市漫步", "居家追剧放松"],
            "bedtime": ["睡前护肤敷面膜", "窝在被窝看小说", "泡澡放松", "床头灯下看书", "深夜emo时间", "和主人说晚安"],
        }
        import random as _rnd
        activity = _rnd.choice(_FALLBACK_ACTIVITIES.get(theme, ["日常活动"]))
        ctx = f"Today's plan: {activity}"
        raw_slot = f"{time_str} {activity}"
        print(f"📋 Schedule fallback: {ctx}", file=sys.stderr)
        return ctx, raw_slot, "", ""
    
    import re
    # Time-based schedule format: "HH:MM activity" or "period：activity"
    # Try period-based matching first (上午/中午/下午/晚上/深夜)
    periods = _THEME_PERIODS[theme]
    parts = re.split(
        r'(?=[\U0001F000-\U0001FFFF\U00002600-\U000027BF\U00002700-\U000027BF]|☕|🖼|🎹|🎵|🎶|💃|🎭|📚|🎨|🛒|🍰|☀️|🌙|🌅|🌇|🌆|🌃|🌤️)',
        schedule
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
                    style_hint = ""
                    if outfit_info:
                        m = re.search(r'风格[：:]\s*(\S+)', outfit_info)
                        if m:
                            style_hint = m.group(1)
                    ctx = f"Today's plan: {activity}"
                    if style_hint:
                        ctx += f". Style: {style_hint}"
                    print(f"📋 Schedule context: {ctx}", file=sys.stderr)
                    return ctx, activity, outfit_kw, scene_kw
    
    # Time-based matching: "HH:MM activity" format
    # Theme → hour ranges
    _THEME_HOURS = {
        "morning": (6, 11),
        "noon": (12, 13),
        "evening": (18, 21),
        "bedtime": (22, 23),
    }
    hour_min, hour_max = _THEME_HOURS.get(theme, (0, 0))
    
    # Split schedule into time slots: "HH:MM activity" → [(hour, min, activity), ...]
    times = re.findall(r'(\d{1,2}):(\d{2})', schedule)
    parts = re.split(r'\d{1,2}:\d{2}\s*', schedule)
    # parts[0] is empty (before first time), parts[1:] are activities
    for (h_str, m_str), activity in zip(times, parts[1:]):
        hour = int(h_str)
        if hour_min <= hour <= hour_max:
            activity = activity.strip().rstrip('～').strip()
            if activity:
                style_hint = ""
                if outfit_info:
                    m = re.search(r'风格[：:]\s*(\S+)', outfit_info)
                    if m:
                        style_hint = m.group(1)
                ctx = f"Today's plan: {activity}"
                if style_hint:
                    ctx += f". Style: {style_hint}"
                print(f"📋 Schedule context: {ctx}", file=sys.stderr)
                # Return both context (for prompt) and raw time slot (for display)
                return ctx, f"{h_str}:{m_str} {activity}", outfit_kw, scene_kw
    # If we get here, no time slot matched - return empty
    return "", "", "", ""


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
):
    # If user didn't specify a hairstyle, let LLM pick one
    if prompt_override and engine == "gptimage" and theme != "sexy":
        hair_keywords = {"马尾", "辫", "丸子头", "双马尾", "编发", "披肩", "散发", "盘发",
                         "ponytail", "braid", "bun", "tails", "updo", "half-up"}
        if not any(kw in prompt_override.lower() for kw in hair_keywords):
            llm_hair = _decide_hairstyle(prompt_override)
            if llm_hair:
                prompt_override = f"{llm_hair}, {prompt_override}"
                print(f"💇 LLM chose hairstyle: {llm_hair}", file=sys.stderr)

    resolved_prompt = resolve_prompt(theme, prompt_override, enhance)

    # Inject daily schedule context for timed photos (not custom/sexy)
    schedule_ctx, schedule_raw, outfit_kw, scene_kw = _get_schedule_context(theme)
    schedule_activity = ""
    if schedule_ctx and theme in DAILY_THEMES and not prompt_override:
        # Extract activity text for schedule-aware prompt building
        import re
        m = re.search(r"Today's plan:\s*(.+?)(?:\.|$)", schedule_ctx)
        if m:
            schedule_activity = m.group(1).strip()
        resolved_prompt = f"{resolved_prompt}. {schedule_ctx}"
        print(f"📋 Injected schedule into prompt (activity: {schedule_activity})", file=sys.stderr)
    
    # Re-build prompt with schedule-aware element selection if we have activity
    if schedule_activity and theme in DAILY_THEMES and not prompt_override:
        resolved_prompt = build_prompt(theme, schedule_activity=schedule_activity,
                                       outfit_keywords=outfit_kw, scene_keywords=scene_kw)
        resolved_prompt = f"{resolved_prompt}. {schedule_ctx}"
        print(f"🎨 Rebuilt prompt with schedule-matched elements (outfit_kw={outfit_kw[:40]}, scene_kw={scene_kw[:40]})", file=sys.stderr)

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

    # Auto-pick a style for GPT Image via LLM to keep face consistent
    # Note: LLM classification works even without reference images (for style label)
    if engine == "gptimage" and not explicit_style and not requested_ref_image and theme != "sexy":
        # Use the user's prompt (before appearance injection) for classification
        classify_input = prompt_override or resolved_prompt
        auto_style = _classify_style(classify_input)
        ref_image = STYLE_REF_MAP.get(auto_style)  # None if no ref images
        if auto_style:
            print(f"🧠 LLM selected style: {auto_style} (ref_image={'✓' if ref_image else '✗'})", file=sys.stderr)
    elif requested_ref_image:
        ref_image = requested_ref_image

    # Track the actual style used (explicit or auto) for filename and metadata
    actual_style = explicit_style or auto_style

    if theme == "sexy":
        path = generate_with_gitee(theme, send=False, caption=caption, prompt_override=resolved_prompt)
    elif engine == "gptimage":
        path = generate_with_gptimage(theme, send=False, caption=caption, prompt_override=resolved_prompt, ref_image=ref_image, size=size, style=actual_style)
        if not path:
            print("GPT Image failed, falling back to Gitee", file=sys.stderr)
            path = generate_with_gitee(theme, send=False, caption=caption, prompt_override=resolved_prompt)
    elif engine == "gemini":
        path = _generate_with_gemini_cpa(theme, resolved_prompt)
        if not path:
            print("Gemini CPA failed, falling back to Gitee", file=sys.stderr)
            path = generate_with_gitee(theme, send=False, caption=caption, prompt_override=resolved_prompt)
    else:  # engine == "gitee"
        path = generate_with_gitee(theme, send=False, caption=caption, prompt_override=resolved_prompt)
        if not path:
            print("Gitee failed, falling back to GPT Image", file=sys.stderr)
            path = generate_with_gptimage(theme, send=False, caption=caption, prompt_override=resolved_prompt, ref_image=ref_image, size=size, style=actual_style)

    caption_text = None
    if path and send:
        caption_text = build_caption(theme) if caption else None
        if caption_text:
            send_photo(path, caption_text)
            print(f"CAPTION:{caption_text}")

    # Sync to Docker portrait gallery
    if path:
        from core import sync_to_gallery
        # Determine which model was used based on path or engine
        used_model = ""
        if "gpt-image" in path or engine == "gptimage":
            used_model = "gpt-image-2"
        elif "z-image" in path or engine == "gitee":
            used_model = "z-image-turbo"
        elif "gemini" in path or engine == "gemini":
            used_model = "gemini-3.1-flash-image"
        sync_to_gallery(path, os.path.basename(path), theme, actual_style,
                        prompt=prompt_override or resolved_prompt,
                        caption=caption_text or "",
                        model_name=used_model,
                        source=source,
                        schedule_time=schedule_raw)

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
    parser.add_argument("--source", choices=["cron", "web", "chat"], default="chat", help="来源标识: cron(定时)/web(现在在干嘛)/chat(聊天生图)")
    parser.add_argument("--ref-image", type=str, default=None, help="参考图本地路径（图生图/img2img 模式）")
    parser.add_argument("--size", type=str, default=None, help="图片尺寸")
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
    )
    if not path:
        print("ERROR: generation failed", file=sys.stderr)
        sys.exit(1)
    print(f"SUCCESS:{path}")
