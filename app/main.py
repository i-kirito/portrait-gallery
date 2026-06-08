"""猪猪肖像画廊 - 主入口

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
    resolve_config_path,
    resolve_data_dir,
    resolve_script_dir,
)

TODAY_PHOTO_SOURCES = {"cron", "web"}
FAILED_SCHEDULE_TEXT = "生成失败"

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
        self.web_server.on_generate_today = self.generate_and_save
        self.web_server.on_generate_custom = self.generate_custom
        self.web_server.on_list_photo_jobs = self.list_photo_jobs
        self.web_server.on_refresh_schedule = self.refresh_schedule
        self.web_server.on_rebuild_photo_jobs = self.rebuild_photo_jobs

        # APScheduler
        timezone = self.config.get("config", {}).get("timezone", "Asia/Shanghai")
        self.aps = AsyncIOScheduler(timezone=timezone)

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

        # 如果选了风格参考图，映射为 style 参数
        style_map = {
            "reference_face.jpg": "cool",
            "ref_style_girly.jpg": "girly",
            "ref_style_sweet.jpg": "sweet",
        }
        style = None
        ref_path = ref_image if ref_image else ""
        if ref_image:
            # Check if it's a built-in style reference
            ref_basename = os.path.basename(ref_image)
            if ref_basename in style_map:
                style = style_map[ref_basename]
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
            caption="✨ 主人定制的专属造型～",
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
                self._schedule_dynamic_photos(preserved.schedule)
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
        self._schedule_dynamic_photos(entry.schedule)
        
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
        self._schedule_dynamic_photos(schedule_text)
        return self.list_photo_jobs()

    def _schedule_dynamic_photos(self, schedule_text: str):
        """Parse HH:mm times from schedule and create one-shot photo jobs."""
        if not schedule_text:
            logger.warning("日程文本为空，跳过动态生图调度")
            return

        today = datetime.now().date()

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

            # Skip if the time has already passed today
            if run_time <= datetime.now():
                logger.info(f"跳过已过期的时间: {h}:{m:02d}")
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

        jobs.sort(key=lambda item: item["time"])
        return jobs

    async def photo_job(self, theme: str, schedule_time: str = ""):
        """定时生图任务 - 调用 generate.py 完整链路"""
        logger.info(f"开始定时生图: theme={theme}, schedule_time={schedule_time}")
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

                # 直接通过 hermes send 发送到微信
                if image_path:
                    self._send_to_wechat(image_path, caption_text)
            else:
                logger.error(f"定时生图失败: theme={theme}, stderr={result.stderr[:500]}")
        except subprocess.TimeoutExpired:
            logger.error(f"定时生图超时: theme={theme} (900s)")
        except Exception as e:
            logger.error(f"定时生图异常: theme={theme}, {e}")

    def _send_to_wechat(self, image_path: str, caption: str):
        """Send image and caption to WeChat via hermes CLI."""
        import subprocess as sp
        integrations = self.config.get("integrations", {})
        if integrations.get("send_enabled") is False:
            logger.info("Hermes 发送已在配置中关闭")
            return
        hermes_cmd = integrations.get("hermes_cli", "") or os.getenv("HERMES_CLI", "") or shutil.which("hermes")
        if not hermes_cmd:
            candidate = os.path.join(os.path.expanduser("~"), ".hermes", "hermes-agent", "venv", "bin", "hermes")
            if os.path.exists(candidate):
                hermes_cmd = candidate
        if not hermes_cmd:
            logger.warning("未找到 Hermes CLI，跳过微信发送")
            return
        target = integrations.get("wechat_target", "weixin")
        try:
            # 发送图片
            logger.info(f"发送图片到微信: {image_path}")
            r1 = sp.run([hermes_cmd, "send", "--to", target, f"MEDIA:{image_path}"],
                        capture_output=True, text=True, timeout=60)
            if r1.returncode != 0:
                logger.error(f"发送图片失败: {r1.stderr[:200]}")
                return

            # 发送文案
            if caption:
                logger.info(f"发送文案到微信: {caption[:50]}...")
                r2 = sp.run([hermes_cmd, "send", "--to", target, caption],
                            capture_output=True, text=True, timeout=60)
                if r2.returncode != 0:
                    logger.error(f"发送文案失败: {r2.stderr[:200]}")

            logger.info("微信发送完成")
        except Exception as e:
            logger.error(f"微信发送异常: {e}")

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
            self._schedule_dynamic_photos(today_schedule)

        # 保持运行
        await asyncio.Event().wait()


def main():
    config_path = resolve_config_path()
    app = PortraitGalleryApp(config_path)

    app.start()


if __name__ == "__main__":
    main()
