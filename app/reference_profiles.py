"""Reference image profiles and selection helpers."""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import requests

from settings import builtin_reference_map, llm_request_config

REFERENCE_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def reference_profiles_path(data_dir: str) -> str:
    return os.path.join(data_dir, "reference_profiles.json")


def _now() -> int:
    return int(time.time())


def _stable_id(prefix: str, filename: str) -> str:
    raw = f"{prefix}:{filename}"
    return f"{prefix}_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"


def _is_reference_image_file(filename: str) -> bool:
    return str(filename or "").lower().endswith(REFERENCE_IMAGE_EXTENSIONS)


def _read_store(data_dir: str) -> list[dict]:
    path = reference_profiles_path(data_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("items", [])
        return [item for item in data if isinstance(item, dict)]
    except Exception:
        return []


def _write_store(data_dir: str, items: list[dict]) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = reference_profiles_path(data_dir)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "items": items}, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _normalize_profile(item: dict) -> dict:
    filename = os.path.basename(str(item.get("filename") or "").strip())
    source = str(item.get("source") or ("default" if item.get("builtin") else "upload")).strip() or "upload"
    profile_id = str(item.get("id") or "").strip() or _stable_id(source, filename)
    profile = {
        "id": profile_id,
        "filename": filename,
        "url": str(item.get("url") or "").strip(),
        "label": str(item.get("label") or "").strip() or "参考图",
        "style": str(item.get("style") or "").strip(),
        "prompt": str(item.get("prompt") or "").strip(),
        "tags": [str(x).strip() for x in item.get("tags", []) if str(x).strip()] if isinstance(item.get("tags"), list) else [],
        "source": source,
        "builtin": bool(item.get("builtin")),
        "active": item.get("active") is not False,
        "analysis_status": str(item.get("analysis_status") or "").strip(),
        "analysis_error": str(item.get("analysis_error") or "").strip(),
        "created_at": int(item.get("created_at") or 0),
        "updated_at": int(item.get("updated_at") or 0),
    }
    if item.get("random_fallback") is True:
        profile["random_fallback"] = True
    return profile


def _default_profile_items(reference_dir: str, app_reference_dir: str) -> list[dict]:
    refs = []
    for filename, info in builtin_reference_map().items():
        url = ""
        for base_dir, prefix in ((reference_dir, "/local-refs"), (app_reference_dir, "/refs")):
            if base_dir and os.path.isfile(os.path.join(base_dir, filename)):
                url = f"{prefix}/{filename}"
                break
        if not url:
            continue
        refs.append({
            "id": _stable_id("default", filename),
            "filename": filename,
            "url": url,
            "label": info.get("label", ""),
            "style": info.get("style", ""),
            "prompt": info.get("prompt", ""),
            "tags": [info.get("style", ""), info.get("label", "")],
            "source": "default",
            "builtin": True,
            "active": True,
            "analysis_status": "seeded",
            "created_at": 0,
            "updated_at": 0,
        })
    return refs


def ensure_reference_profiles(
    data_dir: str,
    reference_dir: str,
    app_reference_dir: str,
    uploaded_reference_dir: str = "",
) -> list[dict]:
    items = [_normalize_profile(item) for item in _read_store(data_dir)]
    by_id = {item["id"]: item for item in items if item.get("id")}

    for default in _default_profile_items(reference_dir, app_reference_dir):
        existing = by_id.get(default["id"])
        if existing:
            existing.update({
                "filename": default["filename"],
                "url": default["url"],
                "label": existing.get("label") or default["label"],
                "style": existing.get("style") or default["style"],
                "prompt": existing.get("prompt") or default["prompt"],
                "tags": existing.get("tags") or default["tags"],
                "source": "default",
                "builtin": True,
                "active": existing.get("active") is not False,
            })
        else:
            by_id[default["id"]] = _normalize_profile(default)

    upload_dir = uploaded_reference_dir or os.path.join(reference_dir, "uploads")
    if os.path.isdir(upload_dir):
        for filename in sorted(os.listdir(upload_dir)):
            if not _is_reference_image_file(filename):
                continue
            profile_id = _stable_id("upload", filename)
            if profile_id in by_id:
                continue
            by_id[profile_id] = _normalize_profile({
                "id": profile_id,
                "filename": filename,
                "url": f"/local-refs/uploads/{filename}",
                "label": "自定义上传",
                "source": "upload",
                "builtin": False,
                "active": True,
                "analysis_status": "pending",
                "created_at": _now(),
                "updated_at": _now(),
            })

    items = list(by_id.values())
    items.sort(key=lambda item: (item.get("source") != "default", item.get("created_at") or 0, item.get("filename", "")))
    _write_store(data_dir, items)
    return items


