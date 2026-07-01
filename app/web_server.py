"""Web 画廊服务器 - aiohttp"""
import asyncio
try:
    import fcntl
except ImportError:
    class _FcntlFallback:
        LOCK_SH = 1
        LOCK_EX = 2
        LOCK_UN = 8

        @staticmethod
        def flock(_fd, _op):
            return None

    fcntl = _FcntlFallback()
import hashlib
import ipaddress
import json
import logging
import os
import shlex
import shutil
import sys
import subprocess
from datetime import date, datetime
from pathlib import Path
import time
import re
from typing import Optional
from urllib.parse import unquote
import uuid

import aiohttp
from aiohttp import web
from PIL import Image

from picxazz_sync import PicxazzSyncClient
from reference_profiles import (
    analyze_reference_image,
    load_reference_profiles,
    reference_response as reference_profile_response,
    remove_reference_profile,
    resolve_reference_profile_path,
    select_reference_profile,
    upsert_reference_profile,
)
from store import ScheduleStore
from settings import (
    DEFAULT_OUTFIT_STYLES,
    auto_push_agent,
    builtin_reference_map,
    build_child_env,
    configured_python,
    image_process_timeout,
    llm_choice_text,
    llm_request_config,
    llm_response_excerpt,
    llm_temperature_param_error,
    load_enabled_outfit_styles,
    load_runtime_persona,
    normalize_chat_url,
    normalize_outfit_styles,
    normalize_custom_image_size,
    normalize_custom_shot_type,
    normalize_persona_source,
    normalize_push_channel,
    default_image_dir,
    normalize_image_dir,
    resolve_builtin_reference_dir,
    resolve_image_dir,
    resolve_project_root,
    resolve_reference_dir,
    resolve_script_dir,
)

logger = logging.getLogger(__name__)

GITHUB_RELEASE_API_URL = "https://api.github.com/repos/i-kirito/portrait-gallery/releases/latest"

# 日期 key 正则：匹配 YYYY-MM-DD 格式
DATE_KEY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
LOG_ENTRY_RE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,\d+)? '
    r'\[(?P<level>[A-Z]+)\] (?P<logger>[^:]+): (?P<message>.*)$'
)
LOG_ACCESS_RE = re.compile(r'"(?P<method>[A-Z]+)\s+(?P<path>[^"]+?)\s+HTTP/[^"]+"\s+(?P<status>\d{3})')
LOG_LEVEL_LABELS = {
    "DEBUG": "调试",
    "INFO": "信息",
    "WARNING": "警告",
    "ERROR": "错误",
    "CRITICAL": "严重错误",
}
LOG_USEFUL_KEYWORDS = (
    "生图",
    "日程",
    "任务",
    "补拍",
    "重试",
    "失败",
    "异常",
    "错误",
    "超时",
    "推送",
    "发送",
    "参考",
    "衣柜",
    "底模",
    "模型",
    "配置",
    "画廊启动",
    "Hermes",
    "Gitee",
    "GPT",
    "Gemini",
    "LLM",
    "Picxazz",
    "Generate now",
    "generate.py failed",
    "Custom generate",
    "image-to-image",
    "text-to-image",
    "fallback",
    "SUCCESS:",
    "CAPTION:",
    "Caption:",
)
LOG_ERROR_DETAIL_KEYWORDS = (
    "error",
    "failed",
    "failure",
    "exception",
    "traceback",
    "timeout",
    "timed out",
    "connection",
    "httpconnectionpool",
    "max retries",
    "host is down",
    "lookup",
    "fallback",
    "unauthorized",
    "forbidden",
    "错误",
    "失败",
    "异常",
    "超时",
)
DEFAULT_PHOTO_JOB_LIMIT = 6
MIN_PHOTO_JOB_LIMIT = 3
MAX_PHOTO_JOB_LIMIT = 6
TODAY_PHOTO_SOURCES = {"cron", "web"}
FAILED_SCHEDULE_TEXT = "生成失败"
REFERENCE_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
CLEANUP_PRESET_DAYS = {
    "3d": 3,
    "7d": 7,
    "1m": 30,
    "3m": 90,
}
REFERENCE_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
BUILTIN_REFERENCE_MAP = builtin_reference_map()
UPDATE_PROTECTED_EXACT = (
    ".env",
    "config/config.yaml",
    "config/local.yaml",
    "docker-compose.override.yml",
)
UPDATE_PROTECTED_PREFIXES = (
    "data/",
    "app/data/",
    "logs/",
    "app/references/uploads/",
)
LOCALHOST_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


