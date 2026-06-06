"""Web 画廊服务器 - aiohttp"""
import fcntl
import json
import logging
import os
import shutil
import sys
import subprocess
from datetime import date
from pathlib import Path
import time
import re
from typing import Optional
from urllib.parse import unquote
import uuid

from aiohttp import web

from store import ScheduleStore

logger = logging.getLogger(__name__)

# 日期 key 正则：匹配 YYYY-MM-DD 格式
DATE_KEY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
DEFAULT_PHOTO_JOB_LIMIT = 6
MIN_PHOTO_JOB_LIMIT = 1
MAX_PHOTO_JOB_LIMIT = 6
TODAY_PHOTO_SOURCES = {"cron", "web"}
REFERENCE_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
REFERENCE_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
BUILTIN_REFERENCE_MAP = {
    "reference_face.jpg": {"style": "cool", "label": "冷御风"},
    "ref_style_girly.jpg": {"style": "girly", "label": "少女风"},
    "ref_style_sweet.jpg": {"style": "sweet", "label": "甜妹风"},
}


class GalleryServer:
    """猪猪画廊 Web 服务器"""

    def __init__(self, config: dict, data_dir: str, config_path: str = ""):
        self.config = config
        self.data_dir = data_dir
        self.config_path = config_path
        self.gallery_config = config.get("gallery", {})
        self.host = self.gallery_config.get("host", "0.0.0.0")
        self.port = self.gallery_config.get("port", 18888)
        self.token = self.gallery_config.get("token", "")
        self.image_dir = os.path.join(data_dir, "images")
        self.app_reference_dir = os.path.join(os.path.dirname(__file__), "references")
        self.reference_dir = os.path.join(data_dir, "references")
        self.uploaded_reference_dir = os.path.join(self.reference_dir, "uploads")
        self.legacy_uploaded_reference_dir = os.path.join(self.app_reference_dir, "uploads")
        os.makedirs(self.image_dir, exist_ok=True)
        os.makedirs(self.reference_dir, exist_ok=True)
        os.makedirs(self.uploaded_reference_dir, exist_ok=True)
        self._migrate_legacy_uploaded_refs()

        # 回调：外部注入
        self.on_generate_today = None
        self.on_generate_custom = None
        self.on_list_photo_jobs = None
        self.on_refresh_schedule = None
        self.on_rebuild_photo_jobs = None

        self.app = web.Application(middlewares=[self.api_key_middleware])
        self._setup_routes()

    @staticmethod
    @web.middleware
    async def api_key_middleware(request: web.Request, handler):
        """X-API-Key authentication for /api/ routes (skip if GALLERY_API_KEY unset)."""
        path = request.path
        if path.startswith("/api/"):
            api_key = os.environ.get("GALLERY_API_KEY", "")
            if api_key:  # Only enforce if key is set and non-empty
                provided = request.headers.get("X-API-Key", "") or request.query.get("key", "")
                if provided != api_key:
                    return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    def _setup_routes(self):
        """设置路由"""
        # 静态文件
        web_dir = os.path.join(os.path.dirname(__file__), "web")
        self.app.router.add_static("/static", web_dir, show_index=False)

        # 参考图静态服务：/refs 为内置资源，/local-refs 为 data/ 下的持久化本地图。
        self.app.router.add_static("/refs", self.app_reference_dir, show_index=False)
        self.app.router.add_static("/local-refs", self.reference_dir, show_index=False)

        # 画廊页面
        self.app.router.add_get("/", self.handle_index)

        # API
        self.app.router.add_get("/api/today", self.handle_today)
        self.app.router.add_get("/api/gallery", self.handle_gallery)
        self.app.router.add_get("/api/entries/{date}", self.handle_entry)
        self.app.router.add_get("/api/ref-list", self.handle_ref_list)
        self.app.router.add_get("/api/uploaded-refs", self.handle_uploaded_refs)
        self.app.router.add_post("/api/upload-ref", self.handle_upload_ref)
        self.app.router.add_delete("/api/uploaded-refs/{filename}", self.handle_delete_uploaded_ref)
        self.app.router.add_post("/api/generate", self.handle_generate)
        self.app.router.add_post("/api/refresh-schedule", self.handle_refresh_schedule)
        self.app.router.add_post("/api/generate-now", self.handle_generate_now)
        self.app.router.add_post("/api/generate-custom", self.handle_generate_custom)
        self.app.router.add_post("/api/images/{img_id}/favorite", self.handle_toggle_favorite)
        self.app.router.add_delete("/api/images/{img_id}", self.handle_delete_image)
        self.app.router.add_get("/api/health", self.handle_health)
        self.app.router.add_get("/api/config/keys", self.handle_get_keys)
        self.app.router.add_post("/api/config/keys", self.handle_save_keys)
        self.app.router.add_get("/api/models", self.handle_models)
        # 版本管理
        self.app.router.add_get("/api/version", self.handle_version)
        self.app.router.add_post("/api/check-update", self.handle_check_update)
        self.app.router.add_post("/api/update", self.handle_update)
        # 日程彩蛋
        self.app.router.add_get("/api/schedule-detail", self.handle_schedule_detail)
        self.app.router.add_get("/api/photo-jobs", self.handle_photo_jobs)
        self.app.router.add_get("/api/photo-job-limit", self.handle_photo_job_limit)
        self.app.router.add_post("/api/photo-job-limit", self.handle_photo_job_limit)

        # 图片服务
        self.app.router.add_static("/images", self.image_dir, show_index=False)

    async def _check_auth(self, request: web.Request) -> bool:
        """简单 token 认证"""
        if not self.token:
            return True  # 无 token 时不认证
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        return token == self.token

    async def handle_index(self, request: web.Request):
        """返回画廊页面"""
        html_path = os.path.join(os.path.dirname(__file__), "web", "index.html")
        if not os.path.exists(html_path):
            return web.Response(text="Gallery not ready", status=503)
        return web.FileResponse(html_path)

    async def handle_health(self, request: web.Request):
        return web.json_response({"status": "ok"})

    def _plugin_config_path(self) -> str:
        return os.path.join(self.data_dir, "plugin_config.json")

    @staticmethod
    def _clamp_photo_job_limit(value) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError):
            limit = DEFAULT_PHOTO_JOB_LIMIT
        return max(MIN_PHOTO_JOB_LIMIT, min(MAX_PHOTO_JOB_LIMIT, limit))

    def get_photo_job_limit(self) -> int:
        """Read daily dynamic photo-job limit from plugin_config.json."""
        path = self._plugin_config_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return self._clamp_photo_job_limit(data.get("photo_job_limit", DEFAULT_PHOTO_JOB_LIMIT))
        except Exception as e:
            logger.error(f"Load photo job limit error: {e}")
        return DEFAULT_PHOTO_JOB_LIMIT

    def _save_photo_job_limit(self, limit: int) -> int:
        """Persist daily dynamic photo-job limit to plugin_config.json."""
        limit = self._clamp_photo_job_limit(limit)
        store = ScheduleStore(self.data_dir)
        lock_path = store.lock_path
        path = self._plugin_config_path()

        with open(lock_path, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                data = {}
                if os.path.exists(path):
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except (json.JSONDecodeError, OSError):
                        data = {}
                data["photo_job_limit"] = limit
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        return limit

    def _today_completed_photo_count(self) -> int:
        today_str = date.today().isoformat()
        seen = set()
        try:
            store = ScheduleStore(self.data_dir)
            all_data = store.load()
            for key, entry in all_data.items():
                if key == "_meta" or not isinstance(entry, dict):
                    continue
                if DATE_KEY_RE.match(key):
                    continue
                if entry.get("date") != today_str or entry.get("status") != "ok":
                    continue
                if not self._is_today_photo_source(entry.get("source", "")):
                    continue
                img_file = entry.get("image_filename", "")
                if not img_file or img_file in seen:
                    continue
                if os.path.exists(os.path.join(self.image_dir, img_file)):
                    seen.add(img_file)
        except Exception as e:
            logger.error(f"Count completed photos error: {e}")
        return len(seen)

    async def handle_photo_jobs(self, request: web.Request):
        """Return actual pending APScheduler image-generation jobs."""
        if not self.on_list_photo_jobs:
            return web.json_response({
                "status": "unavailable",
                "date": date.today().isoformat(),
                "jobs": [],
                "max_daily": self.get_photo_job_limit(),
                "min": MIN_PHOTO_JOB_LIMIT,
                "max": MAX_PHOTO_JOB_LIMIT,
                "completed_today": self._today_completed_photo_count(),
            })
        try:
            jobs = self.on_list_photo_jobs()
            max_daily = self.get_photo_job_limit()
            completed_today = self._today_completed_photo_count()
            return web.json_response({
                "status": "ok",
                "date": date.today().isoformat(),
                "jobs": jobs,
                "max_daily": max_daily,
                "min": MIN_PHOTO_JOB_LIMIT,
                "max": MAX_PHOTO_JOB_LIMIT,
                "completed_today": completed_today,
                "remaining_today": max(0, max_daily - completed_today - len(jobs)),
            })
        except Exception as e:
            logger.error(f"Load photo jobs error: {e}")
            return web.json_response({"error": str(e), "jobs": []}, status=500)

    async def handle_photo_job_limit(self, request: web.Request):
        """Read or update the daily dynamic photo-job limit."""
        if request.method == "GET":
            max_daily = self.get_photo_job_limit()
            return web.json_response({
                "status": "ok",
                "date": date.today().isoformat(),
                "max_daily": max_daily,
                "min": MIN_PHOTO_JOB_LIMIT,
                "max": MAX_PHOTO_JOB_LIMIT,
                "completed_today": self._today_completed_photo_count(),
            })

        try:
            body = await request.json()
            limit = self._save_photo_job_limit(body.get("max_daily", body.get("limit")))
            jobs = []
            if self.on_rebuild_photo_jobs:
                jobs = self.on_rebuild_photo_jobs() or []
            completed_today = self._today_completed_photo_count()
            return web.json_response({
                "status": "ok",
                "date": date.today().isoformat(),
                "max_daily": limit,
                "min": MIN_PHOTO_JOB_LIMIT,
                "max": MAX_PHOTO_JOB_LIMIT,
                "completed_today": completed_today,
                "remaining_today": max(0, limit - completed_today - len(jobs)),
                "jobs": jobs,
            })
        except Exception as e:
            logger.error(f"Save photo job limit error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_refresh_schedule(self, request: web.Request):
        """Regenerate today's schedule without generating an image."""
        if not self.on_refresh_schedule:
            return web.json_response({"error": "no_scheduler"}, status=500)
        try:
            entry = await self.on_refresh_schedule()
            if entry and entry.status == "ok":
                return web.json_response({
                    "status": "ok",
                    "entry": entry.to_dict(),
                })
            return web.json_response({
                "error": "schedule_generate_failed",
                "entry": entry.to_dict() if entry else None,
            }, status=500)
        except Exception as e:
            logger.error(f"Refresh schedule error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_get_keys(self, request: web.Request):
        """获取 API 密钥配置状态（返回 masked 值）"""
        keys_config = {}
        
        # 读取 api_keys_config.json
        api_keys_path = os.path.join(self.data_dir, "api_keys_config.json")
        if os.path.exists(api_keys_path):
            try:
                with open(api_keys_path, 'r') as f:
                    keys_config = json.load(f)
            except Exception as e:
                logger.error(f"Load API keys config error: {e}")
        
        # 读取 plugin_config.json 获取 gitee_config
        plugin_config_path = os.path.join(self.data_dir, "plugin_config.json")
        gitee_key = ""
        gitee_fallback_enabled = False
        if os.path.exists(plugin_config_path):
            try:
                with open(plugin_config_path, 'r') as f:
                    plugin_config = json.load(f)
                    gitee_keys = plugin_config.get("gitee_config", {}).get("api_keys", [])
                    if gitee_keys:
                        gitee_key = gitee_keys[0]
                    gitee_fallback_enabled = bool(plugin_config.get("gitee_fallback_enabled", False))
            except Exception as e:
                logger.error(f"Load plugin config error: {e}")
        
        # 读取 config.yaml 的 llm.model
        llm_model = ""
        if self.config_path and os.path.exists(self.config_path):
            try:
                import yaml
                with open(self.config_path, 'r') as f:
                    full_config = yaml.safe_load(f) or {}
                llm_model = full_config.get("llm", {}).get("model", "")
            except Exception as e:
                logger.error(f"Load config.yaml error: {e}")

        # 返回 masked 状态
        return web.json_response({
            "gitee_key": self._mask_key(gitee_key),
            "gpt_key": self._mask_key(keys_config.get("gpt_key", "")),
            "gpt_base_url": keys_config.get("gpt_base_url", ""),
            "cpa_url": keys_config.get("cpa_url", ""),
            "cpa_key": self._mask_key(keys_config.get("cpa_key", "")),
            "appearance": keys_config.get("appearance", ""),
            "llm_model": llm_model,
            "gitee_fallback_enabled": gitee_fallback_enabled,
        })
    
    def _mask_key(self, key: str) -> str:
        """Mask API key for display"""
        if not key or len(key) < 8:
            return ""
        return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"

    def _parse_outfit_parts(self, outfit_raw: str) -> dict:
        """Parse 风格/发型/穿搭/动作/场景 blocks from stored outfit text."""
        parts = {}
        if not outfit_raw:
            return parts
        segments = re.split(r'(?=风格[：:]|穿搭[：:]|发型[：:]|动作[：:]|场景[：:])', outfit_raw)
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            match = re.match(r'(风格|穿搭|发型|动作|场景)[：:]\s*(.*)', seg)
            if match:
                parts[match.group(1)] = match.group(2).strip()
        return parts

    def _enrich_outfit_parts_from_entry(self, parts: dict, entry: dict) -> dict:
        """Fill missing outfit details from a generated image prompt."""
        if not isinstance(entry, dict):
            return parts

        if not parts.get("风格") and entry.get("outfit_style"):
            parts["风格"] = entry.get("outfit_style", "")

        prompt = entry.get("prompt", "") or ""
        if not prompt:
            return parts

        def _extract(pattern: str) -> str:
            match = re.search(pattern, prompt, re.IGNORECASE | re.DOTALL)
            if not match:
                return ""
            return re.sub(r'\s+', ' ', match.group(1)).strip().rstrip(".")

        hair = _extract(r'Her hair is\s+(.+?)\.\s+She is\s+')
        action = _extract(r'Her hair is.+?\.\s+She is\s+(.+?)\.\s+She is wearing\s+')
        clothing = _extract(r'She is wearing\s+(.+?)\.\s+Background:\s+')
        scene = _extract(r'Background:\s+(.+?)(?:\.\s+Today\'s plan:|$)')

        if hair and not parts.get("发型"):
            parts["发型"] = hair
        if action and not parts.get("动作"):
            parts["动作"] = action
        current_clothing = parts.get("穿搭", "")
        if clothing and (
            not current_clothing
            or "精心搭配" in current_clothing
            or len(current_clothing) < 10
        ):
            parts["穿搭"] = clothing
        if scene and not parts.get("场景"):
            parts["场景"] = scene
        return parts

    @staticmethod
    def _parse_time_activity(value: str) -> tuple[str, str]:
        """Parse "HH:mm activity" into normalized time and activity."""
        match = re.match(r'\s*(\d{1,2}):(\d{2})\s*(.*)', str(value or ""))
        if not match:
            return "", str(value or "").strip()

        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return "", match.group(3).strip()
        return f"{hour:02d}:{minute:02d}", match.group(3).strip()

    @staticmethod
    def _time_sort_value(time_text: str) -> int:
        time_value, _ = GalleryServer._parse_time_activity(time_text)
        if not time_value:
            return 24 * 60
        hour, minute = time_value.split(":")
        return int(hour) * 60 + int(minute)

    @staticmethod
    def _is_today_photo_source(source: str) -> bool:
        return source in TODAY_PHOTO_SOURCES

    @staticmethod
    def _photo_schedule_activity(entry: dict) -> str:
        prompt = (entry.get("prompt") or "").strip()
        plan_match = re.search(r"Today's plan:\s*(.+?)(?:\.\s*Style:|\.|$)", prompt, re.IGNORECASE | re.DOTALL)
        if plan_match:
            return re.sub(r'\s+', ' ', plan_match.group(1)).strip()

        prompt_lower = prompt.lower()
        if "night market" in prompt_lower or "street food" in prompt_lower:
            return "在夜市街头借着日落余晖拍照"
        if "city lights" in prompt_lower and "railing" in prompt_lower:
            return "站在栏杆边欣赏城市夜景"
        if "sunset" in prompt_lower or "golden hour" in prompt_lower:
            return "趁着日落余晖在户外拍照"
        if "cafe" in prompt_lower or "coffee" in prompt_lower:
            return "在咖啡馆享受悠闲时光"
        if "restaurant" in prompt_lower:
            return "在餐厅享用今天的美食"
        if "park" in prompt_lower:
            return "在公园里散步拍照"
        if "bed" in prompt_lower or "bedroom" in prompt_lower:
            return "在卧室里放松休息"
        if "bathroom" in prompt_lower or "vanity" in prompt_lower:
            return "在浴室做护肤放松"

        model_name = (entry.get("model_name") or "").strip()
        if model_name:
            return f"{GalleryServer._display_model_name(model_name)} 生图完成"
        return "生图完成"

    @staticmethod
    def _display_model_name(model_name: str) -> str:
        """Normalize stored model ids to stable gallery display labels."""
        name = (model_name or "").strip()
        lower = name.lower()
        if "gpt-image" in lower or lower == "gpt image":
            return "GPT Image"
        if "z-image" in lower or "gitee" in lower:
            return "Gitee"
        if "gemini" in lower:
            return "Gemini"
        return name

    @classmethod
    def _normalize_entry_display(cls, entry: dict, metadata: Optional[dict] = None, fallback_caption: str = "") -> dict:
        if not isinstance(entry, dict):
            return entry
        normalized = dict(entry)
        img_file = normalized.get("image_filename", "")

        if metadata and img_file:
            meta_prompt = (metadata.get(img_file, {}) or {}).get("prompt", "")
            current_prompt = normalized.get("prompt", "") or ""
            if meta_prompt and len(meta_prompt) > len(current_prompt):
                normalized["prompt"] = meta_prompt

        model_label = cls._display_model_name(normalized.get("model_name", ""))
        if model_label and model_label != normalized.get("model_name"):
            normalized["model_name"] = model_label

        if cls._entry_outfit_needs_repair(normalized.get("outfit", "")):
            repaired = cls._fallback_outfit_keywords_from_prompt(normalized.get("prompt", ""))
            if repaired:
                style_name = normalized.get("outfit_style") or "自定义"
                normalized["outfit"] = f"风格：{style_name} 穿搭：{repaired}"

        if not normalized.get("caption") and fallback_caption and normalized.get("source") != "custom":
            normalized["caption"] = fallback_caption
        return normalized

    @staticmethod
    def _entry_outfit_needs_repair(outfit: str) -> bool:
        outfit = outfit or ""
        if not outfit.strip():
            return True
        broken_markers = (
            "This image should look",
            "Masterpiece clarity",
            "high-quality raw photo",
            "glowing skin texture",
        )
        return any(marker in outfit for marker in broken_markers)

    @staticmethod
    def _fallback_outfit_keywords_from_prompt(prompt: str) -> str:
        prompt = prompt or ""
        match = re.search(r'She is wearing\s+(.+?)\.\s+Background:', prompt, re.IGNORECASE | re.DOTALL)
        clothing = match.group(1).strip() if match else prompt
        if re.search(r'[\u4e00-\u9fff]', clothing) and not re.search(r'[A-Za-z]{12,}', clothing):
            return re.sub(r'\s+', ' ', clothing).strip()[:80].rstrip("，,。. ")

        lower = clothing.lower()
        keywords = []
        phrase_map = [
            (["light gray", "knit", "cardigan"], "浅灰色针织开衫"),
            (["gray", "knit", "cardigan"], "灰色针织开衫"),
            (["pink", "lace", "camisole dress"], "粉色蕾丝吊带裙"),
            (["camisole dress"], "吊带裙"),
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

    def _load_image_metadata(self) -> dict:
        path = os.path.join(self.data_dir, "image_metadata.json")
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"Load image metadata error: {e}")
        return {}

    @staticmethod
    def _date_caption_map(all_data: dict) -> dict:
        captions = {}
        for key, entry in all_data.items():
            if not isinstance(entry, dict):
                continue
            caption = entry.get("caption", "")
            date_text = entry.get("date", "")
            if caption and date_text and (DATE_KEY_RE.match(key) or entry.get("schedule")):
                captions.setdefault(date_text, caption)
        return captions

    def _photo_schedule_item(self, entry: dict) -> dict:
        """Build a schedule item from a generated photo entry."""
        if not isinstance(entry, dict):
            return {}

        # 只用 schedule_time 字段，不用 time（time 是图片生成时间，不是日程时间）
        schedule_time, activity = self._parse_time_activity(entry.get("schedule_time", ""))
        if not schedule_time:
            return {}

        if not activity:
            activity = self._photo_schedule_activity(entry)
        return {"time": schedule_time, "activity": activity}

    def _enrich_photo_schedule_time(self, entry: dict, metadata: Optional[dict] = None, fallback_caption: str = "") -> dict:
        """Return a normalized copy with any parseable schedule_time preserved."""
        if not isinstance(entry, dict):
            return entry
        entry = self._normalize_entry_display(entry, metadata, fallback_caption)
        if entry.get("schedule_time"):
            return entry

        item = self._photo_schedule_item(entry)
        if not item:
            return entry

        enriched = dict(entry)
        enriched["schedule_time"] = f"{item['time']} {item['activity']}"
        return enriched

    async def handle_save_keys(self, request: web.Request):
        """保存 API 密钥配置"""
        try:
            body = await request.json()
            
            # 使用 ScheduleStore 的文件锁保护写入
            store = ScheduleStore(self.data_dir)
            lock_path = store.lock_path
            
            with open(lock_path, "w") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    # 读取现有配置
                    api_keys_path = os.path.join(self.data_dir, "api_keys_config.json")
                    keys_config = {}
                    if os.path.exists(api_keys_path):
                        with open(api_keys_path, 'r') as f:
                            keys_config = json.load(f)
                    
                    # 更新配置（只更新提供的字段）
                    if "gpt_key" in body and body["gpt_key"]:
                        keys_config["gpt_key"] = body["gpt_key"]
                    if "gpt_base_url" in body and body["gpt_base_url"]:
                        keys_config["gpt_base_url"] = body["gpt_base_url"]
                    if "cpa_url" in body and body["cpa_url"]:
                        keys_config["cpa_url"] = body["cpa_url"]
                    if "cpa_key" in body and body["cpa_key"]:
                        keys_config["cpa_key"] = body["cpa_key"]
                    # appearance: always update (empty string = remove local appearance)
                    if "appearance" in body:
                        keys_config["appearance"] = body["appearance"]
                    
                    # 写入 api_keys_config.json
                    with open(api_keys_path, 'w', encoding='utf-8') as f:
                        json.dump(keys_config, f, ensure_ascii=False, indent=2)
                    
                    # 更新 plugin_config.json 的 Gitee 配置
                    if "gitee_key" in body or "gitee_fallback_enabled" in body:
                        plugin_config_path = os.path.join(self.data_dir, "plugin_config.json")
                        plugin_config = {}
                        if os.path.exists(plugin_config_path):
                            with open(plugin_config_path, 'r') as f:
                                plugin_config = json.load(f)

                        if "gitee_fallback_enabled" in body:
                            plugin_config["gitee_fallback_enabled"] = bool(body["gitee_fallback_enabled"])

                        if body.get("gitee_key"):
                            if "gitee_config" not in plugin_config:
                                plugin_config["gitee_config"] = {}
                            if "api_keys" not in plugin_config["gitee_config"]:
                                plugin_config["gitee_config"]["api_keys"] = []

                            # 更新或添加第一个 key
                            if plugin_config["gitee_config"]["api_keys"]:
                                plugin_config["gitee_config"]["api_keys"][0] = body["gitee_key"]
                            else:
                                plugin_config["gitee_config"]["api_keys"].append(body["gitee_key"])
                        
                        with open(plugin_config_path, 'w', encoding='utf-8') as f:
                            json.dump(plugin_config, f, ensure_ascii=False, indent=2)
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            
            # 保存 llm_model 到 config.yaml
            if "llm_model" in body and self.config_path and os.path.exists(self.config_path):
                try:
                    import yaml
                    with open(self.config_path, 'r') as f:
                        full_config = yaml.safe_load(f) or {}
                    if "llm" not in full_config:
                        full_config["llm"] = {}
                    full_config["llm"]["model"] = body["llm_model"]
                    with open(self.config_path, 'w', encoding='utf-8') as f:
                        yaml.dump(full_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
                    # 更新内存中的 config
                    self.config["llm"] = full_config["llm"]
                    logger.info(f"LLM model updated to: {body['llm_model']}")
                except Exception as e:
                    logger.error(f"Save llm_model error: {e}")

            return web.json_response({"success": True})

        except Exception as e:
            logger.error(f"Save keys error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_models(self, request: web.Request):
        """获取 CPA 可用模型列表"""
        try:
            import requests
            # 从 api_keys_config.json 读取 CPA 配置
            cpa_url = "http://127.0.0.1:8327"
            cpa_key = ""
            api_keys_path = os.path.join(self.data_dir, "api_keys_config.json")
            if os.path.exists(api_keys_path):
                try:
                    with open(api_keys_path, 'r') as f:
                        keys = json.load(f)
                    if keys.get("cpa_url"):
                        cpa_url = keys["cpa_url"].rstrip("/").replace("/v1", "")
                    cpa_key = keys.get("cpa_key", "")
                except Exception:
                    pass
            
            headers = {}
            if cpa_key:
                headers["Authorization"] = f"Bearer {cpa_key}"
            
            resp = requests.get(f"{cpa_url}/v1/models", headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                models = sorted([m["id"] for m in data.get("data", [])])
                return web.json_response({"models": models})
            else:
                return web.json_response({"models": [], "error": f"CPA returned {resp.status_code}"})
        except Exception as e:
            logger.error(f"Get models error: {e}")
            return web.json_response({"models": [], "error": str(e)})

    async def handle_today(self, request: web.Request):
        """获取今日数据 - 返回今日所有照片 + 日程信息"""
        today_str = date.today().isoformat()
        try:
            store = ScheduleStore(self.data_dir)
            all_data = store.load()
            if not all_data:
                return web.json_response({"status": "no_data", "date": today_str})

            # 1. 获取日程信息（从日期 key 或图片条目）
            schedule_info = {}
            for key, e in all_data.items():
                if key == today_str and e.get("schedule"):
                    schedule_info = e
                    break
            if not schedule_info:
                for key, e in all_data.items():
                    if e.get("date") == today_str and e.get("schedule") and e.get("status") == "ok":
                        schedule_info = e
                        break

            # 2. 获取今日所有照片
            metadata = self._load_image_metadata()
            fallback_caption = schedule_info.get("caption", "") if isinstance(schedule_info, dict) else ""
            photos = []
            seen = set()
            for key, e in all_data.items():
                if DATE_KEY_RE.match(key):
                    continue
                if (
                    e.get("date") == today_str
                    and e.get("status") == "ok"
                    and self._is_today_photo_source(e.get("source", ""))
                ):
                    img_file = e.get("image_filename", "")
                    if img_file and img_file not in seen:
                        img_path = os.path.join(self.image_dir, img_file)
                        if os.path.exists(img_path):
                            seen.add(img_file)
                            photos.append(self._enrich_photo_schedule_time(e, metadata, fallback_caption))

            if photos:
                # Sort by timestamp in filename (newest first)
                def _ts_key(p):
                    fn = p.get("image_filename", "")
                    m = re.search(r'_(\d{10})\.\w+$', fn)
                    return int(m.group(1)) if m else 0
                photos.sort(key=_ts_key, reverse=True)
                for p in photos:
                    if not p.get("schedule") and schedule_info.get("schedule"):
                        p["schedule"] = schedule_info["schedule"]
                return web.json_response({
                    "date": today_str,
                    "photos": photos,
                    "schedule": schedule_info.get("schedule", ""),
                    "outfit_style": schedule_info.get("outfit_style", ""),
                })
            elif schedule_info:
                return web.json_response({
                    "date": today_str,
                    "photos": [],
                    "schedule": schedule_info.get("schedule", ""),
                    "outfit_style": schedule_info.get("outfit_style", ""),
                    "status": schedule_info.get("status", "no_photos"),
                })
            else:
                return web.json_response({"status": "no_data", "date": today_str})
        except Exception as e:
            logger.error(f"Load today error: {e}")
        return web.json_response({"status": "error", "date": today_str})

    async def handle_schedule_detail(self, request: web.Request):
        """返回今日日程详情（彩蛋弹窗用）"""
        today_str = date.today().isoformat()
        try:
            store = ScheduleStore(self.data_dir)
            all_data = store.load()
            if not all_data:
                return web.json_response({"status": "no_data"})

            import re
            from datetime import datetime

            # 查找今日日程（日期 key 或图片条目）
            schedule_entry = None
            # 优先找日期 key（有 schedule 内容的）
            if today_str in all_data and all_data[today_str].get("schedule"):
                schedule_entry = all_data[today_str]
            # 再找有 schedule 的图片条目
            if not schedule_entry:
                for key, e in all_data.items():
                    if (
                        isinstance(e, dict)
                        and e.get("date") == today_str
                        and e.get("schedule")
                        and e.get("status") == "ok"
                        and self._is_today_photo_source(e.get("source", ""))
                    ):
                        schedule_entry = e
                        break

            # 收集今日所有图片条目的 outfit 和 schedule_time
            today_photos = []
            for key, e in all_data.items():
                if key == "_meta": continue
                if (
                    isinstance(e, dict)
                    and e.get("date") == today_str
                    and e.get("status") == "ok"
                    and self._is_today_photo_source(e.get("source", ""))
                ):
                    today_photos.append(self._enrich_photo_schedule_time(e))

            # 如果有日程条目，用它；否则从图片条目拼凑
            outfit_parts = {}
            schedule_items = []
            outfit_style = ""
            caption = ""

            if schedule_entry:
                outfit_style = schedule_entry.get("outfit_style", "")
                caption = schedule_entry.get("caption", "")
                outfit_parts.update(self._parse_outfit_parts(schedule_entry.get("outfit", "")))
                self._enrich_outfit_parts_from_entry(outfit_parts, schedule_entry)
                # 解析 schedule
                for line in schedule_entry.get("schedule", "").split("\n"):
                    line = line.strip()
                    if not line: continue
                    time_text, activity = self._parse_time_activity(line)
                    if time_text:
                        schedule_items.append({"time": time_text, "activity": activity})

            # 从图片条目补充 schedule_time：日程原文可能缺少手动/补生成的照片
            metadata = self._load_image_metadata()
            fallback_caption = caption
            today_photos = [
                self._normalize_entry_display(p, metadata, fallback_caption)
                for p in today_photos
            ]
            if today_photos:
                seen_times = {item.get("time") for item in schedule_items}
                for p in sorted(today_photos, key=lambda x: self._time_sort_value(x.get("schedule_time") or x.get("time", ""))):
                    item = self._photo_schedule_item(p)
                    if not item or item["time"] in seen_times:
                        continue
                    schedule_items.append(item)
                    seen_times.add(item["time"])
                schedule_items.sort(key=lambda item: self._time_sort_value(item.get("time", "")))

            # 从图片条目补充 outfit（如果日程条目没有）
            if not outfit_parts and today_photos:
                best = sorted(today_photos, key=lambda x: x.get("time", ""), reverse=True)[0]
                outfit_raw = best.get("outfit", "")
                outfit_style = outfit_style or best.get("outfit_style", "")
                outfit_parts.update(self._parse_outfit_parts(outfit_raw))
                self._enrich_outfit_parts_from_entry(outfit_parts, best)
            elif today_photos and not schedule_entry:
                best = sorted(today_photos, key=lambda x: x.get("time", ""), reverse=True)[0]
                self._enrich_outfit_parts_from_entry(outfit_parts, best)

            if not caption and today_photos:
                for p in sorted(today_photos, key=lambda x: x.get("time", ""), reverse=True):
                    if p.get("caption"):
                        caption = p["caption"]
                        break

            # 最终 fallback：从当前时间生成占位日程
            if not schedule_items:
                now = datetime.now()
                h = now.hour
                fallback_map = {
                    (6, 11): ("morning", ["晨间护肤routine", "喝咖啡看日出", "整理穿搭出门"]),
                    (11, 14): ("noon", ["午后小憩", "咖啡厅办公", "和闺蜜约饭"]),
                    (14, 18): ("noon", ["逛街shopping", "公园散步拍照", "喝下午茶吃甜点"]),
                    (18, 21): ("evening", ["下班后放松时刻", "健身房运动", "弹琴唱歌"]),
                    (21, 24): ("bedtime", ["睡前护肤敷面膜", "窝在被窝看小说", "泡澡放松"]),
                    (0, 6): ("bedtime", ["深夜emo时间", "和主人说晚安"]),
                }
                for (lo, hi), (theme, activities) in fallback_map.items():
                    if lo <= h < hi:
                        import random
                        schedule_items.append({
                            "time": f"{h:02d}:{now.minute:02d}",
                            "activity": random.choice(activities)
                        })
                        break

            if not schedule_items and not outfit_parts:
                return web.json_response({"status": "no_schedule"})

            return web.json_response({
                "status": "ok",
                "date": today_str,
                "outfit_style": outfit_style,
                "outfit": outfit_parts,
                "schedule": schedule_items,
                "caption": caption,
            })
        except Exception as e:
            logger.error(f"Schedule detail error: {e}")
            return web.json_response({"status": "error", "detail": str(e)})

    @staticmethod
    def _is_reference_image_file(filename: str) -> bool:
        return filename.lower().endswith(REFERENCE_IMAGE_EXTENSIONS)

    @staticmethod
    def _reference_response(filename: str, url: str, label: str, style: str = "upload", builtin: bool = False) -> dict:
        return {
            "filename": filename,
            "url": url,
            "style": style,
            "label": label,
            "builtin": builtin,
        }

    @staticmethod
    def _safe_reference_path(base_dir: str, relative_path: str) -> str:
        try:
            base = Path(base_dir).resolve()
            candidate = (base / unquote(relative_path).lstrip("/")).resolve()
            candidate.relative_to(base)
        except Exception:
            return ""
        return str(candidate) if candidate.is_file() else ""

    def _migrate_legacy_uploaded_refs(self):
        if not os.path.isdir(self.legacy_uploaded_reference_dir):
            return
        for fname in os.listdir(self.legacy_uploaded_reference_dir):
            if not self._is_reference_image_file(fname):
                continue
            src = os.path.join(self.legacy_uploaded_reference_dir, fname)
            dest = os.path.join(self.uploaded_reference_dir, fname)
            if os.path.isfile(src) and not os.path.exists(dest):
                try:
                    shutil.copy2(src, dest)
                except Exception as e:
                    logger.warning(f"Migrate uploaded reference failed: {fname}: {e}")

    def _reference_ext(self, filename: str, content_type: str) -> str:
        ext = os.path.splitext(filename or "")[1].lower()
        if ext in REFERENCE_IMAGE_EXTENSIONS:
            return ext
        return REFERENCE_MIME_EXTENSIONS.get((content_type or "").split(";")[0].strip().lower(), "")

    def _iter_uploaded_refs(self) -> list[dict]:
        refs = []
        seen = set()
        sources = (
            (self.uploaded_reference_dir, "/local-refs/uploads"),
            (self.legacy_uploaded_reference_dir, "/refs/uploads"),
        )
        for upload_dir, url_prefix in sources:
            if not os.path.isdir(upload_dir):
                continue
            for fname in sorted(os.listdir(upload_dir)):
                if fname in seen or not self._is_reference_image_file(fname):
                    continue
                fpath = os.path.join(upload_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                seen.add(fname)
                refs.append(self._reference_response(
                    fname,
                    f"{url_prefix}/{fname}",
                    "自定义上传",
                    builtin=False,
                ))
        return refs

    def _resolve_reference_image(self, ref_image: str) -> str:
        raw = str(ref_image or "").strip()
        if not raw:
            return ""
        ref_path = unquote(raw.split("?", 1)[0].split("#", 1)[0])

        if ref_path.startswith("/local-refs/"):
            rel_path = ref_path.removeprefix("/local-refs/")
            local_path = self._safe_reference_path(self.reference_dir, rel_path)
            if local_path and self._is_reference_image_file(local_path):
                return local_path
            return ""

        if ref_path.startswith("/refs/"):
            rel_path = ref_path.removeprefix("/refs/")
            for base_dir in (self.app_reference_dir, self.reference_dir):
                local_path = self._safe_reference_path(base_dir, rel_path)
                if local_path and self._is_reference_image_file(local_path):
                    return local_path
            return ""

        if os.path.isabs(ref_path):
            for base_dir in (self.reference_dir, self.app_reference_dir):
                try:
                    candidate = Path(ref_path).resolve()
                    candidate.relative_to(Path(base_dir).resolve())
                except Exception:
                    continue
                if candidate.is_file() and self._is_reference_image_file(str(candidate)):
                    return str(candidate)
        return ""

    async def handle_ref_list(self, request: web.Request):
        """返回参考图列表（内置底模 + 用户上传）"""
        refs = []

        for fname, info in BUILTIN_REFERENCE_MAP.items():
            candidates = (
                (self.reference_dir, "/local-refs"),
                (self.app_reference_dir, "/refs"),
            )
            for ref_dir, url_prefix in candidates:
                fpath = os.path.join(ref_dir, fname)
                if os.path.isfile(fpath):
                    refs.append(self._reference_response(
                        fname,
                        f"{url_prefix}/{fname}",
                        info["label"],
                        style=info["style"],
                        builtin=True,
                    ))
                    break

        refs.extend(self._iter_uploaded_refs())
        return web.json_response(refs)

    async def handle_uploaded_refs(self, request: web.Request):
        """列出已上传的自定义参考图"""
        try:
            return web.json_response(self._iter_uploaded_refs())
        except Exception as e:
            logger.error(f"List uploaded refs error: {e}")
            return web.json_response([])

    async def handle_upload_ref(self, request: web.Request):
        """上传自定义参考图到 data/references/uploads 持久化目录"""
        reader = await request.multipart()
        field = await reader.next()
        if not field or not field.filename:
            return web.json_response({"error": "no_file"}, status=400)

        ext = self._reference_ext(field.filename, field.headers.get("Content-Type", ""))
        if not ext:
            return web.json_response({"error": "invalid_image_type"}, status=400)

        save_name = f"upload_{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
        save_path = os.path.join(self.uploaded_reference_dir, save_name)

        try:
            with open(save_path, "wb") as f:
                while True:
                    chunk = await field.read_chunk()
                    if not chunk:
                        break
                    f.write(chunk)
        except Exception:
            if os.path.exists(save_path):
                os.remove(save_path)
            raise

        return web.json_response(self._reference_response(
            save_name,
            f"/local-refs/uploads/{save_name}",
            "自定义上传",
            builtin=False,
        ))

    async def handle_delete_uploaded_ref(self, request: web.Request):
        """删除已上传的自定义参考图"""
        filename = request.match_info.get("filename")
        if not filename:
            return web.json_response({"error": "no_filename"}, status=400)
        if not re.match(r'^[a-zA-Z0-9_.-]+$', filename) or not self._is_reference_image_file(filename):
            return web.json_response({"error": "invalid_filename"}, status=400)

        try:
            deleted = False
            for upload_dir in (self.uploaded_reference_dir, self.legacy_uploaded_reference_dir):
                filepath = os.path.join(upload_dir, filename)
                if os.path.exists(filepath):
                    os.remove(filepath)
                    deleted = True
            if deleted:
                return web.json_response({"success": True})
            return web.json_response({"error": "not_found"}, status=404)
        except Exception as e:
            logger.error(f"Delete uploaded ref error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    def _entry_sort_key(self, entry):
        """Sort key: date desc, then time desc."""
        return (entry.get("date", ""), entry.get("time", ""))

    async def handle_gallery(self, request: web.Request):
        """获取所有画廊条目"""
        entries = self._load_all_entries()
        # 支持收藏过滤
        favorites_only = request.query.get("favorites", "").lower() == "true"
        if favorites_only:
            entries = [e for e in entries if e.get("favorite")]
        # 按日期+时间倒序
        entries.sort(key=lambda e: self._entry_sort_key(e), reverse=True)
        return web.json_response(entries)

    async def handle_entry(self, request: web.Request):
        """获取指定日期的条目"""
        date_str = request.match_info.get("date")
        entry = self._load_entry(date_str)
        if entry:
            return web.json_response(entry)
        return web.json_response({"error": "not_found"}, status=404)

    def _load_entry(self, date_str: str):
        """按 entry.date 查找单日条目。"""
        if not date_str:
            return None
        try:
            store = ScheduleStore(self.data_dir)
            all_data = store.load()
            for entry in all_data.values():
                if isinstance(entry, dict) and entry.get("date") == date_str:
                    metadata = self._load_image_metadata()
                    fallback_caption = self._date_caption_map(all_data).get(date_str, "")
                    return self._enrich_photo_schedule_time(entry, metadata, fallback_caption)
        except Exception as e:
            logger.error(f"Load entry error: {e}")
        return None

    async def handle_generate(self, request: web.Request):
        """手动触发今日生成 (根据当前时段+日程)"""
        if self.on_generate_today:
            try:
                entry = await self.on_generate_today()
                if entry and entry.status == "ok":
                    return web.json_response(entry.to_dict())
                return web.json_response({"error": "generate_failed", "status": entry.status if entry else "unknown"}, status=500)
            except Exception as e:
                logger.error(f"Generate error: {e}")
                return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"error": "no_generator"}, status=500)

    async def handle_generate_now(self, request: web.Request):
        """根据当前精确时间动态生图 (💭 现在在干嘛)"""
        proc = None
        try:
            from datetime import datetime
            import asyncio
            import json as _json

            now = datetime.now()
            now_str = now.strftime("%H:%M")
            logger.info(f"Generate now: time={now_str}, using LLM dynamic prompt")

            # 1) 读取今日日程作为参考
            schedule_text = ""
            try:
                store = ScheduleStore(self.data_dir)
                all_data = store.load()
                today_str = now.strftime("%Y-%m-%d")
                daily = all_data.get(today_str, {})
                schedule_text = daily.get("schedule", "") if isinstance(daily, dict) else ""
            except Exception:
                pass

            # 2) 用 LLM 根据精确时间生成活动描述
            import urllib.request
            cpa_base_url = os.environ.get("CPA_BASE_URL", "http://127.0.0.1:8327/v1").rstrip("/")
            cpa_key = ""
            try:
                cfg_path = os.path.join(self.data_dir, "api_keys_config.json")
                if os.path.exists(cfg_path):
                    with open(cfg_path, encoding="utf-8") as f:
                        cfg = _json.load(f)
                    cpa_key = cfg.get("cpa_key", "")
                    if cfg.get("cpa_url"):
                        cpa_base_url = cfg["cpa_url"].rstrip("/")
            except Exception:
                pass
            if not cpa_key:
                cpa_key = os.environ.get("CPA_API_KEY", "")
            cpa_url = (
                cpa_base_url
                if cpa_base_url.endswith("/chat/completions")
                else f"{cpa_base_url}/chat/completions"
            )

            schedule_hint = f"\n今日日程参考：\n{schedule_text}" if schedule_text else ""
            llm_prompt = (
                f"现在是 {now_str}。{schedule_hint}\n\n"
                f"根据当前时间和日程，生成一个最适合这个时间点的活动描述（15-30字中文），"
                f"要自然、有画面感、贴合时间。直接输出活动描述，不要解释。"
            )

            activity = ""
            llm_config = self.config.get("llm", {})
            primary_model = (llm_config.get("model") or "deepseek-v4-pro").strip()
            fallback_model = (llm_config.get("fallback_model") or "deepseek-v4-flash").strip()
            if primary_model == "gemini-3.5-flash":
                primary_model = fallback_model or "deepseek-v4-pro"
            llm_models = []
            for model_name in (primary_model, fallback_model, "deepseek-v4-pro"):
                if model_name and model_name not in llm_models:
                    llm_models.append(model_name)

            for model_name in llm_models:
                try:
                    body = _json.dumps({
                        "model": model_name,
                        "messages": [{"role": "user", "content": llm_prompt}],
                        "max_tokens": 100,
                    }).encode()
                    req = urllib.request.Request(
                        cpa_url, data=body,
                        headers={"Content-Type": "application/json", "Authorization": f"Bearer {cpa_key}"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        resp_data = _json.loads(resp.read())
                        msg = resp_data["choices"][0]["message"]
                        activity = (msg.get("content") or msg.get("reasoning_content") or "").strip()
                        activity = activity.strip('"').strip("'")
                    if activity:
                        break
                except Exception as e:
                    logger.warning(f"LLM activity generation failed with {model_name}: {e}")

            if not activity:
                activity = f"{now_str} 的日常时光"
            activity = re.sub(r'\s+', ' ', activity).strip()
            activity = re.sub(r'^\d{1,2}:\d{2}\s*', '', activity).strip()
            schedule_time = f"{now_str} {activity}".strip()

            logger.info(f"LLM generated activity: {activity}")

            # 3) 调用 generate.py --theme custom --prompt <activity>，用 GPT Image 直连出图
            generate_script = os.path.join(os.path.dirname(__file__), "zhuzhu", "generate.py")
            proc = await asyncio.create_subprocess_exec(
                "python3", generate_script, "--theme", "custom", "--caption", "--source", "web",
                "--prompt", activity, "--engine", "gptimage", "--schedule-time", schedule_time,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.path.join(os.path.dirname(__file__), "zhuzhu"),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
            
            if proc.returncode != 0:
                logger.error(f"generate.py failed: {stderr.decode(errors='replace')[-500:]}")
                return web.json_response({"error": "generate_failed", "detail": stderr.decode(errors='replace')[-300:]}, status=500)
            
            stdout_text = stdout.decode(errors='replace')
            # Parse SUCCESS:<path> from output
            import re
            m = re.search(r"SUCCESS:(.+)", stdout_text)
            if not m:
                return web.json_response({"error": "no_output"}, status=500)
            
            image_path = m.group(1).strip()
            filename = os.path.basename(image_path)
            
            # Parse caption if present
            caption_text = ""
            cap_m = re.search(r"CAPTION:(.+)", stdout_text)
            if cap_m:
                caption_text = cap_m.group(1).strip()
            
            # Update schedule_data.json: set source="web" for this entry
            store = ScheduleStore(self.data_dir)
            def _update_source(all_data):
                if filename in all_data:
                    all_data[filename]["source"] = "web"
                    all_data[filename]["schedule_time"] = schedule_time
                    all_data[filename]["time"] = all_data[filename].get("time") or now_str
                    if caption_text:
                        all_data[filename]["caption"] = caption_text
                return all_data
            try:
                store.update(_update_source)
            except Exception as e:
                logger.error(f"Update source error: {e}")
            
            return web.json_response({
                "status": "ok",
                "theme": "custom",
                "filename": filename,
                "image_path": f"/images/{filename}",
                "caption": caption_text,
                "source": "web",
                "schedule_time": schedule_time,
            })
        except asyncio.TimeoutError:
            logger.error("Generate now timeout")
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            return web.json_response({"error": "timeout"}, status=504)
        except Exception as e:
            logger.error(f"Generate now error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_generate_custom(self, request: web.Request):
        """自定义 prompt 生图"""
        if not self.on_generate_custom:
            return web.json_response({"error": "no_generator"}, status=500)
        try:
            body = await request.json()
            user_prompt = body.get("prompt", "").strip()
            if not user_prompt:
                return web.json_response({"error": "prompt_required"}, status=400)
            size = body.get("size", "1024x1024")
            raw_ref_image = body.get("ref_image", "")
            ref_image = self._resolve_reference_image(raw_ref_image)
            if raw_ref_image and not ref_image:
                return web.json_response({"error": "invalid_ref_image"}, status=400)
            entry = await self.on_generate_custom(user_prompt, size, ref_image)
            if entry and entry.status == "ok":
                return web.json_response(entry.to_dict())
            return web.json_response({"error": "generate_failed"}, status=500)
        except Exception as e:
            logger.error(f"Custom generate error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_delete_image(self, request: web.Request):
        """删除图片和条目"""
        img_id = request.match_info.get("img_id")
        # Path traversal validation: only allow safe characters
        if not img_id or not re.match(r'^[a-zA-Z0-9_.-]+$', img_id) or '..' in img_id:
            return web.json_response({"error": "invalid_filename"}, status=400)
        try:
            # 1. Delete image file
            img_path = os.path.join(self.image_dir, img_id)
            if os.path.exists(img_path):
                os.remove(img_path)

            # 2. Remove from schedule_data.json
            store = ScheduleStore(self.data_dir)
            def _delete_entry(all_data):
                removed = False
                # Try direct key match (filename as key)
                if img_id in all_data:
                    del all_data[img_id]
                    removed = True
                else:
                    # Try matching by image_filename field
                    for key, entry in list(all_data.items()):
                        if entry.get("image_filename") == img_id:
                            del all_data[key]
                            removed = True
                return all_data
            store.update(_delete_entry)

            return web.json_response({"success": True})
        except Exception as e:
            logger.error(f"Delete image error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_toggle_favorite(self, request: web.Request):
        """切换收藏状态"""
        img_id = request.match_info.get("img_id")
        try:
            store = ScheduleStore(self.data_dir)
            result = {"new_fav": None}
            def _toggle(all_data):
                entry = all_data.get(img_id)
                if not entry:
                    return all_data
                current_fav = entry.get("favorite", False)
                new_fav = not current_fav
                entry["favorite"] = new_fav
                all_data[img_id] = entry
                result["new_fav"] = new_fav
                return all_data
            store.update(_toggle)
            if result["new_fav"] is None:
                return web.json_response({"error": "not_found"}, status=404)
            return web.json_response({"success": result["new_fav"]})
        except Exception as e:
            logger.error(f"Toggle favorite error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    def _load_all_entries(self) -> list:
        """加载所有条目（按 image_filename 去重，包含日期 key 条目用于日程共享）"""
        try:
            store = ScheduleStore(self.data_dir)
            all_data = store.load()
            if not all_data:
                return []
            result = []
            seen_filenames = set()
            metadata = self._load_image_metadata()
            date_captions = self._date_caption_map(all_data)
            for key, entry in all_data.items():
                is_date_key = bool(DATE_KEY_RE.match(key))
                if is_date_key:
                    # Skip date-key entries — they hold schedule data but
                    # should NOT appear as gallery cards
                    continue
                if entry.get("status") == "ok":
                    img_file = entry.get("image_filename", "")
                    if img_file:
                        # Skip duplicates
                        if img_file in seen_filenames:
                            logger.warning(f"Duplicate image_filename found: {img_file} (key={key}), skipping")
                            continue
                        # Skip broken entries where image file is missing
                        img_path = os.path.join(self.image_dir, img_file)
                        if not os.path.exists(img_path):
                            logger.warning(f"Image file missing: {img_file} (key={key}), skipping")
                            continue
                        seen_filenames.add(img_file)
                    else:
                        # Non-date-key entry without image_filename is broken, skip
                        continue
                    fallback_caption = date_captions.get(entry.get("date", ""), "")
                    result.append(self._enrich_photo_schedule_time(entry, metadata, fallback_caption))
            return result
        except Exception as e:
            logger.error(f"Load entries error: {e}")
            return []

    def _load_version(self) -> str:
        """读取版本文件"""
        version_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")
        if os.path.exists(version_file):
            with open(version_file, "r") as f:
                return f.read().strip()
        return "unknown"

    async def handle_version(self, request: web.Request):
        """返回当前版本信息"""
        version = self._load_version()
        return web.json_response({"version": version})

    async def handle_check_update(self, request: web.Request):
        """检查更新（从 GitHub API 获取最新版本）"""
        import aiohttp
        import json

        github_api = "https://api.github.com/repos/i-kirito/portrait-gallery/releases/latest"
        current_version = self._load_version()

        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(github_api) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"GitHub API error: {resp.status}, {error_text}")
                        return web.json_response(
                            {"error": f"GitHub API 请求失败: {resp.status}"},
                            status=500
                        )

                    data = await resp.json()
                    latest_version = data.get("tag_name", "").lstrip("v")
                    if not latest_version:
                        return web.json_response(
                            {"error": "无法获取最新版本号"},
                            status=500
                        )

                    if latest_version == current_version:
                        return web.json_response({"message": "已是最新版本"})

                    # 返回更新信息
                    return web.json_response({
                        "current": current_version,
                        "latest": latest_version,
                        "update_available": True,
                        "changelog": data.get("body", ""),
                        "html_url": data.get("html_url", ""),
                    })
        except Exception as e:
            logger.error(f"Check update error: {e}")
            return web.json_response(
                {"error": f"检查更新失败: {e}"},
                status=500
            )

    async def handle_update(self, request: web.Request):
        """执行更新（git pull + 重启）"""
        import asyncio

        try:
            # 1. git pull（注入代理环境变量）
            env = os.environ.copy()
            env.setdefault("HTTP_PROXY", "http://192.168.31.213:7890")
            env.setdefault("HTTPS_PROXY", "http://192.168.31.213:7890")
            result = subprocess.run(
                ["git", "pull", "origin", "main"],
                cwd=os.path.dirname(os.path.dirname(__file__)),
                capture_output=True,
                text=True,
                timeout=60,
                env=env
            )

            if result.returncode != 0:
                return web.json_response(
                    {"error": f"git pull 失败: {result.stderr}"},
                    status=500
                )

            # 2. 先返回响应，再稍后重启，避免前端把成功更新误判为网络失败。
            loop = asyncio.get_running_loop()
            loop.call_later(1.0, lambda: os.execv(sys.executable, [sys.executable] + sys.argv))

            return web.json_response({"message": "更新成功，服务即将重启"})
        except subprocess.TimeoutExpired:
            logger.error("Update timeout")
            return web.json_response(
                {"error": "更新超时"},
                status=500
            )
        except Exception as e:
            logger.error(f"Update error: {e}")
            return web.json_response(
                {"error": f"更新失败: {e}"},
                status=500
            )

    def run(self):
        """启动服务器"""
        logger.info(f"画廊服务启动: http://{self.host}:{self.port}")
        web.run_app(self.app, host=self.host, port=self.port)