def load_reference_profiles(
    data_dir: str,
    reference_dir: str,
    app_reference_dir: str,
    uploaded_reference_dir: str = "",
) -> list[dict]:
    return ensure_reference_profiles(data_dir, reference_dir, app_reference_dir, uploaded_reference_dir)


def upsert_reference_profile(data_dir: str, profile: dict) -> dict:
    profile = _normalize_profile(profile)
    profile["updated_at"] = _now()
    if not profile.get("created_at"):
        profile["created_at"] = profile["updated_at"]
    items = [_normalize_profile(item) for item in _read_store(data_dir)]
    replaced = False
    for idx, item in enumerate(items):
        if item.get("id") == profile["id"] or (
            item.get("source") == profile.get("source") and item.get("filename") == profile.get("filename")
        ):
            merged = dict(item)
            merged.update({k: v for k, v in profile.items() if v not in ("", [], None)})
            merged["active"] = profile.get("active") is not False
            items[idx] = _normalize_profile(merged)
            profile = items[idx]
            replaced = True
            break
    if not replaced:
        items.append(profile)
    _write_store(data_dir, items)
    return profile


def remove_reference_profile(data_dir: str, filename: str) -> None:
    filename = os.path.basename(str(filename or "").strip())
    if not filename:
        return
    items = [item for item in _read_store(data_dir) if os.path.basename(str(item.get("filename") or "")) != filename]
    _write_store(data_dir, [_normalize_profile(item) for item in items])


def reference_response(profile: dict) -> dict:
    profile = _normalize_profile(profile)
    result = {
        "id": profile.get("id", ""),
        "filename": profile.get("filename", ""),
        "url": profile.get("url", ""),
        "style": profile.get("style", "") or profile.get("source", "upload"),
        "label": profile.get("label", "") or "参考图",
        "builtin": bool(profile.get("builtin")),
        "source": profile.get("source", ""),
        "active": profile.get("active") is not False,
    }
    for key in ("prompt", "tags", "analysis_status", "analysis_error"):
        value = profile.get(key)
        if value not in ("", [], None):
            result[key] = value
    return result


def resolve_reference_profile_path(profile: dict, reference_dir: str, app_reference_dir: str) -> str:
    profile = _normalize_profile(profile)
    filename = profile.get("filename", "")
    candidates: list[Path] = []
    url = str(profile.get("url") or "")
    if url.startswith("/local-refs/"):
        rel = unquote(url.removeprefix("/local-refs/")).lstrip("/")
        candidates.append(Path(reference_dir) / rel)
    elif url.startswith("/refs/"):
        rel = unquote(url.removeprefix("/refs/")).lstrip("/")
        candidates.append(Path(app_reference_dir) / rel)
    source = profile.get("source", "")
    if source == "upload":
        candidates.append(Path(reference_dir) / "uploads" / filename)
    elif source == "wardrobe":
        candidates.append(Path(reference_dir) / "wardrobe" / filename)
    else:
        candidates.append(Path(reference_dir) / filename)
        candidates.append(Path(app_reference_dir) / filename)

    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
            if resolved.is_file() and _is_reference_image_file(str(resolved)):
                return str(resolved)
        except Exception:
            continue
    return ""


def _extract_json_object(text: str) -> dict:
    text = str(text or "").strip()
    if not text:
        return {}
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _post_llm(config: dict, data_dir: str, messages: list[dict], max_tokens: int = 500, temperature: float = 0.2) -> str:
    req = llm_request_config(config, data_dir)
    chat_url = req.get("chat_url", "")
    models = req.get("models", [])
    if not chat_url or not models:
        return ""
    headers = {"Content-Type": "application/json"}
    if req.get("api_key"):
        headers["Authorization"] = f"Bearer {req['api_key']}"
    for model in models:
        try:
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            resp = requests.post(chat_url, headers=headers, json=payload, timeout=45)
            if resp.status_code != 200:
                continue
            data = resp.json()
            choices = data.get("choices") if isinstance(data, dict) else []
            if not choices:
                continue
            msg = choices[0].get("message", {})
            content = (msg.get("content") or msg.get("reasoning_content") or "").strip()
            if content:
                return content
        except Exception:
            continue
    return ""


