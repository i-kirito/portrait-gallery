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
import random
import re
import shutil
import sys
import subprocess
import time
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, time as dt_time, timedelta
from typing import Optional

from aiohttp import web
from apscheduler.events import EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from data import DailyEntry
from scheduler import DailyScheduler
from image_gen import ImageGenerator
from image_editing import (
    MAX_IMAGE_EDIT_SCHEDULE_DESCRIPTION_LENGTH,
    build_precision_image_edit_prompt,
    image_schedule_description,
    image_edit_target_label,
    normalize_image_edit_instruction,
    normalize_image_edit_schedule_description,
    normalize_image_edit_target,
    replace_image_schedule_description,
    rewrite_image_edit_schedule_description,
)
from photo_plan_edit import (
    normalize_schedule_clock,
    replace_schedule_activity,
    update_schedule_details_activity,
)
from outfit_plan_edit import (
    MAX_OUTFIT_PLAN_EDIT_LENGTH,
    normalize_outfit_plan_field,
    normalize_outfit_plan_value,
    replace_outfit_plan_field,
    update_schedule_details_outfit,
)
from web_server import GalleryServer
from store import ImageMetadataStore, ScheduleStore
from text_repair import repair_mojibake_text
from zhuzhu.core import build_caption_fallback
from characters import (
    LOCAL_CHARACTER_SOURCE,
    build_character_image_prompt,
    build_group_image_prompt,
    load_character_registry,
    sanitize_image_prompt,
    upsert_manual_character,
)
from settings import (
    DEFAULT_QUALITY_PREFIX,
    api_keys_path,
    apply_network_env,
    auto_push_agent,
    configured_timezone,
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
    resolve_project_root,
    resolve_script_dir,
    schedule_image_size,
)

TODAY_PHOTO_SOURCES = {"cron", "web"}
FAILED_SCHEDULE_TEXT = "生成失败"
CAPTION_DELAY_SECONDS = 3
WECHAT_CAPTION_DELAY_SECONDS = 45
WECHAT_SEND_TIMEOUT_SECONDS = 90
PHOTO_JOB_INFLIGHT_STALE_GRACE_SECONDS = 120
WECHAT_RETRY_DELAYS_SECONDS = (60, 180)
WECHAT_COOLDOWN_BUFFER_SECONDS = 15
PHOTO_JOB_MISFIRE_GRACE_SECONDS = 10 * 60
SCHEDULE_GENERATION_DEFAULT_START_MINUTE = 3 * 60
SCHEDULE_GENERATION_DEFAULT_END_MINUTE = 6 * 60
SCHEDULE_GENERATION_RETRY_DELAY_SECONDS = 10 * 60
PHOTO_QUIET_START_MINUTE = 3 * 60
PHOTO_QUIET_END_MINUTE = 6 * 60
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
WECHAT_CONTEXT_ERROR_MARKERS = (
    "requires a fresh wechat conversation context",
    "send the bot a wechat message first",
)
REFERENCE_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
REFERENCE_METADATA_KEYS = ("id", "filename", "url", "label", "prompt", "source", "selection_mode", "selection_reason")
TODAY_SCHEDULE_REFERENCE_MODE = "today_schedule"
LOG_RETENTION_DAYS = 3
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
ACCESS_LOG_RE = re.compile(
    r'"(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+HTTP/[^"]+"\s+(?P<status>\d{3})'
)
SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:key|api_key|token|access_token)=)[^&\s\"]+"
)
ICON_PROBE_BASENAME_RE = re.compile(
    r'^(?:favicon(?:-\d+x\d+)?\.(?:ico|png|svg)|'
    r'apple-touch-icon(?:-\d+x\d+)?\.png|'
    r'icon-maskable(?:-\d+(?:x\d+)?)?\.png)$',
    re.IGNORECASE,
)


class PhotoDeliveryError(RuntimeError):
    """Raised so APScheduler records a generated-but-undelivered photo as failed."""


def _is_icon_probe_path(path: str) -> bool:
    clean_path = str(path or "").split("?", 1)[0].rstrip("/")
    basename = clean_path.rsplit("/", 1)[-1]
    return bool(ICON_PROBE_BASENAME_RE.fullmatch(basename))


