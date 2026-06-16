"""Portrait gallery - main entry point.

整合：
- 每日日程生成 (LLM)
- 拟人生图 (zhuzhu-image-gen)
- WebUI 画廊展示
- 定时任务 (APScheduler)
"""
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import subprocess
from datetime import datetime, time as dt_time

from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from data import DailyEntry
from scheduler import DailyScheduler
from image_gen import ImageGenerator
from web_server import GalleryServer
from store import ScheduleStore
from settings import (
    api_keys_path,
    apply_network_env,
    auto_push_agent,
    configured_python,
    custom_shot_label,
    custom_shot_prompt,
    image_process_timeout,
    load_config,
    load_json_file,
    load_runtime_persona,
    normalize_custom_shot_type,
    normalize_persona_source,
    normalize_push_channel,
    reference_filename_to_style,
    resolve_config_path,
    resolve_data_dir,
    resolve_script_dir,
)

TODAY_PHOTO_SOURCES = {"cron", "web"}
FAILED_SCHEDULE_TEXT = "生成失败"
WECHAT_CAPTION_DELAY_SECONDS = 8
WECHAT_SEND_TIMEOUT_SECONDS = 90
WECHAT_RETRY_DELAYS_SECONDS = (60, 180)
WECHAT_RETRYABLE_MARKERS = (
    "rate limited",
    "too many requests",
    "429",
    "timeout",
    "timed out",
    "connection error",
    "connection reset",
    "remoteprotocolerror",
    "server disconnected",
    "temporarily unavailable",
)
REFERENCE_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("portrait_gallery")


def save_schedule_entry(data_dir: str, entry: DailyEntry):
    """保存日程条目到持久化文件 (thread-safe via ScheduleStore)

    Key strategy:
    - If entry has an image_filename, use it as the dict key (consistent with
      sync_to_gallery in core.py). This prevents the same image from appearing
      under two different keys (once as date, once as filename).
    - If entry has no image (schedule-only / failed), use the date as key.
    - Before writing, remove any existing entry that references the same
      image_filename under a different key (deduplication guard).
    """
    store = ScheduleStore(data_dir)
    try:
        entry_dict = entry.to_dict()
        img_filename = entry.image_filename or ""
        if img_filename:
            for field in ("schedule", "schedule_prompt", "schedule_details"):
                entry_dict.pop(field, None)
        if img_filename:
            new_key = img_filename
        else:
            new_key = entry.date

        def _update(all_data):
            # Deduplication: remove any OTHER key that already points to the same
            # image_filename so the gallery never shows the same photo twice.
            if img_filename:
                keys_to_remove = []
                for existing_key, existing_entry in all_data.items():
                    if existing_key == new_key:
                        continue
                    if existing_entry.get("image_filename") == img_filename:
                        keys_to_remove.append(existing_key)
                for k in keys_to_remove:
                    logger.info(f"移除重复条目 (key={k}, image={img_filename})")
                    del all_data[k]

            # If there's already an entry under new_key, merge rather than overwrite
            if new_key in all_data:
                existing = all_data[new_key]
                preserve_fields = ("favorite", "source", "time", "model_name", "base_style")
                if not img_filename:
                    preserve_fields = preserve_fields + ("schedule_prompt", "schedule_details")
                for field in preserve_fields:
                    if field == "favorite" and field in existing:
                        entry_dict[field] = existing[field]
                    elif field in existing and (field not in entry_dict or not entry_dict.get(field)):
                        entry_dict[field] = existing[field]

            all_data[new_key] = entry_dict
            return all_data

        store.update(_update)
        logger.info(f"日程已保存: key={new_key}, date={entry.date}")
    except Exception as e:
        logger.error(f"保存日程失败: {e}")