def analyze_reference_image(config: dict, data_dir: str, image_path: str, fallback_label: str = "自定义上传") -> dict:
    try:
        with open(image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("ascii")
    except Exception as e:
        return {"label": fallback_label, "prompt": "", "tags": [], "analysis_status": "failed", "analysis_error": str(e)}

    mime = mimetypes.guess_type(image_path)[0] or "image/png"
    system = (
        "You describe reference images for portrait image-to-image selection. "
        "Return only compact JSON. Do not invent identity. Focus on visible style, mood, face/hair/outfit cues, color palette, and what schedule/outfit styles this reference fits."
    )
    user_text = (
        "Analyze this reference image for future automatic matching. Return JSON with: "
        "label_cn (short Chinese label), prompt_en (one English sentence, 35-80 words), "
        "tags (6-12 short Chinese or English tags), usage (face/style/outfit/wardrobe)."
    )
    content = _post_llm(
        config,
        data_dir,
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                ],
            },
        ],
        max_tokens=700,
        temperature=0.1,
    )
    data = _extract_json_object(content)
    label = str(data.get("label_cn") or data.get("label") or fallback_label).strip() or fallback_label
    prompt = str(data.get("prompt_en") or data.get("prompt") or "").strip()
    tags = data.get("tags") if isinstance(data.get("tags"), list) else []
    tags = [str(tag).strip() for tag in tags if str(tag).strip()]
    if prompt:
        return {"label": label, "prompt": prompt, "tags": tags, "analysis_status": "ok", "analysis_error": ""}
    return {
        "label": label,
        "prompt": "",
        "tags": tags,
        "analysis_status": "failed",
        "analysis_error": "LLM did not return prompt",
    }


def _score_profile(context: str, profile: dict) -> int:
    haystack = " ".join([
        str(profile.get("label") or ""),
        str(profile.get("style") or ""),
        str(profile.get("prompt") or ""),
        " ".join(profile.get("tags") or []),
    ]).lower()
    context_lower = context.lower()
    score = 0
    for token in re.findall(r"[a-zA-Z]{3,}|[\u4e00-\u9fff]{2,}", context_lower):
        if token in haystack:
            score += 2 if re.search(r"[\u4e00-\u9fff]", token) else 1
    return score


def select_reference_profile(
    config: dict,
    data_dir: str,
    context: str,
    profiles: list[dict],
) -> dict:
    active_profiles = [_normalize_profile(p) for p in profiles if p and p.get("active") is not False and p.get("url")]
    if not active_profiles:
        return {}

    candidates = []
    for idx, profile in enumerate(active_profiles, 1):
        candidates.append({
            "id": profile["id"],
            "label": profile.get("label", ""),
            "source": profile.get("source", ""),
            "prompt": profile.get("prompt", ""),
            "tags": profile.get("tags", []),
            "index": idx,
        })

    system = (
        "You select exactly one reference image for image-to-image generation. "
        "Choose the candidate whose prompt/tags best match the schedule, outfit, scene, and mood. "
        "If none clearly match, return an empty reference_id so the app can randomly choose one. "
        "Return only JSON: {\"reference_id\":\"...\", \"reason\":\"...\"}."
    )
    content = _post_llm(
        config,
        data_dir,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps({
                "generation_context": str(context or "")[:1800],
                "candidates": candidates[:40],
            }, ensure_ascii=False)},
        ],
        max_tokens=500,
        temperature=0.1,
    )
    data = _extract_json_object(content)
    wanted_id = str(data.get("reference_id") or "").strip()
    if wanted_id:
        for profile in active_profiles:
            if profile.get("id") == wanted_id:
                selected = dict(profile)
                selected["selection_reason"] = str(data.get("reason") or "").strip()
                selected["selection_mode"] = "llm"
                return selected
    elif content and "reference_id" in data:
        selected = random.choice(active_profiles)
        selected["selection_reason"] = str(data.get("reason") or "LLM found no clear match; random fallback").strip()
        selected["selection_mode"] = "llm_random"
        selected["random_fallback"] = True
        return selected

    scored = [(profile, _score_profile(context, profile)) for profile in active_profiles]
    best_score = max((score for _profile, score in scored), default=0)
    if best_score > 0:
        best = [profile for profile, score in scored if score == best_score]
        selected = random.choice(best)
        selected["selection_reason"] = f"local_score:{best_score}"
        selected["selection_mode"] = "local_score"
        return selected

    selected = random.choice(active_profiles)
    selected["selection_reason"] = "no clear match; random fallback"
    selected["selection_mode"] = "random"
    selected["random_fallback"] = True
    return selected
