"""Web 画廊服务器 - aiohttp"""
import json
import logging
import os
import sys
import subprocess
from datetime import date
from pathlib import Path
import time
import re

from aiohttp import web

from store import ScheduleStore

logger = logging.getLogger(__name__)


class GalleryServer:
    """猪猪画廊 Web 服务器"""

    def __init__(self, config: dict, data_dir: str):
        self.config = config
        self.data_dir = data_dir
        self.gallery_config = config.get("gallery", {})
        self.host = self.gallery_config.get("host", "0.0.0.0")
        self.port = self.gallery_config.get("port", 18888)
        self.token = self.gallery_config.get("token", "")
        self.image_dir = os.path.join(data_dir, "images")
        os.makedirs(self.image_dir, exist_ok=True)

        # 回调：外部注入
        self.on_generate_today = None
        self.on_generate_custom = None

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

        # 参考图静态服务
        ref_dir = os.path.join(os.path.dirname(__file__), "references")
        self.app.router.add_static("/refs", ref_dir, show_index=True)

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
        self.app.router.add_post("/api/generate-now", self.handle_generate_now)
        self.app.router.add_post("/api/generate-custom", self.handle_generate_custom)
        self.app.router.add_post("/api/images/{img_id}/favorite", self.handle_toggle_favorite)
        self.app.router.add_delete("/api/images/{img_id}", self.handle_delete_image)
        self.app.router.add_get("/api/health", self.handle_health)
        self.app.router.add_get("/api/config/keys", self.handle_get_keys)
        self.app.router.add_post("/api/config/keys", self.handle_save_keys)
        # 版本管理
        self.app.router.add_get("/api/version", self.handle_version)
        self.app.router.add_post("/api/check-update", self.handle_check_update)
        self.app.router.add_post("/api/update", self.handle_update)
        # 日程彩蛋
        self.app.router.add_get("/api/schedule-detail", self.handle_schedule_detail)

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
        if os.path.exists(plugin_config_path):
            try:
                with open(plugin_config_path, 'r') as f:
                    plugin_config = json.load(f)
                    gitee_keys = plugin_config.get("gitee_config", {}).get("api_keys", [])
                    if gitee_keys:
                        gitee_key = gitee_keys[0]
            except Exception as e:
                logger.error(f"Load plugin config error: {e}")
        
        # 返回 masked 状态
        return web.json_response({
            "gitee_key": self._mask_key(gitee_key),
            "gpt_key": self._mask_key(keys_config.get("gpt_key", "")),
            "gpt_base_url": keys_config.get("gpt_base_url", ""),
            "cpa_url": keys_config.get("cpa_url", ""),
            "cpa_key": self._mask_key(keys_config.get("cpa_key", ""))
        })
    
    def _mask_key(self, key: str) -> str:
        """Mask API key for display"""
        if not key or len(key) < 8:
            return ""
        return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"

    async def handle_save_keys(self, request: web.Request):
        """保存 API 密钥配置"""
        try:
            body = await request.json()
            
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
            
            # 写入 api_keys_config.json
            with open(api_keys_path, 'w', encoding='utf-8') as f:
                json.dump(keys_config, f, ensure_ascii=False, indent=2)
            
            # 更新 plugin_config.json 的 gitee_config.api_keys[0]
            if "gitee_key" in body and body["gitee_key"]:
                plugin_config_path = os.path.join(self.data_dir, "plugin_config.json")
                plugin_config = {}
                if os.path.exists(plugin_config_path):
                    with open(plugin_config_path, 'r') as f:
                        plugin_config = json.load(f)
                
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
            
            return web.json_response({"success": True})
            
        except Exception as e:
            logger.error(f"Save keys error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_today(self, request: web.Request):
        """获取今日数据 - 返回今日所有照片 + 日程信息"""
        today_str = date.today().isoformat()
        DATE_KEY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
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
            photos = []
            seen = set()
            for key, e in all_data.items():
                if DATE_KEY_RE.match(key):
                    continue
                if e.get("date") == today_str and e.get("status") == "ok" and e.get("source") != "custom":
                    img_file = e.get("image_filename", "")
                    if img_file and img_file not in seen:
                        img_path = os.path.join(self.image_dir, img_file)
                        if os.path.exists(img_path):
                            seen.add(img_file)
                            photos.append(e)

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

            # 查找今日日程（日期 key 或图片条目）
            schedule_entry = None
            # 优先找日期 key
            if today_str in all_data and all_data[today_str].get("schedule"):
                schedule_entry = all_data[today_str]
            # 再找有 schedule 的图片条目
            if not schedule_entry:
                for key, e in all_data.items():
                    if e.get("date") == today_str and e.get("schedule") and e.get("status") == "ok":
                        schedule_entry = e
                        break

            if not schedule_entry:
                return web.json_response({"status": "no_schedule"})

            # 解析 outfit 字段为结构化数据
            outfit_raw = schedule_entry.get("outfit", "")
            outfit_parts = {}
            for line in outfit_raw.split("\n"):
                line = line.strip()
                if "：" in line:
                    k, v = line.split("：", 1)
                    outfit_parts[k.strip()] = v.strip()
                elif ":" in line:
                    k, v = line.split(":", 1)
                    outfit_parts[k.strip()] = v.strip()

            # 解析 schedule 字段为列表
            schedule_raw = schedule_entry.get("schedule", "")
            schedule_items = []
            import re
            for line in schedule_raw.split("\n"):
                line = line.strip()
                if not line:
                    continue
                m = re.match(r'(\d{1,2}:\d{2})\s*(.*)', line)
                if m:
                    schedule_items.append({
                        "time": m.group(1),
                        "activity": m.group(2).strip()
                    })

            return web.json_response({
                "status": "ok",
                "date": today_str,
                "outfit_style": schedule_entry.get("outfit_style", ""),
                "outfit": outfit_parts,
                "schedule": schedule_items,
                "caption": schedule_entry.get("caption", ""),
                "prompt": schedule_entry.get("prompt", ""),
            })
        except Exception as e:
            logger.error(f"Schedule detail error: {e}")
            return web.json_response({"status": "error", "detail": str(e)})

    async def handle_ref_list(self, request: web.Request):
        """返回参考图列表（只返回用户上传的自定义参考图）"""
        # 只返回上传的参考图，不再返回内置底模
        return web.json_response([])

    async def handle_uploaded_refs(self, request: web.Request):
        """列出已上传的自定义参考图"""
        upload_dir = os.path.join(os.path.dirname(__file__), "references", "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        refs = []
        try:
            for fname in sorted(os.listdir(upload_dir)):
                fpath = os.path.join(upload_dir, fname)
                if os.path.isfile(fpath) and fname.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
                    refs.append({
                        "filename": fname,
                        "url": f"/refs/uploads/{fname}",
                        "style": "upload",
                        "label": "自定义上传",
                    })
        except Exception as e:
            logger.error(f"List uploaded refs error: {e}")
        return web.json_response(refs)

    async def handle_upload_ref(self, request: web.Request):
        """上传自定义参考图"""
        reader = await request.multipart()
        field = await reader.next()
        if not field or not field.filename:
            return web.json_response({"error": "no_file"}, status=400)

        # 保存到上传目录
        upload_dir = os.path.join(os.path.dirname(__file__), "references", "uploads")
        os.makedirs(upload_dir, exist_ok=True)

        # 生成唯一文件名
        import time
        ext = os.path.splitext(field.filename)[1] or ".jpg"
        save_name = f"upload_{int(time.time())}{ext}"
        save_path = os.path.join(upload_dir, save_name)

        with open(save_path, "wb") as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                f.write(chunk)

        return web.json_response({
            "filename": save_name,
            "url": f"/refs/uploads/{save_name}",
            "style": "upload",
            "label": "自定义上传",
        })

    async def handle_delete_uploaded_ref(self, request: web.Request):
        """删除已上传的自定义参考图"""
        filename = request.match_info.get("filename")
        if not filename:
            return web.json_response({"error": "no_filename"}, status=400)
        # Prevent path traversal
        if ".." in filename or "/" in filename:
            return web.json_response({"error": "invalid_filename"}, status=400)
        upload_dir = os.path.join(os.path.dirname(__file__), "references", "uploads")
        filepath = os.path.join(upload_dir, filename)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
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
        """根据当前时段+日程生图 (💭 现在在干嘛)"""
        try:
            # Determine theme based on current time
            from datetime import datetime
            current_hour = datetime.now().hour
            
            # Time period mapping (6-11 morning, 11-14 noon, 14-19 evening, else bedtime)
            if 6 <= current_hour < 11:
                theme = "morning"
            elif 11 <= current_hour < 14:
                theme = "noon"
            elif 14 <= current_hour < 19:
                theme = "evening"
            else:  # 19-23 or any other time
                theme = "bedtime"
            
            logger.info(f"Generate now: hour={current_hour}, theme={theme}")
            
            # Run generate.py as subprocess with the determined theme
            import asyncio
            generate_script = os.path.join(os.path.dirname(__file__), "zhuzhu", "generate.py")
            proc = await asyncio.create_subprocess_exec(
                "python3", generate_script, "--theme", theme, "--caption", "--source", "web",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.path.join(os.path.dirname(__file__), "zhuzhu"),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
            
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
            })
        except asyncio.TimeoutError:
            logger.error("Generate now timeout")
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
            ref_image = body.get("ref_image", "")
            # 如果 ref_image 是 url 路径，转为本地文件路径
            if ref_image and ref_image.startswith("/refs/"):
                ref_image = ref_image.replace("/refs/", "")
                ref_dir = os.path.join(os.path.dirname(__file__), "references")
                ref_image = os.path.join(ref_dir, ref_image)
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
        DATE_KEY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
        try:
            store = ScheduleStore(self.data_dir)
            all_data = store.load()
            if not all_data:
                return []
            result = []
            seen_filenames = set()
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
                    result.append(entry)
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
            async with aiohttp.ClientSession() as session:
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
        try:
            # 1. git pull
            result = subprocess.run(
                ["git", "pull", "origin", "main"],
                cwd=os.path.dirname(os.path.dirname(__file__)),
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode != 0:
                return web.json_response(
                    {"error": f"git pull 失败: {result.stderr}"},
                    status=500
                )

            # 2. 重启服务
            os.execv(sys.executable, [sys.executable] + sys.argv)

            return web.json_response({"message": "更新成功，服务已重启"})
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
