"""Read-only client for the local xiaohongshu-mcp service."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp


DEFAULT_BASE_URL = "http://127.0.0.1:18060"
MCP_BINARY_NAMES = (
    "xiaohongshu-mcp-darwin-arm64",
    "xiaohongshu-mcp-darwin-amd64",
    "xiaohongshu-mcp-linux-arm64",
    "xiaohongshu-mcp-linux-amd64",
    "xiaohongshu-mcp",
)
DEFAULT_IMAGE_HOST_SUFFIXES = (
    "xhscdn.com",
    "xhscdn.net",
    "xiaohongshu.com",
)
DEFAULT_SEARCH_RESULTS = 30
MAX_SEARCH_RESULTS = 50
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_REDIRECTS = 3
TRUSTED_PROXY_SYNTHETIC_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
)
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


class XiaohongshuError(RuntimeError):
    """Base error surfaced by the read-only bridge."""

    def __init__(self, code: str, message: str, *, status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class XiaohongshuClient:
    """Manage and call one loopback-only xiaohongshu-mcp instance."""

    def __init__(
        self,
        base_url: str | None = None,
        binary_path: str | None = None,
        workdir: str | None = None,
        *,
        allowed_image_hosts: tuple[str, ...] = DEFAULT_IMAGE_HOST_SUFFIXES,
        allow_private_image_hosts: bool = False,
    ):
        self.base_url = (base_url or os.environ.get("XHS_MCP_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.workdir = str(workdir or os.environ.get("XHS_MCP_WORKDIR") or "").strip()
        configured_binary = str(binary_path or os.environ.get("XHS_MCP_BINARY") or "").strip()
        self.binary_path = configured_binary or self._discover_binary(self.workdir)
        self.allowed_image_hosts = tuple(host.lower().lstrip(".") for host in allowed_image_hosts if host)
        self.allow_private_image_hosts = allow_private_image_hosts
        self._process: asyncio.subprocess.Process | None = None
        self._start_lock = asyncio.Lock()
        self._validate_loopback_base_url(self.base_url)

    @staticmethod
    def _discover_binary(workdir: str) -> str:
        if not workdir:
            return ""
        directory = Path(workdir).expanduser()
        for name in MCP_BINARY_NAMES:
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return ""

    @staticmethod
    def _validate_loopback_base_url(value: str) -> None:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("XHS_MCP_BASE_URL 必须是有效的 HTTP 地址")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError as exc:
            if parsed.hostname.lower() != "localhost":
                raise ValueError("XHS_MCP_BASE_URL 只能使用本机回环地址") from exc
        else:
            if not address.is_loopback:
                raise ValueError("XHS_MCP_BASE_URL 只能使用本机回环地址")

    @property
    def configured(self) -> bool:
        return bool(self.binary_path) or self._process is not None

    async def _health(self) -> bool:
        timeout = aiohttp.ClientTimeout(total=2)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{self.base_url}/health") as response:
                    return response.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False

    async def ensure_service(self) -> None:
        if await self._health():
            return
        async with self._start_lock:
            if await self._health():
                return
            binary = Path(self.binary_path).expanduser() if self.binary_path else None
            if not binary or not binary.is_file() or not os.access(binary, os.X_OK):
                raise XiaohongshuError(
                    "service_not_configured",
                    "小红书服务未配置，请设置 XHS_MCP_BINARY。",
                    status=503,
                )
            parsed = urlparse(self.base_url)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            workdir = Path(self.workdir).expanduser() if self.workdir else binary.parent
            if not workdir.is_dir():
                raise XiaohongshuError(
                    "service_workdir_missing",
                    "小红书服务目录不存在。",
                    status=503,
                )
            self._process = await asyncio.create_subprocess_exec(
                str(binary),
                "-headless=true",
                "-port",
                f"127.0.0.1:{port}",
                cwd=str(workdir),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            for _ in range(80):
                if self._process.returncode is not None:
                    break
                if await self._health():
                    return
                await asyncio.sleep(0.25)
            returncode = self._process.returncode
            await self.close()
            detail = f"（退出码 {returncode}）" if returncode is not None else ""
            raise XiaohongshuError(
                "service_start_failed",
                f"小红书服务启动失败{detail}。",
                status=503,
            )

    async def close(self) -> None:
        process = self._process
        self._process = None
        if not process or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        timeout_seconds: int = 90,
    ) -> dict:
        await self.ensure_service()
        timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json_body,
                ) as response:
                    payload = await response.json(content_type=None)
        except asyncio.TimeoutError as exc:
            raise XiaohongshuError("request_timeout", "小红书请求超时，请稍后重试。", status=504) from exc
        except (aiohttp.ClientError, ValueError) as exc:
            raise XiaohongshuError("service_unavailable", "无法连接小红书服务。", status=503) from exc

        if response.status >= 400 or not isinstance(payload, dict) or payload.get("success") is not True:
            message = str(payload.get("error") or payload.get("message") or "小红书请求失败") if isinstance(payload, dict) else "小红书请求失败"
            lowered = message.lower()
            code = "login_required" if "未登录" in message or "login" in lowered else "upstream_error"
            status = 401 if code == "login_required" else 502
            raise XiaohongshuError(code, message, status=status)
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def status(self, *, start_service: bool = True) -> dict:
        running = await self._health()
        if start_service and not running:
            try:
                await self.ensure_service()
                running = True
            except XiaohongshuError as exc:
                return {
                    "configured": bool(self.binary_path),
                    "service_running": False,
                    "is_logged_in": False,
                    "error": exc.code,
                    "message": exc.message,
                }
        if not running:
            return {
                "configured": bool(self.binary_path),
                "service_running": False,
                "is_logged_in": False,
            }
        try:
            data = await self._request("GET", "/api/v1/login/status", timeout_seconds=45)
        except XiaohongshuError as exc:
            return {
                "configured": bool(self.binary_path),
                "service_running": True,
                "is_logged_in": False,
                "error": exc.code,
                "message": exc.message,
            }
        return {
            "configured": bool(self.binary_path) or running,
            "service_running": True,
            "is_logged_in": bool(data.get("is_logged_in")),
            "username": str(data.get("username") or "").strip(),
        }

    async def login_qrcode(self) -> dict:
        data = await self._request("GET", "/api/v1/login/qrcode", timeout_seconds=60)
        image = str(data.get("img") or "").strip()
        if not data.get("is_logged_in") and not image.startswith("data:image/"):
            raise XiaohongshuError("invalid_qrcode", "小红书服务没有返回有效二维码。")
        return {
            "is_logged_in": bool(data.get("is_logged_in")),
            "timeout": int(data.get("timeout") or 0),
            "image": image,
        }

    async def search(self, keyword: str, *, max_results: int = DEFAULT_SEARCH_RESULTS) -> list[dict]:
        keyword = str(keyword or "").strip()
        if not keyword:
            raise XiaohongshuError("keyword_required", "请输入小红书搜索关键词。", status=400)
        if len(keyword) > 80:
            raise XiaohongshuError("keyword_too_long", "搜索关键词不能超过 80 个字符。", status=400)
        try:
            max_results = int(max_results)
        except (TypeError, ValueError) as exc:
            raise XiaohongshuError("invalid_max_results", "搜索数量必须是 1 到 50 的整数。", status=400) from exc
        if not 1 <= max_results <= MAX_SEARCH_RESULTS:
            raise XiaohongshuError("invalid_max_results", "搜索数量必须是 1 到 50 的整数。", status=400)
        try:
            data = await self._request(
                "POST",
                "/api/v1/feeds/search",
                json_body={
                    "keyword": keyword,
                    "max_results": max_results,
                },
                timeout_seconds=70,
            )
        except XiaohongshuError as exc:
            if exc.code not in {"upstream_error", "request_timeout", "service_unavailable"}:
                raise
            await self.close()
            await asyncio.sleep(0.5)
            data = await self._request(
                "POST",
                "/api/v1/feeds/search",
                json_body={
                    "keyword": keyword,
                    "max_results": max_results,
                },
                timeout_seconds=70,
            )
        feeds = data.get("feeds") if isinstance(data.get("feeds"), list) else []
        results = []
        for feed in feeds:
            if not isinstance(feed, dict):
                continue
            card = feed.get("noteCard") if isinstance(feed.get("noteCard"), dict) else {}
            if str(card.get("type") or "").lower() != "normal":
                continue
            cover = card.get("cover") if isinstance(card.get("cover"), dict) else {}
            cover_url = str(cover.get("urlDefault") or cover.get("url") or cover.get("urlPre") or "").strip()
            feed_id = str(feed.get("id") or "").strip()
            token = str(feed.get("xsecToken") or feed.get("xsec_token") or "").strip()
            if not feed_id or not token or not cover_url:
                continue
            user = card.get("user") if isinstance(card.get("user"), dict) else {}
            interact = card.get("interactInfo") if isinstance(card.get("interactInfo"), dict) else {}
            results.append({
                "id": feed_id,
                "xsec_token": token,
                "title": str(card.get("displayTitle") or "未命名穿搭").strip(),
                "user_id": str(user.get("userId") or "").strip(),
                "author": str(user.get("nickname") or user.get("nickName") or "").strip(),
                "avatar_url": str(user.get("avatar") or "").strip(),
                "cover_url": cover_url,
                "width": int(cover.get("width") or 0),
                "height": int(cover.get("height") or 0),
                "liked_count": str(interact.get("likedCount") or "").strip(),
            })
            if len(results) >= max_results:
                break
        return results

    async def search_creators(
        self,
        keyword: str,
        *,
        max_results: int = DEFAULT_SEARCH_RESULTS,
    ) -> list[dict]:
        """Find creator candidates through image-note search results."""
        keyword = str(keyword or "").strip()
        items = await self.search(keyword, max_results=max_results)
        creators: dict[str, dict] = {}
        for item in items:
            user_id = str(item.get("user_id") or "").strip()
            nickname = str(item.get("author") or "").strip()
            if not user_id or not nickname:
                continue
            creator = creators.setdefault(user_id, {
                "user_id": user_id,
                "nickname": nickname,
                "avatar_url": str(item.get("avatar_url") or "").strip(),
                "xsec_token": str(item.get("xsec_token") or "").strip(),
                "matched_note_count": 0,
                "sample_note": {
                    "id": str(item.get("id") or ""),
                    "title": str(item.get("title") or ""),
                    "cover_url": str(item.get("cover_url") or ""),
                },
            })
            creator["matched_note_count"] += 1
            if not creator.get("avatar_url") and item.get("avatar_url"):
                creator["avatar_url"] = str(item.get("avatar_url") or "").strip()
            if item.get("xsec_token"):
                creator["xsec_token"] = str(item.get("xsec_token") or "").strip()

        normalized = "".join(keyword.casefold().split())

        def _score(item: dict) -> tuple:
            nickname = "".join(str(item.get("nickname") or "").casefold().split())
            return (
                2 if nickname == normalized else (1 if normalized and normalized in nickname else 0),
                int(item.get("matched_note_count") or 0),
                nickname,
            )

        return sorted(creators.values(), key=_score, reverse=True)

    async def profile(self, user_id: str, xsec_token: str) -> dict:
        """Return one creator profile and its image-note cards."""
        user_id = str(user_id or "").strip()
        xsec_token = str(xsec_token or "").strip()
        if not user_id or not xsec_token:
            raise XiaohongshuError("creator_required", "缺少小红书博主主页参数。", status=400)
        request_body = {"user_id": user_id, "xsec_token": xsec_token}
        try:
            data = await self._request(
                "POST",
                "/api/v1/user/profile",
                json_body=request_body,
                timeout_seconds=80,
            )
        except XiaohongshuError as exc:
            if exc.code != "upstream_error":
                raise
            await asyncio.sleep(0.5)
            data = await self._request(
                "POST",
                "/api/v1/user/profile",
                json_body=request_body,
                timeout_seconds=80,
            )

        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        basic = payload.get("userBasicInfo") if isinstance(payload.get("userBasicInfo"), dict) else {}
        interactions = payload.get("interactions") if isinstance(payload.get("interactions"), list) else []
        notes = []
        for feed in payload.get("feeds") or []:
            if not isinstance(feed, dict):
                continue
            card = feed.get("noteCard") if isinstance(feed.get("noteCard"), dict) else {}
            if str(card.get("type") or "").lower() != "normal":
                continue
            cover = card.get("cover") if isinstance(card.get("cover"), dict) else {}
            cover_url = str(cover.get("urlDefault") or cover.get("url") or cover.get("urlPre") or "").strip()
            feed_id = str(feed.get("id") or "").strip()
            token = str(feed.get("xsecToken") or feed.get("xsec_token") or "").strip()
            if not feed_id or not token or not cover_url:
                continue
            note_user = card.get("user") if isinstance(card.get("user"), dict) else {}
            interact = card.get("interactInfo") if isinstance(card.get("interactInfo"), dict) else {}
            notes.append({
                "id": feed_id,
                "xsec_token": token,
                "title": str(card.get("displayTitle") or "未命名穿搭").strip(),
                "user_id": str(note_user.get("userId") or user_id).strip(),
                "author": str(note_user.get("nickname") or note_user.get("nickName") or basic.get("nickname") or "").strip(),
                "avatar_url": str(note_user.get("avatar") or basic.get("imageb") or basic.get("images") or "").strip(),
                "cover_url": cover_url,
                "width": int(cover.get("width") or 0),
                "height": int(cover.get("height") or 0),
                "liked_count": str(interact.get("likedCount") or "").strip(),
            })

        stats = {}
        for item in interactions:
            if not isinstance(item, dict):
                continue
            key = str(item.get("type") or item.get("name") or "").strip()
            if key:
                stats[key] = str(item.get("count") or "").strip()
        nickname = str(basic.get("nickname") or (notes[0].get("author") if notes else "") or "").strip()
        return {
            "creator": {
                "user_id": user_id,
                "nickname": nickname,
                "avatar_url": str(basic.get("imageb") or basic.get("images") or (notes[0].get("avatar_url") if notes else "") or "").strip(),
                "description": str(basic.get("desc") or "").strip(),
                "red_id": str(basic.get("redId") or "").strip(),
                "ip_location": str(basic.get("ipLocation") or "").strip(),
                "stats": stats,
                "xsec_token": xsec_token,
            },
            "notes": notes,
        }

    async def detail(self, feed_id: str, xsec_token: str) -> dict:
        feed_id = str(feed_id or "").strip()
        xsec_token = str(xsec_token or "").strip()
        if not feed_id or not xsec_token:
            raise XiaohongshuError("feed_required", "缺少小红书笔记参数。", status=400)
        detail_body = {
            "feed_id": feed_id,
            "xsec_token": xsec_token,
            "load_all_comments": False,
        }
        try:
            data = await self._request(
                "POST",
                "/api/v1/feeds/detail",
                json_body=detail_body,
                timeout_seconds=90,
            )
        except XiaohongshuError as exc:
            if exc.code != "upstream_error":
                raise
            await asyncio.sleep(0.5)
            data = await self._request(
                "POST",
                "/api/v1/feeds/detail",
                json_body=detail_body,
                timeout_seconds=90,
            )
        detail_data = data.get("data") if isinstance(data.get("data"), dict) else data
        note = detail_data.get("note") if isinstance(detail_data.get("note"), dict) else {}
        if str(note.get("type") or "normal").lower() != "normal":
            raise XiaohongshuError("image_note_required", "这条笔记不是图文笔记。", status=400)
        images = []
        for index, item in enumerate(note.get("imageList") or []):
            if not isinstance(item, dict):
                continue
            url = str(item.get("urlDefault") or item.get("urlPre") or item.get("url") or "").strip()
            if not url:
                continue
            images.append({
                "index": index,
                "url": url,
                "width": int(item.get("width") or 0),
                "height": int(item.get("height") or 0),
            })
        user = note.get("user") if isinstance(note.get("user"), dict) else {}
        return {
            "id": str(note.get("noteId") or feed_id),
            "title": str(note.get("title") or "未命名穿搭").strip(),
            "author": str(user.get("nickname") or user.get("nickName") or "").strip(),
            "images": images,
        }

    def _image_host_allowed(self, hostname: str) -> bool:
        hostname = hostname.lower().rstrip(".")
        return any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in self.allowed_image_hosts)

    async def _validate_image_url(self, value: str) -> str:
        parsed = urlparse(str(value or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise XiaohongshuError("invalid_image_url", "图片地址无效。", status=400)
        if not self._image_host_allowed(parsed.hostname):
            raise XiaohongshuError("image_host_not_allowed", "只允许导入小红书图片地址。", status=400)
        if not self.allow_private_image_hosts:
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
            except socket.gaierror as exc:
                raise XiaohongshuError("image_host_unavailable", "无法解析小红书图片地址。", status=502) from exc
            for info in infos:
                address = ipaddress.ip_address(info[4][0])
                proxy_synthetic = any(address in network for network in TRUSTED_PROXY_SYNTHETIC_NETWORKS)
                if not address.is_global and not proxy_synthetic:
                    raise XiaohongshuError("private_image_host", "图片地址不能指向本机或内网。", status=400)
        return parsed.geturl()

    async def import_image(self, url: str, output_dir: str) -> dict:
        current_url = await self._validate_image_url(url)
        timeout = aiohttp.ClientTimeout(total=45, connect=10, sock_read=20)
        headers = {
            "Accept": "image/avif,image/webp,image/apng,image/jpeg,image/png,image/*,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0 PortraitGallery/1.0",
        }
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                for redirect_count in range(MAX_REDIRECTS + 1):
                    async with session.get(current_url, allow_redirects=False) as response:
                        if 300 <= response.status < 400:
                            location = response.headers.get("Location")
                            if not location or redirect_count >= MAX_REDIRECTS:
                                raise XiaohongshuError("image_redirect_failed", "图片跳转次数过多。", status=502)
                            current_url = await self._validate_image_url(urljoin(current_url, location))
                            continue
                        if response.status != 200:
                            raise XiaohongshuError("image_download_failed", f"图片下载失败（HTTP {response.status}）。", status=502)
                        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                        extension = IMAGE_EXTENSIONS.get(content_type)
                        if not extension:
                            raise XiaohongshuError("invalid_image_type", "小红书返回的内容不是支持的图片格式。", status=400)
                        content_length = int(response.headers.get("Content-Length") or 0)
                        if content_length > MAX_IMAGE_BYTES:
                            raise XiaohongshuError("image_too_large", "图片超过 12MB 限制。", status=413)
                        chunks = []
                        size = 0
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            size += len(chunk)
                            if size > MAX_IMAGE_BYTES:
                                raise XiaohongshuError("image_too_large", "图片超过 12MB 限制。", status=413)
                            chunks.append(chunk)
                        payload = b"".join(chunks)
                        break
                else:
                    raise XiaohongshuError("image_download_failed", "图片下载失败。", status=502)
        except XiaohongshuError:
            raise
        except asyncio.TimeoutError as exc:
            raise XiaohongshuError("image_timeout", "图片下载超时。", status=504) from exc
        except aiohttp.ClientError as exc:
            raise XiaohongshuError("image_download_failed", "图片下载失败。", status=502) from exc

        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:24]
        target = output / f"xhs_{digest}{extension}"
        if not target.exists():
            temporary = output / f".{target.name}.{os.getpid()}.tmp"
            try:
                temporary.write_bytes(payload)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return {
            "filename": target.name,
            "path": str(target),
            "size_bytes": target.stat().st_size,
            "source_url": str(url),
        }