class PortraitGalleryApp:
    """主应用"""

    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        self.config_path = config_path
        self.data_dir = resolve_data_dir(self.config, config_path)
        apply_network_env(self.config, data_dir=self.data_dir)
        os.makedirs(self.data_dir, exist_ok=True)

        # 初始化组件
        self.scheduler_gen = DailyScheduler(self.config, self.data_dir)
        script_dir = resolve_script_dir(self.config, config_path)
        image_config = self.config.get("image_gen", {})
        self.image_gen = ImageGenerator(
            script_dir,
            self.data_dir,
            config=self.config,
            config_path=config_path,
            python_executable=configured_python(self.config),
            default_engine=image_config.get("default_engine", ""),
        )

        # Web 服务器
        self.web_server = GalleryServer(self.config, self.data_dir, config_path)
        self.image_gen.set_output_dir(self.web_server.image_dir)
        self.web_server.on_image_dir_changed = self.image_gen.set_output_dir
        self.web_server.on_generate_today = self.generate_and_save
        self.web_server.on_generate_custom = self.generate_custom
        self.web_server.on_list_photo_jobs = self.list_photo_jobs
        self.web_server.on_refresh_schedule = self.refresh_schedule
        self.web_server.on_rebuild_photo_jobs = self.rebuild_photo_jobs
        self.web_server.on_retry_photo_job = self.retry_photo_job
        self.web_server.on_reroll_image = self.reroll_image

        # APScheduler
        timezone = self.config.get("config", {}).get("timezone", "Asia/Shanghai")
        self.aps = AsyncIOScheduler(timezone=timezone)

        # Backfill photo job controls
        self._photo_jobs_inflight: set[str] = set()
        self._failed_photo_jobs: dict[str, dict] = self._load_failed_photo_jobs()
        self._inflight_lock = asyncio.Lock()
        self._backfill_semaphore = asyncio.Semaphore(1)

    def _failed_photo_jobs_path(self) -> str:
        return os.path.join(self.data_dir, "photo_job_failures.json")

    def _load_failed_photo_jobs(self) -> dict[str, dict]:
        path = self._failed_photo_jobs_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            return {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except Exception as e:
            logger.error(f"读取生图失败记录失败: {e}")
            return {}

    def _save_failed_photo_jobs(self):
        path = self._failed_photo_jobs_path()
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._failed_photo_jobs, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.error(f"保存生图失败记录失败: {e}")

    async def generate_and_save(self) -> DailyEntry:
        """生成日程 → 生图 → 保存"""
        # 1. 生成日程
        entry = await self.scheduler_gen.generate_today()
        if not entry or entry.status == "failed":
            logger.error("日程生成失败")
            if entry:
                save_schedule_entry(self.data_dir, entry)
            return entry

        logger.info(f"日程生成成功: {entry.outfit_style} | base_style={entry.base_style}")

        # 先保存 date-key 日程；后续图片条目会去掉全天计划字段，避免卡片重复承载大块日程。
        save_schedule_entry(self.data_dir, entry)

        # 2. 生成图片
        if entry.prompt and entry.status == "ok":
            filename = await self.image_gen.generate_for_outfit(entry.prompt, entry.outfit_style, entry.base_style)
            if filename:
                entry.image_filename = filename
                entry.image_path = f"/images/{filename}"
                logger.info(f"图片生成成功: {filename}")
            else:
                logger.warning("图片生成失败")

        # 3. 保存图片条目
        if entry.image_filename:
            save_schedule_entry(self.data_dir, entry)
        return entry

    async def generate_custom(
        self,
        user_prompt: str,
        size: str = "1024x1024",
        ref_image: str = "",
        shot_type: str = "selfie",
        pure: bool = False,
        api_source: str = "",
        api_caption: str = "",
        image_model: str = "",
    ) -> DailyEntry:
        """自定义 prompt 生图"""
        today_str = datetime.now().strftime("%Y-%m-%d")
        ts = int(datetime.now().timestamp())
        shot_type = normalize_custom_shot_type(shot_type)
        shot_label = custom_shot_label(shot_type)
        shot_prompt = custom_shot_prompt(shot_type, size)
        generation_prompt = f"{user_prompt}. {shot_prompt}"
        normalized_api_source = re.sub(r"[\s_-]+", "", str(api_source or "").strip().lower())
        entry_source = "hermes_api" if normalized_api_source in {"hermes", "hermesapi"} else "custom"

        style = None
        ref_path = ref_image if ref_image else ""
        if ref_image:
            # Check if it's a built-in style reference
            ref_basename = os.path.basename(ref_image)
            ref_style = reference_filename_to_style(ref_basename)
            if ref_style and not pure:
                style = ref_style
                ref_path = ref_image
            else:
                # Custom uploaded reference, or pure mode with any reference, uses img2img only.
                ref_path = ref_image

        kwargs = {}
        if ref_path:
            kwargs["ref_image"] = ref_path
        if size:
            kwargs["size"] = size
        has_reference = bool(ref_path)
        no_auto_style = bool(pure)
        custom_ref_mode = "pure" if pure else ("reference" if has_reference else "text2img")

        filename = await self.image_gen.generate(
            generation_prompt,
            style=style,
            timeout=image_process_timeout(self.config, with_reference_fallback=bool(style or ref_path)),
            source=entry_source,
            theme="custom",
            prompt_final=bool(pure),
            no_auto_style=no_auto_style,
            image_model=image_model,
            **kwargs
        )
        if not filename:
            logger.error("自定义生图失败")
            return DailyEntry(date=today_str, outfit="生成失败", status="failed")

        caption = str(api_caption or "").strip() if entry_source == "hermes_api" else ""
        if entry_source != "hermes_api" and not caption:
            persona = load_runtime_persona(self.config, self.data_dir)
            character_name = persona.get("name") or "角色"
            prompt_hint = re.sub(r"\s+", " ", user_prompt or "").strip(" ，,。.!！?")
            if len(prompt_hint) > 24:
                prompt_hint = prompt_hint[:24].rstrip(" ，,。.!！?") + "..."
            caption_templates = [
                f"顺着「{prompt_hint or '这个念头'}」站进场景里时，{character_name}心里忽然安静了一点。",
                f"镜头的距离刚好留出一点呼吸，{character_name}也跟着慢慢放松下来。",
                f"这一刻像从描述里慢慢走出来，{character_name}只想把脚步放轻一点。",
                f"没有太多刻意安排，{character_name}只是顺着当下的感觉停了一小会儿。",
            ]
            caption = caption_templates[
                sum(ord(ch) for ch in f"{filename}|{user_prompt}|{shot_label}") % len(caption_templates)
            ]

        entry = DailyEntry(
            date=today_str,
            outfit_style="自定义",
            outfit=f"风格：自定义{' 模式：纯' if pure else ''} 视角：{shot_label} 穿搭：{user_prompt[:80]}",
            schedule="",
            prompt=generation_prompt,
            caption=caption,
            image_filename=filename,
            image_path=f"/images/{filename}",
            status="ok",
            source=entry_source,
            base_style=style or "",
            shot_type=shot_type,
            prompt_mode="pure" if pure else "injected",
            pure_prompt=bool(pure),
            custom_prompt=user_prompt,
            custom_ref_mode=custom_ref_mode,
        )
        save_schedule_entry(self.data_dir, entry)
        self._update_image_metadata_caption(filename, caption)
        logger.info(f"自定义生图成功: {filename}")
        return entry

    @staticmethod
    def _find_entry_by_image(all_data: dict, image_filename: str) -> tuple[str, dict]:
        if image_filename in all_data and isinstance(all_data[image_filename], dict):
            return image_filename, dict(all_data[image_filename])
        for key, entry in all_data.items():
            if isinstance(entry, dict) and entry.get("image_filename") == image_filename:
                return key, dict(entry)
        return "", {}

    @staticmethod
    def _extract_custom_user_prompt(entry: dict) -> str:
        """Recover the raw custom prompt so reroll can apply current shot rules."""
        if not isinstance(entry, dict):
            return ""
        for field in ("custom_prompt", "user_prompt"):
            value = str(entry.get(field) or "").strip()
            if value:
                return value

        outfit = str(entry.get("outfit") or "").strip()
        match = re.search(r"穿搭[:：]\s*(.+)$", outfit)
        if match:
            value = match.group(1).strip()
            if value:
                return value

        prompt = str(entry.get("prompt") or "").strip()
        match = re.match(r"(.+?)\s*[.。]\s*camera view\s*:", prompt, flags=re.IGNORECASE | re.DOTALL)
        if match:
            value = match.group(1).strip(" \t\r\n.。 ，,")
            if value:
                return value
        return ""

    @staticmethod
    def _engine_from_model_name(model_name: str) -> str:
        name = (model_name or "").strip().lower()
        if "gitee" in name or "z-image" in name:
            return "gitee"
        if "gemini" in name:
            return "gemini"
        if "gpt" in name or name.startswith("agnes-image-") or "grok-imagine" in name:
            return "gptimage"
        return ""

    @staticmethod
    def _image_time_from_filename(filename: str) -> str:
        match = re.search(r'_(\d{10})\.\w+$', filename or "")
        if not match:
            return ""
        try:
            return datetime.fromtimestamp(int(match.group(1))).strftime("%H:%M")
        except (OSError, ValueError):
            return ""

    def _remove_image_metadata(self, filename: str):
        if not filename:
            return
        path = os.path.join(self.data_dir, "image_metadata.json")
        metadata = load_json_file(path)
        if not isinstance(metadata, dict) or filename not in metadata:
            return
        metadata.pop(filename, None)
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.error("移除旧图片元数据失败: %s, %s", filename, e)
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _update_image_metadata_caption(self, filename: str, caption: str):
        if not filename:
            return
        path = os.path.join(self.data_dir, "image_metadata.json")
        metadata = load_json_file(path)
        if not isinstance(metadata, dict):
            return
        entry = metadata.get(filename)
        if not isinstance(entry, dict):
            return
        entry["caption"] = str(caption or "").strip()
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.error("更新图片元数据小心思失败: %s, %s", filename, e)
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _delete_replaced_image(self, filename: str):
        if not filename:
            return
        self._remove_image_metadata(filename)
        delete_files = getattr(self.web_server, "_delete_image_files", None)
        if not callable(delete_files):
            return
        deleted, errors = delete_files(filename)
        if errors:
            logger.warning("删除被替换图片时有错误: %s, errors=%s", filename, errors)
        elif deleted:
            logger.info("已删除被替换图片文件: %s", filename)

    def _resolve_reroll_reference_image(self, original: dict, meta: dict) -> str:
        """Find the original img2img reference path for a custom reroll."""
        candidates = [
            original.get("requested_ref_image_path"),
            meta.get("requested_ref_image_path"),
            original.get("ref_image_path"),
            meta.get("ref_image_path"),
            original.get("requested_ref_image"),
            meta.get("requested_ref_image"),
            original.get("ref_image"),
            meta.get("ref_image"),
        ]
        resolver = getattr(self.web_server, "_resolve_reference_image", None)
        search_dirs = [
            getattr(self.web_server, "uploaded_reference_dir", ""),
            getattr(self.web_server, "reference_dir", ""),
            getattr(self.web_server, "app_reference_dir", ""),
        ]
        for raw in candidates:
            value = str(raw or "").strip()
            if not value:
                continue
            if callable(resolver):
                resolved = resolver(value, allow_any_path=True)
                if resolved:
                    return resolved
            basename = os.path.basename(value)
            if basename:
                for directory in search_dirs:
                    candidate = os.path.join(directory, basename) if directory else ""
                    if candidate and os.path.isfile(candidate) and candidate.lower().endswith(REFERENCE_IMAGE_EXTENSIONS):
                        return candidate
        return ""

    async def reroll_image(self, image_filename: str) -> dict:
        """Generate a new card from an existing card's final prompt."""
        store = ScheduleStore(self.data_dir)
        all_data = store.load()
        original_key, original = self._find_entry_by_image(all_data, image_filename)
        if not original:
            return {"status": "failed", "error": "not_found"}

        metadata = load_json_file(os.path.join(self.data_dir, "image_metadata.json"))
        meta = metadata.get(image_filename, {}) if isinstance(metadata, dict) else {}
        prompt = (meta.get("prompt") or original.get("prompt") or "").strip()
        schedule_time = (original.get("schedule_time") or meta.get("schedule_time") or "").strip()
        original_source = (original.get("source") or meta.get("source") or "").strip()
        is_scheduled_reroll = original_source == "cron" and bool(schedule_time)
        if not prompt and not is_scheduled_reroll:
            return {"status": "failed", "error": "prompt_missing"}

        today_str = datetime.now().strftime("%Y-%m-%d")
        original_date = original.get("date") or today_str
        reroll_uses_today_schedule = is_scheduled_reroll and original_date == today_str
        if is_scheduled_reroll and not reroll_uses_today_schedule and not prompt:
            return {"status": "failed", "error": "prompt_missing"}
        pure_raw = original.get("pure_prompt", False)
        if isinstance(pure_raw, str):
            pure_flag = pure_raw.strip().lower() in {"1", "true", "yes", "on"}
        else:
            pure_flag = bool(pure_raw)
        original_is_pure = pure_flag or str(original.get("prompt_mode", "")).lower() == "pure"
        raw_model_name = (
            meta.get("model", "")
            or original.get("image_model", "")
            or original.get("model_name", "")
        )
        engine = self._engine_from_model_name(raw_model_name)
        engine = engine or self.image_gen.default_engine or "gptimage"
        base_style = (original.get("base_style") or "").strip().lower()
        is_custom_source = original_source in {"custom", "hermes_api"}
        custom_ref_mode = str(original.get("custom_ref_mode") or "").strip().lower()
        has_custom_reference = custom_ref_mode == "reference" or bool(
            original.get("requested_ref_image")
            or original.get("requested_ref_image_path")
            or meta.get("requested_ref_image")
            or meta.get("requested_ref_image_path")
        )
        custom_ref_image = ""
        if is_custom_source and has_custom_reference and engine == "gptimage":
            custom_ref_image = self._resolve_reroll_reference_image(original, meta)
            if not custom_ref_image:
                logger.warning("自定义参考图重抽未找到原参考图，将退回文生图: %s", image_filename)
        style = None
        if not original_is_pure and engine == "gptimage" and base_style in {"cool", "girly", "sweet"}:
            if not is_custom_source or has_custom_reference:
                style = base_style
        size = (meta.get("size") or "").strip()
        custom_user_prompt = self._extract_custom_user_prompt(original)
        custom_shot_type = normalize_custom_shot_type(original.get("shot_type", ""))
        is_custom_injected_reroll = is_custom_source and not original_is_pure and bool(custom_user_prompt)
        reroll_theme = "custom"
        reroll_source = "custom"
        reroll_prompt = prompt
        reroll_prompt_final = True
        reroll_no_auto_style = original_is_pure
        reroll_caption = False
        if is_custom_injected_reroll:
            reroll_prompt = f"{custom_user_prompt}. {custom_shot_prompt(custom_shot_type, size)}"
            reroll_prompt_final = False
            reroll_no_auto_style = not has_custom_reference
        if is_scheduled_reroll:
            match = re.match(r'\s*(\d{1,2}):(\d{2})', schedule_time)
            if match:
                reroll_theme = self._theme_for_hour(int(match.group(1)))
            if reroll_uses_today_schedule:
                reroll_source = "cron"
                reroll_prompt = ""
                reroll_prompt_final = False
                reroll_no_auto_style = False
                reroll_caption = True
                current_base_style = self._today_schedule_base_style()
                if current_base_style:
                    style = current_base_style
            else:
                reroll_source = "custom"
                reroll_prompt = prompt
                reroll_prompt_final = True
                reroll_no_auto_style = False
                reroll_caption = False
                style = base_style if engine == "gptimage" and base_style in {"cool", "girly", "sweet"} else None

        ref_image = custom_ref_image if is_custom_source and custom_ref_image else ""

        filename = await self.image_gen.generate(
            reroll_prompt,
            style=style,
            engine=engine,
            timeout=image_process_timeout(self.config, with_reference_fallback=bool(style or ref_image)),
            ref_image=ref_image,
            size=size,
            source=reroll_source,
            prompt_final=reroll_prompt_final,
            no_auto_style=reroll_no_auto_style,
            theme=reroll_theme,
            schedule_time=schedule_time if reroll_uses_today_schedule else "",
            caption=reroll_caption,
            image_model=raw_model_name if engine == "gptimage" else "",
        )
        if not filename:
            logger.error("图片重抽失败: %s", image_filename)
            return {"status": "failed", "error": "generate_failed"}

        original_time = original.get("time") or self._image_time_from_filename(image_filename)
        now_time = self._image_time_from_filename(filename) or datetime.now().strftime("%H:%M")
        schedule_context_entry = {}
        if is_scheduled_reroll:
            schedule_context_entry = self._today_schedule_entry() if reroll_uses_today_schedule else original
        result = {}

        def _merge_reroll(all_data: dict):
            generated_key, generated = self._find_entry_by_image(all_data, filename)
            generated_key = generated_key or filename
            generated = dict(generated or {})
            replacement_key = original_key or image_filename
            if re.match(r'^\d{4}-\d{2}-\d{2}$', str(replacement_key)):
                replacement_key = image_filename
            generated.update({
                "id": filename,
                "date": original_date,
                "time": original_time or generated.get("time") or now_time,
                "image_filename": filename,
                "image_path": f"/images/{filename}",
                "prompt": generated.get("prompt") or prompt,
                "status": "ok",
                "source": original_source or reroll_source,
                "favorite": bool(original.get("favorite", False)),
                "rerolled_from": image_filename,
                "replaced_image_filename": image_filename,
                "replacement_key": replacement_key,
            })
            if is_scheduled_reroll:
                for field in ("outfit_style", "outfit", "base_style"):
                    value = schedule_context_entry.get(field) if isinstance(schedule_context_entry, dict) else None
                    if value and not generated.get(field):
                        generated[field] = value
                if schedule_time:
                    generated["schedule_time"] = schedule_time
            else:
                for field in ("outfit_style", "outfit", "schedule_time", "shot_type", "prompt_mode", "pure_prompt", "custom_prompt", "custom_ref_mode"):
                    value = original.get(field)
                    if value or field == "pure_prompt":
                        generated[field] = value
                for field in ("generation_mode", "requested_generation_mode", "ref_image", "ref_image_path", "requested_ref_image", "requested_ref_image_path"):
                    value = original.get(field)
                    if value and not generated.get(field):
                        generated[field] = value
            if (
                (not is_scheduled_reroll or not reroll_uses_today_schedule)
                and not generated.get("caption")
                and original.get("caption")
            ):
                generated["caption"] = original["caption"]
            if is_scheduled_reroll:
                if not generated.get("base_style") and isinstance(schedule_context_entry, dict):
                    generated["base_style"] = schedule_context_entry.get("base_style", "")
            elif is_custom_source and not has_custom_reference:
                generated.pop("base_style", None)
            elif original.get("base_style"):
                generated["base_style"] = original["base_style"]
            if not generated.get("outfit_style"):
                generated["outfit_style"] = (
                    schedule_context_entry.get("outfit_style", "")
                    if is_scheduled_reroll and isinstance(schedule_context_entry, dict)
                    else "自定义"
                )
            if not generated.get("outfit"):
                generated["outfit"] = (
                    schedule_context_entry.get("outfit", "")
                    if is_scheduled_reroll and isinstance(schedule_context_entry, dict)
                    else original.get("outfit") or "风格：自定义 穿搭：重抽生成"
                )
            for key, entry in list(all_data.items()):
                if re.match(r'^\d{4}-\d{2}-\d{2}$', str(key)):
                    continue
                entry_filename = entry.get("image_filename") if isinstance(entry, dict) else ""
                if key in {replacement_key, generated_key, image_filename, filename} or entry_filename in {image_filename, filename}:
                    del all_data[key]
            all_data[replacement_key] = generated
            result.update(generated)
            return all_data

        store.update(_merge_reroll)
        if filename != image_filename:
            self._delete_replaced_image(image_filename)
        logger.info("图片重抽成功: %s -> %s", image_filename, filename)
        return result

    async def daily_job(self):
        """每日自动任务 - 生成日程并根据日程时间动态安排生图"""
        logger.info("执行每日日程生成...")
        await self.refresh_schedule()

    async def refresh_schedule(self):
        """Regenerate today's schedule and rebuild dynamic photo jobs."""
        today_str = datetime.now().strftime("%Y-%m-%d")
        # 生成新日程
        entry = await self.scheduler_gen.generate_today()
        
        if not entry or entry.status != "ok":
            logger.error("日程生成失败")
            if entry and entry.schedule and entry.schedule != FAILED_SCHEDULE_TEXT:
                save_schedule_entry(self.data_dir, entry)
            return entry

        # LLM 不通时 scheduler 会返回 fallback；刷新按钮不应因此覆盖已有可用日程。
        if entry.source == "fallback":
            existing_entry = self._today_schedule_entry()
            existing_missing = self._schedule_missing_required_periods(existing_entry.get("schedule", "")) if existing_entry else []
            if existing_entry and not existing_missing:
                logger.warning("日程生成使用兜底结果，保留现有今日日程")
                preserved = DailyEntry.from_dict(existing_entry)
                preserved.source = "preserved"
                await self._schedule_dynamic_photos(preserved.schedule)
                return preserved
            if existing_entry and existing_missing:
                logger.warning(f"现有今日日程缺少时间段 {existing_missing}，使用兜底日程修复")

        # 清除旧日程（日期 key）
        store = ScheduleStore(self.data_dir)
        all_data = store.load()
        if today_str in all_data:
            del all_data[today_str]
            store.save(all_data)
            logger.info("已清除今日日程数据")
        
        save_schedule_entry(self.data_dir, entry)
        logger.info(f"日程生成成功: {entry.outfit_style}")
        await self._schedule_dynamic_photos(entry.schedule)
        
        return entry

    def _get_photo_job_limit(self) -> int:
        if hasattr(self.web_server, "get_photo_job_limit"):
            return self.web_server.get_photo_job_limit()
        return int(self.config.get("config", {}).get("photo_job_limit", 6))

    def _photo_image_exists(self, filename: str) -> bool:
        if not filename:
            return False
        path = filename
        if filename.startswith("/images/"):
            path = os.path.basename(filename)
        if os.path.isabs(path):
            return os.path.isfile(path)
        image_dir = getattr(self.web_server, "image_dir", os.path.join(self.data_dir, "images"))
        return os.path.isfile(os.path.join(image_dir, os.path.basename(path)))

    def _today_completed_photo_count(self) -> int:
        today_str = datetime.now().strftime("%Y-%m-%d")
        seen = set()
        try:
            all_data = ScheduleStore(self.data_dir).load()
            for key, entry in all_data.items():
                if not isinstance(entry, dict):
                    continue
                if re.match(r'^\d{4}-\d{2}-\d{2}$', key):
                    continue
                if entry.get("date") != today_str or entry.get("status") != "ok":
                    continue
                if entry.get("source", "") != "cron":
                    continue
                img_file = entry.get("image_filename", "")
                if img_file and self._photo_image_exists(img_file):
                    seen.add(img_file)
        except Exception as e:
            logger.error(f"统计今日已完成生图失败: {e}")
        return len(seen)

    def _today_inflight_photo_count(self, today_str: str = "") -> int:
        today_str = today_str or datetime.now().strftime("%Y-%m-%d")
        return sum(1 for key in self._photo_jobs_inflight if key.startswith(f"{today_str} "))

    def _today_scheduled_photo_count(self, today_str: str = "") -> int:
        today_str = today_str or datetime.now().strftime("%Y-%m-%d")
        try:
            target_date = datetime.strptime(today_str, "%Y-%m-%d").date()
        except ValueError:
            target_date = datetime.now().date()
        count = 0
        for job in self.aps.get_jobs():
            if not job.id.startswith("photo_dynamic_"):
                continue
            run_time = self._local_job_run_time(job)
            if run_time and run_time.date() == target_date:
                count += 1
        return count

    @staticmethod
    def _slot_time_from_key(slot_key: str) -> str:
        _date_text, _sep, time_text = (slot_key or "").partition(" ")
        return time_text if re.match(r'^\d{2}:\d{2}$', time_text or "") else ""

    def _today_completed_photo_times(self, today_str: str = "") -> set[str]:
        today_str = today_str or datetime.now().strftime("%Y-%m-%d")
        times: set[str] = set()
        try:
            all_data = ScheduleStore(self.data_dir).load()
            for key, entry in all_data.items():
                if not isinstance(entry, dict):
                    continue
                if re.match(r'^\d{4}-\d{2}-\d{2}$', key):
                    continue
                if entry.get("date") != today_str or entry.get("status") != "ok":
                    continue
                if entry.get("source", "") != "cron":
                    continue
                img_file = entry.get("image_filename", "")
                if not img_file or not self._photo_image_exists(img_file):
                    continue
                raw_time = entry.get("schedule_time") or entry.get("time") or ""
                match = re.match(r'\s*(\d{1,2}):(\d{2})', raw_time)
                if match:
                    times.add(f"{int(match.group(1)):02d}:{int(match.group(2)):02d}")
        except Exception as e:
            logger.error(f"统计今日已完成生图计划时间失败: {e}")
        return times

    def _today_failed_photo_times(self, today_str: str = "") -> set[str]:
        today_str = today_str or datetime.now().strftime("%Y-%m-%d")
        times = set()
        for slot_key in self._failed_photo_jobs:
            date_text, _, time_text = slot_key.partition(" ")
            if date_text == today_str and re.match(r'^\d{2}:\d{2}$', time_text):
                if not self._check_photo_exists_for_slot(today_str, time_text):
                    times.add(time_text)
        return times

    def _today_inflight_photo_times(self, today_str: str = "") -> set[str]:
        today_str = today_str or datetime.now().strftime("%Y-%m-%d")
        times = set()
        for slot_key in self._photo_jobs_inflight:
            date_text, _, time_text = slot_key.partition(" ")
            if date_text == today_str and re.match(r'^\d{2}:\d{2}$', time_text):
                times.add(time_text)
        return times

    def _today_scheduled_photo_times(self, today_str: str = "") -> set[str]:
        today_str = today_str or datetime.now().strftime("%Y-%m-%d")
        try:
            target_date = datetime.strptime(today_str, "%Y-%m-%d").date()
        except ValueError:
            target_date = datetime.now().date()
        times = set()
        for job in self.aps.get_jobs():
            if not job.id.startswith("photo_dynamic_"):
                continue
            run_time = self._local_job_run_time(job)
            if run_time and run_time.date() == target_date:
                times.add(f"{run_time.hour:02d}:{run_time.minute:02d}")
        return times

    def _today_photo_plan_times(
        self,
        today_str: str = "",
        *,
        include_scheduled: bool = False,
        exclude_slot_key: str = "",
    ) -> set[str]:
        today_str = today_str or datetime.now().strftime("%Y-%m-%d")
        times = set()
        times.update(self._today_completed_photo_times(today_str))
        times.update(self._today_failed_photo_times(today_str))
        times.update(self._today_inflight_photo_times(today_str))
        if include_scheduled:
            times.update(self._today_scheduled_photo_times(today_str))
        exclude_time = self._slot_time_from_key(exclude_slot_key)
        if exclude_time:
            times.discard(exclude_time)
        return times

    def _today_photo_plan_periods(self, today_str: str = "", *, include_scheduled: bool = False) -> set[str]:
        labels = set()
        for time_text in self._today_photo_plan_times(today_str, include_scheduled=include_scheduled):
            match = re.match(r'^(\d{2}):(\d{2})$', time_text)
            if not match:
                continue
            label = self._schedule_period_label(int(match.group(1)), int(match.group(2)))
            if label:
                labels.add(label)
        return labels

    def _photo_quota_snapshot(
        self,
        today_str: str = "",
        *,
        include_scheduled: bool = False,
        exclude_slot_key: str = "",
    ) -> tuple[int, int, int, int, int, int, int]:
        today_str = today_str or datetime.now().strftime("%Y-%m-%d")
        max_daily = self._get_photo_job_limit()
        completed_times = self._today_completed_photo_times(today_str)
        failed_times = self._today_failed_photo_times(today_str)
        inflight_times = self._today_inflight_photo_times(today_str)
        scheduled_times = self._today_scheduled_photo_times(today_str) if include_scheduled else set()
        exclude_time = self._slot_time_from_key(exclude_slot_key)
        for slot_set in (completed_times, failed_times, inflight_times, scheduled_times):
            if exclude_time:
                slot_set.discard(exclude_time)
        planned_times = set().union(completed_times, failed_times, inflight_times, scheduled_times)
        remaining = max(0, max_daily - len(planned_times))
        return (
            max_daily,
            len(completed_times),
            len(failed_times),
            len(inflight_times),
            len(scheduled_times),
            len(planned_times),
            remaining,
        )

    def _local_job_run_time(self, job):
        run_at = getattr(job, "next_run_time", None)
        if not run_at:
            return None
        try:
            return run_at.astimezone(self.aps.timezone) if run_at.tzinfo else run_at
        except Exception:
            return run_at

    def _prune_photo_jobs_for_limit(self):
        """Remove pending dynamic jobs that would exceed today's configured plan cap."""
        today = datetime.now().date()
        today_str = today.strftime("%Y-%m-%d")
        max_daily = self._get_photo_job_limit()
        existing_plan_times = self._today_photo_plan_times(today_str, include_scheduled=False)
        existing_periods = self._today_photo_plan_periods(today_str, include_scheduled=False)
        keep_count = max(0, max_daily - len(existing_plan_times))

        scheduled = []
        for job in self.aps.get_jobs():
            if not job.id.startswith("photo_dynamic_"):
                continue
            run_time = self._local_job_run_time(job)
            if not run_time or run_time.date() != today:
                continue
            scheduled.append({
                "id": job.id,
                "run_time": run_time,
                "period_label": self._schedule_period_label(run_time.hour, run_time.minute),
                "job": job,
            })

        selected = self._select_photo_job_candidates(scheduled, keep_count, existing_periods)
        keep_ids = {item["id"] for item in selected}
        for item in scheduled:
            if item["id"] in keep_ids:
                continue
            job = item["job"]
            run_time = item["run_time"]
            logger.info(
                f"移除超出每日计划上限的待执行生图任务: {job.id} "
                f"run_at={run_time.strftime('%H:%M')} existing_plans={len(existing_plan_times)} "
                f"max={max_daily}"
            )
            try:
                job.remove()
            except Exception as e:
                logger.warning(f"移除超额生图任务失败: {job.id}, error={e}")

    def _schedule_period_label(self, hour: int, minute: int = 0) -> str:
        total_minutes = hour * 60 + minute
        try:
            periods = self.scheduler_gen._required_periods()
        except Exception as e:
            logger.error(f"读取日程必需时间段失败: {e}")
            return ""
        for period in periods:
            start = period["start"]
            end = period["end"]
            if start <= end:
                in_period = start <= total_minutes <= end
            else:
                in_period = total_minutes >= start or total_minutes <= end
            if in_period:
                return period["label"]
        return ""

    def _today_completed_photo_periods(self) -> set[str]:
        today_str = datetime.now().strftime("%Y-%m-%d")
        completed_periods = set()
        try:
            all_data = ScheduleStore(self.data_dir).load()
            for entry in all_data.values():
                if not isinstance(entry, dict):
                    continue
                if entry.get("date") != today_str or entry.get("status") != "ok":
                    continue
                if entry.get("source", "") not in TODAY_PHOTO_SOURCES:
                    continue
                raw_time = entry.get("schedule_time") or entry.get("time") or ""
                match = re.match(r'\s*(\d{1,2}):(\d{2})', raw_time)
                if not match:
                    continue
                label = self._schedule_period_label(int(match.group(1)), int(match.group(2)))
                if label:
                    completed_periods.add(label)
        except Exception as e:
            logger.error(f"统计今日已完成生图时间段失败: {e}")
        return completed_periods

    def _check_photo_exists_for_slot(self, date_str: str, period: str) -> bool:
        """检查指定日期 + 生图时间点是否已有图片。

        `period` 这里传入 HH:mm 时间点。优先用 schedule_time/time 精确匹配；
        兼容历史数据没有时间字段时，再用图片文件名日期 + theme 兜底判断。
        """
        period = (period or "").strip()
        try:
            all_data = ScheduleStore(self.data_dir).load()
            date_token = date_str.replace("-", "")
            expected_theme = ""
            match = re.match(r'^(\d{1,2}):(\d{2})$', period)
            if match:
                expected_theme = self._theme_for_hour(int(match.group(1)))

            for entry in all_data.values():
                if not isinstance(entry, dict):
                    continue
                if entry.get("status") != "ok":
                    continue
                if entry.get("source", "") not in TODAY_PHOTO_SOURCES:
                    continue

                image_filename = entry.get("image_filename", "")
                if not image_filename:
                    continue
                entry_date = entry.get("date", "")
                if entry_date != date_str and date_token not in image_filename:
                    continue

                raw_time = entry.get("schedule_time") or entry.get("time") or ""
                time_match = re.match(r'\s*(\d{1,2}):(\d{2})', raw_time)
                if time_match and period == f"{int(time_match.group(1)):02d}:{int(time_match.group(2)):02d}":
                    return True

                if not raw_time and expected_theme and entry.get("theme", "") == expected_theme:
                    return True
        except Exception as e:
            logger.error(f"检查指定时间点是否已有生图失败: date={date_str}, period={period}, error={e}")
        return False

    def _select_photo_job_candidates(
        self,
        candidates: list[dict],
        limit: int,
        existing_periods: set[str] | None = None,
    ) -> list[dict]:
        if limit <= 0:
            return []
        if len(candidates) <= limit:
            return sorted(candidates, key=lambda item: item["run_time"])

        selected = []
        selected_ids = set()
        planned_periods = set(existing_periods if existing_periods is not None else self._today_photo_plan_periods())
        try:
            required_labels = [period["label"] for period in self.scheduler_gen._required_periods()]
        except Exception as e:
            logger.error(f"读取日程必需时间段失败: {e}")
            required_labels = []

        for label in required_labels:
            if len(selected) >= limit:
                break
            if label in planned_periods:
                continue
            for candidate in candidates:
                if candidate["id"] in selected_ids or candidate.get("period_label") != label:
                    continue
                selected.append(candidate)
                selected_ids.add(candidate["id"])
                planned_periods.add(label)
                break

        for candidate in candidates:
            if len(selected) >= limit:
                break
            if candidate["id"] in selected_ids:
                continue
            selected.append(candidate)
            selected_ids.add(candidate["id"])

        return sorted(selected, key=lambda item: item["run_time"])

    def _is_usable_schedule_entry(self, entry: dict) -> bool:
        return (
            isinstance(entry, dict)
            and entry.get("status") == "ok"
            and bool((entry.get("schedule") or "").strip())
            and entry.get("schedule") != FAILED_SCHEDULE_TEXT
        )

    def _schedule_missing_required_periods(self, schedule_text: str) -> list[str]:
        try:
            return self.scheduler_gen._missing_required_periods(schedule_text or "")
        except Exception as e:
            logger.error(f"检查日程早中晚覆盖失败: {e}")
            return []

    def _today_schedule_entry(self) -> dict:
        today_str = datetime.now().strftime("%Y-%m-%d")
        try:
            all_data = ScheduleStore(self.data_dir).load()
            if self._is_usable_schedule_entry(all_data.get(today_str)):
                return all_data[today_str]
            for entry in all_data.values():
                if (
                    self._is_usable_schedule_entry(entry)
                    and entry.get("date") == today_str
                ):
                    return entry
        except Exception as e:
            logger.error(f"读取今日日程条目失败: {e}")
        return {}

    def _today_schedule_text(self) -> str:
        return self._today_schedule_entry().get("schedule", "")

    @staticmethod
    def _normalize_base_style(value: str) -> str:
        base_style = str(value or "").strip().lower()
        return base_style if base_style in {"cool", "girly", "sweet"} else ""

    def _today_existing_photo_base_style(self) -> str:
        today_str = datetime.now().strftime("%Y-%m-%d")
        latest_ts = -1
        latest_style = ""
        try:
            all_data = ScheduleStore(self.data_dir).load()
            for entry in all_data.values():
                if not isinstance(entry, dict):
                    continue
                if entry.get("date") != today_str or entry.get("status") != "ok":
                    continue
                if entry.get("source", "") != "cron":
                    continue
                img_file = entry.get("image_filename", "")
                if not img_file or not self._photo_image_exists(img_file):
                    continue
                base_style = self._normalize_base_style(entry.get("base_style", ""))
                if not base_style:
                    continue
                match = re.search(r'_(\d{10})\.\w+$', img_file)
                ts = int(match.group(1)) if match else 0
                if ts >= latest_ts:
                    latest_ts = ts
                    latest_style = base_style
        except Exception as e:
            logger.error(f"读取今日已完成生图底模失败: {e}")
        return latest_style

    def _today_schedule_base_style(self) -> str:
        base_style = self._normalize_base_style(self._today_schedule_entry().get("base_style", ""))
        if base_style:
            return base_style
        return self._today_existing_photo_base_style()

    def rebuild_photo_jobs(self) -> list:
        """Rebuild dynamic photo jobs from today's saved schedule."""
        self._prune_photo_jobs_for_limit()
        schedule_text = self._today_schedule_text()
        asyncio.create_task(self._schedule_dynamic_photos(schedule_text))
        return self.list_photo_jobs()

    async def _schedule_dynamic_photos(self, schedule_text: str):
        """Parse HH:mm times from schedule and create one-shot photo jobs."""
        if not schedule_text:
            logger.warning("日程文本为空，跳过动态生图调度")
            return

        now = datetime.now()
        today = now.date()
        today_str = today.strftime("%Y-%m-%d")

        # Remove old dynamic photo jobs
        removed = 0
        for job in self.aps.get_jobs():
            if job.id.startswith("photo_dynamic_"):
                job.remove()
                removed += 1
        if removed:
            logger.info(f"移除了 {removed} 个旧的动态生图任务")

        # Parse "HH:mm activity" lines
        time_matches = re.findall(r'(\d{1,2}):(\d{2})\s*(.*)', schedule_text)
        max_daily = self._get_photo_job_limit()
        planned_times = self._today_photo_plan_times(today_str, include_scheduled=False)
        planned_periods = self._today_photo_plan_periods(today_str, include_scheduled=False)
        planned_count = len(planned_times)
        remaining_slots = max(0, max_daily - planned_count)
        if remaining_slots <= 0:
            logger.info(
                f"今日生图计划已达上限: planned={planned_count}, max={max_daily}"
            )
            return

        candidates = []
        for h_str, m_str, activity in time_matches:
            h, m = int(h_str), int(m_str)
            if h < 0 or h > 23 or m < 0 or m > 59:
                continue

            theme = self._theme_for_hour(h)

            job_id = f"photo_dynamic_{h}_{m}"

            # Skip if job already exists
            existing = self.aps.get_job(job_id)
            if existing:
                logger.info(f"跳过已存在的动态任务: {job_id}")
                continue

            run_time = datetime.combine(today, dt_time(h, m))
            schedule_time_str = f"{h:02d}:{m:02d}"
            schedule_text_for_job = f"{schedule_time_str} {activity.strip()}".strip()

            # 已过期的时间点只跳过。自动补拍会在服务重启时把早上的漏拍
            # 晚上补出来，造成“后面没有计划却生成新图”的错觉；补拍改为只走手动重试。
            if run_time <= now:
                if self._check_photo_exists_for_slot(today_str, schedule_time_str):
                    logger.info(f"跳过已过期的时间（已有图片）: {schedule_time_str}")
                    continue
                if schedule_time_str in planned_times:
                    logger.info(f"跳过已过期的时间（已有计划记录）: {schedule_time_str}")
                    continue

                slot_key = f"{today_str} {schedule_time_str}"
                if slot_key in self._failed_photo_jobs:
                    logger.info(f"跳过已失败的过期时间（等待手动重试）: {schedule_time_str}")
                    continue
                logger.info(
                    f"跳过已过期的时间（不自动补拍）: {schedule_time_str} "
                    f"activity={activity.strip()[:30]}"
                )
                continue

            candidates.append({
                "id": job_id,
                "hour": h,
                "minute": m,
                "activity": activity.strip(),
                "theme": theme,
                "run_time": run_time,
                "period_label": self._schedule_period_label(h, m),
            })

        remaining_slots = max(0, max_daily - len(planned_times))
        selected_jobs = self._select_photo_job_candidates(candidates, remaining_slots, planned_periods)
        for item in selected_jobs:
            self.aps.add_job(
                self.photo_job,
                'date',
                run_date=item["run_time"],
                args=[item["theme"], f"{item['hour']:02d}:{item['minute']:02d} {item['activity']}"],
                id=item["id"],
            )
            logger.info(
                f"添加动态生图任务: {item['hour']:02d}:{item['minute']:02d} "
                f"theme={item['theme']} period={item.get('period_label') or '-'} "
                f"activity={item['activity'][:30]}"
            )

        logger.info(
            f"动态生图任务已创建: {len(selected_jobs)} 个 "
            f"(existing_plans={len(planned_times)}, max_daily={max_daily})"
        )

    async def _backfill_photo_job(self, theme: str, schedule_text: str, slot_key: str):
        """补拍任务包装器，串行执行并在结束后释放 inflight 标记。"""
        try:
            async with self._backfill_semaphore:
                try:
                    ok = await self.photo_job(theme, schedule_text, quota_reserved=True)
                    if ok:
                        logger.info(f"补拍任务完成: {slot_key}")
                    else:
                        logger.error(f"补拍任务失败: {slot_key}")
                except Exception as e:
                    logger.error(f"补拍任务失败: {slot_key}, error={e}", exc_info=True)
        finally:
            async with self._inflight_lock:
                self._photo_jobs_inflight.discard(slot_key)

    @staticmethod
    def _theme_for_hour(hour: int) -> str:
        if 0 <= hour < 6:
            return "bedtime"  # 凌晨 0-5 点算深夜
        if hour < 12:
            return "morning"
        if hour < 18:
            return "noon"
        if hour <= 20:
            return "evening"
        return "bedtime"

    def _today_schedule_activity_map(self) -> dict:
        """Return {HH:mm: activity} for today's persisted schedule."""
        today_str = datetime.now().strftime("%Y-%m-%d")
        activity_by_time = {}
        try:
            schedule_text = self._today_schedule_text()
            for h_str, m_str, activity in re.findall(r'(\d{1,2}):(\d{2})\s*(.*)', schedule_text):
                h, m = int(h_str), int(m_str)
                if 0 <= h <= 23 and 0 <= m <= 59:
                    activity_by_time[f"{h:02d}:{m:02d}"] = activity.strip()
        except Exception as e:
            logger.error(f"读取今日生图活动映射失败: {e}")
        return activity_by_time

    def list_photo_jobs(self) -> list:
        """List actual pending APScheduler photo jobs for the Web UI."""
        activity_by_time = self._today_schedule_activity_map()
        jobs = []
        today_str = datetime.now().strftime("%Y-%m-%d")
        seen_times = set()
        for job in self.aps.get_jobs():
            if not job.id.startswith("photo_dynamic_"):
                continue

            run_at = getattr(job, "next_run_time", None)
            if not run_at:
                continue

            try:
                local_run_at = run_at.astimezone(self.aps.timezone) if run_at.tzinfo else run_at
            except Exception:
                local_run_at = run_at

            time_text = f"{local_run_at.hour:02d}:{local_run_at.minute:02d}"
            theme = job.args[0] if getattr(job, "args", None) else self._theme_for_hour(local_run_at.hour)
            seen_times.add(time_text)
            jobs.append({
                "id": job.id,
                "type": "photo",
                "status": "scheduled",
                "theme": theme,
                "time": time_text,
                "run_at": local_run_at.isoformat(),
                "activity": activity_by_time.get(time_text, ""),
                "source": "apscheduler",
            })

        for slot_key in sorted(self._photo_jobs_inflight):
            date_text, _, time_text = slot_key.partition(" ")
            if date_text != today_str or not re.match(r'^\d{2}:\d{2}$', time_text):
                continue
            if time_text in seen_times:
                continue
            seen_times.add(time_text)
            hour = int(time_text.split(":", 1)[0])
            jobs.append({
                "id": f"photo_backfill_{time_text.replace(':', '_')}",
                "type": "photo",
                "status": "running",
                "theme": self._theme_for_hour(hour),
                "time": time_text,
                "run_at": datetime.now().isoformat(),
                "activity": activity_by_time.get(time_text, ""),
                "source": "backfill",
            })

        for slot_key, failed in sorted(self._failed_photo_jobs.items()):
            date_text, _, time_text = slot_key.partition(" ")
            if date_text != today_str or not re.match(r'^\d{2}:\d{2}$', time_text):
                continue
            if time_text in seen_times or self._check_photo_exists_for_slot(today_str, time_text):
                continue
            seen_times.add(time_text)
            hour = int(time_text.split(":", 1)[0])
            jobs.append({
                "id": f"photo_failed_{time_text.replace(':', '_')}",
                "type": "photo",
                "status": "failed",
                "theme": failed.get("theme") or self._theme_for_hour(hour),
                "time": time_text,
                "run_at": failed.get("failed_at") or datetime.now().isoformat(),
                "activity": failed.get("activity") or activity_by_time.get(time_text, ""),
                "source": "failed",
                "error": failed.get("error", ""),
                "error_summary": self._summarize_photo_failure(failed.get("error", ""))
                or failed.get("error_summary", ""),
            })

        jobs.sort(key=lambda item: item["time"])
        return jobs

    def _slot_key_for_schedule_time(self, schedule_time: str) -> tuple[str, str, str]:
        """Return (slot_key, HH:mm, activity) for a schedule-time string."""
        match = re.match(r'\s*(\d{1,2}):(\d{2})\s*(.*)', schedule_time or "")
        if not match:
            return "", "", ""
        time_text = f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
        today_str = datetime.now().strftime("%Y-%m-%d")
        return f"{today_str} {time_text}", time_text, match.group(3).strip()

    @staticmethod
    def _summarize_photo_failure(detail: str) -> str:
        """Return a short UI-friendly reason for a photo generation failure."""
        text = detail or ""
        lower = text.lower()
        reasons = []
        if "ssleoferror" in lower or "unexpected_eof_while_reading" in lower:
            reasons.append("GPT Image 上游 SSL 连接被断开")
        elif "max retries exceeded" in lower:
            reasons.append("GPT Image 上游连接重试耗尽")
        elif "timeout" in lower or "timed out" in lower:
            reasons.append("生图请求超时")
        elif "unauthorized" in lower or "invalid api key" in lower or "401" in lower:
            reasons.append("API Key 校验失败")
        elif "rate limit" in lower or "429" in lower:
            reasons.append("上游限流")
        elif "direct gpt api error 502" in lower:
            reasons.append("GPT Image 上游 502")
        elif "direct gpt api error 503" in lower:
            reasons.append("GPT Image 上游 503")
        elif "direct gpt api error 504" in lower:
            reasons.append("GPT Image 上游 504")
        elif "path not found" in lower or "direct gpt api error 404" in lower or " 404" in lower:
            reasons.append("GPT Image Base URL 端点错误")
        elif "generation failed" in lower:
            reasons.append("生图链路返回失败")

        if "gitee fallback is disabled" in lower:
            reasons.append("Gitee 兜底未启用")

        if reasons:
            return "；".join(dict.fromkeys(reasons))
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        return first_line[:120] if first_line else "未知失败"

    async def retry_photo_job(self, schedule_time: str) -> dict:
        """Queue an immediate retry/backfill for a schedule time."""
        slot_key, time_text, activity_hint = self._slot_key_for_schedule_time(schedule_time)
        if not slot_key:
            return {"status": "error", "message": "invalid_time"}

        today_str = datetime.now().strftime("%Y-%m-%d")
        if self._check_photo_exists_for_slot(today_str, time_text):
            return {"status": "already_done", "time": time_text}

        activity = self._today_schedule_activity_map().get(time_text, activity_hint)
        if not activity:
            return {"status": "error", "time": time_text, "message": "schedule_time_not_found"}

        async with self._inflight_lock:
            if slot_key in self._photo_jobs_inflight:
                return {"status": "running", "time": time_text}
            snapshot = self._photo_quota_snapshot(
                today_str,
                include_scheduled=True,
                exclude_slot_key=slot_key,
            )
            max_daily, completed, failed, inflight, scheduled, planned_total, remaining = snapshot
            if remaining <= 0:
                return {
                    "status": "limit_reached",
                    "time": time_text,
                    "message": f"今日生图计划已达上限 {planned_total}/{max_daily}",
                    "max_daily": max_daily,
                    "completed_today": completed,
                    "failed_today": failed,
                    "running_today": inflight,
                    "scheduled_today": scheduled,
                    "planned_today": planned_total,
                }
            self._photo_jobs_inflight.add(slot_key)
            if self._failed_photo_jobs.pop(slot_key, None) is not None:
                self._save_failed_photo_jobs()

        theme = self._theme_for_hour(int(time_text.split(":", 1)[0]))
        schedule_text_for_job = f"{time_text} {activity}".strip()
        logger.info(f"手动重试生图任务: {time_text} theme={theme} activity={activity[:30]}")
        asyncio.create_task(self._backfill_photo_job(theme, schedule_text_for_job, slot_key))
        return {"status": "queued", "time": time_text, "theme": theme, "activity": activity}

    async def photo_job(self, theme: str, schedule_time: str = "", quota_reserved: bool = False) -> bool:
        """定时生图任务 - 调用 generate.py 完整链路"""
        logger.info(f"开始定时生图: theme={theme}, schedule_time={schedule_time}")
        slot_key, time_text, activity = self._slot_key_for_schedule_time(schedule_time)
        reserved_slot = False
        if slot_key and time_text:
            today_str, _, _ = slot_key.partition(" ")
            async with self._inflight_lock:
                if self._check_photo_exists_for_slot(today_str, time_text):
                    logger.info(f"跳过生图任务（该时间点已有图片）: {time_text}")
                    return True
                already_inflight = slot_key in self._photo_jobs_inflight
                if already_inflight and not quota_reserved:
                    logger.info(f"跳过生图任务（该时间点已有任务进行中）: {time_text}")
                    return True
                if not already_inflight:
                    snapshot = self._photo_quota_snapshot(today_str, exclude_slot_key=slot_key)
                    max_daily, _completed, _failed, inflight, _scheduled, planned_total, remaining = snapshot
                    if remaining <= 0:
                        logger.info(
                            f"跳过生图任务（今日生图计划已达上限）: {time_text} "
                            f"planned={planned_total}, inflight={inflight}, max={max_daily}"
                        )
                        return True
                    self._photo_jobs_inflight.add(slot_key)
                    reserved_slot = True
                elif quota_reserved:
                    logger.info(
                        f"复用已占用的生图额度: {time_text} "
                        f"slot_key={slot_key}"
                    )
        cmd = [
            self.image_gen.python_executable,
            self.image_gen.generate_script,
            "--theme", theme,
            "--caption",
            "--source", "cron",
        ]
        base_style = self._today_schedule_base_style()
        if base_style:
            cmd.extend(["--style", base_style])
            logger.info(f"定时生图使用当天 LLM 选择的底模: {base_style}")
        if schedule_time:
            cmd.extend(["--schedule-time", schedule_time])
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=image_process_timeout(self.config, with_reference_fallback=True),
                    cwd=self.image_gen.script_dir,
                    env=self.image_gen.build_env(),
                )
            )
            if result.returncode == 0:
                # 解析输出获取图片路径和caption
                image_path = ""
                caption_text = ""
                for line in result.stdout.split("\n"):
                    line = line.strip()
                    if line.startswith("SUCCESS:"):
                        image_path = line.split("SUCCESS:", 1)[1].strip()
                    if "Synced to gallery" in line:
                        logger.info(f"定时生图成功: {line}")
                    if "CAPTION:" in line:
                        caption_text = line.split("CAPTION:", 1)[1].strip()
                        logger.info(f"Caption: {caption_text}")
                logger.info(f"定时生图完成: theme={theme}")
                if slot_key:
                    if self._failed_photo_jobs.pop(slot_key, None) is not None:
                        self._save_failed_photo_jobs()

                # 按设置的推送渠道发送到 TG / 微信。
                if image_path:
                    caption_text = self._gallery_caption_for_image(image_path, caption_text)
                    send_ok = await self._send_generated_photo(image_path, caption_text)
                    if not send_ok:
                        logger.warning(f"推送未完全成功: image={image_path}")
                return True
            else:
                detail = (result.stderr or result.stdout or "").strip()
                if len(detail) > 2000:
                    detail = "..." + detail[-2000:]
                logger.error(f"定时生图失败: theme={theme}, stderr={detail}")
                if slot_key:
                    summary = self._summarize_photo_failure(detail)
                    self._failed_photo_jobs[slot_key] = {
                        "theme": theme,
                        "time": time_text,
                        "activity": activity,
                        "failed_at": datetime.now().isoformat(),
                        "error": detail[-1200:],
                        "error_summary": summary,
                    }
                    self._save_failed_photo_jobs()
                return False
        except subprocess.TimeoutExpired:
            timeout = image_process_timeout(self.config, with_reference_fallback=True)
            logger.error(f"定时生图超时: theme={theme} ({timeout}s)")
            if slot_key:
                self._failed_photo_jobs[slot_key] = {
                    "theme": theme,
                    "time": time_text,
                    "activity": activity,
                    "failed_at": datetime.now().isoformat(),
                    "error": f"timeout after {timeout}s",
                    "error_summary": "生图请求超时",
                }
                self._save_failed_photo_jobs()
            return False
        except Exception as e:
            logger.error(f"定时生图异常: theme={theme}, {e}")
            if slot_key:
                summary = self._summarize_photo_failure(str(e))
                self._failed_photo_jobs[slot_key] = {
                    "theme": theme,
                    "time": time_text,
                    "activity": activity,
                    "failed_at": datetime.now().isoformat(),
                    "error": str(e),
                    "error_summary": summary,
                }
                self._save_failed_photo_jobs()
            return False
        finally:
            if reserved_slot:
                async with self._inflight_lock:
                    self._photo_jobs_inflight.discard(slot_key)

    def _runtime_keys_config(self) -> dict:
        return load_json_file(api_keys_path(self.data_dir))

    def _push_delivery_config(self) -> dict:
        integrations = self.config.get("integrations", {}) if isinstance(self.config.get("integrations"), dict) else {}
        keys = self._runtime_keys_config()
        channel = normalize_push_channel(
            keys.get("push_channel")
            or os.getenv("ZHUZHU_SEND_CHANNEL", "")
            or integrations.get("push_channel", "")
        )
        persona = load_runtime_persona(self.config, self.data_dir)
        persona_source = normalize_persona_source(persona.get("persona_source") or keys.get("persona_source"))
        agent = auto_push_agent(persona_source, channel)
        return {
            "channel": channel,
            "agent": agent,
            "telegram_target": (
                str(keys.get("telegram_target") or "").strip()
                or os.getenv("ZHUZHU_SEND_TARGET", "").strip()
                or os.getenv("TELEGRAM_CHAT_ID", "").strip()
                or str(integrations.get("telegram_target") or "").strip()
            ),
            "telegram_account": (
                os.getenv("ZHUZHU_SEND_ACCOUNT", "").strip()
                or str(integrations.get("telegram_account") or "default").strip()
            ),
            "wechat_target": str(integrations.get("wechat_target") or "weixin").strip(),
        }

    async def _send_generated_photo(self, image_path: str, caption: str) -> bool:
        """Send generated image using the configured push channel."""
        integrations = self.config.get("integrations", {}) if isinstance(self.config.get("integrations"), dict) else {}
        if integrations.get("send_enabled") is False:
            logger.info("图片推送已在配置中关闭")
            return True

        delivery = self._push_delivery_config()
        channel = delivery["channel"]
        agent = delivery["agent"]
        logger.info(f"准备推送图片: channel={channel}, agent={agent}")

        if agent == "openclaw":
            openclaw_ok = await self._send_to_openclaw(channel, image_path, caption, delivery)
            if openclaw_ok:
                return True
            logger.warning(f"OpenClaw 推送失败，尝试 Hermes fallback: channel={channel}")

        return await self._send_to_hermes_channel(channel, image_path, caption, delivery)

    def _gallery_caption_for_image(self, image_path: str, fallback: str = "") -> str:
        """Use the gallery card caption as the single source for outbound copy."""
        filename = os.path.basename(str(image_path or ""))
        if not filename:
            return fallback or ""
        try:
            data = ScheduleStore(self.data_dir).load()
            entry = data.get(filename)
            if not isinstance(entry, dict):
                for value in data.values():
                    if isinstance(value, dict) and value.get("image_filename") == filename:
                        entry = value
                        break
            caption = str((entry or {}).get("caption") or "").strip()
            return caption or fallback or ""
        except Exception as e:
            logger.warning("读取画廊小心思失败，使用生成输出文案: %s", e)
            return fallback or ""

    async def _send_to_wechat(self, image_path: str, caption: str) -> bool:
        """Send image and caption to WeChat via hermes CLI."""
        return await self._send_to_hermes_channel("wechat", image_path, caption, self._push_delivery_config())

    async def _send_to_hermes_channel(self, channel: str, image_path: str, caption: str, delivery: dict) -> bool:
        """Send image and optional caption via Hermes CLI."""
        integrations = self.config.get("integrations", {})
        hermes_cmd = integrations.get("hermes_cli", "") or os.getenv("HERMES_CLI", "") or shutil.which("hermes")
        if not hermes_cmd:
            candidate = os.path.join(os.path.expanduser("~"), ".hermes", "hermes-agent", "venv", "bin", "hermes")
            if os.path.exists(candidate):
                hermes_cmd = candidate
        if not hermes_cmd:
            logger.warning("未找到 Hermes CLI，跳过推送")
            return False
        channel = normalize_push_channel(channel)
        target = delivery.get("wechat_target") if channel == "wechat" else (
            str(integrations.get("hermes_telegram_target") or "").strip()
            or "telegram"
        )
        label = "微信" if channel == "wechat" else "TG"

        image_ok = await self._run_hermes_send(hermes_cmd, target, f"MEDIA:{image_path}", f"{label}图片")
        if not image_ok:
            logger.error(f"{label}发送失败: 图片未送达，跳过文案发送")
            return False

        caption_ok = True
        if caption:
            logger.info(f"{label}图片发送成功，等待 {WECHAT_CAPTION_DELAY_SECONDS}s 后发送文案以降低限流概率")
            await asyncio.sleep(WECHAT_CAPTION_DELAY_SECONDS)
            caption_ok = await self._run_hermes_send(hermes_cmd, target, caption, f"{label}文案")

        if image_ok and caption_ok:
            logger.info(f"{label}发送完成")
            return True
        logger.warning(f"{label}发送部分成功: image_ok={image_ok}, caption_ok={caption_ok}")
        return False

    async def _send_to_openclaw(self, channel: str, image_path: str, caption: str, delivery: dict) -> bool:
        """Send image and optional caption through OpenClaw when available."""
        integrations = self.config.get("integrations", {}) if isinstance(self.config.get("integrations"), dict) else {}
        openclaw_cmd = integrations.get("openclaw_cli", "") or os.getenv("OPENCLAW_CLI", "") or shutil.which("openclaw")
        if not openclaw_cmd:
            logger.warning("未找到 OpenClaw CLI，无法使用 OpenClaw 推送")
            return False

        channel = normalize_push_channel(channel)
        if channel == "telegram":
            openclaw_channel = str(integrations.get("openclaw_telegram_channel") or "telegram").strip()
            target = delivery.get("telegram_target", "")
            account = delivery.get("telegram_account", "default") or "default"
        else:
            openclaw_channel = str(
                integrations.get("openclaw_wechat_channel")
                or os.getenv("OPENCLAW_WECHAT_CHANNEL", "")
                or "openclaw-weixin"
            ).strip()
            target = (
                os.getenv("OPENCLAW_WECHAT_TARGET", "").strip()
                or str(integrations.get("openclaw_wechat_target") or "").strip()
                or delivery.get("wechat_target", "")
            )
            account = str(integrations.get("openclaw_wechat_account") or "default").strip()

        if not target:
            logger.warning(f"OpenClaw 推送目标未配置: channel={channel}")
            return False

        cmd = [
            openclaw_cmd,
            "message",
            "send",
            "--channel",
            openclaw_channel,
            "--account",
            account,
            "--target",
            target,
            "--media",
            image_path,
            "--message",
            caption or "",
            "--json",
        ]
        label = "TG" if channel == "telegram" else "微信"
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=WECHAT_SEND_TIMEOUT_SECONDS,
                ),
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"OpenClaw {label}推送超时")
            return False
        except Exception as e:
            logger.warning(f"OpenClaw {label}推送异常: {e}")
            return False

        output = self._hermes_send_output(result.stdout, result.stderr)
        if result.returncode == 0:
            logger.info(f"OpenClaw {label}推送成功")
            return True
        logger.warning(f"OpenClaw {label}推送失败: exit={result.returncode}, output={output}")
        return False

    async def _run_hermes_send(self, hermes_cmd: str, target: str, message: str, label: str) -> bool:
        """Run `hermes send` with outer retry/backoff for Weixin rate limits."""
        attempts = 1 + len(WECHAT_RETRY_DELAYS_SECONDS)
        last_output = ""

        for attempt_idx in range(attempts):
            attempt_no = attempt_idx + 1
            if attempt_idx:
                delay = WECHAT_RETRY_DELAYS_SECONDS[attempt_idx - 1]
                logger.info(f"{label}发送重试等待 {delay}s ({attempt_no}/{attempts})")
                await asyncio.sleep(delay)

            logger.info(f"发送{label}: attempt={attempt_no}/{attempts}")
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: subprocess.run(
                        [hermes_cmd, "send", "--json", "--to", target, message],
                        capture_output=True,
                        text=True,
                        timeout=WECHAT_SEND_TIMEOUT_SECONDS,
                    ),
                )
            except subprocess.TimeoutExpired:
                last_output = f"hermes send timed out after {WECHAT_SEND_TIMEOUT_SECONDS}s"
                logger.warning(f"{label}发送超时: attempt={attempt_no}/{attempts}")
                continue
            except Exception as e:
                last_output = str(e)
                logger.warning(f"{label}发送异常: attempt={attempt_no}/{attempts}, error={e}")
                continue

            output = self._hermes_send_output(result.stdout, result.stderr)
            if result.returncode == 0:
                logger.info(f"{label}发送成功")
                return True

            last_output = output or f"exit code {result.returncode}"
            retryable = self._is_retryable_wechat_error(last_output)
            log_fn = logger.warning if retryable and attempt_no < attempts else logger.error
            log_fn(
                f"{label}发送失败: attempt={attempt_no}/{attempts}, "
                f"exit={result.returncode}, retryable={retryable}, output={last_output}"
            )
            if not retryable:
                break

        logger.error(f"{label}发送最终失败: {last_output}")
        return False

    @staticmethod
    def _hermes_send_output(stdout: str, stderr: str) -> str:
        parts = [part.strip() for part in (stdout, stderr) if part and part.strip()]
        output = "\n".join(parts)
        if len(output) > 1500:
            return "..." + output[-1500:]
        return output

    @staticmethod
    def _is_retryable_wechat_error(output: str) -> bool:
        text = (output or "").lower()
        return any(marker in text for marker in WECHAT_RETRYABLE_MARKERS)

    def _schedule_time(self) -> tuple[int, int]:
        raw = str(self.config.get("config", {}).get("schedule_time", "07:00")).strip()
        match = re.match(r"^(\d{1,2}):(\d{2})$", raw)
        if not match:
            logger.warning(f"schedule_time 配置无效，使用默认 07:00: {raw}")
            return 7, 0
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            logger.warning(f"schedule_time 配置超出范围，使用默认 07:00: {raw}")
            return 7, 0
        return hour, minute

    def start(self):
        """启动所有服务（同步入口）"""
        asyncio.run(self._async_start())

    async def _async_start(self):
        """异步启动"""
        # 每日日程生成（07:00）
        self.aps.add_job(
            self.daily_job,
            "cron",
            hour=self._schedule_time()[0],
            minute=self._schedule_time()[1],
            id="daily_schedule",
        )

        self.aps.start()
        sched_hour, sched_minute = self._schedule_time()
        logger.info(f"定时任务已设置: 日程({sched_hour:02d}:{sched_minute:02d}) + 动态生图(根据日程时间)")

        # 启动 Web 服务器
        runner = web.AppRunner(self.web_server.app)
        await runner.setup()
        site = web.TCPSite(runner, self.web_server.host, self.web_server.port)
        await site.start()
        logger.info(f"画廊启动: http://{self.web_server.host}:{self.web_server.port}")

        # 检查今天是否已有完整日程；启动后需要恢复内存里的动态生图任务
        today_schedule = ""
        need_generate = True
        today_entry = self._today_schedule_entry()
        if today_entry:
            today_schedule = today_entry.get("schedule", "")
            missing_periods = self._schedule_missing_required_periods(today_schedule)
            if missing_periods:
                logger.warning(f"今日日程缺少时间段 {missing_periods}，后台刷新修复")
            else:
                need_generate = False

        if need_generate:
            logger.info("今日尚未生成完整日程，后台生成中...")
            asyncio.create_task(self.daily_job())
        else:
            logger.info("今日已有数据")
            await self._schedule_dynamic_photos(today_schedule)

        # 保持运行
        await asyncio.Event().wait()


def main():
    config_path = resolve_config_path()
    app = PortraitGalleryApp(config_path)

    app.start()


if __name__ == "__main__":
    main()