class _UsefulAccessLogFilter(logging.Filter):
    """Keep real HTTP failures while dropping routine and icon-probe noise."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "aiohttp.access":
            return True
        message = record.getMessage()
        redacted = SENSITIVE_QUERY_RE.sub(r"\1[REDACTED]", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        match = ACCESS_LOG_RE.search(redacted)
        if not match:
            return True
        if _is_icon_probe_path(match.group("path")):
            return False
        return int(match.group("status")) >= 400


def _persistent_log_path() -> str:
    override = str(os.getenv("HERMES_GALLERY_LOG", "") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    root = resolve_project_root(os.getenv("CONFIG_PATH", ""))
    return str(root / "logs" / "gallery.log")


def _stream_targets_path(stream, path: str) -> bool:
    try:
        if not path or not os.path.exists(path):
            return False
        stream_stat = os.fstat(stream.fileno())
        path_stat = os.stat(path)
        return stream_stat.st_dev == path_stat.st_dev and stream_stat.st_ino == path_stat.st_ino
    except Exception:
        return False


def _cleanup_old_log_files(log_path: str, retention_days: int = LOG_RETENTION_DAYS):
    log_dir = os.path.dirname(log_path)
    log_name = os.path.basename(log_path)
    if not log_dir or not os.path.isdir(log_dir):
        return
    cutoff = time.time() - max(1, retention_days) * 86400
    for filename in os.listdir(log_dir):
        if filename == log_name or not filename.startswith(f"{log_name}."):
            continue
        path = os.path.join(log_dir, filename)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            continue


def configure_logging() -> str:
    log_path = _persistent_log_path()
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    _cleanup_old_log_files(log_path)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT)
    access_logger = logging.getLogger("aiohttp.access")
    if not any(isinstance(item, _UsefulAccessLogFilter) for item in access_logger.filters):
        access_logger.addFilter(_UsefulAccessLogFilter())

    existing_files = {
        os.path.abspath(getattr(handler, "baseFilename", ""))
        for handler in root_logger.handlers
        if getattr(handler, "baseFilename", "")
    }
    if os.path.abspath(log_path) not in existing_files:
        file_handler = TimedRotatingFileHandler(
            log_path,
            when="midnight",
            interval=1,
            backupCount=LOG_RETENTION_DAYS,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        root_logger.addHandler(file_handler)

    stdout_is_log = _stream_targets_path(sys.stdout, log_path)
    stderr_is_log = _stream_targets_path(sys.stderr, log_path)
    has_console = any(
        isinstance(handler, logging.StreamHandler)
        and not getattr(handler, "baseFilename", "")
        for handler in root_logger.handlers
    )
    if not has_console and not (stdout_is_log or stderr_is_log):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(logging.INFO)
        root_logger.addHandler(console_handler)

    return log_path


PERSISTENT_LOG_PATH = configure_logging()
logger = logging.getLogger("portrait_gallery")
logger.info("持久化日志已启用: %s，自动清理 %s 天前的轮转日志", PERSISTENT_LOG_PATH, LOG_RETENTION_DAYS)


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
        if "caption" in entry_dict:
            entry_dict["caption"] = repair_mojibake_text(entry_dict.get("caption", "")).strip()
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
                preserve_fields = ("favorite", "source", "time", "model_name", "base_style", "reference_query")
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
        self.web_server.on_generate_character = self.generate_character
        self.web_server.on_generate_character_reference = self.generate_character_reference
        self.web_server.on_generate_group = self.generate_group
        self.web_server.on_list_photo_jobs = self.list_photo_jobs
        self.web_server.on_refresh_schedule = self.refresh_schedule
        self.web_server.on_rebuild_photo_jobs = self.rebuild_photo_jobs
        self.web_server.on_retry_photo_job = self.retry_photo_job
        self.web_server.on_update_photo_plan = self.update_photo_plan
        self.web_server.on_update_outfit_plan = self.update_outfit_plan
        self.web_server.on_reroll_image = self.reroll_image
        self.web_server.on_edit_image = self.edit_image

        # APScheduler
        timezone = self.config.get("config", {}).get("timezone", "Asia/Shanghai")
        self.aps = AsyncIOScheduler(timezone=timezone)
        self.aps.add_listener(self._handle_scheduler_event, EVENT_JOB_MISSED)

        # Backfill photo job controls
        self._photo_jobs_inflight: set[str] = set()
        self._photo_jobs_inflight_started: dict[str, float] = {}
        self._photo_job_schedule_meta: dict[str, dict] = {}
        self._failed_photo_jobs: dict[str, dict] = self._load_failed_photo_jobs()
        self._inflight_lock = asyncio.Lock()
        self._backfill_semaphore = asyncio.Semaphore(1)
        self._hermes_send_lock = asyncio.Lock()
        self._hermes_send_cooldown_until = 0.0
        self._last_delivery_error = ""
        self._schedule_refresh_task: Optional[asyncio.Task] = None
        self._recover_orphaned_generated_photos()

    def _now(self) -> datetime:
        return datetime.now(configured_timezone(getattr(self, "config", {})))

    def _today(self):
        return self._now().date()

    def _naive_now(self) -> datetime:
        return self._now().replace(tzinfo=None)

    @staticmethod
    def _schedule_reference_source(source: str) -> bool:
        return str(source or "").strip().lower() in {
            "daily_initial",
            "cron",
            "cron_reroll",
            "generate_now",
            "group_chat",
        }

    async def _select_reference_for_generation(self, context: dict, include_wardrobe: Optional[bool] = None) -> dict:
        source = str((context or {}).get("source") or "").strip().lower()
        if include_wardrobe is None:
            include_wardrobe = not self._schedule_reference_source(source)
        text = json.dumps(context, ensure_ascii=False, default=str)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.web_server._select_reference_for_generation_sync(
                text,
                include_wardrobe=include_wardrobe,
            ),
        )

    @staticmethod
    def _selected_reference_metadata(selected_reference: dict) -> dict:
        if not isinstance(selected_reference, dict):
            return {}
        return {
            key: selected_reference.get(key, "")
            for key in REFERENCE_METADATA_KEYS
            if selected_reference.get(key, "") not in ("", None)
        }

    def _resolve_selected_reference_path(self, selected_reference: dict) -> str:
        if not isinstance(selected_reference, dict):
            return ""
        resolver = getattr(getattr(self, "web_server", None), "_resolve_reference_image", None)
        filename = str(selected_reference.get("filename") or "").strip()
        candidates = [
            selected_reference.get("path", ""),
            selected_reference.get("url", ""),
            filename,
        ]
        if filename:
            candidates.extend([f"/local-refs/{filename}", f"/refs/{filename}"])
        for value in candidates:
            raw = str(value or "").strip()
            if not raw:
                continue
            if callable(resolver):
                resolved = resolver(raw, allow_any_path=True)
                if resolved:
                    return resolved
            expanded = os.path.abspath(os.path.expanduser(raw))
            if os.path.isfile(expanded):
                return expanded
        return ""

    @staticmethod
    def _schedule_entry_time_sort_value(entry: dict) -> int:
        for field in ("schedule_time", "time"):
            match = re.match(r"\s*(\d{1,2}):(\d{2})", str((entry or {}).get(field) or ""))
            if match:
                return int(match.group(1)) * 60 + int(match.group(2))
        return -1

    def _saved_today_schedule_reference(self) -> dict:
        today_str = self._today().isoformat()
        try:
            all_data = ScheduleStore(self.data_dir).load()
        except Exception as e:
            logger.error("读取今日日程参考图失败: %s", e)
            return {}

        candidates: list[tuple[int, dict, dict, str]] = []
        for entry in all_data.values():
            if not isinstance(entry, dict):
                continue
            if entry.get("date") != today_str or not entry.get("image_filename"):
                continue
            if str(entry.get("source") or "").strip() not in TODAY_PHOTO_SOURCES:
                continue
            selected = entry.get("selected_reference") if isinstance(entry.get("selected_reference"), dict) else {}
            path = self._resolve_selected_reference_path(selected)
            if not path:
                continue
            candidates.append((self._schedule_entry_time_sort_value(entry), entry, selected, path))
        if not candidates:
            return {}

        _, entry, selected, path = max(candidates, key=lambda item: item[0])
        result = dict(selected)
        result["path"] = path
        result.setdefault("selection_mode", "saved_schedule")
        result.setdefault("selection_reason", f"saved_schedule:{entry.get('schedule_time') or entry.get('time') or ''}")
        return result

    async def _today_schedule_reference_for_generation(
        self,
        scene_prompt: str,
        characters: list[dict],
        generation_type: str,
    ) -> dict:
        saved_reference = self._saved_today_schedule_reference()
        if saved_reference.get("path"):
            return saved_reference

        daily_entry = self._today_schedule_entry()
        if not daily_entry:
            return {}
        selected_reference = await self._select_reference_for_generation({
            "source": "group_chat",
            "reference_mode": TODAY_SCHEDULE_REFERENCE_MODE,
            "generation_type": generation_type,
            "scene_request": scene_prompt,
            "character_names": [
                str(character.get("name") or character.get("id") or "")
                for character in characters
                if isinstance(character, dict)
            ],
            "outfit_style": daily_entry.get("outfit_style", ""),
            "outfit": daily_entry.get("outfit", ""),
            "reference_query": daily_entry.get("reference_query", ""),
            "prompt": daily_entry.get("prompt", ""),
            "schedule": daily_entry.get("schedule", ""),
            "schedule_prompt": daily_entry.get("schedule_prompt", ""),
            "schedule_details": daily_entry.get("schedule_details", []),
        }, include_wardrobe=False)
        return selected_reference if selected_reference.get("path") else {}

    def _save_generation_reference_metadata(
        self,
        filename: str,
        ref_image: str,
        selected_reference: dict,
        reference_mode: str = "",
    ):
        if not filename:
            return
        ref_image = str(ref_image or "").strip()
        selected_payload = self._selected_reference_metadata(selected_reference)
        if not ref_image and not selected_payload and not reference_mode:
            return
        ref_display = self._reference_path_for_display(ref_image, selected_reference) if ref_image else ""
        metadata_patch = {}
        if selected_payload:
            metadata_patch["selected_reference"] = selected_payload
        if reference_mode:
            metadata_patch["reference_mode"] = reference_mode
        if ref_image:
            metadata_patch.update({
                "requested_generation_mode": "img2img",
                "generation_mode": "img2img",
                "ref_image": os.path.basename(ref_image),
                "ref_image_path": ref_display,
                "requested_ref_image": os.path.basename(ref_image),
                "requested_ref_image_path": ref_display,
                "custom_ref_mode": "reference",
            })

        try:
            store = ScheduleStore(self.data_dir)

            def _update(all_data):
                targets = []
                if isinstance(all_data.get(filename), dict):
                    targets.append(all_data[filename])
                for entry in all_data.values():
                    if isinstance(entry, dict) and entry.get("image_filename") == filename and entry not in targets:
                        targets.append(entry)
                for entry in targets:
                    entry.update(metadata_patch)
                return all_data

            store.update(_update)
        except Exception as e:
            logger.error("保存生图参考图信息失败: %s", e)

        metadata_updater = getattr(getattr(self, "web_server", None), "_update_image_metadata_entry", None)
        if callable(metadata_updater):
            try:
                metadata_updater(filename, metadata_patch)
            except Exception as e:
                logger.warning("更新生图参考图元数据失败: %s", e)

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

    @staticmethod
    def _minutes_to_hhmm(total_minutes: int) -> str:
        total_minutes = max(0, min(23 * 60 + 59, int(total_minutes)))
        return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"

    @staticmethod
    def _parse_hhmm_minutes(value: str) -> Optional[int]:
        match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(value or ""))
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute

    @classmethod
    def _parse_schedule_time_range(cls, value: str) -> Optional[tuple[int, int]]:
        match = re.match(
            r"^\s*(\d{1,2})(?::(\d{2}))?\s*(?:-|~|至|到)\s*(\d{1,2})(?::(\d{2}))?\s*$",
            str(value or ""),
        )
        if not match:
            return None
        start_hour = int(match.group(1))
        start_minute = int(match.group(2) or 0)
        end_hour = int(match.group(3))
        end_minute = int(match.group(4) or 0)
        if not (
            0 <= start_hour <= 23
            and 0 <= end_hour <= 23
            and 0 <= start_minute <= 59
            and 0 <= end_minute <= 59
        ):
            return None
        start = start_hour * 60 + start_minute
        end = end_hour * 60 + end_minute
        if start >= end:
            return None
        return start, end

    def _schedule_time_config(self) -> tuple[str, int, int]:
        raw = str(self.config.get("config", {}).get("schedule_time", "03:00-06:00")).strip()
        fixed = self._parse_hhmm_minutes(raw)
        if fixed is not None:
            return "fixed", fixed, fixed
        window = self._parse_schedule_time_range(raw)
        if window:
            return "window", window[0], window[1]
        logger.warning("schedule_time 配置无效，使用默认 03:00-06:00: %s", raw)
        return "window", SCHEDULE_GENERATION_DEFAULT_START_MINUTE, SCHEDULE_GENERATION_DEFAULT_END_MINUTE

    def _next_schedule_run_time(self, *, force_tomorrow: bool = False) -> datetime:
        mode, start_minute, end_minute = self._schedule_time_config()
        now = self._naive_now()
        today = now.date()

        if mode == "fixed":
            run_at = datetime.combine(today, dt_time(start_minute // 60, start_minute % 60))
            if force_tomorrow or run_at <= now:
                run_at += timedelta(days=1)
            return run_at

        current_minute = now.hour * 60 + now.minute
        if force_tomorrow or current_minute >= end_minute:
            target_date = today + timedelta(days=1)
            low_minute = start_minute
        elif current_minute < start_minute:
            target_date = today
            low_minute = start_minute
        else:
            target_date = today
            low_minute = current_minute + 1
            if low_minute >= end_minute:
                target_date = today + timedelta(days=1)
                low_minute = start_minute

        selected_minute = random.randint(low_minute, end_minute - 1)
        return datetime.combine(target_date, dt_time(selected_minute // 60, selected_minute % 60))

    def _schedule_next_daily_job(self, *, force_tomorrow: bool = False) -> datetime:
        run_at = self._next_schedule_run_time(force_tomorrow=force_tomorrow)
        existing = self.aps.get_job("daily_schedule")
        if existing:
            existing.remove()
        self.aps.add_job(
            self.daily_job,
            "date",
            run_date=run_at,
            id="daily_schedule",
            misfire_grace_time=60 * 60,
            coalesce=True,
            max_instances=1,
        )
        logger.info("下次日程生成已设置: %s", run_at.strftime("%Y-%m-%d %H:%M"))
        return run_at

    def _is_schedule_generation_window_now(self) -> bool:
        mode, start_minute, end_minute = self._schedule_time_config()
        now = self._naive_now()
        current_minute = now.hour * 60 + now.minute
        if mode == "fixed":
            return current_minute == start_minute
        return start_minute <= current_minute < end_minute

    def _next_schedule_retry_time(self) -> Optional[datetime]:
        mode, start_minute, end_minute = self._schedule_time_config()
        if mode != "window":
            start_minute = SCHEDULE_GENERATION_DEFAULT_START_MINUTE
            end_minute = SCHEDULE_GENERATION_DEFAULT_END_MINUTE

        now = self._naive_now()
        current_minute = now.hour * 60 + now.minute
        retry_delay_minutes = max(1, SCHEDULE_GENERATION_RETRY_DELAY_SECONDS // 60)
        if current_minute < start_minute:
            retry_minute = start_minute
        elif current_minute < end_minute:
            retry_minute = min(end_minute - 1, current_minute + retry_delay_minutes)
        else:
            return None

        run_at = datetime.combine(now.date(), dt_time(retry_minute // 60, retry_minute % 60))
        if run_at <= now:
            return None
        return run_at

    def _schedule_retry_or_next_daily_job(self) -> datetime:
        retry_at = self._next_schedule_retry_time()
        if retry_at:
            existing = self.aps.get_job("daily_schedule")
            if existing:
                existing.remove()
            self.aps.add_job(
                self.daily_job,
                "date",
                run_date=retry_at,
                id="daily_schedule",
                misfire_grace_time=60 * 60,
                coalesce=True,
                max_instances=1,
            )
            logger.warning("日程生成失败，将在窗口内重试: %s", retry_at.strftime("%Y-%m-%d %H:%M"))
            return retry_at
        logger.warning("日程生成失败，当前已不在 03:00-06:00 重试窗口，改为等待下一天窗口")
        return self._schedule_next_daily_job(force_tomorrow=True)

    def _schedule_generation_ready(self, entry: Optional[DailyEntry]) -> bool:
        if not entry or entry.status != "ok" or entry.source == "fallback":
            return False
        if not entry.schedule or entry.schedule == FAILED_SCHEDULE_TEXT:
            return False
        return not self._schedule_missing_required_periods(entry.schedule)

    @staticmethod
    def _is_photo_quiet_time(hour: int, minute: int = 0) -> bool:
        total_minutes = int(hour) * 60 + int(minute)
        return PHOTO_QUIET_START_MINUTE <= total_minutes < PHOTO_QUIET_END_MINUTE

    @staticmethod
    def _is_exact_hour_time(_hour: int, minute: int = 0) -> bool:
        return int(minute) == 0

    def _is_photo_quiet_now(self) -> bool:
        now = self._now()
        return self._is_photo_quiet_time(now.hour, now.minute)

    async def generate_and_save(self) -> DailyEntry:
        """生成日程 → 生图 → 保存"""
        # 1. 生成日程
        entry = await self.scheduler_gen.generate_today()
        if not entry or entry.status != "ok" or entry.source == "fallback":
            logger.error("日程生成失败")
            if entry and entry.source != "fallback":
                save_schedule_entry(self.data_dir, entry)
            return entry

        logger.info(f"日程生成成功: {entry.outfit_style} | reference_query={entry.reference_query[:60]}")

        # 先保存 date-key 日程；后续图片条目会去掉全天计划字段，避免卡片重复承载大块日程。
        save_schedule_entry(self.data_dir, entry)

        if self._is_photo_quiet_now():
            logger.info("当前为 03:00-06:00 生图静默时段，只保存日程并恢复后续动态任务")
            await self._schedule_dynamic_photos(entry.schedule)
            return entry

        # 2. 生成图片
        selected_reference = {}
        if entry.prompt and entry.status == "ok":
            selected_reference = await self._select_reference_for_generation({
                "source": "daily_initial",
                "outfit_style": entry.outfit_style,
                "outfit": entry.outfit,
                "reference_query": entry.reference_query,
                "prompt": entry.prompt,
                "schedule": entry.schedule,
                "schedule_details": entry.schedule_details,
            })
            filename = await self.image_gen.generate_for_outfit(
                entry.prompt,
                entry.outfit_style,
                entry.base_style,
                ref_image=selected_reference.get("path", ""),
                no_auto_style=not bool(selected_reference.get("path")),
                size=schedule_image_size(self.config),
            )
            if filename:
                entry.image_filename = filename
                entry.image_path = f"/images/{filename}"
                logger.info(f"图片生成成功: {filename}")
            else:
                logger.warning("图片生成失败")

        # 3. 保存图片条目
        if entry.image_filename:
            save_schedule_entry(self.data_dir, entry)
            if selected_reference:
                store = ScheduleStore(self.data_dir)
                def _update_reference(all_data):
                    if entry.image_filename in all_data:
                        all_data[entry.image_filename]["selected_reference"] = {
                            key: selected_reference.get(key, "")
                            for key in ("id", "filename", "url", "label", "prompt", "source", "selection_mode", "selection_reason")
                        }
                    return all_data
                try:
                    store.update(_update_reference)
                except Exception as e:
                    logger.error("保存初始生图参考图信息失败: %s", e)
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
        api_description: str = "",
        selected_reference: Optional[dict] = None,
    ) -> DailyEntry:
        """自定义 prompt 生图"""
        today_str = self._today().isoformat()
        ts = int(self._now().timestamp())
        shot_type = normalize_custom_shot_type(shot_type)
        shot_label = custom_shot_label(shot_type)
        shot_prompt = custom_shot_prompt(shot_type, size)
        generation_prompt = f"{user_prompt}. {shot_prompt}"
        normalized_api_source = re.sub(r"[\s_-]+", "", str(api_source or "").strip().lower())
        entry_source = "hermes_api" if normalized_api_source in {"hermes", "hermesapi"} else "custom"
        selected_reference = selected_reference if isinstance(selected_reference, dict) else {}

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

        display_prompt = str(api_description or "").strip()
        if not display_prompt:
            display_prompt = user_prompt[:80] if entry_source != "hermes_api" else "Hermes 自定义生图：按原始描述生成的场景、动作和穿搭。"

        entry = DailyEntry(
            date=today_str,
            outfit_style="自定义",
            outfit=f"风格：自定义{' 模式：纯' if pure else ''} 视角：{shot_label} 穿搭：{display_prompt}",
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
        self._update_image_metadata_caption(filename, caption, display_prompt)
        if ref_path or selected_reference:
            self._save_generation_reference_metadata(
                filename,
                ref_path,
                selected_reference,
                reference_mode=str(selected_reference.get("selection_mode") or "custom_reference"),
            )
        logger.info(f"自定义生图成功: {filename}")
        return entry

    def _character_reference_image(self, character: dict) -> str:
        ref_image = str(character.get("reference_image") or "").strip()
        if not ref_image:
            return ""
        resolver = getattr(getattr(self, "web_server", None), "_resolve_reference_image", None)
        if callable(resolver):
            resolved = resolver(ref_image, allow_any_path=True)
            if resolved:
                return resolved
        expanded = os.path.expanduser(ref_image)
        candidates = [expanded]
        if not os.path.isabs(expanded):
            root = resolve_project_root(self.config_path, self.config)
            candidates.extend([
                str(root / expanded),
                os.path.join(self.data_dir, expanded),
                os.path.join(self.data_dir, "references", expanded),
            ])
        for candidate in candidates:
            path = os.path.abspath(candidate)
            if os.path.isfile(path):
                return path
        return ""

    def _character_generation_prefix(self) -> str:
        image_config = self.config.get("image_gen", {}) if isinstance(self.config.get("image_gen"), dict) else {}
        return str(image_config.get("quality_prefix") or DEFAULT_QUALITY_PREFIX).strip()

    @staticmethod
    def _today_schedule_reference_style_guard(has_reference: bool, style_hint: str = "") -> str:
        reference_line = (
            "Default visual style: use the attached img2img reference as the style, lighting, color, texture, and camera reference."
            if has_reference
            else "Default visual style: realistic candid smartphone photo with natural light, real texture, and camera-captured imperfections."
        )
        parts = [
            reference_line,
            "Render every participant as a real photographed person.",
            "Do not generate anime, manga, cartoon, illustration, 2D art, 3D render, game CG, doll-like plastic skin, digital painting, or stylized poster art.",
            "Treat words like anime, 二次元, VTuber, virtual, doll-like, or character only as identity/personality/aesthetic hints, never as the visual medium.",
            "Keep real skin texture, natural light, photographic lens behavior, and slightly imperfect casual framing.",
        ]
        if style_hint:
            parts.append("Apply any requested style only to clothing, mood, palette, or scene details; keep the visual medium photographic.")
        return " ".join(parts)

    @staticmethod
    def _short_display_prompt(prompt: str, limit: int = 80) -> str:
        text = re.sub(r"\s+", " ", str(prompt or "")).strip(" ，,。.!！?")
        if len(text) > limit:
            text = text[:limit].rstrip(" ，,。.!！?") + "..."
        return text

    def _entry_time_now(self) -> str:
        return self._now().strftime("%H:%M")

    async def generate_character(
        self,
        character_id: str,
        user_prompt: str,
        size: str = "",
        shot_type: str = "",
        image_model: str = "",
        style_hint: str = "",
        reference_mode: str = "",
    ) -> DailyEntry:
        """Generate one image for a selected configured character."""
        registry = load_character_registry(
            self.config,
            load_runtime_persona(self.config, self.data_dir),
            self.data_dir,
        )
        character = registry.get_character(character_id, fallback=False)
        if not character or not character.get("enabled", True):
            return DailyEntry(date=self._today().isoformat(), outfit="角色不存在或已停用", status="failed")

        shot_type = normalize_custom_shot_type(shot_type) if str(shot_type or "").strip() else ""
        shot_label = custom_shot_label(shot_type) if shot_type else ""
        shot_prompt = custom_shot_prompt(shot_type, size) if shot_type else ""
        scene_prompt = str(user_prompt or "").strip()
        display_scene_prompt = scene_prompt
        style_hint = re.sub(r"\s+", " ", str(style_hint or "")).strip()
        scene_request = sanitize_image_prompt(scene_prompt)
        if style_hint:
            safe_style_hint = sanitize_image_prompt(style_hint, limit=240)
            scene_request = f"{scene_request}. Requested visual style: {safe_style_hint}"
        reference_mode = str(reference_mode or "").strip().lower()
        selected_reference = {}
        if reference_mode == TODAY_SCHEDULE_REFERENCE_MODE:
            selected_reference = await self._today_schedule_reference_for_generation(
                scene_prompt,
                [character],
                "character",
            )
            if selected_reference.get("path"):
                scene_request = (
                    f"{scene_request}. Use the attached reference image from today's schedule "
                    "for the current outfit, scene mood, and visual continuity; do not use the character avatar as the reference image."
                )
        reference_style_guard = (
            self._today_schedule_reference_style_guard(
                has_reference=bool(selected_reference.get("path")),
                style_hint=style_hint,
            )
            if reference_mode == TODAY_SCHEDULE_REFERENCE_MODE
            else ""
        )
        final_prompt = " ".join(
            part
            for part in (
                self._character_generation_prefix(),
                build_character_image_prompt(character),
                f"Scene request: {scene_request}.",
                shot_prompt,
                reference_style_guard,
            )
            if part
        ).strip()
        if reference_mode == TODAY_SCHEDULE_REFERENCE_MODE:
            ref_image = str(selected_reference.get("path") or "").strip()
        else:
            ref_image = self._character_reference_image(character)
        style = style_hint or str(character.get("reference_style") or "").strip() or None
        if style not in {"cool", "girly", "sweet"}:
            style = None

        filename = await self.image_gen.generate(
            final_prompt,
            style=style,
            timeout=image_process_timeout(self.config, with_reference_fallback=bool(style or ref_image)),
            source="custom",
            theme="custom",
            prompt_final=True,
            no_auto_style=not bool(style or ref_image),
            ref_image=ref_image,
            size=size,
            image_model=image_model,
        )
        today_str = self._today().isoformat()
        if not filename:
            logger.error("角色生图失败: character=%s", character.get("id"))
            return DailyEntry(date=today_str, outfit="生成失败", status="failed")

        display_prompt = self._short_display_prompt(display_scene_prompt)
        caption = f"{character.get('name') or '角色'}把这一刻留进了画廊。"
        outfit_parts = [f"角色：{character.get('name') or character.get('id')}"]
        if shot_label:
            outfit_parts.append(f"视角：{shot_label}")
        if style_hint:
            outfit_parts.append(f"风格：{style_hint}")
        outfit_parts.append(f"场景：{display_prompt}")
        entry = DailyEntry(
            date=today_str,
            time=self._entry_time_now(),
            outfit_style="角色",
            outfit=" ".join(outfit_parts),
            prompt=final_prompt,
            caption=caption,
            image_filename=filename,
            image_path=f"/images/{filename}",
            status="ok",
            source="character",
            base_style=style or "",
            shot_type=shot_type,
            prompt_mode="character",
            pure_prompt=True,
            custom_prompt=scene_prompt,
            custom_ref_mode="reference" if ref_image else "text2img",
            generation_type="character",
            character_id=character.get("id", ""),
            character_ids=[character.get("id", "")],
            character_names=[character.get("name", "")],
        )
        save_schedule_entry(self.data_dir, entry)
        self._update_image_metadata_caption(filename, caption, display_prompt)
        if reference_mode == TODAY_SCHEDULE_REFERENCE_MODE:
            self._save_generation_reference_metadata(
                filename,
                ref_image,
                selected_reference,
                reference_mode=TODAY_SCHEDULE_REFERENCE_MODE,
            )
        logger.info("角色生图成功: character=%s image=%s", character.get("id"), filename)
        return entry

    async def generate_character_reference(
        self,
        character_id: str,
        user_prompt: str = "",
        size: str = "",
        shot_type: str = "",
        image_model: str = "",
        style_hint: str = "",
        reference_mode: str = "",
    ) -> DailyEntry:
        """Generate a character reference sheet for a selected configured character."""
        registry = load_character_registry(
            self.config,
            load_runtime_persona(self.config, self.data_dir),
            self.data_dir,
        )
        character = registry.get_character(character_id, fallback=False)
        today_str = self._today().isoformat()
        if not character or not character.get("enabled", True):
            return DailyEntry(date=today_str, outfit="角色不存在或已停用", status="failed")

        shot_type = normalize_custom_shot_type(shot_type) if str(shot_type or "").strip() else ""
        shot_label = custom_shot_label(shot_type) if shot_type else ""
        shot_prompt = custom_shot_prompt(shot_type, size) if shot_type else ""
        style_hint = re.sub(r"\s+", " ", str(style_hint or "")).strip()
        extra_request = sanitize_image_prompt(user_prompt)
        reference_request = (
            "Character design reference sheet for image generation, clean neutral background, "
            "clear readable silhouette, consistent face and body identity, detailed hairstyle, "
            "facial features, outfit materials, color palette, accessories, and key personality cues. "
            "Do not include multiple different people; keep the same character identity throughout."
        )
        if extra_request:
            reference_request = f"{reference_request} Extra request: {extra_request}."
        if style_hint:
            safe_style_hint = sanitize_image_prompt(style_hint, limit=240)
            reference_request = f"{reference_request} Requested visual style: {safe_style_hint}."

        final_prompt = " ".join(
            part
            for part in (
                self._character_generation_prefix(),
                build_character_image_prompt(character),
                reference_request,
                shot_prompt,
            )
            if part
        ).strip()

        ref_image = self._character_reference_image(character)
        style = style_hint or str(character.get("reference_style") or "").strip() or None
        if style not in {"cool", "girly", "sweet"}:
            style = None

        filename = await self.image_gen.generate(
            final_prompt,
            style=style,
            timeout=image_process_timeout(self.config, with_reference_fallback=bool(style or ref_image)),
            source="custom",
            theme="custom",
            prompt_final=True,
            no_auto_style=not bool(style or ref_image),
            ref_image=ref_image,
            size=size,
            image_model=image_model,
        )
        if not filename:
            logger.error("角色设定图生成失败: character=%s", character.get("id"))
            return DailyEntry(date=today_str, outfit="生成失败", status="failed")

        name = str(character.get("name") or character.get("id") or "角色")
        display_prompt = self._short_display_prompt(extra_request or "角色设定图")
        caption = f"{name}的角色设定图已保存。"
        outfit_parts = [f"角色：{name}"]
        if shot_label:
            outfit_parts.append(f"视角：{shot_label}")
        if style_hint:
            outfit_parts.append(f"风格：{style_hint}")
        outfit_parts.append(f"设定：{display_prompt}")
        entry = DailyEntry(
            date=today_str,
            time=self._entry_time_now(),
            outfit_style="设定图",
            outfit=" ".join(outfit_parts),
            prompt=final_prompt,
            caption=caption,
            image_filename=filename,
            image_path=f"/images/{filename}",
            status="ok",
            source="character_reference",
            base_style=style or "",
            shot_type=shot_type,
            prompt_mode="character_reference",
            pure_prompt=True,
            custom_prompt=extra_request,
            custom_ref_mode="reference" if ref_image else "text2img",
            generation_type="character_reference",
            character_id=character.get("id", ""),
            character_ids=[character.get("id", "")],
            character_names=[name],
        )
        save_schedule_entry(self.data_dir, entry)
        self._update_image_metadata_caption(filename, caption, display_prompt)

        metadata_updater = getattr(getattr(self, "web_server", None), "_update_image_metadata_entry", None)
        if callable(metadata_updater):
            try:
                metadata_updater(filename, {
                    "purpose": "character_reference",
                    "generation_type": "character_reference",
                    "character_id": character.get("id", ""),
                    "character_name": name,
                    "caption": caption,
                    "display_outfit": display_prompt,
                })
            except Exception as e:
                logger.warning("角色设定图元数据更新失败: %s", e)

        if character.get("source") == LOCAL_CHARACTER_SOURCE:
            try:
                reserved_ids = {
                    str(item.get("id") or "")
                    for item in registry.characters
                    if item.get("source") != LOCAL_CHARACTER_SOURCE and item.get("id")
                }
                updated_character = dict(character)
                updated_character["reference_image"] = f"/images/{filename}"
                upsert_manual_character(self.data_dir, updated_character, reserved_ids=reserved_ids)
            except Exception as e:
                logger.warning("角色设定图回写失败: %s", e)

        logger.info("角色设定图生成成功: character=%s image=%s", character.get("id"), filename)
        return entry

    async def generate_group(
        self,
        character_ids: list[str],
        user_prompt: str,
        size: str = "",
        shot_type: str = "",
        image_model: str = "",
        style_hint: str = "",
        reference_mode: str = "",
    ) -> DailyEntry:
        """Generate one group photo for multiple configured characters."""
        registry = load_character_registry(
            self.config,
            load_runtime_persona(self.config, self.data_dir),
            self.data_dir,
        )
        characters = []
        seen_ids = set()
        for character_id in character_ids or []:
            character = registry.get_character(character_id, fallback=False)
            if not character or not character.get("enabled", True):
                continue
            cid = character.get("id", "")
            if cid and cid not in seen_ids:
                characters.append(character)
                seen_ids.add(cid)
        today_str = self._today().isoformat()
        if len(characters) < 2:
            return DailyEntry(date=today_str, outfit="合照至少需要两个可用角色", status="failed")

        shot_type = normalize_custom_shot_type(shot_type) if str(shot_type or "").strip() else ""
        shot_label = custom_shot_label(shot_type) if shot_type else ""
        shot_prompt = custom_shot_prompt(shot_type, size) if shot_type else ""
        scene_prompt = str(user_prompt or "").strip()
        display_scene_prompt = scene_prompt
        safe_scene_prompt = sanitize_image_prompt(scene_prompt)
        style_hint = re.sub(r"\s+", " ", str(style_hint or "")).strip()
        if style_hint:
            scene_prompt = f"{scene_prompt}. Requested visual style: {style_hint}"
            safe_style_hint = sanitize_image_prompt(style_hint, limit=240)
            safe_scene_prompt = f"{safe_scene_prompt}. Requested visual style: {safe_style_hint}"
        reference_mode = str(reference_mode or "").strip().lower()
        selected_reference = {}
        if reference_mode == TODAY_SCHEDULE_REFERENCE_MODE:
            selected_reference = await self._today_schedule_reference_for_generation(
                scene_prompt,
                characters,
                "group_photo",
            )
            if selected_reference.get("path"):
                safe_scene_prompt = (
                    f"{safe_scene_prompt}. Use the attached reference image from today's schedule "
                    "for the current outfit, scene mood, and visual continuity; do not use character avatar images as references."
                )
        group_scene = " ".join(part for part in (safe_scene_prompt, shot_prompt) if part)
        reference_style_guard = (
            self._today_schedule_reference_style_guard(
                has_reference=bool(selected_reference.get("path")),
                style_hint=style_hint,
            )
            if reference_mode == TODAY_SCHEDULE_REFERENCE_MODE
            else ""
        )
        final_prompt = " ".join(
            part
            for part in (
                self._character_generation_prefix(),
                build_group_image_prompt(characters, group_scene),
                reference_style_guard,
            )
            if part
        ).strip()
        ref_image = str(selected_reference.get("path") or "").strip() if reference_mode == TODAY_SCHEDULE_REFERENCE_MODE else ""

        filename = await self.image_gen.generate(
            final_prompt,
            timeout=image_process_timeout(self.config, with_reference_fallback=bool(ref_image)),
            ref_image=ref_image,
            source="custom",
            theme="custom",
            prompt_final=True,
            no_auto_style=not bool(ref_image),
            size=size,
            image_model=image_model,
        )
        if not filename:
            logger.error("合照生图失败: characters=%s", ",".join(seen_ids))
            return DailyEntry(date=today_str, outfit="生成失败", status="failed")

        names = [str(character.get("name") or character.get("id")) for character in characters]
        ids = [str(character.get("id") or "") for character in characters]
        display_prompt = self._short_display_prompt(display_scene_prompt)
        caption = f"{'、'.join(names)}一起出现在这一张合照里。"
        outfit_parts = [f"角色：{'、'.join(names)}"]
        if shot_label:
            outfit_parts.append(f"视角：{shot_label}")
        if style_hint:
            outfit_parts.append(f"风格：{style_hint}")
        outfit_parts.append(f"场景：{display_prompt}")
        entry = DailyEntry(
            date=today_str,
            time=self._entry_time_now(),
            outfit_style="合照",
            outfit=" ".join(outfit_parts),
            prompt=final_prompt,
            caption=caption,
            image_filename=filename,
            image_path=f"/images/{filename}",
            status="ok",
            source="group_photo",
            shot_type=shot_type,
            prompt_mode="group_photo",
            pure_prompt=True,
            custom_prompt=display_scene_prompt,
            custom_ref_mode="reference" if ref_image else "text2img",
            generation_type="group_photo",
            character_ids=ids,
            character_names=names,
        )
        save_schedule_entry(self.data_dir, entry)
        self._update_image_metadata_caption(filename, caption, display_prompt)
        if reference_mode == TODAY_SCHEDULE_REFERENCE_MODE:
            self._save_generation_reference_metadata(
                filename,
                ref_image,
                selected_reference,
                reference_mode=TODAY_SCHEDULE_REFERENCE_MODE,
            )
        logger.info("合照生图成功: characters=%s image=%s", ",".join(ids), filename)
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
        try:
            def _remove(metadata):
                metadata.pop(filename, None)
                return metadata

            ImageMetadataStore(self.data_dir).update(_remove)
        except Exception as e:
            logger.error("移除旧图片元数据失败: %s, %s", filename, e)

    def _update_image_metadata_caption(self, filename: str, caption: str, display_outfit: str = ""):
        if not filename:
            return
        try:
            repaired_caption = repair_mojibake_text(caption).strip()
            clean_display_outfit = str(display_outfit or "").strip()

            def _update(metadata):
                entry = metadata.get(filename)
                if not isinstance(entry, dict):
                    return metadata
                entry["caption"] = repaired_caption
                if clean_display_outfit:
                    entry["display_outfit"] = clean_display_outfit
                    entry["outfit_description"] = clean_display_outfit
                return metadata

            ImageMetadataStore(self.data_dir).update(_update)
        except Exception as e:
            logger.error("更新图片元数据小心思失败: %s, %s", filename, e)

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

    @staticmethod
    def _selected_reference_for_reroll(original: dict, meta: dict) -> dict:
        merged = {}
        for source in (original, meta):
            if not isinstance(source, dict):
                continue
            selected = source.get("selected_reference")
            if not isinstance(selected, dict):
                continue
            for key, value in selected.items():
                if value not in ("", None) and not merged.get(key):
                    merged[key] = value
        return merged

    def _resolve_reroll_reference_image(self, original: dict, meta: dict) -> str:
        """Find the original img2img reference path for a reroll."""
        selected = self._selected_reference_for_reroll(original, meta)
        candidates = [
            original.get("requested_ref_image_path"),
            meta.get("requested_ref_image_path"),
            original.get("ref_image_path"),
            meta.get("ref_image_path"),
            original.get("requested_ref_image"),
            meta.get("requested_ref_image"),
            original.get("ref_image"),
            meta.get("ref_image"),
            selected.get("path"),
            selected.get("url"),
            selected.get("filename"),
        ]
        resolver = getattr(self.web_server, "_resolve_reference_image", None)
        search_dirs = [
            getattr(self.web_server, "uploaded_reference_dir", ""),
            getattr(self.web_server, "wardrobe_reference_dir", ""),
            getattr(self.web_server, "legacy_uploaded_reference_dir", ""),
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

    @staticmethod
    def _is_precision_edit_reroll(original: dict, meta: dict) -> bool:
        for source in (original, meta):
            if not isinstance(source, dict):
                continue
            precise_raw = source.get("precise_edit")
            if (
                precise_raw is True
                or str(precise_raw or "").strip().lower() in {"1", "true", "yes", "on"}
            ):
                return True
            for field in ("generation_type", "prompt_mode", "custom_ref_mode", "source"):
                if str(source.get(field) or "").strip().lower() in {"image_edit", "precision_edit"}:
                    return True
        return False

    @staticmethod
    def _reference_path_for_display(ref_image: str, selected_reference: dict) -> str:
        if isinstance(selected_reference, dict):
            url = str(selected_reference.get("url") or "").strip()
            if url:
                return url
        return os.path.basename(ref_image) if ref_image else ""

    def _reroll_image_size(self, is_scheduled_reroll: bool, metadata: dict) -> str:
        if is_scheduled_reroll:
            return schedule_image_size(self.config)
        return str((metadata or {}).get("size") or "").strip()

    def _reroll_schedule_context(
        self,
        all_data: dict,
        original: dict,
        original_date: str,
        uses_today_schedule: bool,
    ) -> dict:
        if uses_today_schedule:
            return self._today_schedule_entry()
        historical = all_data.get(original_date) if isinstance(all_data, dict) else None
        if isinstance(historical, dict):
            return historical
        return original if isinstance(original, dict) else {}

    @staticmethod
    def _apply_scheduled_reroll_context(generated: dict, schedule_context: dict) -> None:
        if not isinstance(generated, dict) or not isinstance(schedule_context, dict):
            return
        for field in (
            "outfit_style",
            "outfit",
            "base_style",
            "reference_query",
            "outfit_keywords",
            "scene_keywords",
        ):
            if field in schedule_context:
                generated[field] = schedule_context[field]

    @staticmethod
    def _image_edit_size(image_path: str, metadata: dict) -> str:
        try:
            from PIL import Image

            with Image.open(image_path) as image:
                width, height = image.size
            if width > 0 and height > 0:
                return f"{width}x{height}"
        except Exception:
            pass

        size = str((metadata or {}).get("size") or "").strip().lower()
        if re.fullmatch(r"\d{2,5}x\d{2,5}", size):
            return size
        try:
            width = int((metadata or {}).get("width") or 0)
            height = int((metadata or {}).get("height") or 0)
        except (TypeError, ValueError):
            return ""
        return f"{width}x{height}" if width > 0 and height > 0 else ""

    async def edit_image(
        self,
        image_filename: str,
        target: str,
        instruction: str,
        schedule_description: Optional[str] = None,
    ) -> dict:
        """Precision-edit one gallery image and replace its existing card."""
        target = normalize_image_edit_target(target)
        instruction = normalize_image_edit_instruction(instruction)
        normalized_schedule_description = None
        if schedule_description is not None:
            raw_schedule_description = str(schedule_description or "").strip()
            if len(raw_schedule_description) > MAX_IMAGE_EDIT_SCHEDULE_DESCRIPTION_LENGTH:
                return {"status": "failed", "error": "schedule_description_too_long"}
            normalized_schedule_description = normalize_image_edit_schedule_description(
                raw_schedule_description
            )
            if not normalized_schedule_description:
                return {"status": "failed", "error": "schedule_description_required"}
        if not target:
            return {"status": "failed", "error": "invalid_edit_target"}

        store = ScheduleStore(self.data_dir)
        all_data = store.load()
        original_key, original = self._find_entry_by_image(all_data, image_filename)
        if not original:
            return {"status": "failed", "error": "not_found"}

        image_path_resolver = getattr(self.web_server, "_image_file_path", None)
        try:
            image_path = image_path_resolver(image_filename) if callable(image_path_resolver) else ""
        except ValueError:
            image_path = ""
        if not image_path or not os.path.isfile(image_path):
            return {"status": "failed", "error": "image_file_missing"}

        metadata = ImageMetadataStore(self.data_dir).load()
        original_meta = metadata.get(image_filename, {})
        original_meta = original_meta if isinstance(original_meta, dict) else {}
        size = self._image_edit_size(image_path, original_meta)
        now = self._now()
        replacement_key = original_key or image_filename
        if re.match(r'^\d{4}-\d{2}-\d{2}$', str(replacement_key)):
            replacement_key = image_filename
        original_date = str(original.get("date") or now.strftime("%Y-%m-%d"))
        original_time = str(
            original.get("time")
            or self._image_time_from_filename(image_filename)
            or now.strftime("%H:%M")
        )
        original_schedule_time = str(original.get("schedule_time") or "").strip()
        original_schedule_description = image_schedule_description(original_schedule_time)
        if (
            normalized_schedule_description is None
            or normalized_schedule_description == original_schedule_description
        ):
            inferred_schedule_description = rewrite_image_edit_schedule_description(
                original_schedule_time,
                target,
                instruction,
            )
            normalized_schedule_description = (
                inferred_schedule_description
                if inferred_schedule_description != original_schedule_description
                else None
            )
        updated_schedule_time = original_schedule_time
        if normalized_schedule_description is not None:
            updated_schedule_time = replace_image_schedule_description(
                original_schedule_time,
                original_time,
                normalized_schedule_description,
            )
        schedule_changed = updated_schedule_time != original_schedule_time
        if not instruction and not schedule_changed:
            return {"status": "failed", "error": "edit_instruction_required"}

        effective_target = target if instruction else "schedule"
        target_label = image_edit_target_label(effective_target)
        effective_instruction = instruction or normalized_schedule_description or ""
        prompt = build_precision_image_edit_prompt(
            effective_target,
            instruction,
            previous_schedule_description=original_schedule_description,
            schedule_description=(
                normalized_schedule_description if schedule_changed else ""
            ),
        )
        raw_model_name = str(original_meta.get("model") or "").strip()
        if self._engine_from_model_name(raw_model_name) != "gptimage":
            raw_model_name = ""

        filename = await self.image_gen.generate(
            prompt,
            engine="gptimage",
            timeout=image_process_timeout(self.config, with_reference_fallback=True),
            ref_image=image_path,
            size=size,
            source="custom",
            prompt_final=True,
            no_auto_style=True,
            theme="custom",
            image_model=raw_model_name,
            precise_edit=True,
        )
        if not filename:
            logger.error("图片精准编辑失败: image=%s target=%s", image_filename, effective_target)
            return {"status": "failed", "error": "edit_generate_failed"}

        generated_metadata = ImageMetadataStore(self.data_dir).load()
        generated_meta = generated_metadata.get(filename, {})
        generated_meta = dict(generated_meta) if isinstance(generated_meta, dict) else {}
        if generated_meta and str(generated_meta.get("generation_mode") or "") != "img2img":
            logger.error("精准编辑丢失原图参考，拒绝结果: image=%s generated=%s", image_filename, filename)
            cleanup = getattr(self.web_server, "_delete_gallery_image", None)
            if callable(cleanup):
                try:
                    cleanup(filename)
                except Exception as e:
                    logger.error("清理无效精准编辑结果失败: image=%s error=%s", filename, e)
            return {"status": "failed", "error": "edit_reference_lost"}

        selected_reference = {
            "id": f"edit_source_{image_filename}",
            "filename": image_filename,
            "url": f"/images/{image_filename}",
            "label": "原图",
            "prompt": "",
            "source": "gallery",
            "selection_mode": "precision_edit",
            "selection_reason": f"precision_edit:{effective_target}",
        }
        result = {}
        merge_state = {"source_changed": False, "replacement_key": replacement_key}

        def _merge_edit(all_entries: dict):
            current_key, current_source = self._find_entry_by_image(
                all_entries,
                image_filename,
            )
            if not current_source:
                merge_state["source_changed"] = True
                return all_entries

            active_replacement_key = current_key or replacement_key
            if re.match(r'^\d{4}-\d{2}-\d{2}$', str(active_replacement_key)):
                active_replacement_key = image_filename
            merge_state["replacement_key"] = active_replacement_key
            generated_key, generated_entry = self._find_entry_by_image(all_entries, filename)
            generated = dict(current_source)
            generated.update(dict(generated_entry or {}))
            for field in (
                "outfit_style",
                "outfit",
                "base_style",
                "reference_query",
                "outfit_keywords",
                "scene_keywords",
                "character_id",
                "character_ids",
                "character_names",
                "chat_room_id",
                "schedule",
                "schedule_prompt",
                "schedule_details",
                "schedule_time",
            ):
                if field in current_source:
                    generated[field] = current_source[field]
            edit_history = current_source.get("edit_history")
            edit_history = list(edit_history) if isinstance(edit_history, list) else []
            history_item = {
                "from_image": image_filename,
                "target": effective_target,
                "target_label": target_label,
                "instruction": effective_instruction,
                "edited_at": now.isoformat(timespec="seconds"),
            }
            if schedule_changed:
                history_item.update({
                    "previous_schedule_time": original_schedule_time,
                    "schedule_time": updated_schedule_time,
                    "schedule_description": normalized_schedule_description,
                })
            edit_history.append(history_item)
            generated.update({
                "id": filename,
                "date": str(current_source.get("date") or original_date),
                "time": str(current_source.get("time") or original_time),
                "image_filename": filename,
                "image_path": f"/images/{filename}",
                "prompt": prompt,
                "caption": current_source.get("caption", ""),
                "favorite": bool(current_source.get("favorite", False)),
                "status": "ok",
                "source": current_source.get("source") or "custom",
                "generation_type": "image_edit",
                "prompt_mode": "precision_edit",
                "pure_prompt": True,
                "custom_prompt": effective_instruction,
                "custom_ref_mode": "precision_edit",
                "generation_mode": "img2img",
                "requested_generation_mode": "img2img",
                "requested_ref_image": image_filename,
                "requested_ref_image_path": f"/images/{image_filename}",
                "selected_reference": selected_reference,
                "edited_from": image_filename,
                "edit_target": effective_target,
                "edit_target_label": target_label,
                "edit_instruction": effective_instruction,
                "edit_history": edit_history,
                "original_image_filename": current_source.get("original_image_filename") or image_filename,
                "replaced_image_filename": image_filename,
                "replacement_key": active_replacement_key,
            })
            if schedule_changed:
                generated.update({
                    "schedule_time": updated_schedule_time,
                    "schedule_description": normalized_schedule_description,
                    "schedule_edited_at": now.isoformat(timespec="seconds"),
                    "schedule_edit_source": "image_edit",
                })
            for key, entry in list(all_entries.items()):
                if key == active_replacement_key or re.match(r'^\d{4}-\d{2}-\d{2}$', str(key)):
                    continue
                entry_filename = entry.get("image_filename") if isinstance(entry, dict) else ""
                if key in {generated_key, image_filename, filename} or entry_filename in {image_filename, filename}:
                    del all_entries[key]
            all_entries[active_replacement_key] = generated
            result.update(generated)
            return all_entries

        store.update(_merge_edit)
        if merge_state["source_changed"]:
            self._delete_replaced_image(filename)
            logger.warning(
                "图片精准编辑结果已丢弃，源卡片在生成期间发生变化: source=%s generated=%s",
                image_filename,
                filename,
            )
            return {"status": "failed", "error": "edit_source_changed"}

        replacement_key = str(merge_state["replacement_key"] or replacement_key)

        metadata_updater = getattr(self.web_server, "_update_image_metadata_entry", None)
        if callable(metadata_updater):
            generated_meta.update({
                "source": "image_edit",
                "precise_edit": True,
                "edited_from": image_filename,
                "edit_target": effective_target,
                "edit_target_label": target_label,
                "edit_instruction": effective_instruction,
                "replaced_image_filename": image_filename,
                "replacement_key": replacement_key,
                "requested_generation_mode": "img2img",
                "generation_mode": "img2img",
                "requested_ref_image": image_filename,
                "requested_ref_image_path": image_path,
                "selected_reference": selected_reference,
            })
            if schedule_changed:
                generated_meta.update({
                    "schedule_time": updated_schedule_time,
                    "schedule_description": normalized_schedule_description,
                    "schedule_edited_at": now.isoformat(timespec="seconds"),
                    "schedule_edit_source": "image_edit",
                })
            try:
                metadata_updater(filename, generated_meta)
            except Exception as e:
                logger.error("保存精准编辑元数据失败: image=%s error=%s", filename, e)

        if filename != image_filename:
            self._delete_replaced_image(image_filename)
        logger.info(
            "图片精准编辑替换成功: %s -> %s target=%s schedule_changed=%s",
            image_filename,
            filename,
            effective_target,
            schedule_changed,
        )
        return result

    async def reroll_image(self, image_filename: str) -> dict:
        """Generate a new card from an existing card's final prompt."""
        store = ScheduleStore(self.data_dir)
        all_data = store.load()
        original_key, original = self._find_entry_by_image(all_data, image_filename)
        if not original:
            return {"status": "failed", "error": "not_found"}

        metadata = ImageMetadataStore(self.data_dir).load()
        meta = metadata.get(image_filename, {}) if isinstance(metadata, dict) else {}
        meta = meta if isinstance(meta, dict) else {}
        is_precision_edit = self._is_precision_edit_reroll(original, meta)
        prompt = (meta.get("prompt") or original.get("prompt") or "").strip()
        edit_target = normalize_image_edit_target(
            original.get("edit_target") or meta.get("edit_target"),
            allow_internal=True,
        )
        edit_instruction = normalize_image_edit_instruction(
            original.get("edit_instruction")
            or meta.get("edit_instruction")
            or original.get("custom_prompt")
            or meta.get("custom_prompt")
        )
        edit_target_label_value = str(
            original.get("edit_target_label") or meta.get("edit_target_label") or ""
        ).strip() or image_edit_target_label(edit_target)
        edit_history_raw = original.get("edit_history") or meta.get("edit_history")
        edit_history = list(edit_history_raw) if isinstance(edit_history_raw, list) else []
        edited_from = str(
            original.get("edited_from") or meta.get("edited_from") or image_filename
        ).strip()
        original_image_filename = str(
            original.get("original_image_filename")
            or meta.get("original_image_filename")
            or edited_from
            or image_filename
        ).strip()
        if is_precision_edit and not prompt and edit_target and edit_instruction:
            prompt = build_precision_image_edit_prompt(edit_target, edit_instruction)
        schedule_time = (original.get("schedule_time") or meta.get("schedule_time") or "").strip()
        original_source = (original.get("source") or meta.get("source") or "").strip()
        is_scheduled_reroll = (
            not is_precision_edit
            and original_source in TODAY_PHOTO_SOURCES
            and bool(schedule_time)
        )
        if not prompt and not is_scheduled_reroll:
            return {"status": "failed", "error": "prompt_missing"}

        today_str = self._today().isoformat()
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
        if is_precision_edit:
            engine = "gptimage"
            if self._engine_from_model_name(raw_model_name) != "gptimage":
                raw_model_name = ""
        base_style = (original.get("base_style") or "").strip().lower()
        original_selected_reference = self._selected_reference_for_reroll(original, meta)
        original_ref_image = ""
        if is_precision_edit:
            image_path_resolver = getattr(self.web_server, "_image_file_path", None)
            try:
                original_ref_image = image_path_resolver(image_filename) if callable(image_path_resolver) else ""
            except ValueError:
                original_ref_image = ""
            if not original_ref_image or not os.path.isfile(original_ref_image):
                return {"status": "failed", "error": "image_file_missing"}
            original_selected_reference = {
                "id": f"edit_source_{image_filename}",
                "filename": image_filename,
                "url": f"/images/{image_filename}",
                "label": "当前编辑图",
                "prompt": "",
                "source": "gallery",
                "selection_mode": "precision_edit",
                "selection_reason": f"precision_edit:{edit_target or 'image'}",
            }
        elif engine == "gptimage":
            original_ref_image = self._resolve_reroll_reference_image(original, meta)
        is_custom_source = original_source in {"custom", "hermes_api"}
        custom_ref_mode = str(original.get("custom_ref_mode") or "").strip().lower()
        has_custom_reference = custom_ref_mode == "reference" or bool(
            original.get("requested_ref_image")
            or original.get("requested_ref_image_path")
            or meta.get("requested_ref_image")
            or meta.get("requested_ref_image_path")
            or original_selected_reference
        )
        custom_ref_image = ""
        if is_custom_source and has_custom_reference and engine == "gptimage":
            custom_ref_image = original_ref_image
            if not custom_ref_image:
                logger.warning("自定义参考图重抽未找到原参考图，将退回文生图: %s", image_filename)
        style = None
        size = (
            self._image_edit_size(original_ref_image, meta)
            if is_precision_edit
            else self._reroll_image_size(is_scheduled_reroll, meta)
        )
        custom_user_prompt = self._extract_custom_user_prompt(original)
        custom_shot_type = normalize_custom_shot_type(original.get("shot_type", ""))
        is_custom_injected_reroll = (
            not is_precision_edit
            and is_custom_source
            and not original_is_pure
            and bool(custom_user_prompt)
        )
        reroll_theme = "custom"
        reroll_source = "custom"
        reroll_prompt = prompt
        reroll_prompt_final = True
        reroll_no_auto_style = is_precision_edit or original_is_pure
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
                reroll_source = original_source or "cron"
                reroll_prompt = ""
                reroll_prompt_final = False
                reroll_no_auto_style = False
                reroll_caption = True
            else:
                reroll_source = original_source or "cron"
                reroll_prompt = prompt
                reroll_prompt_final = True
                reroll_no_auto_style = not bool(original_ref_image)
                reroll_caption = False

        ref_image = original_ref_image if is_precision_edit else (
            custom_ref_image if is_custom_source and custom_ref_image else ""
        )
        selected_reference = dict(original_selected_reference) if original_ref_image else {}
        if engine == "gptimage" and is_scheduled_reroll and original_ref_image:
            ref_image = original_ref_image
            reroll_no_auto_style = False
            logger.info(
                "重抽沿用原参考图: %s ref=%s",
                image_filename,
                selected_reference.get("label") or selected_reference.get("filename") or os.path.basename(ref_image),
            )
        elif engine == "gptimage" and is_scheduled_reroll and reroll_uses_today_schedule:
            schedule_context_entry = self._today_schedule_entry()
            selected_reference = await self._select_reference_for_generation({
                "source": "cron_reroll",
                "schedule_time": schedule_time,
                "theme": reroll_theme,
                "activity": schedule_time,
                "outfit_style": schedule_context_entry.get("outfit_style", ""),
                "outfit": schedule_context_entry.get("outfit", ""),
                "reference_query": schedule_context_entry.get("reference_query", ""),
                "prompt": schedule_context_entry.get("prompt", ""),
                "schedule": schedule_context_entry.get("schedule", ""),
                "schedule_prompt": schedule_context_entry.get("schedule_prompt", ""),
                "schedule_details": schedule_context_entry.get("schedule_details", []),
            })
            ref_image = selected_reference.get("path", "")
            reroll_no_auto_style = not bool(ref_image)

        filename = await self.image_gen.generate(
            reroll_prompt,
            style=style,
            engine=engine,
            timeout=image_process_timeout(self.config, with_reference_fallback=bool(ref_image)),
            ref_image=ref_image,
            size=size,
            source=reroll_source,
            prompt_final=reroll_prompt_final,
            no_auto_style=reroll_no_auto_style,
            theme=reroll_theme,
            schedule_time=schedule_time if is_scheduled_reroll else "",
            caption=reroll_caption,
            image_model=raw_model_name if engine == "gptimage" else "",
            precise_edit=is_precision_edit,
        )
        if not filename:
            logger.error("图片重抽失败: %s", image_filename)
            return {"status": "failed", "error": "generate_failed"}

        original_time = original.get("time") or self._image_time_from_filename(image_filename)
        now_time = self._image_time_from_filename(filename) or self._now().strftime("%H:%M")
        schedule_context_entry = {}
        if is_scheduled_reroll:
            schedule_context_entry = self._reroll_schedule_context(
                all_data,
                original,
                original_date,
                reroll_uses_today_schedule,
            )
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
                self._apply_scheduled_reroll_context(generated, schedule_context_entry)
                if schedule_time:
                    generated["schedule_time"] = schedule_time
                if selected_reference:
                    generated["selected_reference"] = {
                        key: selected_reference.get(key, "")
                        for key in ("id", "filename", "url", "label", "prompt", "source", "selection_mode", "selection_reason")
                    }
                if ref_image:
                    generated.setdefault("requested_generation_mode", "img2img")
                    generated.setdefault("requested_ref_image", os.path.basename(ref_image))
                    generated.setdefault(
                        "requested_ref_image_path",
                        self._reference_path_for_display(ref_image, selected_reference),
                    )
            else:
                for field in ("outfit_style", "outfit", "schedule_time", "shot_type", "prompt_mode", "pure_prompt", "custom_prompt", "custom_ref_mode"):
                    value = original.get(field)
                    if value or field == "pure_prompt":
                        generated[field] = value
                for field in ("generation_mode", "requested_generation_mode", "ref_image", "ref_image_path", "requested_ref_image", "requested_ref_image_path"):
                    value = original.get(field)
                    if value and not generated.get(field):
                        generated[field] = value
                if selected_reference and not generated.get("selected_reference"):
                    generated["selected_reference"] = {
                        key: selected_reference.get(key, "")
                        for key in ("id", "filename", "url", "label", "prompt", "source", "selection_mode", "selection_reason")
                    }
                if ref_image:
                    generated.setdefault("requested_generation_mode", "img2img")
                    generated.setdefault("requested_ref_image", os.path.basename(ref_image))
                    generated.setdefault(
                        "requested_ref_image_path",
                        self._reference_path_for_display(ref_image, selected_reference),
                    )
                if is_precision_edit:
                    generated.update({
                        "generation_type": "image_edit",
                        "prompt_mode": "precision_edit",
                        "pure_prompt": True,
                        "custom_prompt": edit_instruction or original.get("custom_prompt", ""),
                        "custom_ref_mode": "precision_edit",
                        "generation_mode": "img2img",
                        "requested_generation_mode": "img2img",
                        "requested_ref_image": image_filename,
                        "requested_ref_image_path": f"/images/{image_filename}",
                        "selected_reference": selected_reference,
                        "edit_target": edit_target,
                        "edit_target_label": edit_target_label_value,
                        "edit_instruction": edit_instruction,
                        "edit_history": edit_history,
                        "edited_from": edited_from,
                        "original_image_filename": original_image_filename,
                    })
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
        if is_precision_edit:
            metadata_updater = getattr(self.web_server, "_update_image_metadata_entry", None)
            if callable(metadata_updater):
                generated_metadata = ImageMetadataStore(self.data_dir).load()
                generated_meta = generated_metadata.get(filename, {}) if isinstance(generated_metadata, dict) else {}
                generated_meta = dict(generated_meta) if isinstance(generated_meta, dict) else {}
                generated_meta.update({
                    "source": "image_edit",
                    "precise_edit": True,
                    "generation_type": "image_edit",
                    "prompt_mode": "precision_edit",
                    "custom_ref_mode": "precision_edit",
                    "custom_prompt": edit_instruction,
                    "edited_from": edited_from,
                    "original_image_filename": original_image_filename,
                    "edit_target": edit_target,
                    "edit_target_label": edit_target_label_value,
                    "edit_instruction": edit_instruction,
                    "edit_history": edit_history,
                    "replaced_image_filename": image_filename,
                    "replacement_key": result.get("replacement_key", original_key or image_filename),
                    "requested_generation_mode": "img2img",
                    "generation_mode": "img2img",
                    "requested_ref_image": image_filename,
                    "requested_ref_image_path": original_ref_image,
                    "selected_reference": selected_reference,
                })
                try:
                    metadata_updater(filename, generated_meta)
                except Exception as e:
                    logger.error("保存精准编辑重抽元数据失败: image=%s error=%s", filename, e)
        if filename != image_filename:
            self._delete_replaced_image(image_filename)
        logger.info("图片重抽成功: %s -> %s", image_filename, filename)
        return result

    async def daily_job(self):
        """每日自动任务 - 生成日程并根据日程时间动态安排生图"""
        logger.info("执行每日日程生成...")
        entry = None
        try:
            entry = await self.refresh_schedule()
        except Exception as e:
            logger.error("每日日程生成异常: %s", e, exc_info=True)
        finally:
            if getattr(self.aps, "running", False):
                if self._schedule_generation_ready(entry):
                    self._schedule_next_daily_job(force_tomorrow=True)
                else:
                    self._schedule_retry_or_next_daily_job()

    async def refresh_schedule(self):
        """Share one schedule refresh across every app-level caller."""
        task = getattr(self, "_schedule_refresh_task", None)
        if task is None or task.done():
            task = asyncio.create_task(self._refresh_schedule_impl())
            self._schedule_refresh_task = task
        else:
            logger.info("日程刷新已在进行，复用当前任务")
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and getattr(self, "_schedule_refresh_task", None) is task:
                self._schedule_refresh_task = None

    async def _refresh_schedule_impl(self):
        """Regenerate today's schedule and rebuild dynamic photo jobs."""
        # 生成新日程
        entry = await self.scheduler_gen.generate_today()
        
        # LLM 不通时 scheduler 会返回 fallback；刷新按钮不应因此覆盖已有可用日程。
        if not entry or entry.source == "fallback" or entry.status != "ok":
            existing_entry = self._today_schedule_entry()
            existing_missing = self._schedule_missing_required_periods(existing_entry.get("schedule", "")) if existing_entry else []
            if existing_entry and not existing_missing:
                logger.warning("日程生成使用兜底结果，保留现有今日日程")
                preserved = DailyEntry.from_dict(existing_entry)
                preserved.source = "preserved"
                await self._schedule_dynamic_photos(preserved.schedule)
                return preserved
            if existing_entry and existing_missing:
                logger.warning(f"现有今日日程缺少时间段 {existing_missing}，无法直接保留")

        if not entry or entry.status != "ok":
            logger.error("日程生成失败")
            if entry and entry.source != "fallback" and entry.schedule and entry.schedule != FAILED_SCHEDULE_TEXT:
                save_schedule_entry(self.data_dir, entry)
            return entry

        # save_schedule_entry performs the replacement under one exclusive lock.
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

    def _photo_image_path(self, filename_or_path: str) -> str:
        raw = str(filename_or_path or "").strip()
        if not raw:
            return ""
        image_dir_is_allowed = getattr(
            getattr(self, "web_server", None),
            "_image_dir_is_allowed",
            None,
        )
        if os.path.isabs(raw) and os.path.isfile(raw):
            if not callable(image_dir_is_allowed) or image_dir_is_allowed(os.path.dirname(raw)):
                return raw
        filename = os.path.basename(raw.removeprefix("/images/"))
        if not filename.lower().endswith(REFERENCE_IMAGE_EXTENSIONS):
            return ""
        search_dirs = []
        image_search_dirs = getattr(getattr(self, "web_server", None), "_image_search_dirs", None)
        if callable(image_search_dirs):
            search_dirs.extend(image_search_dirs())
        else:
            search_dirs.extend([
                getattr(getattr(self, "web_server", None), "image_dir", ""),
                os.path.join(self.data_dir, "images"),
            ])
        for image_dir in dict.fromkeys(path for path in search_dirs if path):
            if callable(image_dir_is_allowed) and not image_dir_is_allowed(image_dir):
                continue
            candidate = os.path.join(image_dir, filename)
            if os.path.isfile(candidate):
                return candidate
        return ""

    def _delivery_enabled(self) -> bool:
        integrations = self.config.get("integrations", {}) if isinstance(self.config.get("integrations"), dict) else {}
        return integrations.get("send_enabled") is not False

    def _set_gallery_delivery_status(
        self,
        filename: str,
        status: str,
        *,
        error: str = "",
    ) -> None:
        filename = os.path.basename(str(filename or "").strip())
        if not filename:
            return
        now_text = self._now().isoformat()
        patch = {
            "delivery_status": str(status or "").strip(),
            "delivery_updated_at": now_text,
        }
        if status == "sent":
            patch["delivery_sent_at"] = now_text
            patch["delivery_error"] = ""
        elif status == "failed":
            patch["delivery_error"] = str(error or "delivery_failed")[:600]

        def _update(all_data):
            for key, entry in all_data.items():
                if not isinstance(entry, dict):
                    continue
                if key == filename or entry.get("image_filename") == filename:
                    entry.update(patch)
            return all_data

        try:
            ScheduleStore(self.data_dir).update(_update)
        except Exception as exc:
            logger.error("保存图片投递状态失败: image=%s error=%s", filename, exc)
        try:
            ImageMetadataStore(self.data_dir).update(
                lambda metadata: {
                    **metadata,
                    filename: {
                        **(metadata.get(filename) if isinstance(metadata.get(filename), dict) else {}),
                        **patch,
                    },
                }
            )
        except Exception as exc:
            logger.warning("保存图片投递元数据失败: image=%s error=%s", filename, exc)

    def _record_photo_delivery_failure(
        self,
        slot_key: str,
        *,
        theme: str,
        time_text: str,
        activity: str,
        image_path: str,
        caption: str,
        error: str,
    ) -> None:
        filename = os.path.basename(str(image_path or ""))
        detail = str(error or "delivery_failed").strip() or "delivery_failed"
        self._set_gallery_delivery_status(filename, "failed", error=detail)
        if not slot_key:
            return
        self._failed_photo_jobs[slot_key] = {
            "reason": "delivery_failed",
            "theme": theme,
            "time": time_text,
            "activity": activity,
            "failed_at": self._now().isoformat(),
            "image_filename": filename,
            "image_path": image_path,
            "caption": caption,
            "error": detail[-1200:],
            "error_summary": "图片已生成，但发送失败；重试只会重发原图，不会重新生图",
        }
        self._save_failed_photo_jobs()

    def _recover_orphaned_generated_photos(self) -> None:
        """Register today's generated files left behind by an interrupted gallery sync."""
        today_text = self._today().isoformat()
        try:
            metadata = ImageMetadataStore(self.data_dir).load()
            schedule_store = ScheduleStore(self.data_dir)
            schedule_data = schedule_store.load()
            daily_entry = schedule_data.get(today_text)
            daily_entry = daily_entry if isinstance(daily_entry, dict) else {}
            changed = False

            recovered_delivery_files = set()
            for entry in schedule_data.values():
                if not isinstance(entry, dict):
                    continue
                if entry.get("date") != today_text or entry.get("status") != "ok":
                    continue
                delivery_status = str(entry.get("delivery_status") or "").strip()
                if entry.get("source") != "cron" or delivery_status not in {"pending", "sending"}:
                    continue
                filename = os.path.basename(str(entry.get("image_filename") or ""))
                image_path = self._photo_image_path(filename)
                if not filename or not image_path or filename in recovered_delivery_files:
                    continue
                raw_time = str(entry.get("schedule_time") or entry.get("time") or "")
                match = re.match(r"\s*(\d{1,2}):(\d{2})\s*(.*)", raw_time)
                if not match:
                    match = re.search(r"_schedule_(\d{2})(\d{2})(?:_|\.)", filename)
                if not match:
                    logger.warning("无法恢复缺少日程时间的发送中图片: image=%s", filename)
                    continue
                time_text = f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
                activity = match.group(3).strip() if match.lastindex and match.lastindex >= 3 else ""
                slot_key = f"{today_text} {time_text}"
                error = (
                    "delivery_interrupted_before_send"
                    if delivery_status == "pending"
                    else "delivery_interrupted_by_restart"
                )
                self._set_gallery_delivery_status(filename, "failed", error=error)
                self._failed_photo_jobs[slot_key] = {
                    "reason": "delivery_failed",
                    "theme": entry.get("theme") or self._theme_for_hour(int(time_text[:2])),
                    "time": time_text,
                    "activity": activity,
                    "failed_at": self._now().isoformat(),
                    "image_filename": filename,
                    "image_path": image_path,
                    "caption": str(entry.get("caption") or ""),
                    "error": error,
                    "error_summary": (
                        "图片生成后尚未发送服务便已中断；重发会使用原图，不会重新生图"
                        if delivery_status == "pending"
                        else "图片发送过程中服务中断；重发会使用原图，不会重新生图"
                    ),
                }
                recovered_delivery_files.add(filename)
                changed = True
                logger.warning(
                    "已恢复发送中断的定时原图，等待手动重发: slot=%s image=%s",
                    slot_key,
                    filename,
                )

            for slot_key, failed in list(self._failed_photo_jobs.items()):
                date_text, _, time_text = slot_key.partition(" ")
                if date_text != today_text or failed.get("reason") == "delivery_failed":
                    continue
                if not re.fullmatch(r"\d{2}:\d{2}", time_text):
                    continue
                if self._check_photo_exists_for_slot(date_text, time_text):
                    continue

                token = time_text.replace(":", "")
                candidates = []
                for filename, meta in metadata.items():
                    if not isinstance(filename, str) or not isinstance(meta, dict):
                        continue
                    if str(meta.get("source") or "") != "cron":
                        continue
                    if not re.search(rf"_schedule_{re.escape(token)}(?:_|\.)", filename):
                        continue
                    image_path = self._photo_image_path(filename)
                    if not image_path:
                        continue
                    try:
                        created_at = int(meta.get("created_at") or os.path.getmtime(image_path))
                        created_date = datetime.fromtimestamp(
                            created_at,
                            configured_timezone(self.config),
                        ).date().isoformat()
                    except (OSError, TypeError, ValueError):
                        continue
                    if created_date == date_text:
                        candidates.append((created_at, filename, image_path, meta))
                if not candidates:
                    continue

                created_at, filename, image_path, meta = max(candidates, key=lambda item: item[0])
                recovered_schedule_time = f"{time_text} {failed.get('activity', '')}".strip()
                recovered_theme = str(
                    failed.get("theme") or self._theme_for_hour(int(time_text[:2]))
                )
                recovered_caption = repair_mojibake_text(meta.get("caption") or "").strip()
                if not recovered_caption:
                    recovered_caption = build_caption_fallback(
                        recovered_theme,
                        recovered_schedule_time,
                        load_runtime_persona(self.config, self.data_dir),
                    )
                if not recovered_caption:
                    recovered_caption = repair_mojibake_text(
                        daily_entry.get("caption") or ""
                    ).strip()
                entry = {
                    "date": date_text,
                    "time": datetime.fromtimestamp(
                        created_at,
                        configured_timezone(self.config),
                    ).strftime("%H:%M"),
                    "schedule_time": recovered_schedule_time,
                    "image_filename": filename,
                    "prompt": str(meta.get("prompt") or ""),
                    "caption": recovered_caption,
                    "outfit": str(daily_entry.get("outfit") or ""),
                    "outfit_style": str(daily_entry.get("outfit_style") or ""),
                    "theme": recovered_theme,
                    "model_name": str(meta.get("model_name") or meta.get("model") or ""),
                    "size": str(meta.get("size") or ""),
                    "generation_time": meta.get("generation_time"),
                    "status": "ok",
                    "source": "cron",
                    "delivery_status": "failed",
                    "delivery_error": "gallery_sync_interrupted_before_delivery",
                    "delivery_updated_at": self._now().isoformat(),
                    "recovered_from_metadata": True,
                }

                def _register(all_data, image_filename=filename, payload=entry):
                    if not isinstance(all_data.get(image_filename), dict):
                        all_data[image_filename] = payload
                    return all_data

                schedule_store.update(_register)
                failed.update({
                    "reason": "delivery_failed",
                    "image_filename": filename,
                    "image_path": image_path,
                    "caption": entry["caption"],
                    "error": "gallery_sync_interrupted_before_delivery",
                    "error_summary": "图片已生成但同步中断；重发会使用原图，不会重新生图",
                })
                self._failed_photo_jobs[slot_key] = failed
                changed = True
                logger.warning(
                    "已恢复同步中断的定时原图，等待手动重发: slot=%s image=%s",
                    slot_key,
                    filename,
                )
            if changed:
                self._save_failed_photo_jobs()
        except Exception as exc:
            logger.error("恢复同步中断的定时原图失败: %s", exc, exc_info=True)

    def _mark_photo_job_inflight(self, slot_key: str):
        if not slot_key:
            return
        self._photo_jobs_inflight.add(slot_key)
        self._photo_jobs_inflight_started[slot_key] = time.time()

    def _clear_photo_job_inflight(self, slot_key: str):
        if not slot_key:
            return
        self._photo_jobs_inflight.discard(slot_key)
        self._photo_jobs_inflight_started.pop(slot_key, None)

    @staticmethod
    def _photo_job_id_for_time(time_text: str) -> str:
        match = re.match(r"^\s*(\d{1,2}):(\d{2})", str(time_text or ""))
        if not match:
            return ""
        return f"photo_dynamic_{int(match.group(1))}_{int(match.group(2))}"

    def _handle_scheduler_event(self, event):
        """Record missed dynamic photo jobs so they do not silently disappear."""
        job_id = str(getattr(event, "job_id", "") or "")
        if not job_id.startswith("photo_dynamic_"):
            return

        run_time = getattr(event, "scheduled_run_time", None)
        if run_time is not None:
            try:
                run_time = run_time.astimezone(self.aps.timezone) if run_time.tzinfo else run_time
            except Exception:
                pass

        meta = self._photo_job_schedule_meta.pop(job_id, {}) or {}
        if run_time is None:
            match = re.match(r"^photo_dynamic_(\d{1,2})_(\d{1,2})$", job_id)
            if not match:
                logger.warning("动态生图任务错过执行但无法解析时间: %s", job_id)
                return
            now = self._naive_now()
            run_time = datetime.combine(now.date(), dt_time(int(match.group(1)), int(match.group(2))))

        date_text = run_time.strftime("%Y-%m-%d")
        time_text = f"{run_time.hour:02d}:{run_time.minute:02d}"
        if self._check_photo_exists_for_slot(date_text, time_text):
            return

        activity = str(meta.get("activity") or self._today_schedule_activity_map().get(time_text, "")).strip()
        theme = str(meta.get("theme") or self._theme_for_hour(run_time.hour)).strip()
        slot_key = f"{date_text} {time_text}"
        self._failed_photo_jobs[slot_key] = {
            "theme": theme,
            "time": time_text,
            "activity": activity,
            "failed_at": self._now().isoformat(),
            "error": f"APScheduler missed dynamic photo job {job_id} scheduled at {run_time.isoformat()}",
            "error_summary": "服务当时繁忙，定时生图错过执行窗口，可手动重试",
        }
        self._save_failed_photo_jobs()
        logger.warning(
            "动态生图任务错过执行窗口，已转为可重试: %s time=%s activity=%s",
            job_id,
            time_text,
            activity[:30],
        )

    def _photo_job_stale_seconds(self) -> int:
        return image_process_timeout(self.config, with_reference_fallback=True) + PHOTO_JOB_INFLIGHT_STALE_GRACE_SECONDS

    def _expire_stale_photo_jobs(self):
        if not self._photo_jobs_inflight:
            return
        now_ts = time.time()
        stale_after = self._photo_job_stale_seconds()
        changed = False
        for slot_key in list(self._photo_jobs_inflight):
            started_at = float(self._photo_jobs_inflight_started.get(slot_key) or now_ts)
            if now_ts - started_at <= stale_after:
                continue
            date_text, _, time_text = slot_key.partition(" ")
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_text or "") or not re.match(r'^\d{2}:\d{2}$', time_text or ""):
                self._clear_photo_job_inflight(slot_key)
                continue
            if not self._check_photo_exists_for_slot(date_text, time_text):
                hour = int(time_text.split(":", 1)[0])
                activity = self._today_schedule_activity_map().get(time_text, "")
                self._failed_photo_jobs[slot_key] = {
                    "theme": self._theme_for_hour(hour),
                    "time": time_text,
                    "activity": activity,
                    "failed_at": self._now().isoformat(),
                    "error": f"stale running state after {int(now_ts - started_at)}s",
                    "error_summary": "生成状态卡住，已转为可重试",
                }
                changed = True
            self._clear_photo_job_inflight(slot_key)
        if changed:
            self._save_failed_photo_jobs()

    def _today_completed_photo_count(self) -> int:
        today_str = self._today().isoformat()
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
                if entry.get("delivery_status") in {"sending", "failed"}:
                    continue
                img_file = entry.get("image_filename", "")
                if img_file and self._photo_image_exists(img_file):
                    seen.add(img_file)
        except Exception as e:
            logger.error(f"统计今日已完成生图失败: {e}")
        return len(seen)

    def _today_inflight_photo_count(self, today_str: str = "") -> int:
        self._expire_stale_photo_jobs()
        today_str = today_str or self._today().isoformat()
        return sum(1 for key in self._photo_jobs_inflight if key.startswith(f"{today_str} "))

    def _today_scheduled_photo_count(self, today_str: str = "") -> int:
        today_str = today_str or self._today().isoformat()
        try:
            target_date = datetime.strptime(today_str, "%Y-%m-%d").date()
        except ValueError:
            target_date = self._today()
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
        today_str = today_str or self._today().isoformat()
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
        today_str = today_str or self._today().isoformat()
        times = set()
        for slot_key, failed in self._failed_photo_jobs.items():
            date_text, _, time_text = slot_key.partition(" ")
            if date_text == today_str and re.match(r'^\d{2}:\d{2}$', time_text):
                if failed.get("reason") == "delivery_failed" or not self._check_photo_exists_for_slot(today_str, time_text):
                    times.add(time_text)
        return times

    def _today_inflight_photo_times(self, today_str: str = "") -> set[str]:
        self._expire_stale_photo_jobs()
        today_str = today_str or self._today().isoformat()
        times = set()
        for slot_key in self._photo_jobs_inflight:
            date_text, _, time_text = slot_key.partition(" ")
            if date_text == today_str and re.match(r'^\d{2}:\d{2}$', time_text):
                times.add(time_text)
        return times

    def _today_scheduled_photo_times(self, today_str: str = "") -> set[str]:
        today_str = today_str or self._today().isoformat()
        try:
            target_date = datetime.strptime(today_str, "%Y-%m-%d").date()
        except ValueError:
            target_date = self._today()
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
        today_str = today_str or self._today().isoformat()
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
        today_str = today_str or self._today().isoformat()
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
        today = self._today()
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
        today_str = self._today().isoformat()
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
            and entry.get("source") != "fallback"
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
        today_str = self._today().isoformat()
        try:
            all_data = ScheduleStore(self.data_dir).load()
            if self._is_usable_schedule_entry(all_data.get(today_str)):
                return all_data[today_str]
            for entry in all_data.values():
                if (
                    self._is_usable_schedule_entry(entry)
                    and not entry.get("image_filename")
                    and entry.get("date") == today_str
                ):
                    return entry
        except Exception as e:
            logger.error(f"读取今日日程条目失败: {e}")
        return {}

    def _today_schedule_text(self) -> str:
        return self._today_schedule_entry().get("schedule", "")

    def update_photo_plan(self, time_text: str, activity: str) -> dict:
        """Update one activity line in today's saved schedule and pending photo job."""
        normalized_time = normalize_schedule_clock(time_text)
        activity = repair_mojibake_text(re.sub(r"\s+", " ", str(activity or "").strip())).strip()
        if not normalized_time:
            return {"status": "error", "message": "invalid_time"}
        if not activity:
            return {"status": "error", "time": normalized_time, "message": "empty_activity"}

        today_str = self._today().isoformat()
        result = {"status": "not_found", "time": normalized_time}

        def _update(all_data):
            if not isinstance(all_data, dict):
                all_data = {}

            schedule_key = ""
            if self._is_usable_schedule_entry(all_data.get(today_str)):
                schedule_key = today_str
            if not schedule_key:
                for key, entry in all_data.items():
                    if (
                        isinstance(entry, dict)
                        and not entry.get("image_filename")
                        and entry.get("date") == today_str
                        and self._is_usable_schedule_entry(entry)
                    ):
                        schedule_key = key
                        break

            if not schedule_key:
                result.update({"status": "not_found", "message": "today_schedule_not_found"})
                return all_data

            entry = dict(all_data.get(schedule_key) or {})
            schedule_text, found = replace_schedule_activity(
                entry.get("schedule", ""),
                normalized_time,
                activity,
            )
            if not found:
                result.update({"status": "not_found", "message": "schedule_time_not_found"})
                return all_data

            entry["schedule"] = schedule_text
            if entry.get("schedule_prompt"):
                prompt_text, _ = replace_schedule_activity(
                    entry.get("schedule_prompt", ""),
                    normalized_time,
                    activity,
                )
                entry["schedule_prompt"] = prompt_text
            if isinstance(entry.get("schedule_details"), list):
                details, _ = update_schedule_details_activity(
                    entry.get("schedule_details"),
                    normalized_time,
                    activity,
                )
                entry["schedule_details"] = details
            entry["schedule_edited_at"] = self._now().isoformat()
            entry["schedule_edit_source"] = "web"
            all_data[schedule_key] = entry
            result.update({
                "status": "ok",
                "time": normalized_time,
                "activity": activity,
                "schedule_key": schedule_key,
            })
            return all_data

        ScheduleStore(self.data_dir).update(_update)
        if result.get("status") != "ok":
            return result

        slot_key = f"{today_str} {normalized_time}"
        failed = self._failed_photo_jobs.get(slot_key)
        if isinstance(failed, dict):
            failed["activity"] = activity
            self._save_failed_photo_jobs()
            result["failed_job_updated"] = True

        job_id = self._photo_job_id_for_time(normalized_time)
        job_updated = False
        if job_id:
            job = self.aps.get_job(job_id)
            if job:
                run_time = self._local_job_run_time(job)
                if not run_time or run_time.date() == self._today():
                    theme = job.args[0] if getattr(job, "args", None) else self._theme_for_hour(int(normalized_time.split(":", 1)[0]))
                    schedule_text_for_job = f"{normalized_time} {activity}".strip()
                    job.modify(args=[theme, schedule_text_for_job])
                    self._photo_job_schedule_meta[job_id] = {
                        "theme": theme,
                        "time": normalized_time,
                        "activity": activity,
                    }
                    job_updated = True
        result["job_updated"] = job_updated
        logger.info("今日生图计划已编辑: time=%s activity=%s job_updated=%s", normalized_time, activity[:40], job_updated)
        return result

    def update_outfit_plan(self, field: str, value: str) -> dict:
        """Update one editable field in today's outfit and pending slot details."""
        field = normalize_outfit_plan_field(field)
        value = normalize_outfit_plan_value(value)
        if not field:
            return {"status": "error", "message": "invalid_outfit_field"}
        if not value:
            return {"status": "error", "field": field, "message": "empty_outfit_value"}
        if len(value) > MAX_OUTFIT_PLAN_EDIT_LENGTH:
            return {"status": "error", "field": field, "message": "outfit_value_too_long"}

        today_str = self._today().isoformat()
        result = {"status": "not_found", "date": today_str, "field": field}

        def _update(all_data):
            schedule_key = ""
            if self._is_usable_schedule_entry(all_data.get(today_str)):
                schedule_key = today_str
            if not schedule_key:
                for key, entry in all_data.items():
                    if (
                        isinstance(entry, dict)
                        and not entry.get("image_filename")
                        and entry.get("date") == today_str
                        and self._is_usable_schedule_entry(entry)
                    ):
                        schedule_key = key
                        break
            if not schedule_key:
                result["message"] = "today_schedule_not_found"
                return all_data

            entry = dict(all_data.get(schedule_key) or {})
            entry["outfit"] = replace_outfit_plan_field(entry.get("outfit", ""), field, value)
            entry["schedule_details"] = update_schedule_details_outfit(
                entry.get("schedule_details"),
                field,
                value,
            )
            if field == "穿搭":
                entry["outfit_keywords"] = value
            entry["outfit_edited_at"] = self._now().isoformat()
            entry["outfit_edit_source"] = "web"
            all_data[schedule_key] = entry
            result.update({
                "status": "ok",
                "value": value,
                "schedule_key": schedule_key,
                "updated_slots": len(entry.get("schedule_details") or []),
            })
            return all_data

        ScheduleStore(self.data_dir).update(_update)
        logger.info(
            "今日穿搭方案已编辑: field=%s updated_slots=%s",
            field,
            result.get("updated_slots", 0),
        )
        return result

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

        now = self._naive_now()
        today = now.date()
        today_str = today.strftime("%Y-%m-%d")

        # Remove old dynamic photo jobs
        removed = 0
        for job in self.aps.get_jobs():
            if job.id.startswith("photo_dynamic_"):
                job.remove()
                self._photo_job_schedule_meta.pop(job.id, None)
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

            schedule_time_str = f"{h:02d}:{m:02d}"
            if self._is_photo_quiet_time(h, m):
                logger.info("跳过生图静默时段任务: %s activity=%s", schedule_time_str, activity.strip()[:30])
                continue
            if self._is_exact_hour_time(h, m):
                logger.info("跳过整点生图任务: %s activity=%s", schedule_time_str, activity.strip()[:30])
                continue

            theme = self._theme_for_hour(h)

            job_id = f"photo_dynamic_{h}_{m}"

            # Skip if job already exists
            existing = self.aps.get_job(job_id)
            if existing:
                logger.info(f"跳过已存在的动态任务: {job_id}")
                continue

            run_time = datetime.combine(today, dt_time(h, m))
            schedule_text_for_job = f"{schedule_time_str} {activity.strip()}".strip()

            # Do not auto-backfill expired slots, but keep selected ones visible
            # so the user can explicitly retry them from the daily plan.
            if run_time <= now:
                if self._check_photo_exists_for_slot(today_str, schedule_time_str):
                    logger.info(f"跳过已过期的时间（已有图片）: {schedule_time_str}")
                    continue
                slot_key = f"{today_str} {schedule_time_str}"
                if schedule_time_str in planned_times:
                    reason = "等待手动重试" if slot_key in self._failed_photo_jobs else "已有计划记录"
                    logger.info(f"跳过已过期的时间（{reason}）: {schedule_time_str}")
                    continue

            candidates.append({
                "id": job_id,
                "hour": h,
                "minute": m,
                "activity": activity.strip(),
                "theme": theme,
                "run_time": run_time,
                "period_label": self._schedule_period_label(h, m),
                "expired": run_time <= now,
            })

        remaining_slots = max(0, max_daily - len(planned_times))
        selected_jobs = self._select_photo_job_candidates(candidates, remaining_slots, planned_periods)
        scheduled_count = 0
        retryable_expired_count = 0
        failed_jobs_changed = False
        for item in selected_jobs:
            if item.get("expired"):
                time_text = f"{item['hour']:02d}:{item['minute']:02d}"
                slot_key = f"{today_str} {time_text}"
                message = "计划时段已过，不自动补拍，可手动重试"
                self._failed_photo_jobs[slot_key] = {
                    "theme": item["theme"],
                    "time": time_text,
                    "activity": item["activity"],
                    "failed_at": now.isoformat(),
                    "error": message,
                    "error_summary": message,
                    "reason": "expired",
                }
                failed_jobs_changed = True
                retryable_expired_count += 1
                logger.info(
                    f"跳过已过期的时间（不自动补拍，可手动重试）: {time_text} "
                    f"activity={item['activity'][:30]}"
                )
                continue
            self.aps.add_job(
                self.photo_job,
                'date',
                run_date=item["run_time"],
                args=[item["theme"], f"{item['hour']:02d}:{item['minute']:02d} {item['activity']}"],
                id=item["id"],
                misfire_grace_time=PHOTO_JOB_MISFIRE_GRACE_SECONDS,
                coalesce=True,
                max_instances=1,
            )
            scheduled_count += 1
            self._photo_job_schedule_meta[item["id"]] = {
                "theme": item["theme"],
                "time": f"{item['hour']:02d}:{item['minute']:02d}",
                "activity": item["activity"],
            }
            logger.info(
                f"添加动态生图任务: {item['hour']:02d}:{item['minute']:02d} "
                f"theme={item['theme']} period={item.get('period_label') or '-'} "
                f"activity={item['activity'][:30]}"
            )

        if failed_jobs_changed:
            self._save_failed_photo_jobs()

        logger.info(
            f"动态生图任务已创建: {scheduled_count} 个 "
            f"(expired_retryable={retryable_expired_count}, "
            f"existing_plans={len(planned_times)}, max_daily={max_daily})"
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
                self._clear_photo_job_inflight(slot_key)

    async def _retry_photo_delivery(self, slot_key: str, failed: dict) -> None:
        """Resend an already generated image without consuming generation quota."""
        image_path = self._photo_image_path(
            failed.get("image_path") or failed.get("image_filename") or ""
        )
        filename = os.path.basename(image_path or str(failed.get("image_filename") or ""))
        caption = str(failed.get("caption") or "").strip()
        try:
            if not image_path:
                error = "delivery_retry_image_missing"
                failed.update({
                    "failed_at": self._now().isoformat(),
                    "error": error,
                    "error_summary": "原图文件已不存在，无法重发",
                })
                self._failed_photo_jobs[slot_key] = failed
                self._save_failed_photo_jobs()
                return

            if not self._delivery_enabled():
                error = "delivery_disabled"
                self._set_gallery_delivery_status(filename, "disabled", error=error)
                failed.update({
                    "failed_at": self._now().isoformat(),
                    "error": error,
                    "error_summary": "图片推送已在设置中关闭，开启后再重发",
                })
                self._failed_photo_jobs[slot_key] = failed
                self._save_failed_photo_jobs()
                return

            self._set_gallery_delivery_status(filename, "sending")
            send_ok = await self._send_generated_photo(image_path, caption)
            if send_ok:
                self._set_gallery_delivery_status(filename, "sent")
                current = self._failed_photo_jobs.get(slot_key)
                if isinstance(current, dict) and current.get("reason") == "delivery_failed":
                    self._failed_photo_jobs.pop(slot_key, None)
                    self._save_failed_photo_jobs()
                logger.info("原图重发成功: %s image=%s", slot_key, filename)
                return

            error = self._last_delivery_error or "delivery_failed"
            self._set_gallery_delivery_status(filename, "failed", error=error)
            failed.update({
                "failed_at": self._now().isoformat(),
                "error": error[-1200:],
                "error_summary": "图片重发仍未送达；请恢复会话或等待限流解除后再试",
            })
            self._failed_photo_jobs[slot_key] = failed
            self._save_failed_photo_jobs()
            logger.error("原图重发失败: %s image=%s error=%s", slot_key, filename, error)
        except Exception as exc:
            error = str(exc) or "delivery_retry_failed"
            self._set_gallery_delivery_status(filename, "failed", error=error)
            failed.update({
                "failed_at": self._now().isoformat(),
                "error": error[-1200:],
                "error_summary": "图片重发异常，可稍后再次重试",
            })
            self._failed_photo_jobs[slot_key] = failed
            self._save_failed_photo_jobs()
            logger.error("原图重发异常: %s error=%s", slot_key, exc, exc_info=True)
        finally:
            async with self._inflight_lock:
                self._clear_photo_job_inflight(slot_key)

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
        today_str = self._today().isoformat()
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
        self._expire_stale_photo_jobs()
        activity_by_time = self._today_schedule_activity_map()
        jobs = []
        today_str = self._today().isoformat()
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
            started_at = float(self._photo_jobs_inflight_started.get(slot_key) or time.time())
            running_seconds = max(0, int(time.time() - started_at))
            stale_after = self._photo_job_stale_seconds()
            failed = self._failed_photo_jobs.get(slot_key) or {}
            delivery_retry = failed.get("reason") == "delivery_failed"
            jobs.append({
                "id": f"photo_backfill_{time_text.replace(':', '_')}",
                "type": "photo",
                "status": "sending" if delivery_retry else "running",
                "theme": self._theme_for_hour(hour),
                "time": time_text,
                "run_at": datetime.fromtimestamp(started_at).isoformat(),
                "started_at": datetime.fromtimestamp(started_at).isoformat(),
                "running_seconds": running_seconds,
                "stale_after_seconds": stale_after,
                "retryable": running_seconds >= stale_after,
                "activity": activity_by_time.get(time_text, ""),
                "source": "delivery_retry" if delivery_retry else "backfill",
                "image_filename": failed.get("image_filename", "") if delivery_retry else "",
            })

        for slot_key, failed in sorted(self._failed_photo_jobs.items()):
            date_text, _, time_text = slot_key.partition(" ")
            if date_text != today_str or not re.match(r'^\d{2}:\d{2}$', time_text):
                continue
            delivery_failed = failed.get("reason") == "delivery_failed"
            if time_text in seen_times or (
                not delivery_failed and self._check_photo_exists_for_slot(today_str, time_text)
            ):
                continue
            seen_times.add(time_text)
            hour = int(time_text.split(":", 1)[0])
            expired = failed.get("reason") == "expired"
            jobs.append({
                "id": f"photo_failed_{time_text.replace(':', '_')}",
                "type": "photo",
                "status": "delivery_failed" if delivery_failed else ("missed" if expired else "failed"),
                "theme": failed.get("theme") or self._theme_for_hour(hour),
                "time": time_text,
                "run_at": failed.get("failed_at") or self._now().isoformat(),
                "activity": failed.get("activity") or activity_by_time.get(time_text, ""),
                "source": "delivery_failed" if delivery_failed else ("expired" if expired else "failed"),
                "error": failed.get("error", ""),
                "error_summary": failed.get("error_summary", "")
                or self._summarize_photo_failure(failed.get("error", "")),
                "image_filename": failed.get("image_filename", ""),
                "retry_label": "重发" if delivery_failed else "重试",
            })

        jobs.sort(key=lambda item: item["time"])
        return jobs

    def _slot_key_for_schedule_time(self, schedule_time: str) -> tuple[str, str, str]:
        """Return (slot_key, HH:mm, activity) for a schedule-time string."""
        match = re.match(r'\s*(\d{1,2}):(\d{2})\s*(.*)', schedule_time or "")
        if not match:
            return "", "", ""
        time_text = f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
        today_str = self._today().isoformat()
        return f"{today_str} {time_text}", time_text, match.group(3).strip()

    @staticmethod
    def _summarize_photo_failure(detail: str) -> str:
        """Return a short UI-friendly reason for a photo generation failure."""
        text = detail or ""
        lower = text.lower()
        reasons = []
        chat_fallback_disabled = "chat-compatible gpt image fallback is disabled" in lower

        endpoint_match = re.search(r'\[(https?://[^/\]]+|[\w.-]+:\d+)\]', text)
        endpoint = endpoint_match.group(1) if endpoint_match else ""

        connection_failed = any(
            token in lower
            for token in (
                "failed to establish a new connection",
                "connecttimeouterror",
                "connection refused",
                "host is down",
                "no route to host",
            )
        )
        request_timed_out = "timeout" in lower or "timed out" in lower
        if endpoint and connection_failed:
            reasons.append(f"GPT Image 中转 {endpoint} 当时连接失败")
        elif endpoint and request_timed_out:
            reasons.append(f"GPT Image 中转 {endpoint} 当时响应超时")

        if "dial tcp: lookup" in lower or "no such host" in lower:
            reasons.append("上游中转内部 DNS 解析失败")
        if "ssleoferror" in lower or "unexpected_eof_while_reading" in lower:
            reasons.append("GPT Image 上游 SSL 连接被断开")
        if "max retries exceeded" in lower and not (connection_failed or request_timed_out):
            reasons.append("连接多次重试仍失败")
        if request_timed_out and not endpoint:
            reasons.append("生图请求超时")
        if "unauthorized" in lower or "invalid api key" in lower or "401" in lower:
            reasons.append("API Key 校验失败")
        elif "rate limit" in lower or "429" in lower:
            reasons.append("上游限流")
        elif "content_policy_violation" in lower or "safety system" in lower:
            reasons.append("上游安全策略拒绝了这次图片请求")
        elif "direct gpt api error 502" in lower:
            reasons.append("GPT Image 上游 502")
        elif "direct gpt api error 503" in lower:
            reasons.append("GPT Image 上游 503")
        elif "direct gpt api error 504" in lower:
            reasons.append("GPT Image 上游 504")
        elif "path not found" in lower or "images api error 404" in lower:
            if chat_fallback_disabled:
                reasons.append("当前中转不支持 Images API")
            else:
                reasons.append("当前中转不支持 Images API，已尝试 Chat 兼容生图")

        if (
            "no base64 image in response" in lower
            and (
                "cannot create" in lower
                or "can't help" in lower
                or "not able to create" in lower
                or "sexualized" in lower
                or "minors" in lower
                or "restricted" in lower
            )
        ):
            reasons.append("GPT Image 返回安全拒绝，未产生图片")
        elif "no base64 image in response" in lower:
            reasons.append("GPT Image 返回了文字但没有图片")

        if "agnes endpoint failed" in lower:
            reasons.append("Agnes 最终没有返回图片")
        elif "generation failed" in lower or "gpt image endpoint failed" in lower:
            reasons.append("GPT Image 最终没有返回图片")

        if "gitee fallback is disabled" in lower:
            reasons.append("当时 Gitee 回退未启用，无法自动换线路")
        if chat_fallback_disabled:
            reasons.append("当时 Chat 端点回退未启用，未改走 /chat/completions")

        if reasons:
            unique = "；".join(dict.fromkeys(reasons))
            if "Gitee" not in unique and ("连接失败" in unique or "响应超时" in unique):
                unique += "；请检查中转服务状态后重试"
            elif "DNS" in unique:
                unique += "；请检查中转内部上游域名或 DNS"
            return unique
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        return first_line[:120] if first_line else "未知失败"

    async def retry_photo_job(self, schedule_time: str) -> dict:
        """Queue an immediate retry/backfill for a schedule time."""
        slot_key, time_text, activity_hint = self._slot_key_for_schedule_time(schedule_time)
        if not slot_key:
            return {"status": "error", "message": "invalid_time"}
        failed = self._failed_photo_jobs.get(slot_key) or {}
        if failed.get("reason") == "delivery_failed":
            image_path = self._photo_image_path(
                failed.get("image_path") or failed.get("image_filename") or ""
            )
            if not image_path:
                return {
                    "status": "image_missing",
                    "time": time_text,
                    "message": "原图文件已不存在，无法重发",
                }
            async with self._inflight_lock:
                self._expire_stale_photo_jobs()
                if slot_key in self._photo_jobs_inflight:
                    return {"status": "running", "time": time_text, "action": "resend"}
                self._mark_photo_job_inflight(slot_key)
            logger.info("手动重发已有图片: %s image=%s", time_text, os.path.basename(image_path))
            asyncio.create_task(self._retry_photo_delivery(slot_key, dict(failed)))
            return {
                "status": "queued",
                "action": "resend",
                "time": time_text,
                "image_filename": os.path.basename(image_path),
            }
        time_match = re.match(r"^(\d{2}):(\d{2})$", time_text or "")
        if time_match and self._is_photo_quiet_time(int(time_match.group(1)), int(time_match.group(2))):
            return {
                "status": "quiet_schedule_time",
                "time": time_text,
                "message": "03:00-05:59 不安排生图日程",
            }
        if time_match and self._is_exact_hour_time(int(time_match.group(1)), int(time_match.group(2))):
            return {
                "status": "exact_schedule_time",
                "time": time_text,
                "message": "日程生图时间需要上下浮动，不能是整点",
            }
        if self._is_photo_quiet_now():
            return {
                "status": "quiet_hours",
                "time": time_text,
                "message": "03:00-06:00 为日程生成时段，不执行生图",
            }

        today_str = self._today().isoformat()
        if self._check_photo_exists_for_slot(today_str, time_text):
            return {"status": "already_done", "time": time_text}

        activity = self._today_schedule_activity_map().get(time_text, activity_hint)
        if not activity:
            return {"status": "error", "time": time_text, "message": "schedule_time_not_found"}

        async with self._inflight_lock:
            self._expire_stale_photo_jobs()
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
            self._mark_photo_job_inflight(slot_key)
            if self._failed_photo_jobs.pop(slot_key, None) is not None:
                self._save_failed_photo_jobs()

        theme = self._theme_for_hour(int(time_text.split(":", 1)[0]))
        schedule_text_for_job = f"{time_text} {activity}".strip()
        logger.info(f"手动重试生图任务: {time_text} theme={theme} activity={activity[:30]}")
        asyncio.create_task(self._backfill_photo_job(theme, schedule_text_for_job, slot_key))
        return {"status": "queued", "time": time_text, "theme": theme, "activity": activity}

    async def photo_job(self, theme: str, schedule_time: str = "", quota_reserved: bool = False) -> bool:
        """定时生图任务 - 调用 generate.py 完整链路"""
        target_size = schedule_image_size(self.config)
        logger.info(f"开始定时生图: theme={theme}, size={target_size}, schedule_time={schedule_time}")
        slot_key, time_text, activity = self._slot_key_for_schedule_time(schedule_time)
        job_id = self._photo_job_id_for_time(time_text)
        if job_id:
            self._photo_job_schedule_meta.pop(job_id, None)
        if self._is_photo_quiet_now():
            logger.info("跳过生图任务（当前为 03:00-06:00 日程生成时段）: %s", time_text or schedule_time)
            return True
        time_match = re.match(r"^(\d{2}):(\d{2})$", time_text or "")
        if time_match and self._is_photo_quiet_time(int(time_match.group(1)), int(time_match.group(2))):
            logger.info("跳过生图任务（计划时间落在 03:00-06:00 静默时段）: %s", time_text)
            return True
        if time_match and self._is_exact_hour_time(int(time_match.group(1)), int(time_match.group(2))):
            logger.info("跳过生图任务（计划时间为整点，需上下浮动）: %s", time_text)
            return True
        reserved_slot = False
        if slot_key and time_text:
            today_str, _, _ = slot_key.partition(" ")
            async with self._inflight_lock:
                self._expire_stale_photo_jobs()
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
                    self._mark_photo_job_inflight(slot_key)
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
            "--size", target_size,
        ]
        daily_entry = self._today_schedule_entry()
        selected_reference = await self._select_reference_for_generation({
            "source": "cron",
            "theme": theme,
            "schedule_time": schedule_time,
            "activity": activity,
            "outfit_style": daily_entry.get("outfit_style", ""),
            "outfit": daily_entry.get("outfit", ""),
            "reference_query": daily_entry.get("reference_query", ""),
            "prompt": daily_entry.get("prompt", ""),
            "schedule": daily_entry.get("schedule", ""),
            "schedule_prompt": daily_entry.get("schedule_prompt", ""),
            "schedule_details": daily_entry.get("schedule_details", []),
        })
        if selected_reference.get("path"):
            cmd.extend(["--ref-image", selected_reference["path"]])
            logger.info(
                "定时生图选择参考图: %s mode=%s",
                selected_reference.get("label") or selected_reference.get("filename"),
                selected_reference.get("selection_mode", ""),
            )
        else:
            cmd.append("--no-auto-style")
        if schedule_time:
            cmd.extend(["--schedule-time", schedule_time])
        try:
            loop = asyncio.get_running_loop()
            child_env = self.image_gen.build_env()
            if self._delivery_enabled():
                child_env["ZHUZHU_DELIVERY_PENDING"] = "1"
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=image_process_timeout(self.config, with_reference_fallback=True),
                    cwd=self.image_gen.script_dir,
                    env=child_env,
                )
            )
            if result.returncode == 0:
                # 解析输出获取图片路径和caption
                image_path = ""
                caption_text = ""
                for line in f"{result.stdout}\n{result.stderr}".split("\n"):
                    line = line.strip()
                    if line.startswith("SUCCESS:"):
                        image_path = line.split("SUCCESS:", 1)[1].strip()
                    if "Synced to gallery" in line:
                        logger.info(f"定时生图成功: {line}")
                        if not image_path:
                            synced_filename = line.rsplit(":", 1)[-1].strip()
                            image_path = self._photo_image_path(synced_filename)
                    if line.startswith("CAPTION:"):
                        caption_text = self._prefer_caption_text(
                            line.split("CAPTION:", 1)[1].strip(),
                            caption_text,
                        )
                        logger.info(f"Caption: {caption_text}")
                logger.info(f"定时生图完成: theme={theme}")

                # 按设置的推送渠道发送到 TG / 微信。
                if image_path:
                    image_path = self._photo_image_path(image_path) or image_path
                    filename = os.path.basename(image_path)
                    if selected_reference and filename:
                        store = ScheduleStore(self.data_dir)
                        def _update_reference(all_data):
                            if filename in all_data:
                                all_data[filename]["selected_reference"] = {
                                    key: selected_reference.get(key, "")
                                    for key in ("id", "filename", "url", "label", "prompt", "source", "selection_mode", "selection_reason")
                                }
                            return all_data
                        try:
                            store.update(_update_reference)
                        except Exception as e:
                            logger.error("保存定时生图参考图信息失败: %s", e)
                    caption_text = self._gallery_caption_for_image(image_path, caption_text)
                    if self._delivery_enabled():
                        self._set_gallery_delivery_status(filename, "sending")
                        send_ok = await self._send_generated_photo(image_path, caption_text)
                        if not send_ok:
                            delivery_error = self._last_delivery_error or "delivery_failed"
                            self._record_photo_delivery_failure(
                                slot_key,
                                theme=theme,
                                time_text=time_text,
                                activity=activity,
                                image_path=image_path,
                                caption=caption_text,
                                error=delivery_error,
                            )
                            logger.error(
                                "图片已生成但推送失败: image=%s error=%s",
                                image_path,
                                delivery_error,
                            )
                            if quota_reserved:
                                return False
                            raise PhotoDeliveryError(delivery_error)
                        self._set_gallery_delivery_status(filename, "sent")
                    else:
                        self._set_gallery_delivery_status(filename, "disabled")
                if slot_key and self._failed_photo_jobs.pop(slot_key, None) is not None:
                    self._save_failed_photo_jobs()
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
                        "failed_at": self._now().isoformat(),
                        "error": detail[-1200:],
                        "error_summary": summary,
                    }
                    self._save_failed_photo_jobs()
                return False
        except PhotoDeliveryError:
            raise
        except subprocess.TimeoutExpired:
            timeout = image_process_timeout(self.config, with_reference_fallback=True)
            logger.error(f"定时生图超时: theme={theme} ({timeout}s)")
            if slot_key:
                self._failed_photo_jobs[slot_key] = {
                    "theme": theme,
                    "time": time_text,
                    "activity": activity,
                    "failed_at": self._now().isoformat(),
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
                    "failed_at": self._now().isoformat(),
                    "error": str(e),
                    "error_summary": summary,
                }
                self._save_failed_photo_jobs()
            return False
        finally:
            if reserved_slot:
                async with self._inflight_lock:
                    self._clear_photo_job_inflight(slot_key)

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
        self._last_delivery_error = ""
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
                self._last_delivery_error = ""
                return True
            logger.warning(f"OpenClaw 推送失败，尝试 Hermes fallback: channel={channel}")

        delivered = await self._send_to_hermes_channel(channel, image_path, caption, delivery)
        if delivered:
            self._last_delivery_error = ""
        elif not self._last_delivery_error:
            self._last_delivery_error = f"{channel}_delivery_failed"
        return delivered

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
            caption = repair_mojibake_text((entry or {}).get("caption") or "").strip()
            fallback = repair_mojibake_text(fallback).strip()
            if self._caption_text_usable(caption) or not self._caption_text_usable(fallback):
                return caption or fallback or ""

            def _repair_caption(all_data):
                target = all_data.get(filename)
                if isinstance(target, dict):
                    target["caption"] = fallback
                    return all_data
                for value in all_data.values():
                    if isinstance(value, dict) and value.get("image_filename") == filename:
                        value["caption"] = fallback
                        break
                return all_data

            try:
                ScheduleStore(self.data_dir).update(_repair_caption)
            except Exception as update_error:
                logger.warning("修复画廊小心思失败，使用生成输出文案: %s", update_error)
            return fallback
        except Exception as e:
            logger.warning("读取画廊小心思失败，使用生成输出文案: %s", e)
            return fallback or ""

    @staticmethod
    def _caption_text_usable(text: str) -> bool:
        text = repair_mojibake_text(text)
        text = re.sub(r"\s+", "", str(text or "")).strip("，,。.!！?；;、")
        return len(text) >= 4

    @classmethod
    def _prefer_caption_text(cls, candidate: str = "", current: str = "") -> str:
        candidate = repair_mojibake_text(candidate).strip()
        current = repair_mojibake_text(current).strip()
        if cls._caption_text_usable(candidate):
            return candidate
        if cls._caption_text_usable(current):
            return current
        return candidate or current

    async def _send_to_wechat(self, image_path: str, caption: str) -> bool:
        """Send image and caption to WeChat via hermes CLI."""
        return await self._send_to_hermes_channel("wechat", image_path, caption, self._push_delivery_config())

    async def _send_to_hermes_channel(self, channel: str, image_path: str, caption: str, delivery: dict) -> bool:
        """Send image and optional caption via Hermes CLI."""
        integrations = self.config.get("integrations", {})
        hermes_cmd = integrations.get("hermes_cli", "") or os.getenv("HERMES_CLI", "") or shutil.which("hermes")
        if not hermes_cmd:
            venv_subdir = "Scripts" if sys.platform.startswith("win") else "bin"
            candidate_names = ("hermes.exe", "hermes") if sys.platform.startswith("win") else ("hermes",)
            for candidate_name in candidate_names:
                candidate = os.path.join(os.path.expanduser("~"), ".hermes", "hermes-agent", "venv", venv_subdir, candidate_name)
                if os.path.exists(candidate):
                    hermes_cmd = candidate
                    break
        if not hermes_cmd:
            logger.warning("未找到 Hermes CLI，跳过推送")
            self._last_delivery_error = "hermes_cli_not_found"
            return False
        channel = normalize_push_channel(channel)
        target = delivery.get("wechat_target") if channel == "wechat" else (
            str(delivery.get("telegram_target") or integrations.get("hermes_telegram_target") or "").strip()
            or "telegram"
        )
        label = "微信" if channel == "wechat" else "TG"

        async with self._hermes_send_lock:
            image_ok = await self._run_hermes_send(
                hermes_cmd,
                target,
                f"MEDIA:{image_path}",
                f"{label}图片",
                required=True,
            )
            if not image_ok:
                logger.error(f"{label}发送失败: 图片未送达，跳过文案发送")
                if not self._last_delivery_error:
                    self._last_delivery_error = f"{channel}_image_delivery_failed"
                return False

            caption_ok = True
            if caption:
                caption_delay = WECHAT_CAPTION_DELAY_SECONDS if channel == "wechat" else CAPTION_DELAY_SECONDS
                logger.info(f"{label}图片发送成功，等待 {caption_delay}s 后发送文案以降低限流概率")
                await asyncio.sleep(caption_delay)
                caption_ok = await self._run_hermes_send(
                    hermes_cmd,
                    target,
                    caption,
                    f"{label}文案",
                    required=False,
                )

        if image_ok and caption_ok:
            logger.info(f"{label}发送完成")
            self._last_delivery_error = ""
            return True
        logger.warning(f"{label}图片已送达，文案发送被限流或失败: image_ok={image_ok}, caption_ok={caption_ok}")
        if image_ok:
            self._last_delivery_error = ""
        return image_ok

    async def _send_to_openclaw(self, channel: str, image_path: str, caption: str, delivery: dict) -> bool:
        """Send image and optional caption through OpenClaw when available."""
        integrations = self.config.get("integrations", {}) if isinstance(self.config.get("integrations"), dict) else {}
        openclaw_cmd = integrations.get("openclaw_cli", "") or os.getenv("OPENCLAW_CLI", "") or shutil.which("openclaw")
        if not openclaw_cmd:
            logger.warning("未找到 OpenClaw CLI，无法使用 OpenClaw 推送")
            self._last_delivery_error = "openclaw_cli_not_found"
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
            self._last_delivery_error = f"openclaw_{channel}_target_missing"
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
            self._last_delivery_error = "openclaw_delivery_timeout"
            return False
        except Exception as e:
            logger.warning(f"OpenClaw {label}推送异常: {e}")
            self._last_delivery_error = str(e) or "openclaw_delivery_error"
            return False

        output = self._hermes_send_output(result.stdout, result.stderr)
        if result.returncode == 0:
            logger.info(f"OpenClaw {label}推送成功")
            self._last_delivery_error = ""
            return True
        logger.warning(f"OpenClaw {label}推送失败: exit={result.returncode}, output={output}")
        self._last_delivery_error = output or f"openclaw_exit_{result.returncode}"
        return False

    async def _run_hermes_send(
        self,
        hermes_cmd: str,
        target: str,
        message: str,
        label: str,
        required: bool = True,
    ) -> bool:
        """Run `hermes send` with outer retry/backoff for Weixin rate limits."""
        attempts = 1 + len(WECHAT_RETRY_DELAYS_SECONDS)
        last_output = ""
        retry_after = 0.0

        for attempt_idx in range(attempts):
            attempt_no = attempt_idx + 1
            if attempt_idx:
                delay = retry_after or WECHAT_RETRY_DELAYS_SECONDS[attempt_idx - 1]
                logger.info(f"{label}发送重试等待 {delay}s ({attempt_no}/{attempts})")
                await asyncio.sleep(delay)
            await self._wait_hermes_send_cooldown(label)

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
                self._last_delivery_error = ""
                return True

            last_output = output or f"exit code {result.returncode}"
            if self._is_wechat_context_error(last_output):
                log_fn = logger.error if required else logger.warning
                log_fn(
                    f"{label}发送停止重试: 微信会话上下文已失效；"
                    f"请先在微信里给 Hermes 发任意一条消息，收到回复后再重试。"
                    f" output={last_output}"
                )
                self._last_delivery_error = last_output
                return False
            retryable = self._is_retryable_wechat_error(last_output)
            cooldown_seconds = self._extract_wechat_cooldown_seconds(last_output)
            if cooldown_seconds:
                retry_after = max(1.0, cooldown_seconds + WECHAT_COOLDOWN_BUFFER_SECONDS)
                self._mark_hermes_send_cooldown(retry_after)
            elif retryable and attempt_no < attempts:
                retry_after = float(WECHAT_RETRY_DELAYS_SECONDS[attempt_idx])
            log_fn = logger.warning if (retryable and attempt_no < attempts) or not required else logger.error
            log_fn(
                f"{label}发送失败: attempt={attempt_no}/{attempts}, "
                f"exit={result.returncode}, retryable={retryable}, output={last_output}"
            )
            if not retryable:
                break

        log_fn = logger.error if required else logger.warning
        log_fn(f"{label}发送最终失败: {last_output}")
        self._last_delivery_error = last_output or f"{label}_delivery_failed"
        return False

    async def _wait_hermes_send_cooldown(self, label: str):
        wait_seconds = max(0.0, self._hermes_send_cooldown_until - time.monotonic())
        if wait_seconds > 0:
            logger.info(f"{label}等待 Hermes/iLink 冷却 {wait_seconds:.1f}s")
            await asyncio.sleep(wait_seconds)

    def _mark_hermes_send_cooldown(self, seconds: float):
        if seconds <= 0:
            return
        self._hermes_send_cooldown_until = max(
            self._hermes_send_cooldown_until,
            time.monotonic() + seconds,
        )

    @staticmethod
    def _hermes_send_output(stdout: str, stderr: str) -> str:
        parts = [part.strip() for part in (stdout, stderr) if part and part.strip()]
        output = "\n".join(parts)
        if len(output) > 1500:
            return "..." + output[-1500:]
        return output

    @staticmethod
    def _is_wechat_context_error(output: str) -> bool:
        text = (output or "").lower()
        if any(marker in text for marker in WECHAT_CONTEXT_ERROR_MARKERS):
            return True
        has_minus_two = bool(
            re.search(r'\b(?:ret|errcode)\b["\']?\s*(?:=|:)\s*-2\b', text)
        )
        return has_minus_two and "unknown error" in text

    @classmethod
    def _is_retryable_wechat_error(cls, output: str) -> bool:
        if cls._is_wechat_context_error(output):
            return False
        text = (output or "").lower()
        return any(marker in text for marker in WECHAT_RETRYABLE_MARKERS)

    @staticmethod
    def _extract_wechat_cooldown_seconds(output: str) -> float:
        text = output or ""
        patterns = (
            r"cooldown active for\s+([0-9]+(?:\.[0-9]+)?)\s*s",
            r"retry(?:\s|-)?after[:=]?\s*([0-9]+(?:\.[0-9]+)?)\s*s",
            r"冷却\s*([0-9]+(?:\.[0-9]+)?)\s*秒",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    return max(0.0, float(match.group(1)))
                except ValueError:
                    return 0.0
        return 0.0

    def start(self):
        """启动所有服务（同步入口）"""
        asyncio.run(self._async_start())

    async def _restore_daily_schedule_state(self):
        """Recover today's schedule and in-memory photo jobs after any restart."""
        today_schedule = ""
        need_generate = True
        today_entry = self._today_schedule_entry()
        if today_entry:
            today_schedule = today_entry.get("schedule", "")
            missing_periods = self._schedule_missing_required_periods(today_schedule)
            if missing_periods:
                logger.warning("今日日程缺少时间段 %s，后台刷新修复", missing_periods)
            else:
                need_generate = False

        if need_generate:
            logger.info("今日尚未生成完整日程，启动后台补生成...")
            asyncio.create_task(self.daily_job())
            return

        logger.info("今日已有数据")
        self._schedule_next_daily_job(force_tomorrow=True)
        await self._schedule_dynamic_photos(today_schedule)

    async def _async_start(self):
        """异步启动"""
        self.aps.start()
        logger.info("动态任务调度器已启动")

        # 启动 Web 服务器
        runner = web.AppRunner(self.web_server.app)
        await runner.setup()
        site = web.TCPSite(runner, self.web_server.host, self.web_server.port)
        await site.start()
        logger.info(f"画廊启动: http://{self.web_server.host}:{self.web_server.port}")

        await self._restore_daily_schedule_state()

        # 保持运行
        await asyncio.Event().wait()


def main():
    config_path = resolve_config_path()
    app = PortraitGalleryApp(config_path)

    app.start()


if __name__ == "__main__":
    main()
