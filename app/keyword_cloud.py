"""Keyword cloud extraction for user-entered image-generation prompts."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from group_chat import GroupChatStore
from store import ScheduleStore

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
DEFAULT_CLOUD_LIMIT = 48
MAX_SCHEDULE_CLOUD_TERMS = 5
MIN_SCHEDULE_CLOUD_POOL = 12
USER_PROMPT_FIELDS = (
    "custom_prompt",
    "user_prompt",
    "original_prompt",
    "input_prompt",
    "request_prompt",
    "raw_prompt",
    "raw_user_prompt",
)

SOURCE_LABELS = {
    "hermes_api": "Hermes",
    "hermes": "Hermes",
    "openclaw": "OpenClaw",
    "favorite_wardrobe": "收藏衣柜",
    "custom": "自定义",
    "custom_ui": "自定义",
    "web": "今日生图",
    "cron": "日程生图",
    "chat": "聊天生图",
    "character": "角色生图",
    "character_reference": "角色设定图",
    "group_photo": "群聊合照",
    "group_chat": "群聊生图",
    "gallery": "画廊",
    "recovered": "历史恢复",
    "import": "导入",
}

EN_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "background",
    "beautiful",
    "body",
    "camera",
    "captured",
    "chinese",
    "cinematic",
    "clear",
    "delicate",
    "face",
    "features",
    "female",
    "flawless",
    "girl",
    "high",
    "image",
    "in",
    "inside",
    "is",
    "light",
    "lighting",
    "look",
    "looking",
    "masterpiece",
    "natural",
    "of",
    "on",
    "photo",
    "photography",
    "photorealistic",
    "portrait",
    "quality",
    "realistic",
    "resolution",
    "scene",
    "she",
    "skin",
    "smartphone",
    "soft",
    "subject",
    "the",
    "this",
    "ultra",
    "wearing",
    "with",
}

ZH_STOPWORDS = {
    "照片",
    "图片",
    "生图",
    "画面",
    "角色",
    "女孩",
    "中国女孩",
    "写实",
    "高清",
    "高质量",
}

REJECT_MARKERS = (
    "18-year",
    "year-old",
    "breast",
    "bust",
    "cleavage",
    "hourglass",
    "slim waist",
    "skin texture",
    "doll-like",
    "natural breasts",
    "realistic body",
    "soft tissue",
    "another view",
    "artifacts",
    "blemishes",
    "bright unedited daylight",
    "breathing room",
    "camera flash",
    "casual lighting",
    "cinematic rim lighting",
    "clean face",
    "cropped face",
    "extreme face crops",
    "flash off",
    "framing",
    "glowing dust",
    "harsh natural sunlight",
    "hyper realistic",
    "intimate atmosphere",
    "landscape or wide canvas",
    "masterpiece clarity",
    "oversized heads",
    "passport-photo",
    "smartphone camera",
    "smudges",
    "soft shadows",
    "volumetric rays",
    "warm smartphone flash",
)

PROMPT_SECTION_PATTERNS = (
    r"she is wearing\s+(.+?)(?:\.\s|$|background:|lighting:)",
    r"wearing\s+(.+?)(?:\.\s|$|background:|lighting:)",
    r"background:\s+(.+?)(?:\.\s|$|lighting:|today's plan:)",
    r"the scene is\s+(.+?)(?:\.\s|$)",
    r"set in\s+(.+?)(?:\.\s|$)",
    r"inside\s+(.+?)(?:\.\s|$)",
)

SPLIT_RE = re.compile(r"[,，、;；\n/|]+|\s+\band\b\s+", re.IGNORECASE)
USER_PROMPT_SPLIT_RE = re.compile(r"[,，、;；\n/|]+|\s+\band\b\s+|[。.!！?？]+", re.IGNORECASE)
WARDROBE_SPLIT_RE = re.compile(r"[,，、;；\n/|]+|[。.!！?？]+|\s+\band\b\s+", re.IGNORECASE)
LEADING_ARTICLE_RE = re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE)
JK_ALIAS_RE = re.compile(r"\bjk\b|jk\s*uniform|jk\s*outfit|jk制服", re.IGNORECASE)


def build_keyword_cloud_payload(data_dir: str, limit: int = DEFAULT_CLOUD_LIMIT) -> dict:
    """Return high-frequency keywords from raw user-entered generation prompts."""
    limit = _bounded_limit(limit)
    counter: Counter[str] = Counter()
    source_counts: dict[str, Counter[str]] = defaultdict(Counter)
    scanned_entries = 0
    used_entries = 0

    for entry in _iter_generation_entries(data_dir):
        scanned_entries += 1
        source = _normalize_source(entry.get("source"), entry.get("image_filename") or entry.get("filename"))
        terms = _entry_terms(entry)
        if not terms:
            continue
        used_entries += 1
        for term in terms:
            counter[term] += 1
            source_counts[term][source] += 1

    if not counter:
        return {
            "keywords": [],
            "total": 0,
            "entry_count": used_entries,
            "scanned_count": scanned_entries,
            "basis": "user_input_and_favorite_wardrobe",
            "updated_at": _utc_now(),
        }

    max_count = max(counter.values()) or 1
    min_count = 2 if used_entries >= 12 else 1
    ranked = [
        (term, count)
        for term, count in counter.most_common()
        if count >= min_count
    ]
    if len(ranked) < min(12, len(counter)):
        ranked = counter.most_common()

    keywords = []
    for term, count in ranked[:limit]:
        source_items = [
            {
                "source": source,
                "label": SOURCE_LABELS.get(source, source or "画廊"),
                "count": source_count,
            }
            for source, source_count in source_counts[term].most_common(4)
        ]
        keywords.append({
            "text": term,
            "count": count,
            "weight": round(count / max_count, 3),
            "sources": source_items,
        })

    return {
        "keywords": keywords,
        "total": len(ranked),
        "entry_count": used_entries,
        "scanned_count": scanned_entries,
        "basis": "user_input_and_favorite_wardrobe",
        "updated_at": _utc_now(),
    }


def build_schedule_keyword_prompt_block(
    data_dir: str,
    limit: int = MAX_SCHEDULE_CLOUD_TERMS,
    selection_key: str = "",
) -> str:
    """Build a low-weight, rotating keyword hint for daily schedule generation."""
    selected_limit = max(1, min(int(limit or 1), MAX_SCHEDULE_CLOUD_TERMS))
    pool_limit = min(
        DEFAULT_CLOUD_LIMIT,
        max(MIN_SCHEDULE_CLOUD_POOL, selected_limit * 3),
    )
    payload = build_keyword_cloud_payload(data_dir, limit=pool_limit)
    keywords = payload.get("keywords") or []
    if not keywords:
        return "（暂无历史用户输入与收藏衣柜词云）"

    candidates = keywords[:pool_limit]
    if selection_key:
        candidates = sorted(
            candidates,
            key=lambda item: hashlib.sha256(
                f"{selection_key}\0{str(item.get('text') or '').casefold()}".encode("utf-8")
            ).digest(),
        )
    selected = candidates[:selected_limit]
    top_terms = "、".join(
        str(item.get("text") or "").strip()
        for item in selected
        if item.get("text")
    )
    if not top_terms:
        return "（暂无历史用户输入与收藏衣柜词云）"
    return (
        "这些词来自历史用户手动输入和收藏衣柜，只是低权重的软偏好参考候选，不是任务，也不要求命中。"
        "今天最多自然采用 1 个，也可以全部忽略。真实日历、当天合理性、近期去重、禁词和不喜欢反馈优先；"
        "不要为了使用词云改变日程，不要复刻旧场景或完整穿搭，同一个词不要连续多日使用。\n"
        f"可选灵感：{top_terms}"
    )


def _iter_generation_entries(data_dir: str) -> Iterable[dict]:
    schedule_data = _load_schedule_data(data_dir)
    metadata = _load_json(os.path.join(data_dir, "image_metadata.json"))
    if not isinstance(metadata, dict):
        metadata = {}

    seen_filenames: set[str] = set()
    if isinstance(schedule_data, dict):
        for key, raw_entry in schedule_data.items():
            if key == "_meta" or not isinstance(raw_entry, dict):
                continue
            filename = str(raw_entry.get("image_filename") or "").strip()
            if not filename and _looks_image_filename(str(key)):
                filename = str(key)
            if not filename:
                continue
            if str(raw_entry.get("status") or "ok").strip().lower() not in {"", "ok", "done"}:
                continue
            meta = metadata.get(filename) if isinstance(metadata.get(filename), dict) else {}
            entry = _merge_entry(raw_entry, meta)
            entry["image_filename"] = filename
            seen_filenames.add(filename)
            yield entry

    for filename, meta in metadata.items():
        if not isinstance(filename, str) or filename in seen_filenames:
            continue
        if not _looks_image_filename(filename) or not isinstance(meta, dict):
            continue
        entry = dict(meta)
        entry["image_filename"] = filename
        seen_filenames.add(filename)
        yield entry

    try:
        chat_data = GroupChatStore(data_dir).load()
    except Exception as exc:
        logger.debug("Load group chat for keyword cloud failed: %s", exc)
        chat_data = {}
    messages_by_room = chat_data.get("messages") if isinstance(chat_data, dict) else {}
    if isinstance(messages_by_room, dict):
        for messages in messages_by_room.values():
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict) or message.get("type") != "image":
                    continue
                meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
                filename = str(
                    meta.get("image_filename")
                    or message.get("image_filename")
                    or ""
                ).strip()
                if filename and filename in seen_filenames:
                    continue
                entry = dict(meta)
                entry.setdefault("source", "group_chat")
                for field in USER_PROMPT_FIELDS:
                    value = meta.get(field) or message.get(field)
                    if value:
                        entry.setdefault(field, value)
                if filename:
                    entry["image_filename"] = filename
                    seen_filenames.add(filename)
                yield entry

    for entry in _iter_favorite_wardrobe_entries(data_dir):
        filename = str(entry.get("image_filename") or "").strip()
        if filename and filename in seen_filenames:
            continue
        if filename:
            seen_filenames.add(filename)
        yield entry


def _iter_favorite_wardrobe_entries(data_dir: str) -> Iterable[dict]:
    data = _load_json(os.path.join(data_dir, "favorite_outfits.json"))
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        outfit = item.get("outfit") if isinstance(item.get("outfit"), dict) else {}
        outfit_keywords = str(item.get("outfit_keywords") or "").strip()
        if not outfit and not outfit_keywords:
            continue
        wardrobe = item.get("wardrobe_image") if isinstance(item.get("wardrobe_image"), dict) else {}
        yield {
            "id": item.get("id", ""),
            "source": "favorite_wardrobe",
            "favorite_outfit": True,
            "outfit": outfit,
            "outfit_style": item.get("outfit_style") or outfit.get("风格") or "",
            "outfit_keywords": outfit_keywords,
            "created_at": item.get("created_at", 0),
            "image_filename": wardrobe.get("filename", ""),
        }


def _load_schedule_data(data_dir: str) -> dict:
    try:
        data = ScheduleStore(data_dir).load()
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.debug("Load schedule store for keyword cloud failed: %s", exc)
        return {}


def _load_json(path: str) -> Any:
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.debug("Load keyword cloud json failed: %s (%s)", path, exc)
        return {}


def _merge_entry(entry: dict, meta: dict) -> dict:
    merged = dict(meta or {})
    merged.update(entry or {})
    for field in (
        "prompt",
        *USER_PROMPT_FIELDS,
        "outfit_keywords",
        "scene_keywords",
        "display_outfit",
        "outfit_description",
        "reference_query",
    ):
        entry_value = str((entry or {}).get(field) or "")
        meta_value = str((meta or {}).get(field) or "")
        if meta_value and (not entry_value or len(meta_value) > len(entry_value)):
            merged[field] = meta_value
    if (meta or {}).get("source") == "hermes_api":
        merged["source"] = "hermes_api"
    return merged


def _entry_terms(entry: dict) -> set[str]:
    terms: list[str] = []
    source = _normalize_source(entry.get("source"), entry.get("image_filename") or entry.get("filename"))
    if source == "favorite_wardrobe" or entry.get("favorite_outfit"):
        terms.extend(_terms_from_favorite_outfit(entry))
    else:
        for field in USER_PROMPT_FIELDS:
            terms.extend(_terms_from_user_prompt_value(entry.get(field)))
        if not terms:
            terms.extend(_terms_from_prompt_fallback(entry.get("prompt")))
    result = []
    seen = set()
    for term in terms:
        cleaned = _clean_term(term)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return set(result[:24])


def _terms_from_favorite_outfit(entry: dict) -> list[str]:
    outfit = entry.get("outfit") if isinstance(entry.get("outfit"), dict) else {}
    terms: list[str] = []

    terms.extend(_terms_from_value(entry.get("outfit_keywords")))
    style = str(entry.get("outfit_style") or outfit.get("风格") or "").strip()
    if style:
        terms.extend(_terms_from_value(style))

    for field in ("风格", "发型", "穿搭"):
        value = outfit.get(field)
        terms.extend(_terms_from_wardrobe_text(value))
    return terms


def _terms_from_wardrobe_text(value: Any) -> list[str]:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return []
    terms: list[str] = _keyword_aliases(text)
    for part in WARDROBE_SPLIT_RE.split(text):
        cleaned = _clean_wardrobe_phrase(part)
        if cleaned:
            terms.append(cleaned)
    return terms


def _clean_wardrobe_phrase(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.strip(" \t\r\n\"'“”‘’`*_#[](){}<>:：.-")
    if not text:
        return ""
    if re.match(r"^(?:整体|材质|廓形|亮点|细节亮点|色系|氛围)", text):
        return ""
    text = re.sub(
        r"^(?:上身|上装|下装|下身|脚上|脚踩|脚穿|手腕|颈间|脖子上|发间|两侧|领口|袖口|裤脚|裙摆|外搭|内搭)\s*",
        "",
        text,
    )
    text = re.sub(
        r"^(?:穿着|穿一件|穿|搭配一条|搭配一双|搭配|配上一双|配一双|踩着|踩|戴着|戴一串|戴一条|戴一只|戴|佩戴一条|佩戴|系着|饰有|有|是|一件|一条|一双|一串|一只|一枚|一个)\s*",
        "",
        text,
    )
    text = text.strip(" \t\r\n\"'“”‘’`*_#[](){}<>:：.-")
    if len(text) > 18 and _contains_cjk(text):
        chunks = re.findall(
            r"[\u4e00-\u9fffA-Za-z0-9]+(?:吊带背心|吊带|背心|开衫|针织衫|衬衫|上衣|短裙|中长裙|百褶裙|A字裙|短裤|长裤|拖鞋|玛丽珍鞋|袜|丝袜|手链|锁骨链|项链|丝带|缎带|蝴蝶结|发夹|发簪|马尾|丸子头|低盘发|麻花辫|蕾丝|荷叶边|针织|真丝|缎面|丝绒|开叉|泡泡袖)",
            text,
        )
        if chunks:
            return chunks[0]
    return text


def _terms_from_user_prompt_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        terms = []
        for item in value:
            terms.extend(_terms_from_user_prompt_value(item))
        return terms
    if isinstance(value, dict):
        terms = []
        for field in USER_PROMPT_FIELDS:
            if field in value:
                terms.extend(_terms_from_user_prompt_value(value.get(field)))
        return terms

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return []

    terms = _keyword_aliases(text)
    for pattern in PROMPT_SECTION_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.DOTALL):
            terms.extend(_terms_from_value(match.group(1)))
    terms.extend(part.strip() for part in USER_PROMPT_SPLIT_RE.split(text) if part.strip())
    return terms


def _terms_from_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        terms = []
        for item in value:
            terms.extend(_terms_from_value(item))
        return terms
    if isinstance(value, dict):
        terms = []
        for item in value.values():
            if isinstance(item, (str, list, tuple, set)):
                terms.extend(_terms_from_value(item))
        return terms

    text = str(value or "").strip()
    if not text:
        return []
    return [part.strip() for part in SPLIT_RE.split(text) if part.strip()]


def _terms_from_promptish_value(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []

    sections = []
    for pattern in PROMPT_SECTION_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.DOTALL):
            sections.append(match.group(1))

    if not sections and _contains_cjk(text):
        sections = re.split(r"[，。；;、\n]+", text)

    terms = _keyword_aliases(text)
    for section in sections:
        terms.extend(_keyword_aliases(section))
        terms.extend(_terms_from_value(section))
    return terms


def _terms_from_prompt_fallback(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    aliases = _keyword_aliases(text)
    if _looks_generated_prompt(text):
        return aliases
    return aliases + _terms_from_promptish_value(text)


def _keyword_aliases(value: Any) -> list[str]:
    """Normalize compact preference names that are otherwise buried in long prompts."""
    text = str(value or "")
    lower = text.casefold()
    aliases: list[str] = []
    if (
        JK_ALIAS_RE.search(text)
        or ("水手服" in text and "百褶裙" in text)
        or ("sailor blouse" in lower and "pleated" in lower)
    ):
        aliases.append("JK制服")
    return aliases


def _looks_generated_prompt(value: Any) -> bool:
    lower = str(value or "").casefold()
    return any(
        marker in lower
        for marker in (
            "this image should look",
            "current scheduled scene",
            "use this schedule text",
            "the scheduled clock time",
            "today's plan:",
            "do not replace it with",
            "forbidden time mismatch",
        )
    )


def _clean_term(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.strip(" \t\r\n\"'“”‘’`*_#[](){}<>:：.-")
    text = LEADING_ARTICLE_RE.sub("", text).strip()
    text = re.sub(r"^(?:she is|she has|her hair is|background|lighting)\s+", "", text, flags=re.IGNORECASE).strip()
    text = text.strip(" \t\r\n\"'“”‘’`*_#[](){}<>:：.-")
    if not text:
        return ""

    lower = text.casefold()
    if lower in EN_STOPWORDS or text in ZH_STOPWORDS:
        return ""
    if lower.startswith(("avoid ", "no ")):
        return ""
    if any(marker in lower for marker in REJECT_MARKERS):
        return ""
    if re.search(r"\d\s*k|8k|4k|1080p", lower):
        return ""
    if not _contains_cjk(text) and not re.search(r"[a-zA-Z]", text):
        return ""

    if _contains_cjk(text):
        if len(text) < 2 or len(text) > 18:
            return ""
        if any(stop in text for stop in ZH_STOPWORDS):
            return ""
        return text

    words = re.findall(r"[a-zA-Z][a-zA-Z0-9'-]*", text)
    if not words:
        return ""
    if len(words) > 7:
        return ""
    meaningful = [word for word in words if word.casefold() not in EN_STOPWORDS]
    if not meaningful:
        return ""
    return " ".join(words).casefold()


def _normalize_source(source: Any, filename: Any = "") -> str:
    raw = re.sub(r"[\s_-]+", "_", str(source or "").strip().casefold())
    name = str(filename or "").strip().casefold()
    if "openclaw" in raw or "openclaw" in name:
        return "openclaw"
    if "hermes" in raw or name.startswith("hermes_"):
        return "hermes_api"
    if raw in {"custom", "custom_ui"} or "_custom_" in name or name.startswith("zhuzhu_custom"):
        return "custom"
    if raw in SOURCE_LABELS:
        return raw
    if raw in {"", "metadata"}:
        return "gallery"
    return raw


def _looks_image_filename(value: str) -> bool:
    return str(value or "").strip().casefold().endswith(IMAGE_EXTENSIONS)


def _contains_cjk(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(value or "")))


def _bounded_limit(value: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = DEFAULT_CLOUD_LIMIT
    return max(1, min(100, limit))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