class GalleryServer:
    """Portrait gallery Web server."""

    def __init__(self, config: dict, data_dir: str, config_path: str = ""):
        self.config = config
        self.data_dir = data_dir
        self.config_path = config_path
        self.gallery_config = config.get("gallery", {})
        self.host = self.gallery_config.get("host", "0.0.0.0")
        self.port = self.gallery_config.get("port", 18888)
        self.token = self.gallery_config.get("token", "")
        self.default_image_dir = default_image_dir(data_dir)
        self.image_dir = self._resolve_image_dir()
        self.app_reference_dir = resolve_builtin_reference_dir(config, config_path)
        self.reference_dir = resolve_reference_dir(config, data_dir, config_path)
        self.uploaded_reference_dir = os.path.join(self.reference_dir, "uploads")
        self.wardrobe_reference_dir = os.path.join(self.reference_dir, "wardrobe")
        self.legacy_uploaded_reference_dir = os.path.join(self.app_reference_dir, "uploads")
        self.picxazz_sync = PicxazzSyncClient(config, data_dir)
        self._image_info_cache = {}
        self._wardrobe_image_locks: dict[str, asyncio.Lock] = {}
        self._restart_scheduled = False
        os.makedirs(self.default_image_dir, exist_ok=True)
        os.makedirs(self.image_dir, exist_ok=True)
        os.makedirs(self.reference_dir, exist_ok=True)
        os.makedirs(self.uploaded_reference_dir, exist_ok=True)
        os.makedirs(self.wardrobe_reference_dir, exist_ok=True)
        load_reference_profiles(
            self.data_dir,
            self.reference_dir,
            self.app_reference_dir,
            self.uploaded_reference_dir,
        )
        self._migrate_legacy_uploaded_refs()

        # 回调：外部注入
        self.on_generate_today = None
        self.on_generate_custom = None
        self.on_reroll_image = None
        self.on_list_photo_jobs = None
        self.on_refresh_schedule = None
        self.on_rebuild_photo_jobs = None
        self.on_retry_photo_job = None
        self.on_image_dir_changed = None

        self.app = web.Application(middlewares=[self.api_key_middleware])
        self._setup_routes()

    @staticmethod
    def _is_local_request(request: web.Request) -> bool:
        remote = request.remote or ""
        if not remote and request.transport:
            peer = request.transport.get_extra_info("peername")
            if isinstance(peer, tuple) and peer:
                remote = str(peer[0])
        remote = str(remote or "").strip().strip("[]")
        if remote in LOCALHOST_NAMES:
            return True
        try:
            return ipaddress.ip_address(remote).is_loopback
        except ValueError:
            host = str(request.host or "").split(":", 1)[0].strip("[]").lower()
            return host in LOCALHOST_NAMES

    @staticmethod
    def _requires_local_or_key(request: web.Request) -> bool:
        if not request.path.startswith("/api/"):
            return False
        if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
            return False
        return True

    @staticmethod
    @web.middleware
    async def api_key_middleware(request: web.Request, handler):
        """Protect API routes with X-API-Key; local writes may run without a key."""
        path = request.path
        if path.startswith("/api/"):
            api_key = os.environ.get("GALLERY_API_KEY", "")
            provided = request.headers.get("X-API-Key", "") or request.query.get("key", "")
            if api_key:
                if provided != api_key:
                    return web.json_response({"error": "unauthorized"}, status=401)
            elif GalleryServer._requires_local_or_key(request) and not GalleryServer._is_local_request(request):
                return web.json_response(
                    {
                        "error": "local_or_api_key_required",
                        "message": "远程写操作需要配置 GALLERY_API_KEY 并通过 X-API-Key 调用。",
                    },
                    status=403,
                )
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
        self.app.router.add_post("/api/images/cleanup", self.handle_cleanup_images)
        self.app.router.add_post("/api/images/{img_id}/reroll", self.handle_reroll_image)
        self.app.router.add_post("/api/images/{img_id}/favorite", self.handle_toggle_favorite)
        self.app.router.add_post("/api/integrations/picxazz/sync-favorites", self.handle_sync_picxazz_favorites)
        self.app.router.add_delete("/api/images/{img_id}", self.handle_delete_image)
        self.app.router.add_get("/api/health", self.handle_health)
        self.app.router.add_get("/api/logs", self.handle_logs)
        self.app.router.add_get("/api/config/keys", self.handle_get_keys)
        self.app.router.add_post("/api/config/keys", self.handle_save_keys)
        self.app.router.add_get("/api/models", self.handle_models)
        self.app.router.add_post("/api/models/test", self.handle_test_llm_model)
        self.app.router.add_get("/api/image-models", self.handle_image_models)
        # Hermes 纯净生图 API（不注入 persona）
        self.app.router.add_post("/api/hermes/text-to-image", self.handle_hermes_text_to_image)
        self.app.router.add_post("/api/hermes/image-to-image", self.handle_hermes_image_to_image)
        self.app.router.add_get("/api/hermes/check-update", self.handle_hermes_check_update)
        self.app.router.add_post("/api/hermes/check-update", self.handle_hermes_check_update)
        self.app.router.add_get("/api/hermes/update", self.handle_hermes_check_update)
        self.app.router.add_post("/api/hermes/update", self.handle_hermes_update)
        self.app.router.add_post("/api/hermes/restart", self.handle_hermes_restart)
        # 版本管理
        self.app.router.add_get("/api/version", self.handle_version)
        self.app.router.add_post("/api/check-update", self.handle_check_update)
        self.app.router.add_post("/api/update", self.handle_update)
        self.app.router.add_post("/api/restart", self.handle_restart)
        # 日程彩蛋
        self.app.router.add_get("/api/schedule-detail", self.handle_schedule_detail)
        self.app.router.add_get("/api/photo-jobs", self.handle_photo_jobs)
        self.app.router.add_post("/api/photo-jobs/retry", self.handle_retry_photo_job)
        self.app.router.add_get("/api/photo-job-limit", self.handle_photo_job_limit)
        self.app.router.add_post("/api/photo-job-limit", self.handle_photo_job_limit)
        self.app.router.add_get("/api/favorite-outfits", self.handle_favorite_outfits)
        self.app.router.add_post("/api/favorite-outfits", self.handle_favorite_outfits)
        self.app.router.add_patch("/api/favorite-outfits/{outfit_id}", self.handle_edit_favorite_outfit)
        self.app.router.add_post("/api/favorite-outfits/{outfit_id}/edit", self.handle_edit_favorite_outfit)
        self.app.router.add_post("/api/favorite-outfits/{outfit_id}/wardrobe-image", self.handle_favorite_outfit_wardrobe_image)
        self.app.router.add_get("/api/disliked-outfits", self.handle_disliked_outfits)
        self.app.router.add_post("/api/disliked-outfits", self.handle_disliked_outfits)

        # 图片服务
        self.app.router.add_get("/images/{filename:.*}", self.handle_image_file)

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

    def _schedule_python_restart(self, reason: str = "manual", delay: float = 0.8) -> tuple[bool, str]:
        if self._restart_scheduled:
            return True, "服务重启已在执行中。"

        project_root = resolve_project_root(self.config_path, self.config)
        run_script = project_root / "app" / "run_launch.sh"
        if not run_script.is_file():
            return False, f"找不到 Python 启动脚本：{run_script}"
        log_path = project_root / "logs" / "gallery.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return False, f"无法创建日志目录：{e}"

        child_delay = max(0.3, delay + 0.4)
        command = (
            f"sleep {child_delay:.1f}; "
            f"cd {shlex.quote(str(project_root))}; "
            f"exec {shlex.quote(str(run_script))} >> {shlex.quote(str(log_path))} 2>&1"
        )
        env = self._child_env()
        self._restart_scheduled = True
        try:
            subprocess.Popen(
                ["/bin/zsh", "-lc", command],
                cwd=str(project_root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            self._restart_scheduled = False
            return False, f"启动 Python 重启进程失败：{e}"

        logger.warning("Python 服务重启已安排: reason=%s delay=%.1fs script=%s", reason, delay, run_script)
        loop = asyncio.get_running_loop()
        loop.call_later(max(0.1, delay), lambda: os._exit(0))
        return True, "服务即将以 Python 模式重启，不会拉取代码或修改本地设置。"

    async def handle_restart(self, request: web.Request):
        """Restart the Python service without git or config changes."""
        scheduled, message = self._schedule_python_restart("api_restart")
        return web.json_response(
            {
                "status": "ok" if scheduled else "restart_failed",
                "restart_scheduled": bool(scheduled),
                "will_restart": bool(scheduled),
                "service_manager": "python",
                "message": message,
            },
            status=202 if scheduled else 500,
        )

    async def handle_hermes_restart(self, request: web.Request):
        """Hermes-friendly Python restart endpoint."""
        scheduled, message = self._schedule_python_restart("hermes_api_restart")
        return web.json_response(
            {
                "api": "hermes_restart",
                "status": "ok" if scheduled else "restart_failed",
                "restart_scheduled": bool(scheduled),
                "will_restart": bool(scheduled),
                "service_manager": "python",
                "message": message,
                "preserves": [
                    "git working tree",
                    "config/config.yaml",
                    "data/",
                    "logs/",
                    "API Key / Base URL / appearance",
                ],
            },
            status=202 if scheduled else 500,
        )

    def _log_file_candidates(self) -> list[str]:
        candidates = []
        for logger_name in ("", "portrait_gallery", "aiohttp.access"):
            try:
                for handler in logging.getLogger(logger_name).handlers:
                    base_filename = getattr(handler, "baseFilename", "")
                    if base_filename:
                        candidates.append(base_filename)
            except Exception:
                continue
        candidates.extend([
            os.environ.get("HERMES_GALLERY_LOG", ""),
            os.path.expanduser("~/Library/Logs/hermes-portrait-gallery/gallery.log"),
            os.path.join(resolve_project_root(self.config_path), "logs", "gallery.log"),
            os.path.join(self.data_dir, "logs", "gallery.log"),
        ])
        result = []
        seen = set()
        for item in candidates:
            path = os.path.abspath(os.path.expanduser(str(item or "").strip()))
            if not path or path in seen:
                continue
            seen.add(path)
            if os.path.isfile(path):
                result.append(path)
        return result

    @staticmethod
    def _tail_log_file(path: str, lines: int, max_bytes: int = 512 * 1024) -> str:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(max(0, size - max_bytes))
                chunk = f.read(max_bytes)
                first_newline = chunk.find(b"\n")
                if first_newline >= 0:
                    chunk = chunk[first_newline + 1:]
            else:
                chunk = f.read()
        text = chunk.decode("utf-8", errors="replace")
        if lines > 0:
            text = "\n".join(text.splitlines()[-lines:])
        return text

    @staticmethod
    def _redact_log_text(text: str) -> str:
        text = re.sub(r'(?i)(authorization:\s*bearer\s+)[^\s"\']+', r'\1***', text or "")
        text = re.sub(r'(?i)((?:api[_-]?key|gpt[_-]?key|cpa[_-]?key|key)=)[^&\s"\']+', r'\1***', text)
        return text

    @staticmethod
    def _format_log_time(ts: str) -> str:
        return (ts or "")[5:]

    @staticmethod
    def _is_error_detail_line(line: str) -> bool:
        low = (line or "").lower()
        return any(k in low for k in LOG_ERROR_DETAIL_KEYWORDS)

    @staticmethod
    def _diagnose_error_text(text: str) -> str:
        low = (text or "").lower()
        if "status=no response" in low or "detail=no response" in low:
            return "该次文本模型请求当时没有拿到响应，不等于模型不可用；如果后续已有成功记录，可以忽略这条旧错误。"
        if "request_failed" in low or "请求超时" in low:
            return "该次请求没有在超时时间内完成，请看原始错误里的超时或连接原因。"
        if "fallback is disabled" in low:
            return "Gitee 回退没有开启，GPT Image 失败后不会自动改走 Gitee。"
        if "content_policy_violation" in low or "safety system" in low:
            return "上游安全策略拒绝了这次图片请求，请调整提示词或参考图后重试。"
        if "lookup" in low and (
            "server misbehaving" in low
            or "name or service not known" in low
            or "no such host" in low
        ):
            return "上游域名解析失败，请检查中转接口域名或 DNS。"
        if (
            "host is down" in low
            or "failed to establish a new connection" in low
            or "connection refused" in low
            or "cannot connect" in low
        ):
            return "生图服务连接不上，请检查接口地址、端口和服务是否在线。"
        if "timed out" in low or "timeout" in low:
            return "请求超时，请检查生图服务响应速度或网络。"
        if "unauthorized" in low or "401" in low:
            return "鉴权失败，请检查 API Key。"
        if "forbidden" in low or "403" in low:
            return "接口拒绝访问，请检查权限、额度或服务配置。"
        if "429" in low or "rate limit" in low:
            return "请求过于频繁或额度受限，请稍后重试或检查额度。"
        if "500" in low or "internal_server_error" in low:
            return "上游接口返回 500，请检查中转服务或上游生图服务状态。"
        if "invalid json" in low or "json parse" in low or "no json found" in low:
            return "文本模型返回内容格式不对，无法解析成需要的 JSON。"
        if "missing chat_url" in low or "chat_url/models" in low:
            return "文本模型配置缺失，请检查聊天接口地址和模型。"
        return "详见下方原始错误。"

    @staticmethod
    def _extract_log_field(message: str, key: str) -> str:
        if key == "activity":
            m = re.search(r'\bactivity=(.+)$', message or "")
        else:
            m = re.search(rf'\b{re.escape(key)}=([^,\s]+)', message or "")
        return m.group(1).strip() if m else ""

    @classmethod
    def _translate_access_log(cls, message: str) -> Optional[str]:
        m = LOG_ACCESS_RE.search(message or "")
        if not m:
            return None
        status = int(m.group("status"))
        if status < 500:
            return None
        method = m.group("method")
        path = m.group("path").split("?", 1)[0]
        return f"接口请求失败：{method} {path} 返回 {status}。"

    @classmethod
    def _translate_log_message(cls, message: str, level: str = "INFO", logger_name: str = "") -> str:
        text = (message or "").strip()

        m = re.search(r'开始定时生图:\s*theme=([^,\s]+),\s*schedule_time=([^\s,]+)\s*(.*)', text)
        if m:
            activity = (m.group(3) or "").strip()
            return f"开始定时生图：{m.group(2)}，动作：{activity or '未记录'}。"

        m = re.search(r'定时任务已设置:\s*日程\(([^)]+)\)\s*\+\s*动态生图\(根据日程时间\)', text)
        if m:
            return f"定时任务已设置：日程 {m.group(1)}，动态生图跟随日程时间。"

        m = re.search(r'画廊启动:\s*(.+)', text)
        if m:
            return f"画廊服务已启动：{m.group(1).strip()}。"

        m = re.search(r'手动重试生图任务:\s*([^\s]+).*?activity=(.*)', text)
        if m:
            return f"手动重试生图任务：{m.group(1)}，动作：{m.group(2).strip() or '未记录'}。"

        m = re.search(r'添加动态生图任务:\s*([^\s]+).*?period=([^\s]+).*?activity=(.*)', text)
        if m:
            return f"已添加动态生图任务：{m.group(1)}，时段：{m.group(2)}，动作：{m.group(3).strip() or '未记录'}。"

        m = re.search(r'动态生图任务已创建:\s*(\d+)\s*个.*existing_plans=(\d+).*max_daily=(\d+)', text)
        if m:
            return f"动态生图任务已创建：{m.group(1)} 个，已有计划：{m.group(2)}，今日上限：{m.group(3)}。"

        m = re.search(r'跳过已过期的时间(?:（([^）]+)）)?:\s*([^\s]+)(?:\s+activity=(.*))?', text)
        if m:
            reason = m.group(1) or "已过期"
            activity = (m.group(3) or "").strip()
            suffix = f"，动作：{activity}" if activity else ""
            return f"跳过已过期时段：{m.group(2)}，原因：{reason}{suffix}。"

        m = re.search(r'跳过生图任务（([^）]+)）:\s*(.+)', text)
        if m:
            return f"跳过生图任务：{m.group(2).strip()}，原因：{m.group(1)}。"

        m = re.search(r'复用已占用的生图额度:\s*([^\s]+)', text)
        if m:
            return f"复用已占用的生图额度：{m.group(1)}。"

        m = re.search(r'定时生图使用当天 LLM 选择的底模:\s*(.+)', text)
        if m:
            return f"定时生图使用当天模型选择的底模：{m.group(1).strip()}。"

        m = re.search(r'定时生图成功:\s*(.+)', text)
        if m:
            return f"定时生图成功：{m.group(1).strip()}。"

        m = re.search(r'定时生图完成:\s*theme=([^\s,]+)', text)
        if m:
            return "定时生图流程完成。"

        m = re.search(r'定时生图失败:\s*(.*)', text)
        if m:
            return f"定时生图失败：{cls._diagnose_error_text(m.group(1))}"

        m = re.search(r'定时生图超时:\s*theme=([^\s,]+)\s*\((\d+)s\)', text)
        if m:
            return f"定时生图超时：等待 {m.group(2)} 秒后仍未完成。"

        m = re.search(r'补拍任务失败:\s*(.+)', text)
        if m:
            return f"补拍任务失败：{m.group(1).strip()}。"

        m = re.search(r'补拍任务完成:\s*(.+)', text)
        if m:
            return f"补拍任务完成：{m.group(1).strip()}。"

        m = re.search(r'LLM model updated to:\s*(.+)', text)
        if m:
            return f"文本模型已更新为：{m.group(1).strip()}。"

        m = re.search(r'正在生成\s+(.+?)\s+的日程', text)
        if m:
            return f"正在生成 {m.group(1)} 的日程。"

        m = re.search(r'日程生成成功:\s*([^|]+)(?:\|\s*reference_query=(.*?))?(?:\|\s*outfit_kw=(.*?))?(?:\|\s*scene_kw=(.*))?$', text)
        if m:
            parts = [f"风格：{m.group(1).strip()}"]
            if m.group(3):
                parts.append(f"穿搭关键词：{m.group(3).strip()}")
            if m.group(4):
                parts.append(f"场景关键词：{m.group(4).strip()}")
            return "日程生成成功：" + "，".join(parts) + "。"

        if text.startswith("日程生成失败"):
            return "日程生成失败：重试后仍未拿到合格结果。"
        if text.startswith("日程生成使用兜底结果，保留现有今日日程"):
            return "文本模型暂时没有返回可用日程，已保留现有今日日程。"

        m = re.search(r'LLM 返回为空 \(attempt (\d+)\)', text)
        if m:
            return f"文本模型返回为空：第 {m.group(1)} 次。"

        m = re.search(r'解析失败 \(attempt (\d+)\)', text)
        if m:
            return f"日程解析失败：第 {m.group(1)} 次。"

        m = re.search(r'(日程.*?不合格|日程字段不完整.*?|outfit 展示字段不完整.*?|schedule_details 不合格).*?\(attempt (\d+)\)[:：]?\s*(.*)', text)
        if m:
            detail = m.group(3).strip()
            suffix = f"原因：{detail}。" if detail else ""
            return f"{m.group(1)}：第 {m.group(2)} 次。{suffix}"

        if text.startswith("No JSON found in LLM response"):
            return "文本模型返回内容里没有可用 JSON。"
        if text.startswith("JSON parse error"):
            return "文本模型 JSON 解析失败。"
        if text.startswith("LLM config missing"):
            return "文本模型配置缺失：聊天接口地址或模型未填写。"
        m = re.search(r'LLM call failed:\s*model=([^,\s]+),\s*status=([^,\s]+),\s*detail=(.*)', text)
        if m:
            model = m.group(1).strip()
            detail = m.group(3).strip()
            return f"文本模型请求失败（模型：{model}）：{cls._diagnose_error_text(detail)}"
        if text.startswith("LLM call error"):
            return "文本模型调用失败：" + cls._diagnose_error_text(text)
        if text.startswith("LLM call returned invalid response"):
            m = re.search(r'model=([^,\s]+)', text)
            model = f"（模型：{m.group(1).strip()}）" if m else ""
            return f"文本模型返回格式不完整{model}，没有可用内容。"
        if text.startswith("LLM call returned empty content"):
            return "文本模型返回空内容。"

        m = re.search(r'开始生图:\s*theme=([^,\s]+),\s*engine=([^,\s]+),\s*model=([^,\s]+),\s*style=([^,\s]+),\s*size=([^,\s]+)', text)
        if m:
            return f"开始生图：引擎 {m.group(2)}，模型 {m.group(3)}，风格 {m.group(4)}，尺寸 {m.group(5)}。"

        m = re.search(r'生图成功:\s*(.+)', text)
        if m:
            return f"生图成功：{m.group(1).strip()}。"

        if text.startswith("生图失败"):
            return "生图失败：" + cls._diagnose_error_text(text)
        if text.startswith("生图超时"):
            return "生图超时，请检查生图服务响应速度。"
        if text.startswith("生图异常"):
            return "生图异常：" + cls._diagnose_error_text(text)
        if text.startswith("图片生成成功"):
            return text + "。"
        if text.startswith("图片生成失败"):
            return "图片生成失败，请查看下方原始错误。"
        if text.startswith("自定义生图成功"):
            return text + "。"
        if text.startswith("自定义生图失败") or text.startswith("Custom generate error"):
            return "自定义生图失败：" + cls._diagnose_error_text(text)
        if text.startswith("图片重抽成功"):
            return text + "。"
        if text.startswith("图片重抽失败") or text.startswith("Reroll image error"):
            return "图片重抽失败：" + cls._diagnose_error_text(text)

        m = re.search(r'Generate now:\s*time=([^,\s]+),\s*using today\'s schedule chain', text)
        if m:
            return f"现在在干嘛生图：时间 {m.group(1)}，已走今日日程链路。"
        if text.startswith("Generate-now LLM inference skipped"):
            return "现在在干嘛没有调用文本模型：聊天接口地址或模型未填写。"
        if text.startswith("Generate-now LLM inference invalid response"):
            return "现在在干嘛的文本模型返回无效。"
        if text.startswith("Generate-now LLM inference error"):
            return "现在在干嘛的文本模型调用失败：" + cls._diagnose_error_text(text)
        if text.startswith("generate.py failed"):
            return "生成脚本执行失败：" + cls._diagnose_error_text(text)
        if text.startswith("Generate now timeout"):
            return "现在在干嘛生图超时。"
        if text.startswith("Generate now error"):
            return "现在在干嘛生图异常：" + cls._diagnose_error_text(text)

        if text.startswith("Agnes Images API failed; not retrying chat-compatible GPT Image endpoint"):
            return "Agnes 图片接口失败，已停止，不再改走 GPT 聊天兼容端点。"
        if text.startswith("Agnes Images API unsupported; not retrying chat-compatible GPT Image endpoint"):
            return "当前中转不支持 Agnes 图片接口，已停止，不再改走 GPT 聊天兼容端点。"
        if text.startswith("Images API failed; retrying chat-compatible GPT Image endpoint"):
            return "图片接口失败，正在改用聊天兼容 GPT Image 端点重试。"
        if (
            text.startswith("Images API error")
            or text.startswith("Images API failed")
            or text.startswith("Agnes Images API")
            or text.startswith("GPT Image Images API")
        ):
            m = re.search(r'\[([^\]]+)\].*?\(attempt ([^)]+)\)', text)
            where = f"：{m.group(1)}" if m else ""
            attempt = f"，第 {m.group(2)} 次" if m else ""
            return f"图片生成接口调用失败{where}{attempt}。" + cls._diagnose_error_text(text)
        if text.startswith("Direct GPT API error") or text.startswith("Direct GPT API failed"):
            m = re.search(r'\[([^\]]+)\].*?\(attempt ([^)]+)\)', text)
            where = f"：{m.group(1)}" if m else ""
            attempt = f"，第 {m.group(2)} 次" if m else ""
            return f"聊天兼容 GPT Image 端点调用失败{where}{attempt}。" + cls._diagnose_error_text(text)
        if text.startswith("Agnes img2img failed"):
            return "Agnes 图生图失败，正在改用 Agnes 文生图重试。"
        if text.startswith("GPT Image img2img failed"):
            return "图生图失败，正在改用文生图重试。"
        if text.startswith("GPT Image failed; Gitee fallback is disabled"):
            return "GPT Image 失败，Gitee 回退没有开启。"
        if text.startswith("ERROR: GPT Image endpoint failed"):
            endpoint = text.split(":", 2)[-1].strip()
            return f"GPT Image 端点最终失败：{endpoint}。"
        if text.startswith("ERROR: Agnes endpoint failed"):
            endpoint = text.split(":", 2)[-1].strip()
            return f"Agnes 端点最终失败：{endpoint}。"
        if text.startswith("ERROR: generation failed"):
            return "图片生成最终失败。"

        if text.startswith("Caption:") or text.startswith("CAPTION:"):
            return "生成文案：" + text.split(":", 1)[1].strip()
        if text.startswith("SUCCESS:"):
            return "生成成功：" + text.split(":", 1)[1].strip()

        if text.startswith("Favorite outfit wardrobe image error") or text.startswith("Save favorite outfit wardrobe image error"):
            return "衣柜展示图生成或保存失败：" + cls._diagnose_error_text(text)
        if text.startswith("Hermes text-to-image error"):
            return "Hermes 文生图失败：" + cls._diagnose_error_text(text)
        if text.startswith("Hermes image-to-image error"):
            return "Hermes 图生图失败：" + cls._diagnose_error_text(text)
        if text.startswith("Hermes image validation failed"):
            return "Hermes 图片校验失败：" + cls._diagnose_error_text(text)
        if text.startswith("Hermes image style classification failed"):
            return "Hermes 图片风格识别失败：" + cls._diagnose_error_text(text)
        if text.startswith("Save Hermes image metadata error"):
            return "保存 Hermes 图片元数据失败：" + cls._diagnose_error_text(text)

        if level in {"ERROR", "CRITICAL"}:
            return "发生错误：" + cls._diagnose_error_text(text)
        if level == "WARNING" and not re.search(r'[\u4e00-\u9fff]', text):
            return "有一条警告，需要查看相关配置或下方原始错误。"

        cleaned = text
        cleaned = re.sub(r'\(attempt (\d+)\)', r'（第 \1 次）', cleaned)
        cleaned = cleaned.replace("theme=", "主题=")
        cleaned = cleaned.replace("engine=", "引擎=")
        cleaned = cleaned.replace("model=", "模型=")
        cleaned = cleaned.replace("style=", "风格=")
        cleaned = cleaned.replace("size=", "尺寸=")
        cleaned = cleaned.replace("activity=", "动作=")
        cleaned = cleaned.replace("schedule_time=", "日程时间=")
        return cleaned

    @classmethod
    def _is_useful_log_message(cls, level: str, logger_name: str, message: str, raw_line: str) -> bool:
        if logger_name == "aiohttp.access":
            return cls._translate_access_log(message) is not None
        if level in {"WARNING", "ERROR", "CRITICAL"}:
            return True
        haystack = f"{logger_name} {message} {raw_line}"
        return any(keyword in haystack for keyword in LOG_USEFUL_KEYWORDS)

    @classmethod
    def _format_diagnostic_logs(cls, text: str, max_items: int = 120, max_raw_errors: int = 3) -> dict:
        diagnostic_items = []
        raw_error_blocks = []
        current_raw_error_block = None
        in_error_block = False
        resolved_after_index = 0
        total_count = 0

        def add_diagnostic(severity: int, key: str, line: str):
            diagnostic_items.append((total_count, severity, key, line))

        def start_raw_error_block(line: str):
            nonlocal current_raw_error_block
            current_raw_error_block = {"index": total_count, "lines": [line]}
            raw_error_blocks.append(current_raw_error_block)

        def append_raw_error_line(line: str):
            if current_raw_error_block is None:
                start_raw_error_block(line)
            else:
                current_raw_error_block["lines"].append(line)

        def marks_resolved(level: str, logger_name: str, message: str) -> bool:
            if level != "INFO":
                return False
            if message.startswith("持久化日志已启用") or message.startswith("画廊启动"):
                return True
            if message.startswith("日程生成成功"):
                return True
            if logger_name == "portrait_gallery" and message.startswith("日程已保存"):
                return True
            return False

        for raw in (text or "").splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            total_count += 1
            match = LOG_ENTRY_RE.match(line)
            if match:
                current_raw_error_block = None
                ts = match.group("ts")
                level = match.group("level")
                logger_name = match.group("logger")
                message = match.group("message")
                in_error_block = level in {"ERROR", "CRITICAL"}
                if marks_resolved(level, logger_name, message):
                    resolved_after_index = total_count
                    raw_error_blocks = []
                    current_raw_error_block = None
                    in_error_block = False
                if logger_name == "aiohttp.access":
                    translated = cls._translate_access_log(message)
                    if not translated:
                        in_error_block = False
                        continue
                elif cls._is_useful_log_message(level, logger_name, message, line):
                    translated = cls._translate_log_message(message, level, logger_name)
                else:
                    continue
                level_label = LOG_LEVEL_LABELS.get(level, level)
                severity = 2 if level in {"ERROR", "CRITICAL"} else 1 if level == "WARNING" else 0
                add_diagnostic(
                    severity,
                    f"{level_label}:{translated}",
                    f"- {cls._format_log_time(ts)} {level_label}：{translated}",
                )
                if level in {"ERROR", "CRITICAL"}:
                    start_raw_error_block(line)
                continue

            if in_error_block:
                append_raw_error_line(line)
                if cls._is_error_detail_line(line):
                    translated = cls._translate_log_message(line, "ERROR", "")
                    add_diagnostic(2, f"错误:{translated}", f"- 错误：{translated}")
                continue

            if cls._is_error_detail_line(line):
                start_raw_error_block(line)
                translated = cls._translate_log_message(line, "ERROR", "")
                add_diagnostic(2, f"错误:{translated}", f"- 错误：{translated}")

        deduped = {}
        for item in diagnostic_items:
            deduped[item[2]] = item
        diagnostic_items = sorted(deduped.values(), key=lambda item: item[0])
        priority_items = [
            item
            for item in diagnostic_items
            if item[1] > 0 and (not resolved_after_index or item[0] > resolved_after_index)
        ]
        info_items = [item for item in diagnostic_items if item[1] == 0]
        selected = priority_items[-max_items:]
        remaining = max(0, max_items - len(selected))
        if remaining:
            selected.extend(info_items[-remaining:])
        selected.sort(key=lambda item: item[0])
        diagnostics = [item[3] for item in selected]
        selected_raw_error_blocks = [
            block
            for block in raw_error_blocks
            if not resolved_after_index or block["index"] > resolved_after_index
        ][-max_raw_errors:]
        raw_error_lines = []
        for idx, block in enumerate(selected_raw_error_blocks):
            if idx:
                raw_error_lines.append("")
            raw_error_lines.extend(block["lines"])
        output = [
            "运行诊断",
            f"已隐藏普通访问日志，只保留最近 {len(diagnostics)} 条有用信息。",
        ]
        if diagnostics:
            output.extend(diagnostics)
        else:
            output.append("最近没有生图、日程或错误相关日志。普通接口访问日志已隐藏。")
        if raw_error_lines:
            output.extend(["", f"原始错误（最新 {len(selected_raw_error_blocks)} 条）"])
            output.extend(raw_error_lines)
        return {
            "text": "\n".join(output),
            "filtered_count": len(diagnostics),
            "total_count": total_count,
            "raw_error_count": len(selected_raw_error_blocks),
        }

    async def handle_logs(self, request: web.Request):
        """Return a tail of the live gallery service log for the UI log viewer."""
        api_key = os.environ.get("GALLERY_API_KEY", "")
        if not api_key and not self._is_local_request(request):
            return web.json_response(
                {
                    "error": "local_only",
                    "message": "实时日志只允许本机查看；远程查看请配置 GALLERY_API_KEY。",
                },
                status=403,
            )
        try:
            lines = int(request.query.get("lines", "300"))
        except ValueError:
            lines = 300
        lines = max(50, min(1200, lines))
        candidates = self._log_file_candidates()
        if not candidates:
            return web.json_response({
                "status": "missing",
                "text": "",
                "path": "",
                "lines": lines,
                "message": "未找到日志文件",
            })
        path = candidates[0]
        try:
            mode = str(request.query.get("mode") or "").strip().lower()
            raw_mode = mode == "raw" or request.query.get("raw") == "1"
            read_lines = lines if raw_mode else min(5000, lines * 4)
            text = self._redact_log_text(self._tail_log_file(path, read_lines))
            stat = os.stat(path)
            payload = {
                "status": "ok",
                "path": path,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "updated_at": int(time.time()),
                "lines": lines,
                "mode": "raw" if raw_mode else "diagnostic",
            }
            if raw_mode:
                payload["text"] = text
                payload["total_count"] = len(text.splitlines())
                payload["filtered_count"] = payload["total_count"]
                payload["raw_error_count"] = 0
            else:
                diagnostic = self._format_diagnostic_logs(text, max_items=min(lines, 120))
                payload.update(diagnostic)
            return web.json_response(payload)
        except Exception as e:
            logger.error("Read live logs error: %s", e)
            return web.json_response({"error": "read_failed", "detail": str(e)}, status=500)

    def _favorite_outfits_path(self) -> str:
        return os.path.join(self.data_dir, "favorite_outfits.json")

    def _favorite_outfits_lock_path(self) -> str:
        return os.path.join(self.data_dir, "favorite_outfits.lock")

    def _disliked_outfits_path(self) -> str:
        return os.path.join(self.data_dir, "disliked_outfits.json")

    def _disliked_outfits_lock_path(self) -> str:
        return os.path.join(self.data_dir, "disliked_outfits.lock")

    @staticmethod
    def _favorite_outfit_payload(outfit: dict) -> dict:
        if not isinstance(outfit, dict):
            return {}
        result = {}
        for key in ("风格", "发型", "穿搭"):
            value = str(outfit.get(key) or "").strip()
            if value:
                result[key] = value
        return result

    @classmethod
    def _favorite_outfit_id(cls, date_text: str, outfit_style: str, outfit: dict) -> str:
        payload = {
            "date": str(date_text or ""),
            "outfit_style": str(outfit_style or ""),
            "outfit": cls._favorite_outfit_payload(outfit),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _favorite_outfit_item_id(cls, item: dict) -> str:
        if not isinstance(item, dict):
            return ""
        outfit = item.get("outfit") if isinstance(item.get("outfit"), dict) else {}
        outfit_style = str(item.get("outfit_style") or outfit.get("风格") or "").strip()
        return cls._favorite_outfit_id(str(item.get("date") or ""), outfit_style, outfit)

    @classmethod
    def _favorite_outfit_response_item(cls, item: dict) -> dict:
        cleaned = dict(item)
        cleaned["outfit"] = cls._favorite_outfit_payload(cleaned.get("outfit"))
        cleaned.pop("prompt", None)
        cleaned.pop("scene_keywords", None)
        return cleaned

    @staticmethod
    def _favorite_outfit_wardrobe_payload(item: dict) -> dict:
        if not isinstance(item, dict):
            return {}
        payload = item.get("wardrobe_image")
        return dict(payload) if isinstance(payload, dict) else {}

    @classmethod
    def _favorite_outfit_wardrobe_response_item(cls, item: dict) -> dict:
        wardrobe = cls._favorite_outfit_wardrobe_payload(item)
        if not wardrobe:
            return {}
        result = {}
        for key in (
            "filename",
            "url",
            "prompt",
            "size",
            "source",
            "model_name",
            "generation_mode",
            "created_at",
            "file_size_bytes",
            "width",
            "height",
        ):
            value = wardrobe.get(key)
            if value not in ("", None):
                result[key] = value
        return result

    @staticmethod
    def _reference_basename(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        text = unquote(text.split("?", 1)[0].split("#", 1)[0]).replace("\\", "/")
        return os.path.basename(text)

    def _wardrobe_reference_for_value(self, value: str) -> dict:
        ref_name = self._reference_basename(value)
        if not ref_name:
            return {}
        ref_text = str(value or "").replace("\\", "/")
        if not ref_name.startswith("wardrobe_") and "/wardrobe/" not in ref_text:
            return {}

        for item in self._load_favorite_outfits():
            wardrobe = self._favorite_outfit_wardrobe_response_item(item)
            filename = str(wardrobe.get("filename") or "").strip()
            url = str(wardrobe.get("url") or "").strip()
            candidate_names = {
                self._reference_basename(filename),
                self._reference_basename(url),
            }
            if ref_name not in candidate_names:
                continue

            outfit = item.get("outfit") if isinstance(item.get("outfit"), dict) else {}
            style = str(item.get("outfit_style") or outfit.get("风格") or "").strip()
            label = f"衣柜 · {style}" if style else "衣柜"
            result = {
                "id": f"wardrobe_{hashlib.sha1((filename or ref_name).encode('utf-8')).hexdigest()[:12]}",
                "filename": filename or ref_name,
                "url": url,
                "label": label,
                "style": style or "wardrobe",
                "source": "wardrobe",
            }
            prompt = str(wardrobe.get("prompt") or "").strip()
            if prompt:
                result["prompt"] = prompt
            return result

        return {
            "filename": ref_name,
            "label": "衣柜",
            "style": "wardrobe",
            "source": "wardrobe",
        }

    def _ensure_entry_reference_label(self, entry: dict) -> dict:
        if not isinstance(entry, dict):
            return entry

        selected = entry.get("selected_reference") if isinstance(entry.get("selected_reference"), dict) else {}
        if str(selected.get("label") or "").strip():
            return entry

        for field in ("requested_ref_image_path", "ref_image_path", "requested_ref_image", "ref_image"):
            resolved = self._wardrobe_reference_for_value(entry.get(field, ""))
            if not resolved:
                continue
            merged = dict(selected)
            for key, value in resolved.items():
                if value not in ("", None) and not merged.get(key):
                    merged[key] = value
            merged["label"] = resolved.get("label") or merged.get("label", "")
            entry["selected_reference"] = merged
            return entry

        return entry

    @staticmethod
    def _favorite_outfit_wardrobe_status_response_item(item: dict) -> dict:
        if not isinstance(item, dict):
            return {}
        status = item.get("wardrobe_image_status")
        if not isinstance(status, dict):
            return {}
        result = {}
        for key in ("status", "message", "error", "started_at", "updated_at"):
            value = status.get(key)
            if value not in ("", None):
                result[key] = value
        return result

    @classmethod
    def _favorite_outfit_prompt_lines(cls, items: list[dict], limit: int = 5) -> list[str]:
        lines = []
        for item in sorted(
            [x for x in items if isinstance(x, dict)],
            key=lambda x: x.get("created_at", 0),
            reverse=True,
        )[:limit]:
            outfit = cls._favorite_outfit_payload(item.get("outfit"))
            if not outfit:
                continue
            parts = []
            for key in ("风格", "发型", "穿搭"):
                value = str(outfit.get(key) or "").strip()
                if value:
                    parts.append(f"{key}：{value[:140]}")
            if not parts:
                continue
            style = str(item.get("outfit_style") or outfit.get("风格") or "").strip()
            date_text = str(item.get("date") or "").strip()
            meta = f"[{date_text}]"
            if style:
                meta += f" 风格：{style}"
            lines.append(meta + "；" + "；".join(parts))
        return lines

    def _favorite_outfit_generation_context(self, limit: int = 5) -> str:
        lines = self._favorite_outfit_prompt_lines(self._load_favorite_outfits(), limit=limit)
        return "\n".join(lines)

    def _load_favorite_outfits(self) -> list[dict]:
        path = self._favorite_outfits_path()
        if not os.path.exists(path):
            return []
        try:
            with open(self._favorite_outfits_lock_path(), "w") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            if isinstance(data, dict):
                data = data.get("items", [])
            return [item for item in data if isinstance(item, dict)]
        except Exception as e:
            logger.error("Load favorite outfits error: %s", e)
            return []

    def _favorite_outfit_by_id(self, outfit_id: str) -> Optional[dict]:
        if not outfit_id:
            return None
        for item in self._load_favorite_outfits():
            for candidate in (item.get("id"), self._favorite_outfit_item_id(item)):
                if candidate and candidate == outfit_id:
                    return item
        return None

    def _update_favorite_outfits(self, callback) -> list[dict]:
        path = self._favorite_outfits_path()
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self._favorite_outfits_lock_path(), "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                items = []
                if os.path.exists(path):
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        if isinstance(data, dict):
                            data = data.get("items", [])
                        if isinstance(data, list):
                            items = [item for item in data if isinstance(item, dict)]
                    except Exception:
                        items = []
                items = callback(items) or []
                tmp_path = f"{path}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump({"items": items}, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, path)
                return items
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load_disliked_outfits(self) -> list[dict]:
        path = self._disliked_outfits_path()
        if not os.path.exists(path):
            return []
        try:
            with open(self._disliked_outfits_lock_path(), "w") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            if isinstance(data, dict):
                data = data.get("items", [])
            return [item for item in data if isinstance(item, dict)]
        except Exception as e:
            logger.error("Load disliked outfits error: %s", e)
            return []

    def _update_disliked_outfits(self, callback) -> list[dict]:
        path = self._disliked_outfits_path()
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self._disliked_outfits_lock_path(), "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                items = []
                if os.path.exists(path):
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        if isinstance(data, dict):
                            data = data.get("items", [])
                        if isinstance(data, list):
                            items = [item for item in data if isinstance(item, dict)]
                    except Exception:
                        items = []
                items = callback(items) or []
                tmp_path = f"{path}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump({"items": items}, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, path)
                return items
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    async def handle_favorite_outfits(self, request: web.Request):
        """收藏今日穿搭方案，供后续日程 LLM 参考。"""
        if request.method == "GET":
            items = sorted(
                [self._favorite_outfit_response_item(item) for item in self._load_favorite_outfits()],
                key=lambda item: item.get("created_at", 0),
                reverse=True,
            )
            return web.json_response({
                "items": [
                    {
                        **item,
                        "wardrobe_image": self._favorite_outfit_wardrobe_response_item(item),
                        "wardrobe_image_status": self._favorite_outfit_wardrobe_status_response_item(item),
                    }
                    for item in items
                ],
                "count": len(items),
                "generation_reference": bool(items),
                "reference_scope": "hair_outfit_style_only",
            })

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid_json"}, status=400)

        outfit = self._favorite_outfit_payload(body.get("outfit"))
        if not isinstance(outfit, dict) or not outfit:
            return web.json_response({"error": "outfit_required"}, status=400)

        date_text = str(body.get("date") or date.today().isoformat()).strip()
        outfit_style = str(body.get("outfit_style") or outfit.get("风格") or "").strip()
        outfit_id = self._favorite_outfit_id(date_text, outfit_style, outfit)

        existing_items = self._load_favorite_outfits()
        existing_ids = {
            favorite_id
            for item in existing_items
            for favorite_id in (item.get("id"), self._favorite_outfit_item_id(item))
            if favorite_id
        }
        desired_state = body.get("favorite")
        should_favorite = (not (outfit_id in existing_ids)) if not isinstance(desired_state, bool) else desired_state

        item = {
            "id": outfit_id,
            "date": date_text,
            "outfit_style": outfit_style,
            "base_style": str(body.get("base_style") or "").strip(),
            "outfit": outfit,
            "outfit_keywords": str(body.get("outfit_keywords") or "").strip(),
            "created_at": int(time.time()),
        }

        previous_item = self._favorite_outfit_by_id(outfit_id)
        if previous_item:
            wardrobe_image = self._favorite_outfit_wardrobe_payload(previous_item)
            if wardrobe_image:
                item["wardrobe_image"] = wardrobe_image
            wardrobe_status = self._favorite_outfit_wardrobe_status_response_item(previous_item)
            if wardrobe_status and not wardrobe_image:
                item["wardrobe_image_status"] = wardrobe_status

        def _apply(items: list[dict]) -> list[dict]:
            next_items = [
                x for x in items
                if x.get("id") != outfit_id and self._favorite_outfit_item_id(x) != outfit_id
            ]
            if should_favorite:
                next_items.insert(0, item)
            next_items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
            return next_items[:50]

        try:
            items = self._update_favorite_outfits(_apply)
        except Exception as e:
            logger.error("Favorite outfit update error: %s", e)
            return web.json_response({"error": "save_failed", "detail": str(e)}, status=500)

        disliked_removed = False
        if should_favorite:
            def _remove_disliked(items: list[dict]) -> list[dict]:
                nonlocal disliked_removed
                next_items = [
                    x for x in items
                    if x.get("id") != outfit_id and self._favorite_outfit_item_id(x) != outfit_id
                ]
                disliked_removed = len(next_items) != len(items)
                return next_items

            try:
                self._update_disliked_outfits(_remove_disliked)
            except Exception as e:
                logger.error("Remove disliked outfit after favorite error: %s", e)

        auto_generation = "skipped"
        if should_favorite:
            current_item = next(
                (
                    x for x in items
                    if isinstance(x, dict)
                    and outfit_id in {x.get("id"), self._favorite_outfit_item_id(x)}
                ),
                {},
            )
            if self._favorite_outfit_wardrobe_payload(current_item):
                auto_generation = "exists"
            elif self._start_favorite_outfit_wardrobe_task(outfit_id):
                auto_generation = "queued"
            else:
                auto_generation = "running"

        return web.json_response({
            "success": True,
            "favorite": should_favorite,
            "id": outfit_id,
            "count": len(items),
            "disliked_removed": disliked_removed,
            "wardrobe_auto_generation": auto_generation,
        })

    async def handle_edit_favorite_outfit(self, request: web.Request):
        """编辑衣柜里的收藏穿搭，并重新生成对应衣架图。"""
        outfit_id = str(request.match_info.get("outfit_id") or "").strip()
        if not outfit_id:
            return web.json_response({"error": "favorite_outfit_not_found"}, status=404)

        item = self._favorite_outfit_by_id(outfit_id)
        if not item:
            return web.json_response({"error": "favorite_outfit_not_found"}, status=404)

        lock = self._wardrobe_image_locks.get(outfit_id)
        if lock and lock.locked():
            return web.json_response(
                {
                    "error": "wardrobe_image_generating",
                    "message": "这套衣架图正在生成中，请稍后再编辑。",
                },
                status=409,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid_json"}, status=400)

        raw_outfit = body.get("outfit") if isinstance(body.get("outfit"), dict) else {}
        current_outfit = self._favorite_outfit_payload(item.get("outfit"))

        def _pick_text(current: str, *sources: tuple[dict, str]) -> str:
            for source, key in sources:
                if isinstance(source, dict) and key in source:
                    return str(source.get(key) or "").strip()
            return str(current or "").strip()

        current_style = str(item.get("outfit_style") or current_outfit.get("风格") or "").strip()
        style = _pick_text(
            current_style,
            (body, "outfit_style"),
            (body, "style"),
            (raw_outfit, "风格"),
            (raw_outfit, "outfit_style"),
            (raw_outfit, "style"),
        )
        hair = _pick_text(
            current_outfit.get("发型", ""),
            (body, "hair"),
            (body, "hairstyle"),
            (body, "发型"),
            (raw_outfit, "发型"),
            (raw_outfit, "hair"),
            (raw_outfit, "hairstyle"),
        )
        wear = _pick_text(
            current_outfit.get("穿搭", ""),
            (body, "wear"),
            (body, "outfit_text"),
            (body, "clothing"),
            (body, "穿搭"),
            (raw_outfit, "穿搭"),
            (raw_outfit, "wear"),
            (raw_outfit, "outfit_text"),
            (raw_outfit, "clothing"),
        )

        edited_outfit = {}
        if style:
            edited_outfit["风格"] = style
        if hair:
            edited_outfit["发型"] = hair
        if wear:
            edited_outfit["穿搭"] = wear
        if not (hair or wear):
            return web.json_response({"error": "outfit_required", "message": "发型和穿搭不能同时为空"}, status=400)

        now_ts = int(time.time())
        previous_wardrobe = self._favorite_outfit_wardrobe_payload(item)
        previous_filename = str(previous_wardrobe.get("filename") or "").strip()
        previous_path = self._safe_reference_path(self.wardrobe_reference_dir, previous_filename) if previous_filename else ""

        def _apply(items: list[dict]) -> list[dict]:
            updated = []
            found = False
            for existing in items:
                if not isinstance(existing, dict):
                    continue
                candidate_ids = {existing.get("id"), self._favorite_outfit_item_id(existing)}
                if outfit_id in candidate_ids:
                    merged = dict(existing)
                    merged["id"] = str(existing.get("id") or outfit_id).strip() or outfit_id
                    merged["outfit_style"] = style
                    merged["outfit"] = edited_outfit
                    merged["updated_at"] = now_ts
                    merged.pop("wardrobe_image", None)
                    merged["wardrobe_image_status"] = {
                        "status": "queued",
                        "message": "穿搭已更新，衣架图等待重新生成",
                        "started_at": now_ts,
                        "updated_at": now_ts,
                    }
                    updated.append(merged)
                    found = True
                else:
                    updated.append(existing)
            if not found:
                raise ValueError("favorite_outfit_not_found")
            return updated

        try:
            items = self._update_favorite_outfits(_apply)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=404)
        except Exception as e:
            logger.error("Edit favorite outfit error: %s", e)
            return web.json_response({"error": "save_failed", "detail": str(e)}, status=500)

        if previous_path:
            try:
                os.unlink(previous_path)
            except OSError as e:
                logger.warning("Delete edited wardrobe reference failed: %s", e)

        auto_generation = "queued" if self._start_favorite_outfit_wardrobe_task(outfit_id) else "running"
        updated_item = next(
            (
                x for x in items
                if isinstance(x, dict)
                and outfit_id in {x.get("id"), self._favorite_outfit_item_id(x)}
            ),
            self._favorite_outfit_by_id(outfit_id) or {},
        )
        response_item = self._favorite_outfit_response_item(updated_item) if updated_item else {}
        return web.json_response({
            "success": True,
            "id": outfit_id,
            "item": {
                **response_item,
                "wardrobe_image": self._favorite_outfit_wardrobe_response_item(updated_item),
                "wardrobe_image_status": self._favorite_outfit_wardrobe_status_response_item(updated_item),
            },
            "wardrobe_auto_generation": auto_generation,
        })

    async def handle_disliked_outfits(self, request: web.Request):
        """记录用户不喜欢的今日穿搭方案，供后续日程 LLM 减少相似风格。"""
        if request.method == "GET":
            items = sorted(
                [self._favorite_outfit_response_item(item) for item in self._load_disliked_outfits()],
                key=lambda item: item.get("created_at", 0),
                reverse=True,
            )
            return web.json_response({
                "items": items,
                "count": len(items),
                "generation_reference": bool(items),
                "reference_scope": "negative_hair_outfit_style_only",
            })

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid_json"}, status=400)

        outfit = self._favorite_outfit_payload(body.get("outfit"))
        if not isinstance(outfit, dict) or not outfit:
            return web.json_response({"error": "outfit_required"}, status=400)

        date_text = str(body.get("date") or date.today().isoformat()).strip()
        outfit_style = str(body.get("outfit_style") or outfit.get("风格") or "").strip()
        outfit_id = self._favorite_outfit_id(date_text, outfit_style, outfit)

        existing_items = self._load_disliked_outfits()
        existing_ids = {
            disliked_id
            for item in existing_items
            for disliked_id in (item.get("id"), self._favorite_outfit_item_id(item))
            if disliked_id
        }
        desired_state = body.get("disliked")
        should_dislike = (not (outfit_id in existing_ids)) if not isinstance(desired_state, bool) else desired_state

        item = {
            "id": outfit_id,
            "date": date_text,
            "outfit_style": outfit_style,
            "base_style": str(body.get("base_style") or "").strip(),
            "outfit": outfit,
            "outfit_keywords": str(body.get("outfit_keywords") or "").strip(),
            "created_at": int(time.time()),
        }

        def _apply(items: list[dict]) -> list[dict]:
            next_items = [
                x for x in items
                if x.get("id") != outfit_id and self._favorite_outfit_item_id(x) != outfit_id
            ]
            if should_dislike:
                next_items.insert(0, item)
            next_items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
            return next_items[:50]

        try:
            items = self._update_disliked_outfits(_apply)
        except Exception as e:
            logger.error("Disliked outfit update error: %s", e)
            return web.json_response({"error": "save_failed", "detail": str(e)}, status=500)

        favorite_removed = False
        if should_dislike:
            def _remove_favorite(items: list[dict]) -> list[dict]:
                nonlocal favorite_removed
                next_items = [
                    x for x in items
                    if x.get("id") != outfit_id and self._favorite_outfit_item_id(x) != outfit_id
                ]
                favorite_removed = len(next_items) != len(items)
                return next_items

            try:
                self._update_favorite_outfits(_remove_favorite)
            except Exception as e:
                logger.error("Remove favorite outfit after dislike error: %s", e)

        return web.json_response({
            "success": True,
            "disliked": should_dislike,
            "id": outfit_id,
            "count": len(items),
            "favorite_removed": favorite_removed,
        })

    @staticmethod
    def _favorite_outfit_wardrobe_prompt(item: dict) -> str:
        outfit = item.get("outfit") if isinstance(item.get("outfit"), dict) else {}
        style = str(item.get("outfit_style") or outfit.get("风格") or "收藏穿搭").strip()
        hair = str(outfit.get("发型") or "").strip()
        wear = str(outfit.get("穿搭") or "").strip()
        wear = re.sub(r"\s+", " ", wear)
        hair = re.sub(r"\s+", " ", hair)
        prompt_parts = [
            "A clean wardrobe catalog photo of a single complete outfit displayed on a minimalist boutique clothing rack with a matching wig displayed beside it on a simple wig stand or hook, soft neutral studio background.",
            "Show the clothes, matching wig, accessories, and shoes only; no person, no face, no hands, no body parts, no lifestyle model.",
            "Use a front-facing full outfit display with the entire look fully visible from top to bottom, styled like a premium GPT-generated wardrobe hanger reference image.",
            "Keep the garment silhouette, fabric layering, colors, accessories, footwear, and wig hairstyle coherent and neatly arranged for later image-to-image outfit and hair reference.",
            "Do not turn this into a lifestyle scene, portrait, flat lay, selfie, or model shoot.",
        ]
        if style:
            prompt_parts.append(f"Style vibe: {style}.")
        if wear:
            prompt_parts.append(f"Outfit details: {wear}")
        if hair:
            prompt_parts.append(f"Matching wig hairstyle to display: {hair}")
        prompt_parts.append("High detail fashion catalog lighting, realistic textiles and hair fibers, sharp edges, centered composition, generous margins around the outfit and wig.")
        return " ".join(part.strip() for part in prompt_parts if part).strip()

    def _set_favorite_outfit_wardrobe_status(self, outfit_id: str, status: str, message: str = "", error: str = ""):
        outfit_id = str(outfit_id or "").strip()
        status = str(status or "").strip()
        if not outfit_id or not status:
            return
        payload = {
            "status": status,
            "message": str(message or "").strip(),
            "updated_at": int(time.time()),
        }
        if status in {"queued", "generating"}:
            payload["started_at"] = int(time.time())
        if error:
            payload["error"] = str(error).strip()[:500]

        def _apply(items: list[dict]) -> list[dict]:
            updated = []
            for existing in items:
                if not isinstance(existing, dict):
                    continue
                candidate_ids = {existing.get("id"), self._favorite_outfit_item_id(existing)}
                if outfit_id in candidate_ids:
                    merged = dict(existing)
                    merged["wardrobe_image_status"] = payload
                    updated.append(merged)
                else:
                    updated.append(existing)
            return updated

        try:
            self._update_favorite_outfits(_apply)
        except Exception as e:
            logger.error("Save wardrobe image status error: %s", e)

    def _start_favorite_outfit_wardrobe_task(self, outfit_id: str) -> bool:
        outfit_id = str(outfit_id or "").strip()
        if not outfit_id:
            return False
        lock = self._wardrobe_image_locks.get(outfit_id)
        if lock and lock.locked():
            return False
        self._set_favorite_outfit_wardrobe_status(outfit_id, "queued", "衣架图已加入生成队列")
        task = asyncio.create_task(self._generate_and_store_favorite_outfit_wardrobe_image(outfit_id))

        def _log_task_error(done_task: asyncio.Task):
            try:
                done_task.result()
            except Exception as e:
                logger.error("Favorite outfit auto wardrobe image task failed: %s", e)

        task.add_done_callback(_log_task_error)
        return True

    async def _generate_and_store_favorite_outfit_wardrobe_image(
        self,
        outfit_id: str,
        prompt: str = "",
        size: str = "",
        replace_existing: bool = True,
    ) -> dict:
        outfit_id = str(outfit_id or "").strip()
        if not outfit_id:
            raise ValueError("favorite_outfit_not_found")
        item = self._favorite_outfit_by_id(outfit_id)
        if not item:
            raise ValueError("favorite_outfit_not_found")
        prompt = str(prompt or "").strip() or self._favorite_outfit_wardrobe_prompt(item)
        size = size or normalize_custom_image_size("", "3:4", "1k")

        lock = self._wardrobe_image_locks.setdefault(outfit_id, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("wardrobe_image_generating")

        try:
            async with lock:
                item = self._favorite_outfit_by_id(outfit_id)
                if not item:
                    raise ValueError("favorite_outfit_not_found")
                if self._favorite_outfit_wardrobe_payload(item) and not replace_existing:
                    return self._favorite_outfit_wardrobe_payload(item)

                self._set_favorite_outfit_wardrobe_status(outfit_id, "generating", "衣架图生成中")
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: self._run_hermes_image_generation(
                        "gptimage",
                        prompt,
                        size=size,
                        output_dir=self.wardrobe_reference_dir,
                        url_prefix="/local-refs/wardrobe",
                        source="wardrobe",
                        category="wardrobe_reference",
                        persist_metadata=False,
                        filename_prefix=f"wardrobe_{outfit_id[:8]}",
                        classify_style=False,
                    ),
                )

                if not result or not result.get("filename") or not result.get("path"):
                    self._set_favorite_outfit_wardrobe_status(outfit_id, "failed", "衣架图生成失败", "generate_failed")
                    raise RuntimeError("generate_failed")

                wardrobe_payload = {
                    "filename": result.get("filename", ""),
                    "url": result.get("url", ""),
                    "prompt": prompt,
                    "size": size,
                    "source": result.get("source") or "wardrobe",
                    "model_name": result.get("model_name") or "",
                    "generation_mode": "text2img",
                    "created_at": int(time.time()),
                    "file_size_bytes": int(result.get("file_size_bytes") or 0),
                    "width": int(result.get("width") or 0),
                    "height": int(result.get("height") or 0),
                }

                previous_wardrobe = self._favorite_outfit_wardrobe_payload(item)
                previous_filename = str(previous_wardrobe.get("filename") or "").strip()
                previous_path = self._safe_reference_path(self.wardrobe_reference_dir, previous_filename) if previous_filename else ""
                new_path = str(result.get("path") or "").strip()
                if previous_path and previous_path != new_path:
                    try:
                        os.unlink(previous_path)
                    except OSError as e:
                        logger.warning("Delete previous wardrobe reference failed: %s", e)

                def _apply(items: list[dict]) -> list[dict]:
                    updated = []
                    found = False
                    for existing in items:
                        if not isinstance(existing, dict):
                            continue
                        candidate_ids = {existing.get("id"), self._favorite_outfit_item_id(existing)}
                        if outfit_id in candidate_ids:
                            merged = dict(existing)
                            merged["wardrobe_image"] = wardrobe_payload
                            merged.pop("wardrobe_image_status", None)
                            updated.append(merged)
                            found = True
                        else:
                            updated.append(existing)
                    if not found:
                        raise ValueError("favorite_outfit_not_found")
                    return updated

                try:
                    self._update_favorite_outfits(_apply)
                except ValueError:
                    try:
                        os.unlink(new_path)
                    except OSError:
                        pass
                    raise
                except Exception as e:
                    self._set_favorite_outfit_wardrobe_status(outfit_id, "failed", "衣架图保存失败", str(e))
                    logger.error("Save favorite outfit wardrobe image error: %s", e)
                    raise

                return wardrobe_payload
        finally:
            try:
                if not lock.locked():
                    self._wardrobe_image_locks.pop(outfit_id, None)
            except Exception:
                pass

    async def handle_favorite_outfit_wardrobe_image(self, request: web.Request):
        outfit_id = str(request.match_info.get("outfit_id") or "").strip()
        item = self._favorite_outfit_by_id(outfit_id)
        if not item:
            return web.json_response({"error": "favorite_outfit_not_found"}, status=404)

        body = {}
        try:
            if request.can_read_body:
                body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        prompt = str(body.get("prompt") or "").strip() or self._favorite_outfit_wardrobe_prompt(item)
        size = normalize_custom_image_size(
            body.get("size", ""),
            body.get("aspect", "") or "3:4",
            body.get("resolution", "") or "1k",
        )
        raw_image_model = str(body.get("model") or body.get("image_model") or "").strip()
        image_model = self._normalize_image_model_id(raw_image_model)
        if raw_image_model and not image_model and raw_image_model.lower() not in {"default", "auto", "current"}:
            return web.json_response({"error": "invalid_image_model"}, status=400)

        lock = self._wardrobe_image_locks.get(outfit_id)
        if lock and lock.locked():
            return web.json_response(
                {
                    "error": "wardrobe_image_generating",
                    "message": "这套衣架图正在生成中，请稍后刷新。",
                },
                status=409,
            )

        try:
            wardrobe_payload = await self._generate_and_store_favorite_outfit_wardrobe_image(
                outfit_id,
                prompt=prompt,
                size=size,
                replace_existing=True,
            )
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=404)
        except RuntimeError as e:
            if str(e) == "wardrobe_image_generating":
                return web.json_response(
                    {
                        "error": "wardrobe_image_generating",
                        "message": "这套衣架图正在生成中，请稍后刷新。",
                    },
                    status=409,
                )
            logger.error("Favorite outfit wardrobe image error: %s", e)
            return web.json_response({"error": str(e)}, status=500)
        except Exception as e:
            logger.error("Favorite outfit wardrobe image error: %s", e)
            return web.json_response({"error": str(e)}, status=500)

        return web.json_response({
            "success": True,
            "id": outfit_id,
            "wardrobe_image": wardrobe_payload,
        })

    def _plugin_config_path(self) -> str:
        return os.path.join(self.data_dir, "plugin_config.json")

    def _api_keys_config_path(self) -> str:
        return os.path.join(self.data_dir, "api_keys_config.json")

    def _load_api_keys_config(self) -> dict:
        path = self._api_keys_config_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception as e:
            logger.error(f"Load API keys config error: {e}")
            return {}

    @staticmethod
    def _store_local_url_override(keys_config: dict, key: str, raw_value, default_value: str):
        value = str(raw_value or "").strip()
        default_value = str(default_value or "").strip()
        had_local = bool(str(keys_config.get(key, "") or "").strip())
        if value:
            if value != default_value or had_local:
                keys_config[key] = value
        else:
            keys_config.pop(key, None)

    @staticmethod
    def _drop_redundant_local_url_override(keys_config: dict, key: str, default_value: str):
        value = str(keys_config.get(key, "") or "").strip()
        default_value = str(default_value or "").strip()
        if value and default_value and value == default_value:
            keys_config.pop(key, None)

    @staticmethod
    def _configured_image_base_url(url: str) -> str:
        base = str(url or "").strip().rstrip("/")
        for suffix in ("/chat/completions", "/images/generations", "/images/edits"):
            if base.endswith(suffix):
                return base[: -len(suffix)]
        return base

    @classmethod
    def _configured_image_generations_url(cls, url: str) -> str:
        base = cls._configured_image_base_url(url)
        return f"{base}/images/generations" if base else ""

    @classmethod
    def _gpt_image_endpoint_identity(cls, url: str) -> tuple[str, str]:
        raw = str(url or "").strip().rstrip("/")
        if not raw:
            return ("", "")
        if raw.endswith("/chat/completions"):
            return ("chat", cls._configured_image_base_url(raw))
        return ("images", cls._configured_image_base_url(raw))

    @classmethod
    def _drop_redundant_gpt_url_override(cls, keys_config: dict, default_value: str):
        value = str(keys_config.get("gpt_base_url", "") or "").strip()
        if value and cls._gpt_image_endpoint_identity(value) == cls._gpt_image_endpoint_identity(default_value):
            keys_config.pop("gpt_base_url", None)

    @classmethod
    def _store_gpt_url_override(cls, keys_config: dict, raw_value, default_value: str):
        value = str(raw_value or "").strip()
        if not value:
            keys_config.pop("gpt_base_url", None)
            return
        if cls._gpt_image_endpoint_identity(value) == cls._gpt_image_endpoint_identity(default_value):
            keys_config.pop("gpt_base_url", None)
            return
        keys_config["gpt_base_url"] = value

    @staticmethod
    def _normalize_image_model_id(value) -> str:
        model = str(value or "").strip()
        if model.lower() in {"default", "auto", "current"}:
            return ""
        if not model:
            return ""
        if len(model) > 120 or not re.match(r"^[A-Za-z0-9._:/+-]+$", model):
            return ""
        return model

    @staticmethod
    def _is_image_model_id(model: str) -> bool:
        lower = str(model or "").strip().lower()
        if not lower or "video" in lower:
            return False
        return "image" in lower or "imagine" in lower

    def _effective_gpt_image_model(self, keys_config: Optional[dict] = None) -> tuple[str, str]:
        keys_config = keys_config if isinstance(keys_config, dict) else self._load_api_keys_config()
        image_config = self.config.get("image_gen", {}) if isinstance(self.config.get("image_gen"), dict) else {}
        default_model = str(image_config.get("gpt_model", "") or "").strip()
        local_model = self._normalize_image_model_id(keys_config.get("gpt_model", ""))
        return local_model or default_model, default_model

    def _effective_gpt_image_base_url(self, keys_config: Optional[dict] = None) -> str:
        keys_config = keys_config if isinstance(keys_config, dict) else self._load_api_keys_config()
        image_config = self.config.get("image_gen", {}) if isinstance(self.config.get("image_gen"), dict) else {}
        raw_default = str(image_config.get("gpt_base_url", "") or "").strip()
        configured = self._configured_image_base_url(raw_default)
        local = str(keys_config.get("gpt_base_url", "") or "").strip()
        if (
            local == raw_default
            or self._gpt_image_endpoint_identity(local) == self._gpt_image_endpoint_identity(configured)
        ):
            local = ""
        return local or os.environ.get("GPT_IMAGE_BASE_URL", "") or configured

    @classmethod
    def _models_base_url(cls, url: str) -> str:
        base = cls._configured_image_base_url(url)
        if base and not base.endswith("/v1"):
            base = f"{base}/v1"
        return base

    def _resolve_image_dir(self) -> str:
        image_dir = resolve_image_dir(self.config, self.data_dir)
        if os.path.exists(image_dir) and not os.path.isdir(image_dir):
            logger.error(f"Configured image dir is not a directory: {image_dir}; using default")
            return self.default_image_dir
        return image_dir

    def _set_runtime_image_dir(self, image_dir: str):
        image_dir = image_dir or self.default_image_dir
        self.image_dir = os.path.abspath(os.path.expanduser(image_dir))
        os.makedirs(self.image_dir, exist_ok=True)
        if self.on_image_dir_changed:
            self.on_image_dir_changed(self.image_dir)

    def _image_search_dirs(self) -> list[str]:
        result = []
        for path in (self.image_dir, self.default_image_dir):
            clean = os.path.abspath(os.path.expanduser(path or ""))
            if clean and clean not in result:
                result.append(clean)
        return result

    @staticmethod
    def _safe_image_relative_path(filename: str) -> Optional[Path]:
        raw = unquote(filename or "").strip()
        if not raw or raw.startswith(("/", "\\")) or "\x00" in raw:
            return None
        rel = Path(raw)
        if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
            return None
        return rel

    def _image_file_path(self, filename: str) -> str:
        rel = self._safe_image_relative_path(filename)
        if rel is None:
            return ""
        for base in self._image_search_dirs():
            base_path = Path(base).resolve()
            candidate = (base_path / rel).resolve()
            try:
                candidate.relative_to(base_path)
            except ValueError:
                continue
            if candidate.exists() and candidate.is_file():
                return str(candidate)
        return ""

    def _image_exists(self, filename: str) -> bool:
        return bool(self._image_file_path(filename))

    def _image_stat(self, filename: str):
        path = self._image_file_path(filename)
        if not path:
            return None
        try:
            return os.stat(path)
        except OSError:
            return None

    def _image_file_info(self, filename: str) -> dict:
        path = self._image_file_path(filename)
        if not path:
            return {}
        try:
            stat = os.stat(path)
        except OSError:
            return {}

        cache_key = path
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = self._image_info_cache.get(cache_key)
        if cached and cached.get("signature") == signature:
            return dict(cached.get("info") or {})

        info = {"file_size_bytes": stat.st_size}
        try:
            with Image.open(path) as img:
                width, height = img.size
            info.update({
                "width": width,
                "height": height,
                "size": f"{width}x{height}",
            })
        except Exception as exc:
            logger.debug("Failed to probe image dimensions for %s: %s", filename, exc)

        self._image_info_cache[cache_key] = {"signature": signature, "info": dict(info)}
        return info

    def _delete_image_files(self, filename: str) -> tuple[int, list[str]]:
        rel = self._safe_image_relative_path(filename)
        if rel is None:
            return 0, ["invalid_filename"]
        deleted = 0
        errors = []
        for base in self._image_search_dirs():
            base_path = Path(base).resolve()
            candidate = (base_path / rel).resolve()
            try:
                candidate.relative_to(base_path)
            except ValueError:
                errors.append(f"unsafe_path:{base}")
                continue
            if not candidate.exists():
                continue
            try:
                if candidate.is_file():
                    candidate.unlink()
                    deleted += 1
            except OSError as e:
                errors.append(f"{candidate}: {e}")
        return deleted, errors

    async def handle_image_file(self, request: web.Request):
        filename = request.match_info.get("filename", "")
        path = self._image_file_path(filename)
        if not path:
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    def _github_proxy(self) -> str:
        """Return the effective GitHub-only proxy URL, if configured."""
        keys = self._load_api_keys_config()
        update_config = self.config.get("update", {}) if isinstance(self.config.get("update"), dict) else {}
        for value in (
            keys.get("github_proxy"),
            os.getenv("GITHUB_PROXY"),
            update_config.get("github_proxy"),
        ):
            proxy = str(value or "").strip()
            if proxy:
                return proxy
        return ""

    def _github_api_url(self) -> str:
        """Return the fixed update-check GitHub API URL."""
        update_config = self.config.get("update", {}) if isinstance(self.config.get("update"), dict) else {}
        for value in (
            os.getenv("GITHUB_RELEASE_API"),
            update_config.get("github_api"),
            GITHUB_RELEASE_API_URL,
        ):
            url = str(value or "").strip()
            if url:
                return url
        return ""

    def _github_proxy_env(self) -> dict[str, str]:
        proxy = self._github_proxy()
        if not proxy:
            return {}
        return {
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "http_proxy": proxy,
            "https_proxy": proxy,
        }

    def _load_plugin_config(self) -> dict:
        path = self._plugin_config_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception as e:
            logger.error(f"Load plugin config error: {e}")
            return {}

    def _has_image_generation_key(self) -> bool:
        keys = self._load_api_keys_config()
        image_config = self.config.get("image_gen", {}) if isinstance(self.config.get("image_gen"), dict) else {}
        if (
            keys.get("gpt_base_url")
            or os.getenv("GPT_IMAGE_BASE_URL")
            or image_config.get("gpt_base_url")
            or keys.get("gpt_key")
            or os.getenv("GPT_IMAGE_API_KEY")
        ):
            return True
        plugin_config = self._load_plugin_config()
        gitee_keys = plugin_config.get("gitee_config", {}).get("api_keys", [])
        return bool(gitee_keys and gitee_keys[0])

    def _python_executable(self) -> str:
        return configured_python(self.config) or sys.executable

    def _generate_script(self) -> str:
        return os.path.join(resolve_script_dir(self.config, self.config_path), "generate.py")

    def _child_env(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        merged = {"ZHUZHU_MEDIA_DIR": self.image_dir}
        if extra:
            merged.update(extra)
        return build_child_env(self.config, self.config_path, self.data_dir, merged)

    @staticmethod
    def _is_protected_update_path(path: str) -> bool:
        """Return True for local data/secrets that online update must never overwrite."""
        clean = str(path or "").strip().replace("\\", "/").lstrip("./")
        if not clean or clean.startswith("../") or "/../" in clean:
            return True
        if clean in UPDATE_PROTECTED_EXACT:
            return True
        return any(clean.startswith(prefix) for prefix in UPDATE_PROTECTED_PREFIXES)

    @staticmethod
    def _update_protection_summary() -> dict:
        return {
            "protected_exact": list(UPDATE_PROTECTED_EXACT),
            "protected_prefixes": list(UPDATE_PROTECTED_PREFIXES),
            "preserves": [
                "API Key / Base URL / appearance / persona_source",
                "config/config.yaml",
                "data/ runtime files",
                "gallery images",
                "uploaded reference images",
                "logs",
            ],
        }

    @staticmethod
    def _git_run(args: list[str], cwd: Path, env: dict[str, str], timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

    def _safe_update_ref(self, remote: str, branch: str) -> str:
        remote_ref = f"{remote}/{branch}"
        if not re.match(r"^[A-Za-z0-9._/-]+$", remote_ref):
            raise ValueError("更新源包含非法字符")
        return remote_ref

    def _safe_update_changed_files(self, project_root: Path, remote_ref: str, env: dict[str, str]) -> list[str]:
        result = self._git_run(["diff", "--name-only", "HEAD.." + remote_ref, "--"], project_root, env)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "无法读取远端改动列表")
        files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return [path for path in files if not self._is_protected_update_path(path)]

    @staticmethod
    def _body_bool(body: dict, key: str, default: bool = False) -> bool:
        if not isinstance(body, dict) or key not in body:
            return default
        value = body.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off", ""}:
            return False
        return default

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
                if entry.get("source", "") != "cron":
                    continue
                img_file = entry.get("image_filename", "")
                if not img_file or img_file in seen:
                    continue
                if self._image_exists(img_file):
                    seen.add(img_file)
        except Exception as e:
            logger.error(f"Count completed photos error: {e}")
        return len(seen)

    async def handle_photo_jobs(self, request: web.Request):
        """Return actual pending APScheduler image-generation jobs."""
        if not self.on_list_photo_jobs:
            completed_today = self._today_completed_photo_count()
            max_daily = self.get_photo_job_limit()
            return web.json_response({
                "status": "unavailable",
                "date": date.today().isoformat(),
                "jobs": [],
                "max_daily": max_daily,
                "min": MIN_PHOTO_JOB_LIMIT,
                "max": MAX_PHOTO_JOB_LIMIT,
                "completed_today": completed_today,
                "active_today": 0,
                "failed_today": 0,
                "planned_today": completed_today,
                "remaining_today": max(0, max_daily - completed_today),
            })
        try:
            jobs = self.on_list_photo_jobs()
            max_daily = self.get_photo_job_limit()
            completed_today = self._today_completed_photo_count()
            active_today = sum(1 for job in jobs if job.get("status") in ("scheduled", "running"))
            failed_today = sum(1 for job in jobs if job.get("status") == "failed")
            planned_today = completed_today + len(jobs)
            return web.json_response({
                "status": "ok",
                "date": date.today().isoformat(),
                "jobs": jobs,
                "max_daily": max_daily,
                "min": MIN_PHOTO_JOB_LIMIT,
                "max": MAX_PHOTO_JOB_LIMIT,
                "completed_today": completed_today,
                "active_today": active_today,
                "failed_today": failed_today,
                "planned_today": planned_today,
                "remaining_today": max(0, max_daily - planned_today),
            })
        except Exception as e:
            logger.error(f"Load photo jobs error: {e}")
            return web.json_response({"error": str(e), "jobs": []}, status=500)

    async def handle_retry_photo_job(self, request: web.Request):
        """Queue a retry for a missed/failed dynamic photo job."""
        if not self.on_retry_photo_job:
            return web.json_response({"error": "retry_unavailable"}, status=503)
        try:
            body = await request.json()
        except Exception:
            body = {}
        raw_time = str(body.get("time") or body.get("schedule_time") or "").strip()
        match = re.match(r'^\s*(\d{1,2}):(\d{2})', raw_time)
        if not match:
            return web.json_response({"error": "invalid_time"}, status=400)

        schedule_time = f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
        try:
            result = await self.on_retry_photo_job(schedule_time)
            status = result.get("status") if isinstance(result, dict) else ""
            http_status = 400 if status == "error" else 200
            return web.json_response(result, status=http_status)
        except Exception as e:
            logger.error(f"Retry photo job error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

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
            active_today = sum(1 for job in jobs if job.get("status") in ("scheduled", "running"))
            failed_today = sum(1 for job in jobs if job.get("status") == "failed")
            planned_today = completed_today + len(jobs)
            return web.json_response({
                "status": "ok",
                "date": date.today().isoformat(),
                "max_daily": limit,
                "min": MIN_PHOTO_JOB_LIMIT,
                "max": MAX_PHOTO_JOB_LIMIT,
                "completed_today": completed_today,
                "active_today": active_today,
                "failed_today": failed_today,
                "planned_today": planned_today,
                "remaining_today": max(0, limit - planned_today),
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
                source = getattr(entry, "source", "") or ""
                if source == "preserved":
                    status_text = "preserved"
                    message = "LLM 暂不可用，已保留当前今日日程。"
                else:
                    status_text = "ok"
                    message = "日程已刷新。"
                return web.json_response({
                    "status": status_text,
                    "message": message,
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

        image_config = self.config.get("image_gen", {})
        llm_config = self.config.get("llm", {})
        raw_default_gpt_base_url = str(image_config.get("gpt_base_url", "") or "").strip()
        default_gpt_base_url = self._configured_image_base_url(raw_default_gpt_base_url)
        local_gpt_base_url = str(keys_config.get("gpt_base_url", "") or "").strip()
        if (
            local_gpt_base_url == raw_default_gpt_base_url
            or self._gpt_image_endpoint_identity(local_gpt_base_url) == self._gpt_image_endpoint_identity(default_gpt_base_url)
        ):
            local_gpt_base_url = ""
        default_cpa_url = str(llm_config.get("base_url", "") or "").strip()
        local_cpa_url = str(keys_config.get("cpa_url", "") or "").strip()
        if local_cpa_url == default_cpa_url:
            local_cpa_url = ""
        default_gitee_url = str(image_config.get("gitee_url", "") or "").strip()
        local_gitee_url = str(keys_config.get("gitee_url", "") or "").strip()
        if local_gitee_url == default_gitee_url:
            local_gitee_url = ""
        default_github_api = self._github_api_url()
        persona = load_runtime_persona(self.config, self.data_dir)
        persona_source = normalize_persona_source(keys_config.get("persona_source"))
        local_image_dir = normalize_image_dir(keys_config.get("image_dir"), self.data_dir)
        configured_image_dir = resolve_image_dir(self.config, self.data_dir)
        effective_image_dir = self.image_dir or configured_image_dir
        default_dir = self.default_image_dir
        gallery_title = str(self.gallery_config.get("title", "") or "每日穿搭画廊").strip()
        integrations = self.config.get("integrations", {}) if isinstance(self.config.get("integrations"), dict) else {}
        local_push_channel_raw = str(keys_config.get("push_channel", "") or "").strip()
        configured_push_channel = (
            local_push_channel_raw
            or os.getenv("ZHUZHU_SEND_CHANNEL", "")
            or str(integrations.get("push_channel", "") or "")
        )
        push_channel = normalize_push_channel(configured_push_channel)
        push_agent = auto_push_agent(persona_source, push_channel)

        # 返回 masked 状态
        return web.json_response({
            "gallery_title": gallery_title,
            "gitee_key": self._mask_key(gitee_key),
            "gitee_url": local_gitee_url or default_gitee_url,
            "gitee_url_local": local_gitee_url,
            "gitee_url_default": default_gitee_url,
            "gpt_key": self._mask_key(keys_config.get("gpt_key", "")),
            "gpt_base_url": local_gpt_base_url or default_gpt_base_url,
            "gpt_base_url_local": local_gpt_base_url,
            "gpt_base_url_default": default_gpt_base_url,
            "gpt_image_endpoints": [
                {
                    "label": str(endpoint.get("label", "") or "").strip(),
                    "base_url": str(endpoint.get("base_url", "") or "").strip(),
                    "api_key": self._mask_key(str(endpoint.get("api_key", "") or "")),
                }
                for endpoint in (keys_config.get("gpt_image_endpoints") or [])
                if isinstance(endpoint, dict)
            ],
            "cpa_url": local_cpa_url or default_cpa_url,
            "cpa_url_local": local_cpa_url,
            "cpa_url_default": default_cpa_url,
            "cpa_key": self._mask_key(keys_config.get("cpa_key", "")),
            "appearance": keys_config.get("appearance", ""),
            "persona_source": persona_source,
            "persona": keys_config.get("persona", ""),
            "resolved_persona": {
                "name": persona.get("name", ""),
                "user_name": persona.get("user_name", ""),
                "persona": persona.get("persona", ""),
                "caption_voice": persona.get("caption_voice", ""),
                "appearance": persona.get("appearance", ""),
                "source": persona.get("source", ""),
                "sources": persona.get("sources", {}),
                "persona_source": persona.get("persona_source", persona_source),
            },
            "outfit_styles": DEFAULT_OUTFIT_STYLES,
            "enabled_outfit_styles": load_enabled_outfit_styles(self.config, self.data_dir),
            "github_proxy": self._github_proxy(),
            "github_api": default_github_api,
            "github_api_local": "",
            "github_api_default": default_github_api,
            "image_dir": effective_image_dir,
            "image_dir_local": local_image_dir,
            "image_dir_default": default_dir,
            "image_dir_exists": os.path.isdir(effective_image_dir),
            "llm_model": llm_model,
            "llm_models": self.config.get("llm", {}),
            "gitee_fallback_enabled": gitee_fallback_enabled,
            "push_channel": push_channel,
            "push_channel_local": normalize_push_channel(local_push_channel_raw) if local_push_channel_raw else "",
            "push_agent": push_agent,
            "hermes_cli": str(integrations.get("hermes_cli", "") or "").strip(),
            "openclaw_cli": str(integrations.get("openclaw_cli", "") or "").strip(),
        })

    def _mask_key(self, key: str) -> str:
        """Mask API key for display"""
        if not key or len(key) < 8:
            return ""
        return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"

    @staticmethod
    def _looks_masked_key(value: str) -> bool:
        return "*" in str(value or "")

    def _clean_gpt_image_endpoints(self, raw_endpoints, existing_endpoints) -> tuple[list[dict], str]:
        if raw_endpoints in (None, ""):
            return [], ""
        if not isinstance(raw_endpoints, list):
            return [], "GPT Image 多端点格式不正确"

        existing_by_key = {}
        if isinstance(existing_endpoints, list):
            for idx, endpoint in enumerate(existing_endpoints):
                if not isinstance(endpoint, dict):
                    continue
                label = str(endpoint.get("label", "") or "").strip()
                base_url = self._configured_image_base_url(str(endpoint.get("base_url", "") or "").strip())
                existing_by_key[(label, base_url)] = endpoint
                existing_by_key[(str(idx), base_url)] = endpoint

        cleaned = []
        for idx, endpoint in enumerate(raw_endpoints):
            if not isinstance(endpoint, dict):
                continue
            label = str(endpoint.get("label", "") or "").strip()
            base_url = self._configured_image_base_url(str(endpoint.get("base_url", "") or "").strip())
            api_key = str(endpoint.get("api_key", "") or "").strip()
            if not label and not base_url and not api_key:
                continue
            existing = existing_by_key.get((label, base_url)) or existing_by_key.get((str(idx), base_url)) or {}
            if self._looks_masked_key(api_key):
                api_key = str(existing.get("api_key", "") or "").strip()
            if not base_url:
                return [], f"第 {idx + 1} 个 GPT Image 端点缺少 Base URL"
            if not api_key:
                return [], f"第 {idx + 1} 个 GPT Image 端点缺少 API Key"
            cleaned.append({
                "label": label,
                "base_url": base_url,
                "api_key": api_key,
            })
        return cleaned, ""

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

    @staticmethod
    def _compact_outfit_text(text: str, max_items: int = 4) -> str:
        """Return a short gallery-facing outfit summary."""
        text = re.sub(r"\s+", "", str(text or "")).strip(" ，,。；;")
        if not text:
            return ""
        if len(text) <= 42 and not any(marker in text for marker in ("上装", "下装", "脚穿", "整体", "细节")):
            return text

        item_markers = (
            "针织衫", "开衫", "衬衫", "吊带", "背心", "T恤", "毛衣", "卫衣", "外套",
            "连衣裙", "百褶长裙", "百褶裙", "半身裙", "长裙", "短裙", "裙",
            "慢跑裤", "工装裤", "牛仔裤", "长裤", "短裤", "裤",
            "平底鞋", "运动鞋", "乐福鞋", "玛丽珍鞋", "高跟鞋", "靴", "鞋",
            "锁骨链", "项链", "耳环", "耳饰", "手链", "发带", "发夹", "包",
        )
        skip_markers = ("整体", "细节", "随着", "摇曳", "设计")
        cleanup_patterns = (
            r"^(?:上装|上身|内搭|外搭|下装|裙装|脚上|脚|脖子上|颈间|耳朵上|手腕上|腰间|头上|领口处)",
            r"^(?:是|有|搭配|配|脚穿|穿着|身穿|佩戴|戴着|选择)",
            r"^(?:一件|一条|一双|一款|一枚|一个)",
            r"(?:修身剪裁的|精致的|极细的|充满高级感|慵懒的精致)",
        )

        items = []
        for raw_part in re.split(r"[，,。；;]", text):
            part = raw_part.strip()
            if not part:
                continue
            if any(marker in part for marker in skip_markers) and not any(marker in part for marker in ("鞋", "裙", "衫", "裤", "链", "包")):
                continue
            if not any(marker in part for marker in item_markers):
                continue
            for pattern in cleanup_patterns:
                part = re.sub(pattern, "", part)
            part = part.strip(" ，,。；;的")
            part = re.sub(r"^(?:搭配|配|是|有|穿|戴|佩戴)", "", part).strip("的")
            if not part or len(part) > 18:
                for marker in item_markers:
                    idx = part.find(marker)
                    if idx > 0:
                        start = max(0, idx - 8)
                        part = part[start:idx + len(marker)]
                        break
            if part and not any(part in existing or existing in part for existing in items):
                items.append(part)
            if len(items) >= max_items:
                break
        if items:
            return "、".join(items)
        return text[:42].rstrip("，,。；;、") + ("…" if len(text) > 42 else "")

    def _compact_outfit_for_display(self, outfit_raw: str) -> str:
        parts = self._parse_outfit_parts(outfit_raw)
        clothing = parts.get("穿搭", "")
        if not clothing:
            return outfit_raw
        compact = self._compact_outfit_text(clothing)
        if not compact or compact == clothing:
            return outfit_raw
        prefix_parts = []
        for key in ("风格", "发型"):
            value = parts.get(key)
            if value:
                prefix_parts.append(f"{key}：{value}")
        prefix_parts.append(f"穿搭：{compact}")
        for key in ("动作", "场景"):
            value = parts.get(key)
            if value:
                prefix_parts.append(f"{key}：{value}")
        return " \n".join(prefix_parts)

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
        if scene and not parts.get("场景") and re.search(r'[\u4e00-\u9fff]', scene):
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
    def _caption_activity_label(activity: str, limit: int = 18) -> str:
        text = re.sub(r"\s+", "", str(activity or ""))
        text = re.sub(r"(?:，|,).*$", "", text)
        replacements = (
            ("给自己做一份", "做份"),
            ("一份", ""),
            ("水果松饼早餐", "水果松饼"),
            ("窝在沙发上看动漫新番", "窝着看会儿新番"),
            ("在阳台的摇椅上小憩打盹", "去阳台眯一小会儿"),
            ("整理房间，顺便给多肉植物浇水", "收拾下房间，给多肉浇浇水"),
            ("调一杯冰柠薄荷水", "给自己调杯冰柠薄荷水"),
            ("坐在窗边发呆看夕阳", "坐窗边看看夕阳"),
            ("打开直播和主人聊天互动，对着镜头撒娇", "开个直播聊聊天"),
            ("泡个香香的热水澡，涂上身体乳准备休息", "泡个热水澡再慢慢休息"),
        )
        for old, new in replacements:
            text = text.replace(old, new)
        text = text.replace("主人", "").replace("对着镜头撒娇", "开播互动")
        text = text.strip("，,。.!！?；;、")
        if len(text) > limit:
            return text[:limit].rstrip("，,。.!！?；;、") + "…"
        return text

    @classmethod
    def _build_schedule_plan_caption(cls, schedule_items: list[dict]) -> str:
        buckets = {"上午": [], "午后": [], "晚上": []}
        for item in schedule_items:
            time_text = str(item.get("time") or "")
            if not re.match(r"^\d{1,2}:\d{2}$", time_text):
                continue
            hour = int(time_text.split(":", 1)[0])
            label = cls._caption_activity_label(item.get("activity", ""))
            if not label:
                continue
            if hour < 12:
                buckets["上午"].append(label)
            elif hour < 18:
                buckets["午后"].append(label)
            else:
                buckets["晚上"].append(label)

        morning = buckets["上午"][0] if buckets["上午"] else ""
        noon = buckets["午后"][:2]
        evening = buckets["晚上"][0] if buckets["晚上"] else ""
        parts = []
        if morning:
            parts.append("早上" + morning)
        if noon:
            parts.append("午后" + "，再".join(noon))
        if evening:
            parts.append("晚上" + evening)
        if not parts:
            return ""

        caption = "今天先按这个节奏来：" + "，".join(parts) + "，别把事情都拖到最后。"
        return caption[:90].rstrip("，,。.!！?；;、") + "。"

    @staticmethod
    def _caption_is_schedule_plan(caption: str) -> bool:
        text = re.sub(r"\s+", "", str(caption or ""))
        if not text:
            return False
        bad_markers = (
            "主人", "亲一口", "抱抱", "怀里", "来找我玩", "被夸",
            "美照", "自拍", "拍照", "照片", "画面", "造型", "画廊",
            "记录", "收藏", "穿得这么", "好看", "性感",
            "水珠", "叶尖", "擦亮", "像被阳光揉", "温柔照顾", "书签", "光落下来",
        )
        if any(marker in text for marker in bad_markers):
            return False
        intent_markers = ("想过", "想怎么过", "打算", "准备", "安排", "计划", "节奏", "先", "再", "然后")
        time_markers = ("一整天", "早上", "上午", "午后", "下午", "晚上")
        return any(marker in text for marker in intent_markers) and any(marker in text for marker in time_markers)

    @staticmethod
    def _is_today_photo_source(source: str) -> bool:
        return source in TODAY_PHOTO_SOURCES

    @staticmethod
    def _has_usable_schedule(entry: dict) -> bool:
        return (
            isinstance(entry, dict)
            and bool((entry.get("schedule") or "").strip())
            and entry.get("schedule") != FAILED_SCHEDULE_TEXT
            and entry.get("status") == "ok"
            and entry.get("source") != "fallback"
        )

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

    def _display_photo_schedule_activity(self, entry: dict, activity: str) -> str:
        cleaned = self._clean_activity_text(activity)
        if cleaned:
            prompt = (entry.get("prompt") or "").strip()
            if (entry.get("source") or "") == "web" and prompt and self._generate_now_prompt_conflicts(cleaned, prompt):
                fallback = self._clean_activity_text(self._photo_schedule_activity(entry), max_len=64)
                if fallback and not self._generate_now_prompt_conflicts(fallback, prompt):
                    return fallback
            return cleaned

        fallback = self._clean_activity_text(self._photo_schedule_activity(entry), max_len=64)
        if fallback:
            return fallback
        return "即时生图完成"

    @staticmethod
    def _display_model_name(model_name: str) -> str:
        """Normalize stored model ids to stable gallery display labels."""
        name = (model_name or "").strip()
        lower = name.lower()
        if lower.startswith("agnes-image-"):
            return "Agnes"
        if "gpt-image" in lower or lower == "gpt image":
            return "GPT Image"
        if "z-image" in lower or "gitee" in lower:
            return "Gitee"
        if "gemini" in lower:
            return "Gemini"
        return name

    def _normalize_entry_display(self, entry: dict, metadata: Optional[dict] = None) -> dict:
        if not isinstance(entry, dict):
            return entry
        normalized = dict(entry)
        img_file = normalized.get("image_filename", "")
        if img_file:
            for field in ("schedule", "schedule_prompt", "schedule_details"):
                normalized.pop(field, None)
        source = (normalized.get("source") or "").strip()
        base_style = (normalized.get("base_style") or "").strip()
        raw_outfit_style = (normalized.get("outfit_style") or "").strip()

        if raw_outfit_style in {"cool", "girly", "sweet"} or (source in {"chat", "custom", "hermes_api"} and base_style in {"cool", "girly", "sweet"}):
            normalized["outfit_style"] = "自定义"
            outfit = normalized.get("outfit") or ""
            if outfit:
                normalized["outfit"] = re.sub(r'风格[：:]\s*[^ \n，,。；;]+', "风格：自定义", outfit, count=1)

        if metadata and img_file:
            meta_entry = metadata.get(img_file, {}) or {}
            if meta_entry.get("source") == "hermes_api" or img_file.startswith("hermes_"):
                normalized["source"] = "hermes_api"
                source = "hermes_api"
            for field in (
                "prompt_mode",
                "pure_prompt",
                "custom_ref_mode",
                "requested_generation_mode",
                "generation_mode",
                "ref_image",
                "ref_image_path",
                "requested_ref_image",
                "requested_ref_image_path",
                "base_style",
                "reference_query",
                "selected_reference",
                "model_name",
                "caption",
                "display_outfit",
                "outfit_description",
            ):
                if field in meta_entry and (field not in normalized or normalized.get(field) in ("", None)):
                    normalized[field] = meta_entry.get(field)
            meta_prompt = meta_entry.get("prompt", "")
            current_prompt = normalized.get("prompt", "") or ""
            if meta_prompt and len(meta_prompt) > len(current_prompt):
                normalized["prompt"] = meta_prompt
            if not normalized.get("size") and meta_entry.get("size"):
                normalized["size"] = meta_entry.get("size")
            if normalized.get("generation_time") is None and meta_entry.get("generation_time") is not None:
                normalized["generation_time"] = meta_entry.get("generation_time")

        if source == "hermes_api":
            display_outfit = self._clean_display_description(
                normalized.get("display_outfit") or normalized.get("outfit_description") or ""
            )
            current_outfit = normalized.get("outfit", "")
            if display_outfit and self._has_cjk(display_outfit) and (
                not self._has_cjk(current_outfit) or re.search(r"[A-Za-z]{16,}", current_outfit)
            ):
                style_name = normalized.get("outfit_style") or "自定义"
                view_match = re.search(r"视角[：:]\s*([^ \n，,。；;]+)", current_outfit)
                mode_match = re.search(r"模式[：:]\s*([^ \n，,。；;]+)", current_outfit)
                parts = [f"风格：{style_name}"]
                if mode_match:
                    parts.append(f"模式：{mode_match.group(1)}")
                if view_match:
                    parts.append(f"视角：{view_match.group(1)}")
                parts.append(f"穿搭：{display_outfit}")
                normalized["outfit"] = " ".join(parts)
        if img_file:
            image_info = self._image_file_info(img_file)
            if image_info.get("size"):
                normalized["size"] = image_info["size"]
                normalized["width"] = image_info.get("width")
                normalized["height"] = image_info.get("height")
            if image_info.get("file_size_bytes"):
                normalized["file_size_bytes"] = image_info["file_size_bytes"]

        model_label = self._display_model_name(normalized.get("model_name", ""))
        if model_label and model_label != normalized.get("model_name"):
            normalized["model_name"] = model_label

        if self._entry_outfit_needs_repair(normalized.get("outfit", "")):
            repaired = self._fallback_outfit_keywords_from_prompt(normalized.get("prompt", ""))
            if repaired:
                style_name = normalized.get("outfit_style") or "自定义"
                normalized["outfit"] = f"风格：{style_name} 穿搭：{repaired}"

        outfit_for_display = self._compact_outfit_for_display(normalized.get("outfit", ""))
        if outfit_for_display and outfit_for_display != normalized.get("outfit", ""):
            normalized.setdefault("outfit_full", normalized.get("outfit", ""))
            normalized["outfit"] = outfit_for_display

        self._ensure_entry_reference_label(normalized)
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
            "未检测到服装",
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

    def _save_image_metadata(self, metadata: dict):
        path = os.path.join(self.data_dir, "image_metadata.json")
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    def _update_image_metadata_entry(self, filename: str, meta_entry: dict):
        """Atomically merge one metadata entry without clobbering concurrent writes."""
        if not filename:
            raise ValueError("filename_required")
        lock_path = os.path.join(self.data_dir, ".image_metadata.lock")
        with open(lock_path, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                metadata = self._load_image_metadata()
                metadata[filename] = meta_entry
                self._save_image_metadata(metadata)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _iter_gallery_image_files(self) -> dict[str, str]:
        files = {}
        for image_dir in self._image_search_dirs():
            try:
                for item in Path(image_dir).iterdir():
                    if not item.is_file():
                        continue
                    if item.name.lower().endswith(REFERENCE_IMAGE_EXTENSIONS):
                        files.setdefault(item.name, str(item))
            except OSError as e:
                logger.error(f"Scan image dir error: {image_dir}, {e}")
        return files

    @staticmethod
    def _timestamp_from_entry(entry: dict) -> int:
        if not isinstance(entry, dict):
            return 0
        date_text = str(entry.get("date") or "").strip()
        time_text = str(entry.get("time") or "").strip()
        if not date_text:
            return 0
        try:
            if re.match(r"^\d{1,2}:\d{2}", time_text):
                dt = datetime.strptime(f"{date_text} {time_text[:5]}", "%Y-%m-%d %H:%M")
            else:
                dt = datetime.strptime(date_text, "%Y-%m-%d")
            return int(time.mktime(dt.timetuple()))
        except ValueError:
            return 0

    @classmethod
    def _image_created_timestamp(cls, filename: str, entry: dict, meta: dict, path: str) -> int:
        ts = cls._timestamp_from_image_filename(filename)
        if ts:
            return ts
        if isinstance(meta, dict):
            try:
                ts = int(float(meta.get("created_at") or 0))
            except (TypeError, ValueError):
                ts = 0
            if ts:
                return ts
        ts = cls._timestamp_from_entry(entry)
        if ts:
            return ts
        try:
            return int(os.stat(path).st_mtime)
        except OSError:
            return 0

    @staticmethod
    def _cleanup_days_from_body(body: dict) -> int:
        preset = str(body.get("preset") or body.get("older_than") or "").strip()
        if preset in CLEANUP_PRESET_DAYS:
            return CLEANUP_PRESET_DAYS[preset]
        if preset in {"3", "7", "30", "90"}:
            return int(preset)

        raw_days = body.get("custom_days") if preset == "custom" else body.get("older_than_days")
        if raw_days in (None, ""):
            raw_days = body.get("days")
        try:
            days = int(raw_days)
        except (TypeError, ValueError):
            raise ValueError("请选择清理时间范围")
        if days < 1 or days > 3650:
            raise ValueError("自定义天数需在 1-3650 之间")
        return days

    def _cleanup_image_plan(self, days: int) -> dict:
        now_ts = int(time.time())
        cutoff_ts = now_ts - days * 86400
        store = ScheduleStore(self.data_dir)
        all_data = store.load()
        metadata = self._load_image_metadata()
        image_files = self._iter_gallery_image_files()
        entry_by_filename = {}
        favorite_filenames = set()

        for key, entry in all_data.items():
            if key == "_meta" or not isinstance(entry, dict) or DATE_KEY_RE.match(str(key)):
                continue
            filename = entry.get("image_filename") or (key if str(key).lower().endswith(REFERENCE_IMAGE_EXTENSIONS) else "")
            if not filename:
                continue
            entry_by_filename.setdefault(filename, entry)
            if entry.get("favorite") is True:
                favorite_filenames.add(filename)

        known_filenames = set(image_files) | set(metadata) | set(entry_by_filename)
        candidates = []
        favorite_kept = 0
        missing_files = 0

        for filename in sorted(known_filenames):
            path = image_files.get(filename) or self._image_file_path(filename)
            if not path:
                missing_files += 1
                continue
            if filename in favorite_filenames:
                favorite_kept += 1
                continue

            entry = entry_by_filename.get(filename, {})
            meta = metadata.get(filename, {})
            created_ts = self._image_created_timestamp(filename, entry, meta, path)
            if not created_ts or created_ts > cutoff_ts:
                continue

            candidates.append({
                "filename": filename,
                "image_path": f"/images/{filename}",
                "date": entry.get("date") or self._date_time_from_timestamp(created_ts)[0],
                "source": entry.get("source", "") or ("metadata" if filename in metadata else "file"),
                "age_days": max(0, (now_ts - created_ts) // 86400),
                "created_at": created_ts,
            })

        return {
            "older_than_days": days,
            "cutoff_ts": cutoff_ts,
            "scanned_count": len(known_filenames),
            "candidate_count": len(candidates),
            "favorite_kept": favorite_kept,
            "missing_files": missing_files,
            "candidates": candidates,
        }

    @staticmethod
    def _timestamp_from_image_filename(filename: str) -> int:
        match = re.search(r'_(\d{10})\.\w+$', filename or "")
        if not match:
            return 0
        try:
            return int(match.group(1))
        except ValueError:
            return 0

    @staticmethod
    def _date_time_from_timestamp(timestamp: int) -> tuple[str, str]:
        if not timestamp:
            return "", ""
        try:
            local_time = time.localtime(int(timestamp))
            return time.strftime("%Y-%m-%d", local_time), time.strftime("%H:%M", local_time)
        except (OSError, OverflowError, ValueError):
            return "", ""

    def _metadata_gallery_entry(self, filename: str, meta: dict) -> dict:
        """Build a gallery-only entry for images that only have metadata."""
        if not isinstance(meta, dict):
            meta = {}
        created_at = meta.get("created_at") or self._timestamp_from_image_filename(filename)
        date_text, time_text = self._date_time_from_timestamp(created_at)
        if not date_text:
            try:
                stat = self._image_stat(filename)
                if stat is None:
                    raise OSError("image file missing")
                date_text, time_text = self._date_time_from_timestamp(int(stat.st_mtime))
            except OSError:
                date_text = date.today().isoformat()
                time_text = ""

        prompt = meta.get("prompt", "")
        model_name = meta.get("model") or meta.get("model_name", "")
        source = "hermes_api" if meta.get("source") == "hermes_api" or filename.startswith("hermes_") else "chat"
        mode = str(meta.get("generation_mode") or meta.get("requested_generation_mode") or "").lower()
        is_img2img = mode == "img2img" or "img2img" in prompt.lower() or "参考这张图" in prompt
        outfit_label = "Hermes 图生图" if source == "hermes_api" and is_img2img else (
            "Hermes 文生图" if source == "hermes_api" else (
                "聊天图生图" if is_img2img else "聊天生图"
            )
        )
        display_outfit = self._clean_display_description(
            meta.get("display_outfit") or meta.get("outfit_description") or ""
        )
        if source == "hermes_api" and not display_outfit:
            display_outfit = self._fallback_hermes_display_description(prompt, outfit_label)
        return {
            "id": filename,
            "date": date_text,
            "time": time_text,
            "model_name": self._display_model_name(model_name),
            "base_style": str(meta.get("base_style") or "").strip(),
            "outfit_style": "自定义",
            "outfit": f"风格：自定义 穿搭：{display_outfit or outfit_label}",
            "image_path": f"/images/{filename}",
            "image_filename": filename,
            "prompt": prompt,
            "caption": str(meta.get("caption") or "").strip(),
            "favorite": False,
            "status": "ok",
            "source": source,
            "prompt_mode": meta.get("prompt_mode", "pure" if source == "hermes_api" else ""),
            "pure_prompt": meta.get("pure_prompt", True if source == "hermes_api" else False),
            "custom_ref_mode": meta.get("custom_ref_mode", "reference" if is_img2img else "text2img"),
            "generation_mode": mode,
            "requested_generation_mode": meta.get("requested_generation_mode", ""),
            "ref_image": meta.get("ref_image", ""),
            "ref_image_path": meta.get("ref_image_path", ""),
            "requested_ref_image": meta.get("requested_ref_image", ""),
            "requested_ref_image_path": meta.get("requested_ref_image_path", ""),
            "selected_reference": meta.get("selected_reference", {}) if isinstance(meta.get("selected_reference"), dict) else {},
            "reference_query": str(meta.get("reference_query") or "").strip(),
            "metadata_only": True,
        }

    def _photo_schedule_item(self, entry: dict) -> dict:
        """Build a schedule item from a generated photo entry."""
        if not isinstance(entry, dict):
            return {}

        # 只用 schedule_time 字段，不用 time（time 是图片生成时间，不是日程时间）
        schedule_time, activity = self._parse_time_activity(entry.get("schedule_time", ""))
        if not schedule_time:
            return {}

        activity = self._display_photo_schedule_activity(entry, activity)
        return {"time": schedule_time, "activity": activity}

    def _enrich_photo_schedule_time(self, entry: dict, metadata: Optional[dict] = None) -> dict:
        """Return a normalized copy with any parseable schedule_time preserved."""
        if not isinstance(entry, dict):
            return entry
        entry = self._normalize_entry_display(entry, metadata)
        if entry.get("schedule_time"):
            schedule_time, activity = self._parse_time_activity(entry.get("schedule_time", ""))
            if schedule_time:
                cleaned_activity = self._display_photo_schedule_activity(entry, activity)
                cleaned_schedule_time = f"{schedule_time} {cleaned_activity}".strip()
                if cleaned_schedule_time != entry.get("schedule_time"):
                    enriched = dict(entry)
                    enriched["schedule_time"] = cleaned_schedule_time
                    return enriched
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
            image_dir_changed = "image_dir" in body

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

                    image_config = self.config.get("image_gen", {}) if isinstance(self.config.get("image_gen"), dict) else {}
                    llm_config = self.config.get("llm", {}) if isinstance(self.config.get("llm"), dict) else {}
                    raw_default_gpt_base_url = str(image_config.get("gpt_base_url", "") or "").strip()
                    default_gpt_base_url = self._configured_image_base_url(raw_default_gpt_base_url)
                    default_gitee_url = str(image_config.get("gitee_url", "") or "").strip()
                    default_github_api = self._github_api_url()
                    self._drop_redundant_local_url_override(
                        keys_config,
                        "gpt_base_url",
                        raw_default_gpt_base_url,
                    )
                    self._drop_redundant_gpt_url_override(keys_config, default_gpt_base_url)
                    self._drop_redundant_local_url_override(
                        keys_config,
                        "cpa_url",
                        llm_config.get("base_url", ""),
                    )
                    self._drop_redundant_local_url_override(
                        keys_config,
                        "gitee_url",
                        default_gitee_url,
                    )
                    keys_config.pop("github_api", None)

                    # 更新配置（只更新提供的字段）
                    if "gpt_key" in body and body["gpt_key"]:
                        keys_config["gpt_key"] = body["gpt_key"]
                    if "gpt_base_url" in body:
                        self._store_gpt_url_override(
                            keys_config,
                            body.get("gpt_base_url"),
                            default_gpt_base_url,
                        )
                    if "gpt_image_endpoints" in body:
                        cleaned_endpoints, endpoint_error = self._clean_gpt_image_endpoints(
                            body.get("gpt_image_endpoints"),
                            keys_config.get("gpt_image_endpoints") or [],
                        )
                        if endpoint_error:
                            return web.json_response({"error": endpoint_error, "message": endpoint_error}, status=400)
                        keys_config["gpt_image_endpoints"] = cleaned_endpoints
                    if "cpa_url" in body:
                        self._store_local_url_override(
                            keys_config,
                            "cpa_url",
                            body.get("cpa_url"),
                            llm_config.get("base_url", ""),
                        )
                    if "cpa_key" in body and body["cpa_key"]:
                        keys_config["cpa_key"] = body["cpa_key"]
                    if "gitee_url" in body:
                        self._store_local_url_override(
                            keys_config,
                            "gitee_url",
                            body.get("gitee_url"),
                            default_gitee_url,
                        )
                    # appearance: always update (empty string = remove local appearance)
                    if "appearance" in body:
                        keys_config["appearance"] = body["appearance"]
                    if "persona_source" in body:
                        keys_config["persona_source"] = normalize_persona_source(body.get("persona_source"))
                    if "push_channel" in body:
                        keys_config["push_channel"] = normalize_push_channel(body.get("push_channel"))
                    if "persona" in body:
                        value = str(body.get("persona") or "").strip()
                        if value:
                            keys_config["persona"] = value
                        else:
                            keys_config.pop("persona", None)
                    for removed_persona_field in ("character_name", "user_name", "caption_voice"):
                        keys_config.pop(removed_persona_field, None)
                    if "enabled_outfit_styles" in body:
                        styles = normalize_outfit_styles(body.get("enabled_outfit_styles"))
                        if not styles:
                            return web.json_response({"error": "至少保留一个穿搭风格"}, status=400)
                        keys_config["enabled_outfit_styles"] = styles
                    # GitHub proxy is local-only and may be cleared with an empty string.
                    if "github_proxy" in body:
                        keys_config["github_proxy"] = str(body["github_proxy"] or "").strip()
                    if "image_dir" in body:
                        image_dir_raw = str(body.get("image_dir") or "").strip()
                        if "\x00" in image_dir_raw:
                            return web.json_response({"error": "图片目录包含非法字符"}, status=400)
                        if image_dir_raw:
                            target_image_dir = normalize_image_dir(image_dir_raw, self.data_dir)
                            if os.path.exists(target_image_dir) and not os.path.isdir(target_image_dir):
                                return web.json_response({"error": "图片存放位置不是文件夹"}, status=400)
                            os.makedirs(target_image_dir, exist_ok=True)
                            keys_config["image_dir"] = target_image_dir
                        else:
                            keys_config.pop("image_dir", None)

                    if self._body_bool(body, "validate_required_config"):
                        plugin_config = self._load_plugin_config()
                        gitee_keys = plugin_config.get("gitee_config", {}).get("api_keys", [])
                        existing_gitee_key = str((gitee_keys[0] if gitee_keys else "") or "").strip()
                        required_fields = [
                            ("Gitee API URL", keys_config.get("gitee_url")),
                            ("Gitee API Key", body.get("gitee_key") or existing_gitee_key),
                            ("GPT Image Base URL", keys_config.get("gpt_base_url")),
                            ("GPT Image Key", body.get("gpt_key") or keys_config.get("gpt_key")),
                            ("CPA Base URL", keys_config.get("cpa_url")),
                            ("CPA API Key", body.get("cpa_key") or keys_config.get("cpa_key")),
                            ("GitHub Release API URL", default_github_api),
                            ("GitHub API 代理", keys_config.get("github_proxy")),
                        ]
                        missing = [label for label, value in required_fields if not str(value or "").strip()]
                        if missing:
                            return web.json_response({
                                "error": "missing_required_config",
                                "message": "请先填写：" + "、".join(missing),
                                "missing": missing,
                            }, status=400)

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

            hermes_or_openclaw_keys = [key for key in ("hermes_cli", "openclaw_cli") if key in body]
            if hermes_or_openclaw_keys and self.config_path and os.path.exists(self.config_path):
                try:
                    import yaml
                    with open(self.config_path, 'r', encoding='utf-8') as f:
                        full_config = yaml.safe_load(f) or {}
                    if "integrations" not in full_config or not isinstance(full_config.get("integrations"), dict):
                        full_config["integrations"] = {}
                    for key in hermes_or_openclaw_keys:
                        value = str(body.get(key) or "").strip()
                        if value:
                            full_config["integrations"][key] = value
                        else:
                            full_config["integrations"].pop(key, None)
                    with open(self.config_path, 'w', encoding='utf-8') as f:
                        yaml.dump(full_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
                    self.config["integrations"] = full_config["integrations"]
                    logger.info("Integrations updated: %s", hermes_or_openclaw_keys)
                except Exception as e:
                    logger.error(f"Save integrations error: {e}")

            if image_dir_changed:
                self._set_runtime_image_dir(self._resolve_image_dir())

            return web.json_response({"success": True, "image_dir": self.image_dir})

        except Exception as e:
            logger.error(f"Save keys error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _fetch_model_ids(self, base_url: str, api_key: str = "", timeout_seconds: int = 6) -> tuple[list[str], str]:
        endpoint = f"{base_url.rstrip('/')}/models"
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
            async with session.get(endpoint, headers=headers) as resp:
                if resp.status != 200:
                    return [], f"returned {resp.status}"
                data = await resp.json()
        models = []
        for item in data.get("data", []):
            model = item.get("id", "") if isinstance(item, dict) else item
            model = self._normalize_image_model_id(model)
            if model:
                models.append(model)
        return sorted(models), ""

    async def handle_models(self, request: web.Request):
        """获取 CPA 可用模型列表"""
        try:
            request_config = llm_request_config(self.config, self.data_dir)
            base_url = self._models_base_url(request_config["base_url"])
            cpa_key = request_config["api_key"]
            if not base_url:
                return web.json_response({"models": [], "error": "CPA URL 未配置"})

            models, error = await self._fetch_model_ids(base_url, cpa_key, timeout_seconds=5)
            if error:
                return web.json_response({"models": [], "error": f"CPA {error}"})
            return web.json_response({"models": models})
        except Exception as e:
            logger.error(f"Get models error: {e}")
            return web.json_response({"models": [], "error": str(e)})

    @staticmethod
    def _llm_test_response_error(resp: aiohttp.ClientResponse, data) -> str:
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                message = error.get("message") or error.get("code") or error.get("type")
                if message:
                    return str(message)[:500]
            for key in ("message", "msg", "detail"):
                if data.get(key):
                    return str(data.get(key))[:500]
            if data.get("status") and "choices" not in data:
                return json.dumps(data, ensure_ascii=False)[:500]
        if isinstance(data, str) and data.strip():
            return data.strip()[:500]
        return f"HTTP {resp.status}"

    @staticmethod
    def _llm_test_failure_summary(attempts: list[dict]) -> str:
        if not attempts:
            return ""
        parts = []
        for item in attempts[-5:]:
            index = item.get("attempt", "?")
            status = item.get("status")
            detail = str(item.get("detail") or "").strip()
            message = str(item.get("message") or "").strip()
            text = detail or message
            if status:
                head = f"第 {index} 次 HTTP {status}"
            else:
                head = f"第 {index} 次"
            if text:
                parts.append(f"{head}: {text[:160]}")
            else:
                parts.append(head)
        return "；".join(parts)

    @staticmethod
    def _llm_test_final_message(failures: list[dict], attempts: int) -> str:
        if failures and all(item.get("kind") == "empty_content" for item in failures):
            return f"原始接口已连通，但连续 {attempts} 次没有返回可读取文本；日程生成需要文本内容。"
        return f"模型连续测试 {attempts} 次都失败。"

    async def handle_test_llm_model(self, request: web.Request):
        """Send a tiny chat completion request to verify the selected schedule LLM model."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        try:
            request_config = llm_request_config(self.config, self.data_dir)
            base_url = str(body.get("cpa_url") or request_config.get("base_url") or "").strip()
            chat_url = normalize_chat_url(base_url) if base_url else request_config.get("chat_url", "")
            api_key = str(body.get("cpa_key") or request_config.get("api_key") or "").strip()
            model = str(body.get("model") or "").strip()
            if not model:
                models = request_config.get("models") or []
                model = str(models[0] if models else "").strip()
            if not chat_url:
                return web.json_response({
                    "success": False,
                    "message": "LLM Base URL 未配置，请先填写 CPA Base URL。",
                }, status=400)
            if not model:
                return web.json_response({
                    "success": False,
                    "message": "未选择要测试的日程生成模型。",
                }, status=400)
            disable_thinking = "deepseek" in model.lower()
            try:
                attempts = int(body.get("attempts") or 5)
            except (TypeError, ValueError):
                attempts = 5
            attempts = max(1, min(8, attempts))
            try:
                timeout_seconds = int(body.get("timeout_seconds") or 12)
            except (TypeError, ValueError):
                timeout_seconds = 12
            timeout_seconds = max(5, min(20, timeout_seconds))

            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            payload = {
                "model": model,
                "messages": [
                    {"role": "user", "content": "请只回复 OK，用于测试模型是否可用。"},
                ],
                "max_tokens": 16,
                "temperature": 0,
                "stream": False,
            }
            if disable_thinking:
                payload["thinking"] = {"type": "disabled"}
            failures = []
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
                for attempt in range(1, attempts + 1):
                    started = time.monotonic()
                    try:
                        async with session.post(chat_url, headers=headers, json=payload) as resp:
                            elapsed_ms = int((time.monotonic() - started) * 1000)
                            try:
                                data = await resp.json()
                            except Exception:
                                data = await resp.text()
                            adjusted = False
                            if (
                                resp.status == 400
                                and "temperature" in payload
                                and llm_temperature_param_error(data)
                            ):
                                retry_payload = dict(payload)
                                retry_payload.pop("temperature", None)
                                adjusted = True
                                started = time.monotonic()
                                async with session.post(chat_url, headers=headers, json=retry_payload) as retry_resp:
                                    elapsed_ms = int((time.monotonic() - started) * 1000)
                                    try:
                                        data = await retry_resp.json()
                                    except Exception:
                                        data = await retry_resp.text()
                                    resp = retry_resp
                            if (
                                resp.status == 400
                                and "thinking" in payload
                                and "thinking" in llm_response_excerpt(data, 500).lower()
                            ):
                                retry_payload = dict(payload)
                                retry_payload.pop("thinking", None)
                                adjusted = True
                                started = time.monotonic()
                                async with session.post(chat_url, headers=headers, json=retry_payload) as retry_resp:
                                    elapsed_ms = int((time.monotonic() - started) * 1000)
                                    try:
                                        data = await retry_resp.json()
                                    except Exception:
                                        data = await retry_resp.text()
                                    resp = retry_resp
                            if resp.status != 200:
                                detail = self._llm_test_response_error(resp, data)
                                failures.append({
                                    "attempt": attempt,
                                    "status": resp.status,
                                    "latency_ms": elapsed_ms,
                                    "message": f"HTTP {resp.status}",
                                    "detail": detail,
                                    "adjusted_temperature": adjusted,
                                })
                            else:
                                choices = data.get("choices") if isinstance(data, dict) else None
                                if not choices:
                                    detail = self._llm_test_response_error(resp, data)
                                    failures.append({
                                        "attempt": attempt,
                                        "kind": "invalid_response",
                                        "status": resp.status,
                                        "latency_ms": elapsed_ms,
                                        "message": "模型返回格式不完整，没有 choices",
                                        "detail": detail,
                                    })
                                else:
                                    content = llm_choice_text(choices[0])
                                    if not content:
                                        failures.append({
                                            "attempt": attempt,
                                            "kind": "empty_content",
                                            "status": resp.status,
                                            "latency_ms": elapsed_ms,
                                            "message": "接口连通，但未返回可读取文本",
                                            "detail": llm_response_excerpt(data),
                                        })
                                    else:
                                        return web.json_response({
                                            "success": True,
                                            "model": model,
                                            "attempt": attempt,
                                            "attempts": attempts,
                                            "failed_attempts": failures[-3:],
                                            "adjusted_temperature": adjusted,
                                            "latency_ms": elapsed_ms,
                                            "message": (
                                                f"模型可用，第 {attempt}/{attempts} 次测试成功，响应 {elapsed_ms}ms。"
                                                + ("已自动去掉不兼容参数。" if adjusted else "")
                                            ),
                                            "reply": content[:80],
                                        })
                    except asyncio.TimeoutError:
                        failures.append({
                            "attempt": attempt,
                            "message": f"测试超时：{timeout_seconds} 秒内没有收到模型响应",
                        })
                    except Exception as exc:
                        failures.append({
                            "attempt": attempt,
                            "message": f"请求失败：{exc}",
                        })
                    if attempt < attempts:
                        await asyncio.sleep(0.35)

            detail = self._llm_test_failure_summary(failures)
            return web.json_response({
                "success": False,
                "model": model,
                "reachable": any(item.get("status") == 200 for item in failures),
                "attempts": attempts,
                "failed_attempts": failures[-5:],
                "message": self._llm_test_final_message(failures, attempts),
                "detail": detail,
            }, status=200)
        except asyncio.TimeoutError:
            return web.json_response({
                "success": False,
                "message": "测试超时：没有收到模型响应。",
            }, status=200)
        except Exception as e:
            logger.error(f"Test LLM model error: {e}")
            return web.json_response({
                "success": False,
                "message": f"测试失败：{e}",
            }, status=200)

    async def handle_image_models(self, request: web.Request):
        """获取 GPT Image/CPA/AxonHub 可用生图模型列表"""
        keys_config = self._load_api_keys_config()
        current_model, default_model = self._effective_gpt_image_model(keys_config)
        fallback_models = [
            model for model in (
                current_model,
                default_model,
                "gpt-image-2",
            )
            if model
        ]
        models = []
        source_errors = []
        source_status = []
        try:
            gpt_key = (
                keys_config.get("gpt_key", "")
                or os.getenv("GPT_IMAGE_API_KEY", "")
                or keys_config.get("cpa_key", "")
                or os.getenv("CPA_API_KEY", "")
            )
            cpa_config = llm_request_config(self.config, self.data_dir)
            sources = [
                ("GPT Image", self._effective_gpt_image_base_url(keys_config), gpt_key),
                ("CPA/AxonHub", cpa_config.get("base_url", ""), cpa_config.get("api_key", "")),
            ]
            seen_endpoints = set()
            for label, raw_url, key in sources:
                base_url = self._models_base_url(raw_url)
                if not base_url:
                    continue
                endpoint = f"{base_url}/models"
                if endpoint in seen_endpoints:
                    continue
                seen_endpoints.add(endpoint)
                fetched_models, error = await self._fetch_model_ids(base_url, key, timeout_seconds=6)
                if error:
                    source_errors.append(f"{label} {error}")
                    continue
                source_count = 0
                for model in fetched_models:
                    if self._is_image_model_id(model):
                        models.append(model)
                        source_count += 1
                source_status.append({"source": label, "url": base_url, "count": source_count})
        except Exception as e:
            logger.warning(f"Get image models error: {e}")
            source_errors.append(str(e))

        ordered = []
        for model in [*fallback_models, *models]:
            if model and model not in ordered:
                ordered.append(model)
        return web.json_response({
            "models": ordered,
            "current_model": current_model,
            "default_model": default_model,
            "sources": source_status,
            "error": "；".join(source_errors),
        })

    async def handle_today(self, request: web.Request):
        """获取今日数据 - 返回今日所有照片 + 日程信息"""
        today_str = date.today().isoformat()
        try:
            store = ScheduleStore(self.data_dir)
            all_data = store.load()
            if not all_data:
                return web.json_response({"status": "no_data", "date": today_str})

            # 1. 获取日程信息（只从日期日程条目读取，图片条目不承载全天计划）
            schedule_info = {}
            for key, e in all_data.items():
                if key == today_str and self._has_usable_schedule(e):
                    schedule_info = e
                    break
            if not schedule_info:
                for key, e in all_data.items():
                    if (
                        isinstance(e, dict)
                        and not e.get("image_filename")
                        and e.get("date") == today_str
                        and self._has_usable_schedule(e)
                    ):
                        schedule_info = e
                        break

            # 2. 获取今日所有照片
            metadata = self._load_image_metadata()
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
                        if self._image_exists(img_file):
                            seen.add(img_file)
                            photos.append(self._enrich_photo_schedule_time(e, metadata))

            if photos:
                # Sort by the card's original time slot so rerolled replacement
                # images do not jump just because the filename timestamp changed.
                def _ts_key(p):
                    fn = p.get("image_filename", "")
                    m = re.search(r'_(\d{10})\.\w+$', fn)
                    return int(m.group(1)) if m else 0
                def _photo_sort_key(p):
                    slot = self._time_sort_value(p.get("schedule_time") or p.get("time", ""))
                    if slot >= 24 * 60:
                        slot = -1
                    return (slot, _ts_key(p))
                photos.sort(key=_photo_sort_key, reverse=True)
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

            # 查找今日日程（只从日期日程条目读取，图片条目不承载全天计划）
            schedule_entry = None
            # 优先找日期 key（有 schedule 内容的）
            if today_str in all_data and self._has_usable_schedule(all_data[today_str]):
                schedule_entry = all_data[today_str]
            # 再找没有图片文件的日程条目
            if not schedule_entry:
                for key, e in all_data.items():
                    if (
                        isinstance(e, dict)
                        and not e.get("image_filename")
                        and e.get("date") == today_str
                        and self._has_usable_schedule(e)
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
            base_style = ""
            prompt = ""
            outfit_keywords = ""
            scene_keywords = ""
            caption = ""

            if schedule_entry:
                outfit_style = schedule_entry.get("outfit_style", "")
                base_style = schedule_entry.get("base_style", "")
                prompt = schedule_entry.get("prompt", "")
                outfit_keywords = schedule_entry.get("outfit_keywords", "")
                scene_keywords = schedule_entry.get("scene_keywords", "")
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
            today_photos = [
                self._normalize_entry_display(p, metadata)
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
                base_style = base_style or best.get("base_style", "")
                prompt = prompt or best.get("prompt", "")
                outfit_keywords = outfit_keywords or best.get("outfit_keywords", "")
                scene_keywords = scene_keywords or best.get("scene_keywords", "")
                outfit_parts.update(self._parse_outfit_parts(outfit_raw))
                self._enrich_outfit_parts_from_entry(outfit_parts, best)
            elif today_photos and not schedule_entry:
                best = sorted(today_photos, key=lambda x: x.get("time", ""), reverse=True)[0]
                outfit_style = outfit_style or best.get("outfit_style", "")
                base_style = base_style or best.get("base_style", "")
                prompt = prompt or best.get("prompt", "")
                outfit_keywords = outfit_keywords or best.get("outfit_keywords", "")
                scene_keywords = scene_keywords or best.get("scene_keywords", "")
                self._enrich_outfit_parts_from_entry(outfit_parts, best)

            if schedule_items and not self._caption_is_schedule_plan(caption):
                caption = self._build_schedule_plan_caption(schedule_items)

            if not schedule_items and not outfit_parts:
                return web.json_response({"status": "no_schedule"})

            outfit_id = self._favorite_outfit_id(today_str, outfit_style, outfit_parts) if outfit_parts else ""
            favorite_ids = {
                favorite_id
                for item in self._load_favorite_outfits()
                for favorite_id in (item.get("id"), self._favorite_outfit_item_id(item))
                if favorite_id
            }
            disliked_ids = {
                disliked_id
                for item in self._load_disliked_outfits()
                for disliked_id in (item.get("id"), self._favorite_outfit_item_id(item))
                if disliked_id
            }

            return web.json_response({
                "status": "ok",
                "date": today_str,
                "outfit_style": outfit_style,
                "base_style": base_style,
                "outfit": outfit_parts,
                "schedule": schedule_items,
                "caption": caption,
                "prompt": prompt,
                "outfit_keywords": outfit_keywords,
                "scene_keywords": scene_keywords,
                "outfit_favorite_id": outfit_id,
                "outfit_favorite": bool(outfit_id and outfit_id in favorite_ids),
                "outfit_disliked_id": outfit_id,
                "outfit_disliked": bool(outfit_id and outfit_id in disliked_ids),
            })
        except Exception as e:
            logger.error(f"Schedule detail error: {e}")
            return web.json_response({"status": "error", "detail": str(e)})

    @staticmethod
    def _is_reference_image_file(filename: str) -> bool:
        return filename.lower().endswith(REFERENCE_IMAGE_EXTENSIONS)

    @staticmethod
    def _reference_response(
        filename: str,
        url: str,
        label: str,
        style: str = "upload",
        builtin: bool = False,
        prompt: str = "",
        ref_id: str = "",
        source: str = "",
        tags: list[str] | None = None,
        active: bool = True,
        analysis_status: str = "",
        analysis_error: str = "",
    ) -> dict:
        item = {
            "id": ref_id,
            "filename": filename,
            "url": url,
            "style": style,
            "label": label,
            "builtin": builtin,
            "source": source or ("default" if builtin else style or "upload"),
            "active": active,
        }
        prompt = str(prompt or "").strip()
        if prompt:
            item["prompt"] = prompt
        if tags:
            item["tags"] = [str(tag) for tag in tags if str(tag).strip()]
        if analysis_status:
            item["analysis_status"] = analysis_status
        if analysis_error:
            item["analysis_error"] = analysis_error
        return item

    @staticmethod
    def _safe_reference_path(base_dir: str, relative_path: str) -> str:
        try:
            base = Path(base_dir).resolve()
            candidate = (base / unquote(relative_path).lstrip("/")).resolve()
            candidate.relative_to(base)
        except Exception:
            return ""
        return str(candidate) if candidate.is_file() else ""

    def _safe_image_path_in_roots(self, raw_path: str, roots: tuple[str, ...]) -> str:
        try:
            candidate = Path(raw_path).resolve()
            if not candidate.is_file() or not self._is_reference_image_file(str(candidate)):
                return ""
            for root in roots:
                if not root:
                    continue
                try:
                    candidate.relative_to(Path(root).resolve())
                    return str(candidate)
                except ValueError:
                    continue
        except Exception:
            return ""
        return ""

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

    def _reference_profiles(self) -> list[dict]:
        return load_reference_profiles(
            self.data_dir,
            self.reference_dir,
            self.app_reference_dir,
            self.uploaded_reference_dir,
        )

    def _all_reference_profiles_for_selection(self, include_wardrobe: bool = True) -> list[dict]:
        profiles = list(self._reference_profiles())
        if include_wardrobe:
            profiles.extend(self._iter_wardrobe_refs())
        return profiles

    def _select_reference_for_generation_sync(self, context: str, include_wardrobe: bool = True) -> dict:
        selected = select_reference_profile(
            self.config,
            self.data_dir,
            context,
            self._all_reference_profiles_for_selection(include_wardrobe=include_wardrobe),
        )
        if not selected:
            return {}
        path = resolve_reference_profile_path(selected, self.reference_dir, self.app_reference_dir)
        if not path:
            return {}
        result = reference_profile_response(selected)
        result["path"] = path
        result["selection_mode"] = selected.get("selection_mode", "")
        result["selection_reason"] = selected.get("selection_reason", "")
        result["random_fallback"] = bool(selected.get("random_fallback"))
        return result

    def _iter_uploaded_refs(self) -> list[dict]:
        refs = []
        seen = set()
        profile_map = {
            item.get("filename"): item
            for item in self._reference_profiles()
            if item.get("source") == "upload"
        }
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
                profile = profile_map.get(fname, {})
                refs.append(self._reference_response(
                    fname,
                    profile.get("url") or f"{url_prefix}/{fname}",
                    profile.get("label") or "自定义上传",
                    style=profile.get("style") or "upload",
                    builtin=False,
                    prompt=profile.get("prompt", ""),
                    ref_id=profile.get("id", ""),
                    source="upload",
                    tags=profile.get("tags") if isinstance(profile.get("tags"), list) else None,
                    active=profile.get("active") is not False,
                    analysis_status=profile.get("analysis_status", ""),
                    analysis_error=profile.get("analysis_error", ""),
                ))
        return refs

    def _iter_wardrobe_refs(self) -> list[dict]:
        refs = []
        for item in self._load_favorite_outfits():
            wardrobe = self._favorite_outfit_wardrobe_response_item(item)
            url = str(wardrobe.get("url") or "").strip()
            filename = str(wardrobe.get("filename") or "").strip()
            if not url or not filename:
                continue
            outfit = item.get("outfit") if isinstance(item.get("outfit"), dict) else {}
            style = str(item.get("outfit_style") or outfit.get("风格") or "衣柜").strip() or "衣柜"
            label = f"衣柜 · {style}"
            refs.append(self._reference_response(
                filename,
                url,
                label,
                style="wardrobe",
                builtin=False,
                prompt=wardrobe.get("prompt", ""),
                ref_id=f"wardrobe_{hashlib.sha1(filename.encode('utf-8')).hexdigest()[:12]}",
                source="wardrobe",
                tags=[style],
            ))
        return refs

    def _resolve_reference_image(self, ref_image: str, allow_any_path: bool = False) -> str:
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

        if allow_any_path and ref_path.startswith("/images/"):
            rel_path = ref_path.removeprefix("/images/")
            local_path = self._safe_reference_path(self.image_dir, rel_path)
            if local_path and self._is_reference_image_file(local_path):
                return local_path
            return ""

        if os.path.isabs(ref_path):
            if allow_any_path:
                return self._safe_image_path_in_roots(
                    ref_path,
                    (
                        self.image_dir,
                        self.default_image_dir,
                        self.reference_dir,
                        self.uploaded_reference_dir,
                        self.app_reference_dir,
                        self.legacy_uploaded_reference_dir,
                    ),
                )

            # 严格模式：必须在 references/ 目录下（用于 hermes/image-to-image）
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
        refs = [
            reference_profile_response(profile)
            for profile in self._reference_profiles()
            if profile.get("source") != "wardrobe"
        ]
        refs.extend(self._iter_wardrobe_refs())
        return web.json_response(refs)

    async def handle_uploaded_refs(self, request: web.Request):
        """列出已上传的自定义参考图"""
        try:
            refs = [
                reference_profile_response(profile)
                for profile in self._reference_profiles()
                if profile.get("source") == "upload"
            ]
            return web.json_response(refs)
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

        loop = asyncio.get_running_loop()
        analysis = await loop.run_in_executor(
            None,
            lambda: analyze_reference_image(
                self.config,
                self.data_dir,
                save_path,
                fallback_label="自定义上传",
            ),
        )
        profile = upsert_reference_profile(self.data_dir, {
            "filename": save_name,
            "url": f"/local-refs/uploads/{save_name}",
            "label": analysis.get("label") or "自定义上传",
            "prompt": analysis.get("prompt", ""),
            "tags": analysis.get("tags", []),
            "source": "upload",
            "builtin": False,
            "active": True,
            "analysis_status": analysis.get("analysis_status", ""),
            "analysis_error": analysis.get("analysis_error", ""),
        })

        return web.json_response(reference_profile_response(profile))

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
                remove_reference_profile(self.data_dir, filename)
                return web.json_response({"success": True})
            return web.json_response({"error": "not_found"}, status=404)
        except Exception as e:
            logger.error(f"Delete uploaded ref error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    def _entry_sort_key(self, entry):
        """Sort key: date desc, then time desc."""
        return (entry.get("date", ""), entry.get("time", ""))

    @staticmethod
    def _normalize_api_source(*values) -> str:
        """Normalize external API caller labels stored for gallery display."""
        for value in values:
            text = str(value or "").strip().lower()
            if not text:
                continue
            compact = re.sub(r"[\s_-]+", "", text)
            if compact in {"hermes", "hermesapi"} or "hermes" in text:
                return "hermes"
            if compact in {"custom", "customui", "galleryui", "browserui", "ui", "webui"}:
                return "custom_ui"
        return ""

    @staticmethod
    def _clean_caption_text(text: str, limit: int = 180) -> str:
        text = re.sub(r"\r\n?", "\n", str(text or "")).strip()
        text = re.sub(r"^```(?:json|text|markdown)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
        text = text.strip(" \t\n\r\"'“”‘’")
        if len(text) > limit:
            text = text[:limit].rstrip(" \t\n\r，,。.!！?；;、") + "…"
        return text

    @staticmethod
    def _caption_text_usable(text: str) -> bool:
        text = re.sub(r"\s+", "", str(text or "")).strip("，,。.!！?；;、")
        return len(text) >= 4

    @classmethod
    def _prefer_caption_text(cls, candidate: str = "", current: str = "") -> str:
        candidate = cls._clean_caption_text(candidate)
        current = cls._clean_caption_text(current)
        if cls._caption_text_usable(candidate):
            return candidate
        if cls._caption_text_usable(current):
            return current
        return candidate or current

    @classmethod
    def _parse_stdout_caption(cls, stdout_text: str) -> str:
        caption = ""
        for line in str(stdout_text or "").splitlines():
            line = line.strip()
            if line.startswith("CAPTION:"):
                caption = cls._prefer_caption_text(line.split("CAPTION:", 1)[1].strip(), caption)
        return caption

    @classmethod
    def _request_caption(cls, body: dict) -> str:
        """Read caller-provided copy that should be shown as gallery 小心思."""
        if not isinstance(body, dict):
            return ""
        keys = (
            "caption",
            "thought",
            "small_thought",
            "smallThought",
            "inner_thought",
            "innerThought",
            "mind",
            "comment",
            "commentary",
            "copy",
            "copy_text",
            "copyText",
            "copywriting",
            "message",
            "text",
        )

        def _iter_sources(value):
            if isinstance(value, dict):
                yield value
                for nested_key in ("gallery", "meta", "metadata", "extra", "data"):
                    nested = value.get(nested_key)
                    if isinstance(nested, dict):
                        yield nested
            else:
                return

        for source in _iter_sources(body):
            for key in keys:
                value = source.get(key)
                if value is None:
                    continue
                text = cls._clean_caption_text(value)
                if text:
                    return text
        return ""

    @staticmethod
    def _fallback_hermes_caption(description: str, prompt: str = "") -> str:
        """Build a short Chinese 小心思 only when Hermes did not provide one."""
        description = str(description or "")
        prompt = str(prompt or "")
        source = re.sub(r"\s+", " ", description or prompt).strip(" ，,。.!！?；;、")
        if len(source) > 28:
            source = source[:28].rstrip(" ，,。.!！?；;、")
        combined = f"{description} {prompt}"
        lower = combined.lower()

        def has_any(words: tuple[str, ...]) -> bool:
            return any(word in combined or word.lower() in lower for word in words)

        rest_scene = has_any((
            "午休", "小憩", "打盹", "眯一会", "眯一会儿", "沙发", "毯子", "躺",
            "半闭", "休息", "nap", "sleepy", "drowsy", "couch", "sofa", "blanket",
            "lying back", "rest",
        ))
        if rest_scene:
            return "先靠着沙发眯一会儿，醒了再把后面的事慢慢接上。"

        meal_scene = has_any((
            "早餐", "午餐", "晚餐", "吃饭", "用餐", "便当", "饭团", "餐桌", "叉子",
            "筷子", "食物", "料理", "一口", "eating", "dining table", "fork",
            "chopsticks", "rice ball", "bento", "meal", "food", "cutlery",
        ))
        if meal_scene:
            return "先把这一口吃完，再把今天剩下的事慢慢接住。"
        if has_any(("desk", "电脑", "办公")):
            return "忙里偷出这一小会儿，感觉今天也能轻一点。"
        if has_any(("book", "书房", "复古")):
            return "这个角落刚好安静，适合把心情也放慢一点。"
        if source:
            return f"看着「{source}」这一刻，心里忽然有点想把它留住。"
        return "这一张先收进今天的小格子里，回头再慢慢看。"

    @staticmethod
    def _has_cjk(text: str) -> bool:
        return bool(re.search(r"[\u4e00-\u9fff]", str(text or "")))

    def _clean_display_description(self, text: str, limit: int = 120) -> str:
        """Normalize caller/LLM text into one Chinese gallery-facing description."""
        text = re.sub(r"\r\n?", "\n", str(text or "")).strip()
        text = re.sub(r"^```(?:json|text|markdown)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
        text = text.strip(" \t\n\r\"'“”‘’")
        if not text:
            return ""
        parts = self._parse_outfit_parts(text)
        for key in ("穿搭", "描述", "场景"):
            if parts.get(key):
                text = parts[key]
                break
        text = re.sub(r"^(?:中文)?(?:穿搭)?(?:描述|说明|文案|展示文案|outfit|description)\s*[：:]\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip(" ，,。.!！?；;、")
        if len(text) > limit:
            text = text[:limit].rstrip(" ，,。.!！?；;、") + "…"
        return text

    def _request_outfit_description(self, body: dict) -> str:
        """Read caller-provided outfit/scene copy for gallery display."""
        if not isinstance(body, dict):
            return ""
        keys = (
            "outfit_description",
            "outfit_text",
            "display_outfit",
            "display_description",
            "description",
            "summary",
            "outfit",
        )
        for key in keys:
            value = body.get(key)
            if value is None:
                continue
            text = self._clean_display_description(value)
            if text:
                return text
        return ""

    def _fallback_hermes_display_description(self, prompt: str, mode_label: str = "") -> str:
        prompt = str(prompt or "").strip()
        if self._has_cjk(prompt) and not re.search(r"[A-Za-z]{16,}", prompt):
            return self._clean_display_description(prompt)
        keywords = self._fallback_outfit_keywords_from_prompt(prompt)
        if keywords:
            return f"Hermes 自定义生图：{keywords}"
        mode = self._clean_display_description(mode_label) or "自定义生图"
        return f"Hermes {mode}：按原始描述生成的场景、动作和穿搭。"

    @classmethod
    def _is_usable_hermes_display_description(cls, text: str) -> bool:
        text = cls._clean_display_description_static(text)
        if not text or not cls._has_cjk(text):
            return False
        lower = text.lower()
        forbidden_markers = (
            "用户的指令",
            "用户要求",
            "我们被要求",
            "只输出",
            "不要英文",
            "不要解释",
            "提示词",
            "给定的",
            "下面的",
            "prompt",
        )
        if any(marker in lower for marker in forbidden_markers):
            return False
        return not bool(re.search(r"[A-Za-z]{4,}", text))

    @staticmethod
    def _clean_display_description_static(text: str, limit: int = 120) -> str:
        text = re.sub(r"\r\n?", "\n", str(text or "")).strip()
        text = re.sub(r"^```(?:json|text|markdown)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
        text = text.strip(" \t\n\r\"'“”‘’")
        text = re.sub(r"^(?:中文)?(?:穿搭)?(?:描述|说明|文案|展示文案|outfit|description)\s*[：:]\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip(" ，,。.!！?；;、")
        if len(text) > limit:
            text = text[:limit].rstrip(" ，,。.!！?；;、") + "…"
        return text

    async def _translate_hermes_prompt_for_display(self, prompt: str) -> str:
        prompt = re.sub(r"\s+", " ", str(prompt or "")).strip()
        if not prompt:
            return ""
        request_config = llm_request_config(self.config, self.data_dir)
        chat_url = request_config.get("chat_url", "")
        models = request_config.get("models") or []
        if not chat_url or not models:
            logger.warning("Hermes display description translation skipped: missing chat_url/models")
            return ""

        source_prompt = prompt[:1200]
        instruction = (
            "把下面 Hermes 生图 prompt 压缩成一条中文画廊展示描述。"
            "只输出中文一句话，45-90字，概括场景、动作、穿搭和发型；"
            "不要英文，不要解释，不要列表，不要复述画质参数。\n\n"
            f"Prompt:\n{source_prompt}"
        )
        headers = {"Content-Type": "application/json"}
        api_key = request_config.get("api_key", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        def _post_llm(model: str):
            import requests
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": instruction}],
                "max_tokens": 220,
                "temperature": 0.2,
            }
            resp = requests.post(chat_url, headers=headers, json=payload, timeout=5)
            if resp.status_code == 400:
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                if llm_temperature_param_error(body):
                    payload.pop("temperature", None)
                    return requests.post(chat_url, headers=headers, json=payload, timeout=5)
            return resp

        loop = asyncio.get_running_loop()
        for model in models[:1]:
            try:
                resp = await loop.run_in_executor(None, lambda m=model: _post_llm(m))
                if resp is None or resp.status_code != 200:
                    status = resp.status_code if resp is not None else "no response"
                    logger.warning("Hermes display description translation failed: model=%s status=%s", model, status)
                    continue
                data = resp.json()
                choices = data.get("choices") if isinstance(data, dict) else None
                if not choices:
                    logger.warning("Hermes display description translation invalid response: model=%s", model)
                    continue
                content = self._clean_display_description(llm_choice_text(choices[0]))
                if self._is_usable_hermes_display_description(content):
                    return content
            except Exception as e:
                logger.warning("Hermes display description translation error: model=%s err=%s", model, e)
        return ""

    async def _normalize_hermes_display_description(self, description: str, prompt: str, mode_label: str = "") -> str:
        description = self._clean_display_description(description)
        if self._is_usable_hermes_display_description(description):
            return description
        translated = await self._translate_hermes_prompt_for_display(prompt)
        if translated:
            return translated
        return self._fallback_hermes_display_description(prompt, mode_label)

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
                    return self._enrich_photo_schedule_time(entry, metadata)
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

    @staticmethod
    def _has_cjk(value: str) -> bool:
        return bool(re.search(r'[\u4e00-\u9fff]', value or ""))

    @staticmethod
    def _clean_activity_text(value: str, max_len: int = 56) -> str:
        text = re.sub(r'\s+', ' ', str(value or "")).strip().strip('"').strip("'")
        text = re.sub(r'^\d{1,2}:\d{2}\s*', '', text).strip()
        if not text:
            return ""

        lower = text.lower()
        leaked_markers = (
            "activity_zh",
            "image_prompt",
            "outfit_en",
            "reasoning_content",
            "json",
            "字段",
            "只输出",
            "当前时间",
            "我们根据",
            "所以当前",
            "可以确定",
            "当前活动",
        )
        if any(marker in lower for marker in leaked_markers):
            return ""
        if len(text) > max_len:
            return ""
        if not GalleryServer._has_cjk(text):
            return ""
        return text

    def _parse_generate_now_llm(self, text: str) -> tuple[str, str, str]:
        raw = (text or "").strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        activity = ""
        image_prompt = ""
        outfit_prompt = ""

        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(raw[start:end + 1])
                activity = str(data.get("activity_zh") or data.get("activity") or "").strip()
                image_prompt = str(
                    data.get("image_prompt_en")
                    or data.get("prompt_en")
                    or data.get("image_prompt")
                    or ""
                ).strip()
                outfit_prompt = str(
                    data.get("outfit_en")
                    or data.get("clothing_en")
                    or data.get("outfit")
                    or ""
                ).strip()
            except json.JSONDecodeError:
                pass

        if not activity and not image_prompt and raw and not self._has_cjk(raw):
            image_prompt = raw

        activity = self._clean_activity_text(activity)
        image_prompt = re.sub(r'\s+', ' ', image_prompt).strip().strip('"').strip("'")
        if self._has_cjk(image_prompt):
            image_prompt = ""
        outfit_prompt = re.sub(r'\s+', ' ', outfit_prompt).strip().strip('"').strip("'")
        if self._has_cjk(outfit_prompt):
            outfit_prompt = ""
        return activity, image_prompt, outfit_prompt

    @staticmethod
    def _fallback_visual_for_activity(activity: str, hour: int) -> tuple[str, str]:
        key = re.sub(r"\s+", "", str(activity or ""))
        if not key:
            return "", ""

        def has(words: tuple[str, ...]) -> bool:
            return any(word in key for word in words)

        meal = has(("早餐", "午餐", "晚餐", "早饭", "午饭", "晚饭", "吃饭", "用餐", "做饭", "料理", "厨房", "甜点", "松饼", "奶茶", "牛排"))
        stream = has(("直播", "开播", "歌会", "唱歌", "麦克风", "台本", "互动", "内容"))
        if meal and stream:
            return (
                "at home at a small dining table with a simple dinner, checking a laptop and handwritten livestream notes, warm apartment lighting, relaxed candid pose, cozy evening atmosphere",
                "soft white camisole dress, light cardigan, delicate necklace, simple slippers",
            )
        if meal:
            return (
                "enjoying or preparing a simple meal at home, tidy kitchen or dining table, warm natural light, gentle everyday pose, cozy domestic atmosphere, visible plates and small dishes",
                "cream knit cardigan, white camisole top, relaxed skirt, delicate necklace",
            )
        if stream:
            return (
                "sitting at a tidy livestream desk, reviewing notes beside a microphone and soft monitor glow, focused gentle expression, cozy room lighting, ready-to-start evening broadcast mood",
                "soft satin camisole, sheer cardigan, delicate earrings, neat skirt",
            )
        if has(("电脑", "游戏", "速通", "Live2D", "建模", "平板", "剪辑", "耳机")):
            return (
                "working at a desk with a laptop or tablet, screen glow on her face, focused casual pose, organized room, small tech accessories nearby, calm productive atmosphere",
                "oversized hoodie, pleated skirt, simple earrings, soft socks",
            )
        if has(("动漫", "新番", "追番", "电视", "沙发", "抱枕", "电影")):
            return (
                "curled up on a sofa watching anime or a show, holding a soft pillow, warm living room light, relaxed playful expression, cozy afternoon or evening atmosphere",
                "loose knit sweater, camisole dress, lace socks, small hair ribbon",
            )
        if has(("阳台", "摇椅", "小憩", "打盹", "发呆", "薄毯")):
            return (
                "resting on a balcony rocking chair under soft daylight, thin blanket over her lap, relaxed sleepy expression, plants nearby, quiet breezy home atmosphere",
                "light cardigan, soft camisole dress, delicate necklace, bare shoulders",
            )
        if has(("整理", "房间", "收拾", "浇水", "多肉", "植物", "打扫")):
            return (
                "tidying a bright room and watering small plants, natural window light, gentle focused pose, clean shelves and soft home details, peaceful everyday atmosphere",
                "white blouse, high-waisted skirt, soft cardigan, simple flats",
            )
        if has(("床", "睡", "睡前", "护肤", "洗澡", "泡澡", "被窝", "枕头", "晚安")):
            return (
                "winding down in a cozy bedroom after skincare, soft bedside lamp, relaxed sleepy pose near a bed with pillows, quiet intimate late-night atmosphere",
                "white lace camisole sleep dress, soft robe, delicate bracelet",
            )
        if has(("咖啡", "咖啡馆", "下午茶", "蛋糕", "茶")):
            return (
                "sitting in a quiet cafe with a drink and small dessert, soft window light, relaxed candid pose, warm table details, gentle slow-life atmosphere",
                "fitted knit top, high-waisted skirt, small shoulder bag, earrings",
            )
        if has(("街", "散步", "路灯", "公园", "出门", "逛", "夜市", "外出")):
            return (
                "taking a relaxed walk outside under city lights, gentle candid pose near a sidewalk or park path, soft evening breeze, lively but romantic urban atmosphere",
                "elegant satin slip dress, sheer lace cardigan, delicate necklace, low heels",
            )
        if hour < 11:
            return (
                "doing a quiet morning routine at home in soft window light, natural relaxed pose, tidy room and mirror nearby, calm start-of-day atmosphere",
                "cream knit cardigan, white camisole top, light pleated skirt, mary jane shoes",
            )
        if hour < 18:
            return (
                "spending a relaxed daytime moment indoors with soft sunlight, natural candid pose, small personal items nearby, clean cozy everyday atmosphere",
                "fitted crop top, high-waisted wide-leg trousers, simple earrings",
            )
        return (
            "spending a calm evening at home in warm ambient light, relaxed candid pose, cozy room details, gentle private atmosphere, soft shadows around her",
            "elegant satin slip dress, light cardigan, delicate necklace, low heels",
        )

    @classmethod
    def _generate_now_prompt_conflicts(cls, activity: str, image_prompt: str) -> bool:
        key = re.sub(r"\s+", "", str(activity or ""))
        prompt = str(image_prompt or "").lower()
        if not key or not prompt:
            return False

        def activity_has(words: tuple[str, ...]) -> bool:
            return any(word in key for word in words)

        def prompt_has(words: tuple[str, ...]) -> bool:
            return any(word in prompt for word in words)

        outdoor_prompt = prompt_has(("street", "sidewalk", "road", "city lights", "night market", "park path", "railing", "outdoor walk", "evening walk"))
        home_prompt = prompt_has(("home", "apartment", "bedroom", "kitchen", "dining table", "livestream desk", "sofa", "living room", "bedside"))
        meal_prompt = prompt_has(("meal", "dinner", "breakfast", "lunch", "supper", "food", "cooking", "kitchen", "dining", "table", "plates", "dishes", "dessert"))
        stream_prompt = prompt_has(("livestream", "microphone", "broadcast", "streaming", "monitor glow", "notes", "script", "desk setup"))

        home_activity = activity_has(("在家", "家里", "房间", "卧室", "厨房", "晚餐", "晚饭", "直播", "开播", "追番", "沙发", "睡前", "护肤", "泡澡"))
        outdoor_activity = activity_has(("街", "散步", "路灯", "公园", "出门", "逛", "夜市", "外出", "户外", "路边", "夜景", "栏杆", "城市"))
        meal_activity = activity_has(("早餐", "午餐", "晚餐", "早饭", "午饭", "晚饭", "吃饭", "用餐", "做饭", "料理"))
        stream_activity = activity_has(("直播", "开播", "歌会", "唱歌", "麦克风", "台本", "互动", "内容"))

        if home_activity and outdoor_prompt and not home_prompt:
            return True
        if outdoor_activity and home_prompt and not outdoor_prompt:
            return True
        if meal_activity and not meal_prompt:
            return True
        if stream_activity and not stream_prompt:
            return True
        return False

    def _fallback_generate_now_context(self, now_str: str, schedule_text: str = "") -> tuple[str, str, str]:
        time_value, nearest_activity = "", ""
        target_time, _ = self._parse_time_activity(now_str)
        if target_time and schedule_text:
            target_score = self._time_sort_value(target_time)
            candidates = []
            for line in schedule_text.splitlines():
                item_time, activity = self._parse_time_activity(line)
                if item_time and activity:
                    candidates.append((abs(self._time_sort_value(item_time) - target_score), activity))
            if candidates:
                _, nearest_activity = min(candidates, key=lambda item: item[0])

        hour = int((target_time or now_str or "00:00").split(":", 1)[0])
        if nearest_activity:
            prompt, outfit = self._fallback_visual_for_activity(nearest_activity, hour)
            if prompt and outfit:
                return nearest_activity, prompt, outfit

        if 0 <= hour < 6 or hour >= 22:
            activity = nearest_activity or "在柔软床边安静放松准备入睡"
            prompt = "relaxing beside a soft bed late at night, sleepy gentle expression, cozy bedroom, warm bedside lamp, quiet intimate atmosphere"
            outfit = "soft white lace camisole sleep dress, delicate lace trim, partly covered by a white duvet"
        elif hour < 11:
            activity = nearest_activity or "在晨光里整理今天的穿搭"
            prompt = "arranging today's outfit in soft morning light, relaxed natural pose, tidy bedroom mirror, warm calm atmosphere"
            outfit = "cream knit cardigan, white camisole top, light blue pleated skirt, beige mary jane shoes"
        elif hour < 18:
            activity = nearest_activity or "在午后阳光里享受轻松日常"
            prompt = "enjoying a relaxed afternoon moment, casual natural pose, bright cafe or city street setting, clean daylight atmosphere"
            outfit = "fitted crop top, high-waisted wide-leg trousers, small shoulder bag, simple earrings"
        else:
            activity = nearest_activity or "在傍晚灯光下散步放松"
            prompt = "taking a relaxed evening walk under warm city lights, gentle candid pose, softly glowing street scene, cozy dusk atmosphere"
            outfit = "elegant satin slip dress, sheer lace cardigan, delicate necklace, low heels"
        return activity, prompt, outfit

    def _today_schedule_entry(self, today_str: str = "") -> dict:
        today_str = today_str or date.today().isoformat()
        try:
            all_data = ScheduleStore(self.data_dir).load()
        except Exception as e:
            logger.error("Load today schedule for generate-now failed: %s", e)
            return {}

        daily = all_data.get(today_str)
        if self._has_usable_schedule(daily):
            return daily
        for entry in all_data.values():
            if (
                isinstance(entry, dict)
                and not entry.get("image_filename")
                and entry.get("date") == today_str
                and self._has_usable_schedule(entry)
            ):
                return entry
        return {}

    def _nearest_schedule_item(self, schedule_text: str, now: datetime) -> dict:
        """Pick the schedule item closest to current clock time."""
        target_minutes = now.hour * 60 + now.minute
        candidates = []
        for line in str(schedule_text or "").splitlines():
            time_text, activity = self._parse_time_activity(line)
            if not time_text or not activity:
                continue
            hour, minute = [int(part) for part in time_text.split(":", 1)]
            slot_minutes = hour * 60 + minute
            candidates.append({
                "time": time_text,
                "activity": activity,
                "distance": abs(slot_minutes - target_minutes),
                "minutes": slot_minutes,
            })
        if not candidates:
            return {}
        return min(candidates, key=lambda item: (item["distance"], item["minutes"]))

    def _schedule_items_for_inference(self, schedule_text: str) -> list[dict]:
        items = []
        for line in str(schedule_text or "").splitlines():
            time_text, activity = self._parse_time_activity(line)
            if not time_text or not activity:
                continue
            hour, minute = [int(part) for part in time_text.split(":", 1)]
            items.append({
                "time": time_text,
                "activity": activity,
                "minutes": hour * 60 + minute,
            })
        items.sort(key=lambda item: item["minutes"])
        return items

    @staticmethod
    def _compact_schedule_phrase(value: str, limit: int = 24) -> str:
        text = re.sub(r"\s+", "", str(value or ""))
        text = re.sub(r"[。！？!?，,；;：:、]+$", "", text)
        return text[:limit] if len(text) > limit else text

    def _fallback_generate_now_detail(self, now: datetime, daily: dict) -> dict:
        """Build a schedule-derived current moment if the LLM is unavailable."""
        now_str = now.strftime("%H:%M")
        target = now.hour * 60 + now.minute
        items = self._schedule_items_for_inference(daily.get("schedule", "") if daily else "")
        prev_item = None
        next_item = None
        for item in items:
            if item["minutes"] <= target:
                prev_item = item
            elif item["minutes"] > target and next_item is None:
                next_item = item

        if prev_item and next_item:
            prev_text = self._compact_schedule_phrase(prev_item["activity"])
            next_text = self._compact_schedule_phrase(next_item["activity"])
            prev_delta = target - prev_item["minutes"]
            next_delta = next_item["minutes"] - target
            if next_delta <= 90:
                if next_text.startswith(("准备", "开始", "处理", "整理", "完成", "戴上", "冲泡")):
                    activity = f"收起手头的事，开始{next_text}"
                else:
                    activity = f"收起手头的事，为接下来的{next_text}做准备"
            elif prev_delta <= 45:
                activity = f"继续{prev_text}"
            else:
                activity = f"把{prev_text}收尾，准备接下来的{next_text}"
            activity_en = "transitioning naturally between the previous schedule item and the upcoming schedule item"
        elif next_item:
            next_text = self._compact_schedule_phrase(next_item["activity"])
            activity = f"提前为{next_text}做准备"
            activity_en = "preparing for the upcoming schedule item"
        elif prev_item:
            prev_text = self._compact_schedule_phrase(prev_item["activity"])
            activity = f"完成{prev_text}后放慢节奏整理收尾"
            activity_en = "winding down after the final schedule item"
        else:
            activity = "根据今日日程整理当下要做的事"
            activity_en = "organizing the current moment according to today's schedule"

        return {
            "time": now_str,
            "activity_zh": activity,
            "activity_en": activity_en,
            "action_en": "a natural candid action derived from the current point in today's schedule",
            "scene_en": "the realistic setting implied by the surrounding schedule items at the current time",
            "props_en": "only props that fit the inferred current activity and today's schedule",
            "outfit_en": "the outfit from today's schedule plan",
            "hair_en": "the hairstyle from today's schedule plan",
            "lighting_en": "realistic lighting matching the current time of day",
        }

    @staticmethod
    def _clean_generate_now_en(value: str, limit: int = 220) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip().strip('"').strip("'")
        if not text or GalleryServer._has_cjk(text):
            return ""
        if len(text) > limit:
            text = text[:limit].rstrip(" ,.;:")
        return text

    def _parse_generate_now_detail(self, text: str, now_str: str) -> dict:
        raw = (text or "").strip().replace("```json", "").replace("```", "").strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            logger.warning("Generate-now LLM returned invalid JSON: %s", raw[:240])
            return {}
        if not isinstance(data, dict):
            return {}

        parsed_time, _ = self._parse_time_activity(str(data.get("time") or ""))
        activity = self._clean_activity_text(str(data.get("activity_zh") or data.get("activity") or ""), max_len=72)
        if not activity:
            return {}

        detail = {
            "time": parsed_time or now_str,
            "activity_zh": activity,
        }
        for field in ("activity_en", "action_en", "scene_en", "props_en", "outfit_en", "hair_en", "lighting_en"):
            cleaned = self._clean_generate_now_en(data.get(field, ""))
            if cleaned:
                detail[field] = cleaned
        return detail

    def _build_generate_now_inference_prompt(self, now_str: str, daily: dict) -> str:
        schedule = str(daily.get("schedule") or "").strip()
        schedule_prompt = str(daily.get("schedule_prompt") or "").strip()
        outfit = str(daily.get("outfit") or "").strip()
        outfit_style = str(daily.get("outfit_style") or "").strip()
        base_style = str(daily.get("base_style") or "").strip()
        details = daily.get("schedule_details") if isinstance(daily.get("schedule_details"), list) else []
        details_json = json.dumps(details, ensure_ascii=False, indent=2)
        return f"""你是“现在在干嘛”即时生图链路里的日程推断层。
请根据【当前时间】和【今日日程】推断这一刻最自然、最具体、适合生图的动作场景。

【当前时间】
{now_str}

【今日日程（中文展示）】
{schedule}

【生图用日程/英文明细，可为空】
{schedule_prompt or "（无）"}

【结构化日程明细，可为空】
{details_json}

【今日穿搭】
风格：{outfit_style or base_style or "（未写明）"}
{outfit or "（未写明）"}

输出规则：
1. 只输出一个 JSON 对象，不要 Markdown，不要解释。
2. time 必须等于当前时间 "{now_str}"。
3. activity_zh 要自然，像这个时间她心里真的正在做的事；可以参考前后日程推断过渡动作。
4. 不要机械照抄最近一条日程，除非当前时间确实还在做那件事。
5. 不要发散到全天日程外的新活动；所有动作、场景、道具都必须能从今日计划合理推出来。
6. 英文字段必须是纯英文，用于生图；不要写中文。
7. action_en / scene_en / props_en 要让画面明确，但不要强制看镜头；是否看镜头由动作自然决定。
8. outfit_en / hair_en 参考今日穿搭和结构化明细，保持同一天一致。

JSON 格式：
{{
  "time": "{now_str}",
  "activity_zh": "此刻具体在做什么",
  "activity_en": "English summary of the inferred current activity",
  "action_en": "specific visible body/hand action",
  "scene_en": "specific realistic location and surroundings",
  "props_en": "specific props related to the inferred action",
  "outfit_en": "today's outfit in concise English",
  "hair_en": "today's hairstyle in concise English",
  "lighting_en": "realistic lighting for this time"
}}"""

    async def _infer_generate_now_detail(self, now: datetime, daily: dict) -> dict:
        now_str = now.strftime("%H:%M")
        fallback = self._fallback_generate_now_detail(now, daily)
        request_config = llm_request_config(self.config, self.data_dir)
        chat_url = request_config.get("chat_url", "")
        models = request_config.get("models") or []
        if not chat_url or not models:
            logger.warning("Generate-now LLM inference skipped: missing chat_url/models")
            return fallback

        headers = {"Content-Type": "application/json"}
        api_key = request_config.get("api_key", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        prompt = self._build_generate_now_inference_prompt(now_str, daily)

        def _post_llm(model: str):
            import requests
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 900,
                "temperature": 0.25,
            }
            resp = requests.post(chat_url, headers=headers, json=payload, timeout=45)
            if resp.status_code == 400:
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                if llm_temperature_param_error(body):
                    payload.pop("temperature", None)
                    return requests.post(chat_url, headers=headers, json=payload, timeout=45)
            return resp

        loop = asyncio.get_running_loop()
        for model in models:
            try:
                resp = await loop.run_in_executor(None, lambda m=model: _post_llm(m))
                if resp is None or resp.status_code != 200:
                    status = resp.status_code if resp is not None else "no response"
                    detail = ""
                    if resp is not None:
                        try:
                            body = resp.json()
                            error = body.get("error") if isinstance(body, dict) else None
                            if isinstance(error, dict):
                                detail = str(error.get("message") or error.get("code") or "")[:240]
                            elif isinstance(body, dict):
                                detail = str(body.get("msg") or body.get("status") or "")[:240]
                        except Exception:
                            detail = (resp.text or "")[:240]
                    logger.warning(
                        "Generate-now LLM inference failed: model=%s status=%s detail=%s",
                        model,
                        status,
                        detail,
                    )
                    continue
                data = resp.json()
                choices = data.get("choices") if isinstance(data, dict) else None
                if not choices:
                    logger.warning("Generate-now LLM inference invalid response: model=%s", model)
                    continue
                content = llm_choice_text(choices[0])
                parsed = self._parse_generate_now_detail(content, now_str)
                if parsed:
                    merged = dict(fallback)
                    merged.update(parsed)
                    merged["time"] = now_str
                    return merged
            except Exception as e:
                logger.warning("Generate-now LLM inference error: model=%s err=%s", model, e)
        return fallback

    @staticmethod
    def _theme_for_schedule_time(time_text: str) -> str:
        match = re.match(r"^\s*(\d{1,2}):(\d{2})", str(time_text or ""))
        if not match:
            return "morning"
        hour = int(match.group(1))
        if 0 <= hour < 6:
            return "bedtime"
        if hour < 12:
            return "morning"
        if hour < 18:
            return "noon"
        if hour <= 20:
            return "evening"
        return "bedtime"

    async def handle_generate_now(self, request: web.Request):
        """根据今日日程的当前时间点生图 (💭 现在在干嘛)"""
        proc = None
        try:
            # B1 (2026-06-26): 可选 extra_hint，让聊天生图能在日程活动上叠加主人微调
            extra_hint = ""
            try:
                if request.can_read_body:
                    body = await request.json()
                    if isinstance(body, dict):
                        extra_hint = str(body.get("extra_hint", "") or "").strip()[:200]
            except Exception:
                extra_hint = ""

            now = datetime.now()
            now_str = now.strftime("%H:%M")
            today_str = now.strftime("%Y-%m-%d")
            logger.info("Generate now: time=%s, extra_hint=%r, using today's schedule chain", now_str, extra_hint)

            # 1) 读取今日日程，并让 LLM 基于全天计划推断当前具体活动。
            daily = self._today_schedule_entry(today_str)
            schedule_text = daily.get("schedule", "") if daily else ""
            if not schedule_text:
                return web.json_response({
                    "error": "schedule_missing",
                    "message": "请先刷新今日日程，再使用“现在在干嘛”。",
                }, status=400)
            if not self._schedule_items_for_inference(schedule_text):
                return web.json_response({
                    "error": "schedule_time_not_found",
                    "message": "今日日程里没有可用于生图的时间点。",
                }, status=400)

            now_detail = await self._infer_generate_now_detail(now, daily)
            now_activity = self._clean_activity_text(now_detail.get("activity_zh", ""), max_len=72)
            if not now_activity:
                return web.json_response({
                    "error": "generate_now_inference_failed",
                    "message": "当前活动推断失败，请先刷新今日日程后再试。",
                }, status=500)

            now_detail["time"] = now_str
            now_detail["activity_zh"] = now_activity
            # B1 (2026-06-26): 把主人微调 hint 叠加进活动与日程时间，影响 prompt/参考图选择
            if extra_hint:
                now_activity = self._clean_activity_text(f"{now_activity}，{extra_hint}", max_len=96)
                now_detail["activity_zh"] = now_activity
                now_detail["extra_hint"] = extra_hint
            schedule_time = f"{now_str} {now_activity}".strip()
            schedule_detail_json = json.dumps(now_detail, ensure_ascii=False, separators=(",", ":"))
            theme = self._theme_for_schedule_time(now_str)
            base_style = str(daily.get("base_style") or "").strip().lower()
            if base_style not in {"cool", "girly", "sweet"}:
                base_style = ""

            keys_config = self._load_api_keys_config()
            plugin_config = self._load_plugin_config()
            gpt_key = keys_config.get("gpt_key", "") or os.environ.get("GPT_IMAGE_API_KEY", "")
            raw_configured_gpt_base_url = str(self.config.get("image_gen", {}).get("gpt_base_url", "") or "").strip()
            configured_gpt_base_url = self._configured_image_base_url(
                raw_configured_gpt_base_url
            )
            local_gpt_base_url = str(keys_config.get("gpt_base_url", "") or "").strip()
            if (
                local_gpt_base_url == raw_configured_gpt_base_url
                or self._gpt_image_endpoint_identity(local_gpt_base_url) == self._gpt_image_endpoint_identity(configured_gpt_base_url)
            ):
                local_gpt_base_url = ""
            gpt_base_url = (
                local_gpt_base_url
                or os.environ.get("GPT_IMAGE_BASE_URL", "")
                or configured_gpt_base_url
            )
            gitee_keys = plugin_config.get("gitee_config", {}).get("api_keys", [])
            gitee_key = gitee_keys[0] if gitee_keys else ""
            gpt_available = bool(str(gpt_base_url or "").strip() or str(gpt_key or "").strip())
            if not gpt_available and not gitee_key:
                return web.json_response({
                    "error": "missing_image_key",
                    "message": "请先在设置里配置 GPT Image Base URL 或 Gitee Key，再使用“现在在干嘛”。",
                }, status=400)

            # 2) 调用统一日程生图链路。generate.py 会根据 schedule_time 读取
            # schedule_prompt / schedule_details，并使用当天 outfit/reference context。
            request_config = llm_request_config(self.config, self.data_dir)
            cpa_base_url = request_config["base_url"]
            cpa_key = request_config["api_key"]
            generate_script = self._generate_script()
            engine = self.config.get("image_gen", {}).get("default_engine", "gptimage") if gpt_available else "gitee"
            child_env_extra = {}
            if gpt_base_url:
                child_env_extra["GPT_IMAGE_BASE_URL"] = gpt_base_url
            if cpa_key:
                child_env_extra["CPA_API_KEY"] = cpa_key
            if gpt_key or cpa_key:
                child_env_extra["GPT_IMAGE_API_KEY"] = gpt_key or cpa_key
            gpt_image_endpoints = keys_config.get("gpt_image_endpoints") or []
            if isinstance(gpt_image_endpoints, list) and gpt_image_endpoints:
                child_env_extra["GPT_IMAGE_ENDPOINTS"] = json.dumps(gpt_image_endpoints, ensure_ascii=False)
            if cpa_base_url:
                child_env_extra["CPA_BASE_URL"] = cpa_base_url
            child_env = self._child_env(child_env_extra)
            selected_reference = {}
            if engine == "gptimage":
                reference_context = json.dumps({
                    "source": "generate_now",
                    "time": now_str,
                    "activity": now_activity,
                    "outfit_style": daily.get("outfit_style", ""),
                    "outfit": daily.get("outfit", ""),
                    "reference_query": daily.get("reference_query", ""),
                    "prompt": daily.get("prompt", ""),
                    "schedule_time": schedule_time,
                    "schedule_detail": now_detail,
                }, ensure_ascii=False)
                loop = asyncio.get_running_loop()
                selected_reference = await loop.run_in_executor(
                    None,
                    lambda: self._select_reference_for_generation_sync(
                        reference_context,
                        include_wardrobe=False,
                    ),
                )
            cmd = [
                self._python_executable(),
                generate_script,
                "--theme", theme,
                "--caption",
                "--source", "web",
                "--engine", engine,
                "--schedule-time", schedule_time,
                "--schedule-detail-json", schedule_detail_json,
            ]
            if selected_reference.get("path") and engine == "gptimage":
                cmd.extend(["--ref-image", selected_reference["path"]])
                logger.info(
                    "Generate now selected reference: %s mode=%s",
                    selected_reference.get("label") or selected_reference.get("filename"),
                    selected_reference.get("selection_mode", ""),
                )
            else:
                cmd.append("--no-auto-style")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.path.dirname(generate_script),
                env=child_env,
            )
            process_timeout = image_process_timeout(self.config, with_reference_fallback=True)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=process_timeout)

            if proc.returncode != 0:
                logger.error(f"generate.py failed: {stderr.decode(errors='replace')[-500:]}")
                detail = stderr.decode(errors='replace')[-500:]
                if "GPT_IMAGE_API_KEY or gpt_key is required" in detail:
                    return web.json_response({
                        "error": "missing_image_key",
                        "message": "请先在设置里配置 GPT Image Base URL 或 Gitee Key，再使用“现在在干嘛”。",
                    }, status=400)
                return web.json_response({
                    "error": "generate_failed",
                    "message": "生图失败，请检查 GPT Image/Gitee 配置或稍后重试。",
                    "detail": detail[-300:],
                }, status=500)

            stdout_text = stdout.decode(errors='replace')
            # Parse SUCCESS:<path> from output
            m = re.search(r"SUCCESS:(.+)", stdout_text)
            if not m:
                return web.json_response({"error": "no_output"}, status=500)

            image_path = m.group(1).strip()
            filename = os.path.basename(image_path)

            # Parse caption if present
            caption_text = self._parse_stdout_caption(stdout_text)

            # Update schedule_data.json: set source="web" for this entry
            store = ScheduleStore(self.data_dir)
            def _update_source(all_data):
                if filename in all_data:
                    all_data[filename]["source"] = "web"
                    all_data[filename]["schedule_time"] = schedule_time
                    all_data[filename]["schedule_now_detail"] = now_detail
                    all_data[filename]["time"] = all_data[filename].get("time") or now_str
                    for field in ("outfit_style", "base_style", "reference_query"):
                        value = daily.get(field)
                        if value:
                            all_data[filename][field] = value
                    if selected_reference:
                        all_data[filename]["selected_reference"] = {
                            key: selected_reference.get(key, "")
                            for key in ("id", "filename", "url", "label", "prompt", "source", "selection_mode", "selection_reason")
                        }
                    if caption_text:
                        all_data[filename]["caption"] = caption_text
                return all_data
            try:
                store.update(_update_source)
            except Exception as e:
                logger.error(f"Update source error: {e}")

            return web.json_response({
                "status": "ok",
                "theme": theme,
                "filename": filename,
                "image_path": f"/images/{filename}",
                "caption": caption_text,
                "source": "web",
                "schedule_time": schedule_time,
                "schedule_now_detail": now_detail,
                "outfit_style": daily.get("outfit_style", ""),
                "base_style": base_style,
                "selected_reference": {
                    key: selected_reference.get(key, "")
                    for key in ("id", "filename", "url", "label", "source", "selection_mode", "selection_reason")
                } if selected_reference else {},
            })
        except asyncio.TimeoutError:
            process_timeout = image_process_timeout(self.config, with_reference_fallback=True)
            logger.error(f"Generate now timeout ({process_timeout}s)")
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            return web.json_response({"error": "timeout", "message": f"生图请求超时（{process_timeout}s）"}, status=504)
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
            size = normalize_custom_image_size(
                body.get("size", ""),
                body.get("aspect", ""),
                body.get("resolution", ""),
            )
            shot_type = normalize_custom_shot_type(body.get("shot_type", ""))
            pure_raw = body.get("pure", False)
            if isinstance(pure_raw, str):
                pure = pure_raw.strip().lower() in {"1", "true", "yes", "on"}
            else:
                pure = bool(pure_raw)
            raw_ref_image = body.get("ref_image", "")
            ref_image = self._resolve_reference_image(raw_ref_image, allow_any_path=True)
            if raw_ref_image and not ref_image:
                return web.json_response({"error": "invalid_ref_image"}, status=400)
            api_source = self._normalize_api_source(
                body.get("api_source"),
                body.get("source"),
                body.get("caller"),
                body.get("client"),
                request.headers.get("X-API-Source"),
                request.headers.get("X-Hermes-Source"),
                request.headers.get("X-Caller"),
                request.headers.get("User-Agent"),
            )
            if not api_source:
                referer = str(request.headers.get("Referer", "") or "").lower()
                sec_fetch_site = str(request.headers.get("Sec-Fetch-Site", "") or "").lower()
                if referer.startswith(("http://localhost", "http://127.0.0.1")) or sec_fetch_site in {"same-origin", "same-site"}:
                    api_source = "custom_ui"
                else:
                    api_source = "hermes"
            api_caption = self._request_caption(body)
            api_description = self._request_outfit_description(body)
            if api_source == "hermes":
                api_description = await self._normalize_hermes_display_description(
                    api_description,
                    user_prompt,
                    "自定义生图",
                )
                if not api_caption:
                    api_caption = self._fallback_hermes_caption(api_description, user_prompt)
            image_model = self._normalize_image_model_id(body.get("model") or body.get("image_model") or body.get("gpt_model"))
            raw_image_model = str(body.get("model") or body.get("image_model") or body.get("gpt_model") or "").strip()
            if raw_image_model and not image_model and raw_image_model.lower() not in {"default", "auto", "current"}:
                return web.json_response({"error": "invalid_image_model"}, status=400)
            entry = await self.on_generate_custom(user_prompt, size, ref_image, shot_type, pure, api_source, api_caption, image_model, api_description)
            if entry and entry.status == "ok":
                payload = entry.to_dict()
                try:
                    payload = self._normalize_entry_display(payload, self._load_image_metadata())
                except Exception as e:
                    logger.warning("Normalize custom generate response failed: %s", e)
                return web.json_response(payload)
            return web.json_response({"error": "generate_failed"}, status=500)
        except Exception as e:
            logger.error(f"Custom generate error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_cleanup_images(self, request: web.Request):
        """Preview or delete old non-favorite gallery images."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            days = self._cleanup_days_from_body(body if isinstance(body, dict) else {})
            dry_run = bool((body or {}).get("dry_run", True))
            plan = self._cleanup_image_plan(days)
            candidates = plan["candidates"]

            if dry_run:
                return web.json_response({
                    "success": True,
                    "dry_run": True,
                    **plan,
                })

            deleted_filenames = []
            errors = []
            for item in candidates:
                filename = item["filename"]
                _, delete_errors = self._delete_image_files(filename)
                if delete_errors:
                    errors.extend(delete_errors)
                    if self._image_exists(filename):
                        continue
                deleted_filenames.append(filename)

            deleted_set = set(deleted_filenames)
            if deleted_set:
                store = ScheduleStore(self.data_dir)

                def _remove_deleted_entries(all_data):
                    for key, entry in list(all_data.items()):
                        if key in deleted_set:
                            del all_data[key]
                            continue
                        if isinstance(entry, dict) and entry.get("image_filename") in deleted_set:
                            del all_data[key]
                    return all_data

                store.update(_remove_deleted_entries)

                metadata = self._load_image_metadata()
                changed = False
                for filename in deleted_set:
                    if filename in metadata:
                        del metadata[filename]
                        changed = True
                if changed:
                    self._save_image_metadata(metadata)

            return web.json_response({
                "success": True,
                "dry_run": False,
                **plan,
                "deleted_count": len(deleted_filenames),
                "deleted": deleted_filenames,
                "errors": errors,
            })
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            logger.error(f"Cleanup images error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def handle_delete_image(self, request: web.Request):
        """删除图片和条目"""
        img_id = request.match_info.get("img_id")
        # Path traversal validation: only allow safe characters
        if not img_id or not re.match(r'^[a-zA-Z0-9_.-]+$', img_id) or '..' in img_id:
            return web.json_response({"error": "invalid_filename"}, status=400)
        try:
            # 1. Delete image file
            self._delete_image_files(img_id)

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

            metadata = self._load_image_metadata()
            if img_id in metadata:
                del metadata[img_id]
                self._save_image_metadata(metadata)

            return web.json_response({"success": True})
        except Exception as e:
            logger.error(f"Delete image error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_reroll_image(self, request: web.Request):
        """Generate a fresh image from an existing gallery card."""
        img_id = request.match_info.get("img_id")
        if not img_id or not re.match(r'^[a-zA-Z0-9_.-]+$', img_id) or '..' in img_id:
            return web.json_response({"error": "invalid_filename"}, status=400)
        if not self.on_reroll_image:
            return web.json_response({"error": "reroll_unavailable"}, status=503)
        try:
            entry = await self.on_reroll_image(img_id)
            if not entry or entry.get("status") != "ok":
                return web.json_response(
                    {"error": (entry or {}).get("error") or "generate_failed"},
                    status=500 if (entry or {}).get("error") != "not_found" else 404,
                )
            metadata = self._load_image_metadata()
            normalized = self._enrich_photo_schedule_time(entry, metadata)
            return web.json_response(normalized)
        except Exception as e:
            logger.error(f"Reroll image error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    @staticmethod
    def _coerce_bool(value, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "y", "on"}:
                return True
            if normalized in {"0", "false", "no", "n", "off"}:
                return False
        return default

    @staticmethod
    def _find_image_entry(all_data: dict, img_id: str) -> tuple[Optional[str], Optional[dict]]:
        entry = all_data.get(img_id)
        if isinstance(entry, dict):
            return img_id, entry
        for key, item in all_data.items():
            if isinstance(item, dict) and item.get("image_filename") == img_id:
                return key, item
        return None, None

    @staticmethod
    def _sync_timestamp() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _pending_picxazz_sync_state(self) -> dict:
        cfg = self.picxazz_sync.load_config()
        return {
            "status": "pending",
            "base_url": cfg.base_url,
            "updated_at": self._sync_timestamp(),
        }

    def _save_picxazz_sync_result(self, img_id: str, result: dict):
        store = ScheduleStore(self.data_dir)

        def _update(all_data):
            key, entry = self._find_image_entry(all_data, img_id)
            if not key or not entry:
                return all_data
            entry["picxazz_sync"] = result
            if result.get("status") == "synced":
                entry["picxazz_url"] = result.get("url", "")
                entry["picxazz_key"] = result.get("key", "")
                entry.pop("picxazz_error", None)
            elif result.get("error"):
                entry["picxazz_error"] = result.get("error")
            all_data[key] = entry
            return all_data

        store.update(_update)

    async def _sync_image_to_picxazz(self, img_id: str, entry: dict, force: bool = False) -> dict:
        image_path = self._image_file_path(img_id)
        result = await self.picxazz_sync.upload(image_path, entry, force=force)
        if not result.get("skipped"):
            try:
                self._save_picxazz_sync_result(img_id, result)
            except Exception as exc:
                logger.error(f"Save picxazz sync result failed for {img_id}: {exc}")
        return result

    def _start_picxazz_sync_task(self, img_id: str, entry: dict):
        task = asyncio.create_task(self._sync_image_to_picxazz(img_id, dict(entry)))

        def _log_task_error(done_task: asyncio.Task):
            try:
                done_task.result()
            except Exception as exc:
                logger.error(f"Picxazz background sync failed for {img_id}: {exc}")

        task.add_done_callback(_log_task_error)

    async def handle_toggle_favorite(self, request: web.Request):
        """切换收藏状态，并在收藏后自动同步到 picxazz。"""
        img_id = request.match_info.get("img_id")
        if not img_id or not re.match(r'^[a-zA-Z0-9_.-]+$', img_id) or '..' in img_id:
            return web.json_response({"error": "invalid_filename"}, status=400)
        try:
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            requested_fav = payload.get("favorite") if isinstance(payload, dict) else None
            store = ScheduleStore(self.data_dir)
            result = {"new_fav": None, "entry": None, "picxazz_sync": None}

            def _toggle(all_data):
                key, entry = self._find_image_entry(all_data, img_id)
                if not key or not entry:
                    return all_data
                current_fav = entry.get("favorite", False)
                new_fav = self._coerce_bool(requested_fav, not current_fav) if requested_fav is not None else not current_fav
                entry["favorite"] = new_fav
                if new_fav and not self.picxazz_sync.should_skip(entry):
                    entry["picxazz_sync"] = self._pending_picxazz_sync_state()
                all_data[key] = entry
                result["new_fav"] = new_fav
                result["entry"] = dict(entry)
                result["picxazz_sync"] = dict(entry.get("picxazz_sync") or {})
                return all_data

            store.update(_toggle)
            if result["new_fav"] is None:
                return web.json_response({"error": "not_found"}, status=404)
            if result["new_fav"] and result["entry"] and result["picxazz_sync"].get("status") == "pending":
                self._start_picxazz_sync_task(img_id, result["entry"])
            return web.json_response({
                "success": result["new_fav"],
                "favorite": result["new_fav"],
                "picxazz_sync": result["picxazz_sync"],
            })
        except Exception as e:
            logger.error(f"Toggle favorite error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_sync_picxazz_favorites(self, request: web.Request):
        """Upload all current favorite images to picxazz."""
        try:
            force = False
            limit = 0
            if request.can_read_body:
                try:
                    body = await request.json()
                    if isinstance(body, dict):
                        force = self._coerce_bool(body.get("force"), False)
                        if body.get("limit") is not None:
                            limit = max(0, int(body.get("limit")))
                except Exception:
                    pass

            entries = [
                entry for entry in self._load_all_entries()
                if entry.get("favorite") and entry.get("image_filename")
            ]
            if limit:
                entries = entries[:limit]

            results = []
            for entry in entries:
                img_id = entry.get("image_filename", "")
                sync_result = await self._sync_image_to_picxazz(img_id, entry, force=force)
                results.append({
                    "image_filename": img_id,
                    "status": sync_result.get("status"),
                    "skipped": bool(sync_result.get("skipped")),
                    "url": sync_result.get("url", ""),
                    "error": sync_result.get("error", ""),
                })

            summary = {
                "total": len(results),
                "synced": sum(1 for item in results if item.get("status") == "synced" and not item.get("skipped")),
                "skipped": sum(1 for item in results if item.get("skipped")),
                "failed": sum(1 for item in results if item.get("status") == "failed"),
                "disabled": sum(1 for item in results if item.get("status") == "disabled"),
            }
            return web.json_response({"success": True, "summary": summary, "results": results})
        except Exception as e:
            logger.error(f"Sync picxazz favorites error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    def _load_all_entries(self) -> list:
        """加载所有条目（按 image_filename 去重，包含日期 key 条目用于日程共享）"""
        try:
            store = ScheduleStore(self.data_dir)
            all_data = store.load()
            if not isinstance(all_data, dict):
                all_data = {}
            result = []
            seen_filenames = set()
            metadata = self._load_image_metadata()
            for key, entry in all_data.items():
                if not isinstance(entry, dict):
                    continue
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
                        if not self._image_exists(img_file):
                            logger.warning(f"Image file missing: {img_file} (key={key}), skipping")
                            continue
                        seen_filenames.add(img_file)
                    else:
                        # Non-date-key entry without image_filename is broken, skip
                        continue
                    result.append(self._enrich_photo_schedule_time(entry, metadata))
            for img_file, meta in metadata.items():
                if not isinstance(img_file, str) or img_file in seen_filenames:
                    continue
                if not img_file.lower().endswith(REFERENCE_IMAGE_EXTENSIONS):
                    continue
                if not self._image_exists(img_file):
                    continue
                seen_filenames.add(img_file)
                entry = self._metadata_gallery_entry(img_file, meta)
                result.append(self._normalize_entry_display(entry, metadata))
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

    @staticmethod
    def _version_key(version: str) -> tuple[int, ...]:
        """Build a comparable key for simple semantic versions like 1.1.2."""
        parts = []
        for part in str(version or "").lstrip("v").split("."):
            match = re.match(r"(\d+)", part)
            parts.append(int(match.group(1)) if match else 0)
        return tuple(parts or [0])

    async def handle_version(self, request: web.Request):
        """返回当前版本信息"""
        version = self._load_version()
        return web.json_response({"version": version})

    async def _check_update_payload(self) -> tuple[dict, int]:
        import aiohttp

        github_api = self._github_api_url()
        current_version = self._load_version()
        protection = self._update_protection_summary()
        if not github_api:
            return {
                "status": "unavailable",
                "message": "未配置更新检查地址",
                "current": current_version,
                "update_available": False,
                "safe_update": True,
                "protection": protection,
            }, 200

        github_proxy = self._github_proxy()
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "portrait-gallery-updater",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(trust_env=True, timeout=timeout, headers=headers) as session:
                async with session.get(github_api, proxy=github_proxy or None) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"GitHub API error: {resp.status}, {error_text}")
                        error_message = f"GitHub API 请求失败: {resp.status}"
                        if resp.status == 403 and not github_proxy:
                            error_message += "（可在设置中填写 GitHub 代理后重试）"
                        return {"error": error_message}, 500

                    data = await resp.json()
                    latest_version = data.get("tag_name", "").lstrip("v")
                    if not latest_version:
                        return {"error": "无法获取最新版本号"}, 500

                    if self._version_key(latest_version) <= self._version_key(current_version):
                        return {
                            "status": "ok",
                            "message": "已是最新版本",
                            "current": current_version,
                            "latest": latest_version,
                            "update_available": False,
                            "safe_update": True,
                            "protection": protection,
                        }, 200

                    return {
                        "status": "ok",
                        "current": current_version,
                        "latest": latest_version,
                        "update_available": True,
                        "changelog": data.get("body", ""),
                        "html_url": data.get("html_url", ""),
                        "safe_update": True,
                        "protection": protection,
                    }, 200
        except Exception as e:
            logger.error(f"Check update error: {e}")
            return {"error": f"检查更新失败: {e}"}, 500

    def _safe_update_plan(self, project_root: Path, remote_ref: str, env: dict[str, str]) -> dict:
        all_changed = self._git_run(["diff", "--name-status", "HEAD.." + remote_ref, "--"], project_root, env)
        if all_changed.returncode != 0:
            raise RuntimeError(all_changed.stderr.strip() or all_changed.stdout.strip() or "无法读取远端改动列表")
        all_files = []
        checkout_files = []
        deleted_files = []
        skipped_files = []
        for line in all_changed.stdout.splitlines():
            parts = line.strip().split("\t")
            if len(parts) < 2:
                continue
            status = parts[0]
            path = parts[-1].strip()
            if not path:
                continue
            all_files.append(path)
            if self._is_protected_update_path(path):
                skipped_files.append(path)
                continue
            if status.startswith("D"):
                deleted_files.append(path)
            else:
                checkout_files.append(path)
        changed_files = checkout_files + deleted_files
        return {
            "all_changed_files": all_files,
            "updated_files": changed_files,
            "checkout_files": checkout_files,
            "deleted_files": deleted_files,
            "skipped_files": skipped_files,
            "safe_update": True,
            "protection": self._update_protection_summary(),
        }

    async def _perform_safe_update(self, dry_run: bool = False, restart: bool = True) -> tuple[dict, int]:
        project_root = resolve_project_root(self.config_path, self.config)
        if not (project_root / ".git").exists():
            return {
                "status": "unavailable",
                "message": "当前项目目录不是 Git 仓库，无法自动更新。请从发布包或仓库同步后重启服务。",
                "safe_update": True,
                "protection": self._update_protection_summary(),
            }, 200

        env = self._child_env(self._github_proxy_env())
        update_config = self.config.get("update", {})
        remote = update_config.get("remote", "origin")
        branch = update_config.get("branch", "main")
        remote_ref = self._safe_update_ref(remote, branch)

        fetch = self._git_run(["fetch", "--prune", remote, branch], project_root, env, timeout=90)
        if fetch.returncode != 0:
            return {"error": f"git fetch 失败: {fetch.stderr.strip() or fetch.stdout.strip()}"}, 500

        plan = self._safe_update_plan(project_root, remote_ref, env)
        changed_files = plan["updated_files"]
        response = {
            **plan,
            "status": "ok",
            "remote": remote,
            "branch": branch,
            "remote_ref": remote_ref,
            "dry_run": bool(dry_run),
            "will_restart": bool(restart and changed_files and not dry_run),
        }

        if dry_run:
            response["message"] = (
                f"可安全更新 {len(changed_files)} 个代码文件；"
                f"会跳过 {len(plan['skipped_files'])} 个本地设置/数据文件"
            )
            return response, 200

        if not changed_files:
            response["message"] = "没有可更新的代码文件；本地数据与配置已保持不变"
            return response, 200

        checkout_files = plan.get("checkout_files") or []
        deleted_files = plan.get("deleted_files") or []
        if checkout_files:
            result = self._git_run(["checkout", remote_ref, "--", *checkout_files], project_root, env, timeout=90)
            if result.returncode != 0:
                return {"error": f"安全更新失败: {result.stderr.strip() or result.stdout.strip()}"}, 500
        if deleted_files:
            result = self._git_run(["rm", "-r", "--ignore-unmatch", "--", *deleted_files], project_root, env, timeout=90)
            if result.returncode != 0:
                return {"error": f"安全更新删除旧文件失败: {result.stderr.strip() or result.stdout.strip()}"}, 500

        response["message"] = "更新成功，服务即将重启；本地 API Key、Base URL、appearance、图片和参考图已保留"
        if restart:
            scheduled, restart_message = self._schedule_python_restart("safe_update", delay=1.0)
            response["will_restart"] = bool(scheduled)
            response["message"] = (
                "更新成功，服务即将以 Python 模式重启；本地 API Key、Base URL、appearance、图片和参考图已保留"
                if scheduled
                else f"更新成功，但未能自动重启：{restart_message}"
            )
        else:
            response["message"] = "更新成功；本地 API Key、Base URL、appearance、图片和参考图已保留"
        return response, 200

    async def handle_check_update(self, request: web.Request):
        """检查更新（从 GitHub API 获取最新版本）"""
        payload, status = await self._check_update_payload()
        return web.json_response(payload, status=status)

    async def handle_hermes_check_update(self, request: web.Request):
        """Hermes-friendly update check API with explicit safe-update metadata."""
        payload, status = await self._check_update_payload()
        payload = {
            "api": "hermes_check_update",
            "can_apply_endpoint": "/api/hermes/update",
            **payload,
        }
        return web.json_response(payload, status=status)

    async def handle_update(self, request: web.Request):
        """执行安全更新：只拉取仓库代码，保留本地数据、密钥和图片。"""
        try:
            payload, status = await self._perform_safe_update(dry_run=False, restart=True)
            return web.json_response(payload, status=status)
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

    async def handle_hermes_update(self, request: web.Request):
        """Hermes-friendly safe update API.

        POST body:
        - {"dry_run": true}: fetch and return update plan only.
        - {"restart": false}: apply code files but do not restart automatically.
        """
        try:
            try:
                body = await request.json()
            except Exception:
                body = {}
            query_dry_run = str(request.query.get("dry_run", "")).strip().lower() in {"1", "true", "yes", "on"}
            dry_run = query_dry_run or self._body_bool(body, "dry_run", False)
            restart = self._body_bool(body, "restart", True)
            payload, status = await self._perform_safe_update(dry_run=dry_run, restart=restart)
            payload = {
                "api": "hermes_update",
                **payload,
            }
            return web.json_response(payload, status=status)
        except subprocess.TimeoutExpired:
            logger.error("Hermes update timeout")
            return web.json_response({"error": "更新超时"}, status=500)
        except Exception as e:
            logger.error(f"Hermes update error: {e}")
            return web.json_response({"error": f"更新失败: {e}"}, status=500)

    def _run_hermes_image_generation(
        self,
        engine: str,
        prompt: str,
        size: str = "",
        ref_image: str = "",
        caption: str = "",
        display_outfit: str = "",
        output_dir: str = "",
        url_prefix: str = "/images",
        source: str = "hermes_api",
        category: str = "portrait",
        persist_metadata: bool = True,
        filename_prefix: str = "hermes",
        classify_style: bool = True,
    ) -> Optional[dict]:
        """Run a pure image-generation request outside the aiohttp event loop."""
        zhuzhu_dir = os.path.join(os.path.dirname(__file__), "zhuzhu")
        if zhuzhu_dir not in sys.path:
            sys.path.insert(0, zhuzhu_dir)

        model_name = ""
        if engine == "gptimage":
            from generate_gptimage import _generate_via_direct_gpt
            from generate_gptimage import GPTIMAGE_DIRECT_MODEL
            model_name = GPTIMAGE_DIRECT_MODEL
            result = _generate_via_direct_gpt(prompt, ref_image=ref_image or None, size=size)
        elif engine == "gitee":
            from generate_gitee import generate_image_bytes
            from generate_gitee import MODEL_NAME
            model_name = MODEL_NAME
            result = generate_image_bytes(prompt)
        else:
            return None

        if not result:
            return None

        img_data, elapsed = result
        created_at = int(time.time())
        generation_mode = "img2img" if ref_image else "text2img"
        base_style = ""
        target_dir = os.path.abspath(os.path.expanduser(output_dir or self.image_dir))
        os.makedirs(target_dir, exist_ok=True)
        if classify_style:
            try:
                from generate import _classify_style
                base_style = _classify_style(prompt)
            except Exception as e:
                logger.warning("Hermes image style classification failed: %s", e)
        filename = f"{filename_prefix}_{created_at}_{uuid.uuid4().hex[:8]}.png"
        img_path = os.path.join(target_dir, filename)
        with open(img_path, "wb") as f:
            f.write(img_data)

        try:
            with Image.open(img_path) as img:
                width, height = img.size
                img.verify()
        except Exception as e:
            logger.error("Hermes image validation failed: %s", e)
            try:
                os.unlink(img_path)
            except OSError:
                pass
            return None

        caption = str(caption or "").strip()
        display_outfit = self._clean_display_description(display_outfit)

        meta_entry = {
            "category": category,
            "prompt": prompt,
            "model": model_name,
            "model_name": self._display_model_name(model_name),
            "base_style": base_style,
            "caption": caption,
            "display_outfit": display_outfit,
            "outfit_description": display_outfit,
            "size": size or "",
            "created_at": created_at,
            "generation_time": elapsed,
            "source": source,
            "prompt_mode": "pure",
            "pure_prompt": True,
            "custom_ref_mode": "reference" if ref_image else "text2img",
            "requested_generation_mode": generation_mode,
            "generation_mode": generation_mode,
            "ref_image": os.path.basename(ref_image) if ref_image else "",
            "ref_image_path": ref_image,
            "requested_ref_image": os.path.basename(ref_image) if ref_image else "",
            "requested_ref_image_path": ref_image,
            "fallback_used": False,
            "fallback_from": "",
            "fallback_to": "",
        }
        selected_reference = self._wardrobe_reference_for_value(ref_image)
        if selected_reference:
            meta_entry["selected_reference"] = selected_reference
        try:
            meta_entry["width"], meta_entry["height"] = width, height
            meta_entry["file_size_bytes"] = os.path.getsize(img_path)
            if persist_metadata:
                self._update_image_metadata_entry(filename, meta_entry)
        except Exception as e:
            logger.error("Save Hermes image metadata error: %s", e)
            try:
                os.unlink(img_path)
            except OSError:
                pass
            return None

        return {
            "success": True,
            "filename": filename,
            "path": img_path,
            "url": f"{url_prefix.rstrip('/')}/{filename}",
            "elapsed": elapsed,
            "engine": engine,
            "source": source,
            "base_style": base_style,
            "model_name": self._display_model_name(model_name),
            "caption": caption,
            "display_outfit": display_outfit,
            "outfit_description": display_outfit,
            "prompt_mode": "pure",
            "pure_prompt": True,
            "custom_ref_mode": "reference" if ref_image else "text2img",
            "generation_mode": generation_mode,
            "width": width,
            "height": height,
            "file_size_bytes": meta_entry.get("file_size_bytes", 0),
            "selected_reference": selected_reference,
        }

    async def handle_hermes_text_to_image(self, request: web.Request):
        """Hermes 纯文生图 API（不注入 persona）"""
        try:
            body = await request.json()
            prompt = str(body.get("prompt", "") or "").strip()
            if not prompt:
                return web.json_response({"error": "prompt_required"}, status=400)

            engine = str(body.get("engine", "gptimage") or "gptimage").strip().lower()
            size = str(body.get("size", "") or "").strip()
            caption = self._request_caption(body)
            display_outfit = await self._normalize_hermes_display_description(
                self._request_outfit_description(body),
                prompt,
                "文生图",
            )
            if not caption:
                caption = self._fallback_hermes_caption(display_outfit, prompt)

            if engine not in {"gptimage", "gitee"}:
                return web.json_response({"error": "invalid_engine"}, status=400)

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self._run_hermes_image_generation(engine, prompt, size=size, caption=caption, display_outfit=display_outfit),
            )

            if not result:
                return web.json_response({"error": "generate_failed"}, status=500)
            return web.json_response(result)
        except Exception as e:
            logger.error(f"Hermes text-to-image error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_hermes_image_to_image(self, request: web.Request):
        """Hermes 纯图生图 API（不注入 persona）"""
        try:
            body = await request.json()
            prompt = str(body.get("prompt", "") or "").strip()
            ref_image = str(body.get("ref_image", "") or "").strip()

            if not prompt:
                return web.json_response({"error": "prompt_required"}, status=400)
            if not ref_image:
                return web.json_response({"error": "ref_image_required"}, status=400)

            engine = str(body.get("engine", "gptimage") or "gptimage").strip().lower()
            size = str(body.get("size", "") or "").strip()
            caption = self._request_caption(body)
            display_outfit = await self._normalize_hermes_display_description(
                self._request_outfit_description(body),
                prompt,
                "图生图",
            )
            if not caption:
                caption = self._fallback_hermes_caption(display_outfit, prompt)

            if engine != "gptimage":
                return web.json_response({"error": "engine_not_support_img2img"}, status=400)

            resolved_ref = self._resolve_reference_image(ref_image)
            if not resolved_ref:
                return web.json_response({"error": "invalid_ref_image"}, status=400)

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self._run_hermes_image_generation(engine, prompt, size=size, ref_image=resolved_ref, caption=caption, display_outfit=display_outfit),
            )

            if not result:
                return web.json_response({"error": "generate_failed"}, status=500)
            return web.json_response(result)
        except Exception as e:
            logger.error(f"Hermes image-to-image error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    def run(self):
        """启动服务器"""
        logger.info(f"画廊服务启动: http://{self.host}:{self.port}")
        web.run_app(self.app, host=self.host, port=self.port)
