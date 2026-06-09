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
    apply_network_env,
    configured_python,
    load_config,
    load_runtime_persona,
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
                    old = all_data[k]
                    if old.get("schedule") and not entry_dict.get("schedule"):
                        entry_dict["schedule"] = old["schedule"]
                    if old.get("schedule_prompt") and not entry_dict.get("schedule_prompt"):
                        entry_dict["schedule_prompt"] = old["schedule_prompt"]
                    del all_data[k]

            # If there's already an entry under new_key, merge rather than overwrite
            if new_key in all_data:
                existing = all_data[new_key]
                for field in ("favorite", "source", "time", "model_name", "base_style", "schedule_prompt"):
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
        apply_network_env(self.config)
        self.data_dir = resolve_data_dir(self.config, config_path)
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

        logger.info(f"日程生成成功: {entry.outfit_style}")

        # 2. 生成图片
        if entry.prompt and entry.status == "ok":
            filename = await self.image_gen.generate_for_outfit(entry.prompt, entry.outfit_style)
            if filename:
                entry.image_filename = filename
                entry.image_path = f"/images/{filename}"
                logger.info(f"图片生成成功: {filename}")
            else:
                logger.warning("图片生成失败")

        # 3. 保存
        save_schedule_entry(self.data_dir, entry)
        return entry

    async def generate_custom(self, user_prompt: str, size: str = "1024x1024", ref_image: str = "") -> DailyEntry:
        """自定义 prompt 生图"""
        today_str = datetime.now().strftime("%Y-%m-%d")
        ts = int(datetime.now().timestamp())

        style = None
        ref_path = ref_image if ref_image else ""
        if ref_image:
            # Check if it's a built-in style reference
            ref_basename = os.path.basename(ref_image)
            ref_style = reference_filename_to_style(ref_basename)
            if ref_style:
                style = ref_style
                ref_path = ref_image
            else:
                # Custom uploaded reference - use as --ref-image
                ref_path = ref_image

        kwargs = {}
        if ref_path:
            kwargs["ref_image"] = ref_path
        if size:
            kwargs["size"] = size

        filename = await self.image_gen.generate(
            user_prompt,
            style=style,
            timeout=900,
            **kwargs
        )
        if not filename:
            logger.error("自定义生图失败")
            return DailyEntry(date=today_str, outfit="生成失败", status="failed")

        entry = DailyEntry(
            date=today_str,
            outfit_style=style or "自定义",
            outfit=f"风格：{style or '自定义'} 穿搭：{user_prompt[:80]}",
            schedule="",
            prompt=user_prompt,
            caption=f"✨ {load_runtime_persona(self.config, self.data_dir).get('name') or '角色'}的定制专属造型完成啦～",
            image_filename=filename,
            image_path=f"/images/{filename}",
            status="ok",
            source="custom",
        )
        save_schedule_entry(self.data_dir, entry)
        logger.info(f"自定义生图成功: {filename}")
        return entry

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
                if entry.get("source", "") not in TODAY_PHOTO_SOURCES:
                    continue
                img_file = entry.get("image_filename", "")
                if img_file:
                    seen.add(img_file)
        except Exception as e:
            logger.error(f"统计今日已完成生图失败: {e}")
        return len(seen)

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

    def _select_photo_job_candidates(self, candidates: list[dict], limit: int) -> list[dict]:
        if limit <= 0:
            return []
        if len(candidates) <= limit:
            return candidates

        selected = []
        selected_ids = set()
        completed_periods = self._today_completed_photo_periods()
        try:
            required_labels = [period["label"] for period in self.scheduler_gen._required_periods()]
        except Exception as e:
            logger.error(f"读取日程必需时间段失败: {e}")
            required_labels = []

        for label in required_labels:
            if len(selected) >= limit:
                break
            if label in completed_periods:
                continue
            for candidate in candidates:
                if candidate["id"] in selected_ids or candidate.get("period_label") != label:
                    continue
                selected.append(candidate)
                selected_ids.add(candidate["id"])
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

    def rebuild_photo_jobs(self) -> list:
        """Rebuild dynamic photo jobs from today's saved schedule."""
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
        completed_count = self._today_completed_photo_count()
        remaining_slots = max(0, max_daily - completed_count)
        if remaining_slots <= 0:
            logger.info(f"今日生图已达上限: completed={completed_count}, max={max_daily}")
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

            # 已过期的时间点：如已有图片则跳过；未拍则立即补拍，不占用 APScheduler。
            if run_time <= now:
                if self._check_photo_exists_for_slot(today_str, schedule_time_str):
                    logger.info(f"跳过已过期的时间（已有图片）: {schedule_time_str}")
                    continue
                if completed_count >= max_daily:
                    logger.info(
                        f"跳过已过期的时间（今日生图已达上限）: {schedule_time_str} "
                        f"(completed={completed_count}, max_daily={max_daily})"
                    )
                    continue

                slot_key = f"{today_str} {schedule_time_str}"
                if slot_key in self._failed_photo_jobs:
                    logger.info(f"跳过已失败的过期时间（等待手动重试）: {schedule_time_str}")
                    continue

                async with self._inflight_lock:
                    if slot_key in self._photo_jobs_inflight:
                        logger.info(f"跳过已过期的时间（补拍任务进行中）: {schedule_time_str}")
                        continue
                    self._photo_jobs_inflight.add(slot_key)

                period_label = self._schedule_period_label(h, m)
                logger.info(
                    f"补拍已过期的时间点: {schedule_time_str} "
                    f"theme={theme} period={period_label or '-'} activity={activity.strip()[:30]}"
                )
                asyncio.create_task(self._backfill_photo_job(theme, schedule_text_for_job, slot_key))
                completed_count += 1
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

        remaining_slots = max(0, max_daily - completed_count)
        selected_jobs = self._select_photo_job_candidates(candidates, remaining_slots)
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
            f"(completed={completed_count}, max_daily={max_daily})"
        )

    async def _backfill_photo_job(self, theme: str, schedule_text: str, slot_key: str):
        """补拍任务包装器，串行执行并在结束后释放 inflight 标记。"""
        try:
            async with self._backfill_semaphore:
                try:
                    ok = await self.photo_job(theme, schedule_text)
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
            self._photo_jobs_inflight.add(slot_key)
            if self._failed_photo_jobs.pop(slot_key, None) is not None:
                self._save_failed_photo_jobs()

        theme = self._theme_for_hour(int(time_text.split(":", 1)[0]))
        schedule_text_for_job = f"{time_text} {activity}".strip()
        logger.info(f"手动重试生图任务: {time_text} theme={theme} activity={activity[:30]}")
        asyncio.create_task(self._backfill_photo_job(theme, schedule_text_for_job, slot_key))
        return {"status": "queued", "time": time_text, "theme": theme, "activity": activity}

    async def photo_job(self, theme: str, schedule_time: str = "") -> bool:
        """定时生图任务 - 调用 generate.py 完整链路"""
        logger.info(f"开始定时生图: theme={theme}, schedule_time={schedule_time}")
        slot_key, time_text, activity = self._slot_key_for_schedule_time(schedule_time)
        cmd = [
            self.image_gen.python_executable,
            self.image_gen.generate_script,
            "--theme", theme,
            "--caption",
            "--source", "cron",
        ]
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
                    timeout=900,
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

                # 直接通过 hermes send 发送到微信
                if image_path:
                    send_ok = await self._send_to_wechat(image_path, caption_text)
                    if not send_ok:
                        logger.warning(f"微信发送未完全成功: image={image_path}")
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
            logger.error(f"定时生图超时: theme={theme} (900s)")
            if slot_key:
                self._failed_photo_jobs[slot_key] = {
                    "theme": theme,
                    "time": time_text,
                    "activity": activity,
                    "failed_at": datetime.now().isoformat(),
                    "error": "timeout",
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

    async def _send_to_wechat(self, image_path: str, caption: str) -> bool:
        """Send image and caption to WeChat via hermes CLI."""
        integrations = self.config.get("integrations", {})
        if integrations.get("send_enabled") is False:
            logger.info("Hermes 发送已在配置中关闭")
            return True
        hermes_cmd = integrations.get("hermes_cli", "") or os.getenv("HERMES_CLI", "") or shutil.which("hermes")
        if not hermes_cmd:
            candidate = os.path.join(os.path.expanduser("~"), ".hermes", "hermes-agent", "venv", "bin", "hermes")
            if os.path.exists(candidate):
                hermes_cmd = candidate
        if not hermes_cmd:
            logger.warning("未找到 Hermes CLI，跳过微信发送")
            return False
        target = integrations.get("wechat_target", "weixin")

        image_ok = await self._run_hermes_send(hermes_cmd, target, f"MEDIA:{image_path}", "图片")
        if not image_ok:
            logger.error("微信发送失败: 图片未送达，跳过文案发送")
            return False

        caption_ok = True
        if caption:
            logger.info(f"微信图片发送成功，等待 {WECHAT_CAPTION_DELAY_SECONDS}s 后发送文案以降低限流概率")
            await asyncio.sleep(WECHAT_CAPTION_DELAY_SECONDS)
            caption_ok = await self._run_hermes_send(hermes_cmd, target, caption, "文案")

        if image_ok and caption_ok:
            logger.info("微信发送完成")
            return True
        logger.warning(f"微信发送部分成功: image_ok={image_ok}, caption_ok={caption_ok}")
        return False

    async def _run_hermes_send(self, hermes_cmd: str, target: str, message: str, label: str) -> bool:
        """Run `hermes send` with outer retry/backoff for Weixin rate limits."""
        attempts = 1 + len(WECHAT_RETRY_DELAYS_SECONDS)
        last_output = ""

        for attempt_idx in range(attempts):
            attempt_no = attempt_idx + 1
            if attempt_idx:
                delay = WECHAT_RETRY_DELAYS_SECONDS[attempt_idx - 1]
                logger.info(f"微信{label}发送重试等待 {delay}s ({attempt_no}/{attempts})")
                await asyncio.sleep(delay)

            logger.info(f"发送{label}到微信: attempt={attempt_no}/{attempts}")
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
                logger.warning(f"微信{label}发送超时: attempt={attempt_no}/{attempts}")
                continue
            except Exception as e:
                last_output = str(e)
                logger.warning(f"微信{label}发送异常: attempt={attempt_no}/{attempts}, error={e}")
                continue

            output = self._hermes_send_output(result.stdout, result.stderr)
            if result.returncode == 0:
                logger.info(f"微信{label}发送成功")
                return True

            last_output = output or f"exit code {result.returncode}"
            retryable = self._is_retryable_wechat_error(last_output)
            log_fn = logger.warning if retryable and attempt_no < attempts else logger.error
            log_fn(
                f"微信{label}发送失败: attempt={attempt_no}/{attempts}, "
                f"exit={result.returncode}, retryable={retryable}, output={last_output}"
            )
            if not retryable:
                break

        logger.error(f"微信{label}发送最终失败: {last_output}")
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
