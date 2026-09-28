"""Scoped Chrome client for X/local-image -> WIND Qwen edits, with durable jobs.

The extension has no gallery-admin privileges. Local/trusted-LAN extension clients
connect automatically without asking the user for credentials. Requests never accept a
local path or arbitrary download URL, and only the Qwen img2img provider is used.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import zipfile

from aiohttp import web
from PIL import Image, ImageOps
import requests
from store import LockedJsonDictStore, ImageMetadataStore

log = logging.getLogger(__name__)
PREFIX = '/api/browser-extension'
ACTIVE = {'queued', 'downloading', 'generating'}
MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 25_000_000
UPLOAD_CONTENT_TYPES = {'image/jpeg', 'image/jpg', 'image/png', 'image/webp'}
# A batch is capped in the popup as well; queued uploads are serialized by the
# existing Qwen gate, so allowing the selected batch to wait avoids silently
# rejecting the fourth image while retaining a finite abuse boundary.
MAX_UPLOAD_QUEUE = 12
MAX_SOURCE_TEXT = 20_000
MEDIA_RE = re.compile(r'/media/[A-Za-z0-9_-]+(?:\.(?:jpg|jpeg|png|webp))?\Z')
JOB_RE = re.compile(r'/api/browser-extension/jobs/([a-f0-9]{32})(?:/(image|source|save))?\Z')
LOCAL_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                       ('127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '::1/128'))


def is_local_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return any(address in network for network in LOCAL_NETWORKS)
    except ValueError:
        return False


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_media(value: str) -> str:
    try:
        url = urlsplit(str(value or ''))
        if (url.scheme != 'https' or url.netloc != 'pbs.twimg.com'
                or url.username or url.password or url.fragment or not MEDIA_RE.fullmatch(url.path)):
            raise ValueError()
        query = parse_qs(url.query)
        fmt = query.get('format', [''])[0]
        if not fmt:
            fmt = url.path.rsplit('.', 1)[-1] if '.' in url.path else 'jpg'
        if fmt not in {'jpg', 'jpeg', 'png', 'webp'}:
            raise ValueError()
        return 'https://pbs.twimg.com' + url.path + '?' + urlencode({'format': fmt, 'name': 'orig'})
    except (ValueError, TypeError):
        raise ValueError('只支持 X 帖子里的静态图片，不支持头像、视频或任意网址。') from None


def canonical_post(value: str) -> str:
    if not value:
        return ''
    u = urlsplit(str(value))
    if u.scheme != 'https' or u.netloc not in {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'}:
        return ''
    m = re.fullmatch(r'/([A-Za-z0-9_]{1,50})/status/(\d+)(?:/photo/[1-4])?', u.path)
    return f'https://x.com/{m[1]}/status/{m[2]}' if m else ''


def validate_prompt(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 4000:
        raise ValueError('请先在画廊或扩展中保存修改提示词（1–4000 字）。')
    return value.strip()


def validate_steps(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 50:
        raise ValueError('步数必须是 1–50 的整数。')
    return value


def normalize_source_text(value):
    """Keep only bounded visible post text supplied by the X content script."""
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError('原帖文字格式无效。')
    value = value.replace('\xa0', ' ').replace('\r\n', '\n').replace('\r', '\n')
    value = re.sub(r'[ \t]+\n', '\n', value)
    value = re.sub(r'\n{3,}', '\n\n', value).strip()
    if len(value) > MAX_SOURCE_TEXT:
        raise ValueError('原帖文字过长，无法保存。')
    return value


class BrowserExtension:
    def __init__(self, server):
        self.server = server
        self.package_dir = Path(__file__).resolve().parents[1] / 'extensions' / 'gallery-qwen-x'
        manifest = json.loads((self.package_dir / 'manifest.json').read_text())
        key_hash = hashlib.sha256(base64.b64decode(manifest['key'])).hexdigest()[:32]
        self.extension_id = ''.join(chr(ord('a') + int(c, 16)) for c in key_hash)
        self.version = manifest['version']
        self.config = LockedJsonDictStore(str(Path(server.data_dir) / 'browser_extension_config.json'))
        self.jobs = LockedJsonDictStore(str(Path(server.data_dir) / 'browser_extension_jobs.json'))
        self.tasks = set()
        self.gate = asyncio.Semaphore(1)
        self.request_lock = asyncio.Lock()
        self.pair_attempts = []
        self.closing = False
        self.result_dir = Path(server.reference_dir) / 'browser-extension' / 'results'
        self.result_dir.mkdir(parents=True, exist_ok=True)

    def setup(self, app):
        app.router.add_get(PREFIX + '/manage', self.manage_get)
        app.router.add_post(PREFIX + '/manage', self.manage_post)
        app.router.add_get(PREFIX + '/download', self.download)
        app.router.add_post(PREFIX + '/pair', self.pair)
        app.router.add_post(PREFIX + '/connect', self.connect)
        app.router.add_get(PREFIX + '/config', self.client_config)
        app.router.add_post(PREFIX + '/config', self.client_config)
        app.router.add_get(PREFIX + '/health', self.health)
        app.router.add_post(PREFIX + '/jobs', self.submit)
        app.router.add_post(PREFIX + '/uploads', self.submit_upload)
        app.router.add_get(PREFIX + '/jobs/{job_id:[a-f0-9]{32}}', self.status)
        app.router.add_get(PREFIX + '/jobs/{job_id:[a-f0-9]{32}}/{kind:image|source}', self.image)
        app.router.add_post(PREFIX + '/jobs/{job_id:[a-f0-9]{32}}/save', self.save)
        app.router.add_options(PREFIX + '/{tail:.*}', self.options)
        app.on_startup.append(self.recover)
        app.on_cleanup.append(self.cleanup)

    def client_route(self, path):
        return path in {PREFIX + '/pair', PREFIX + '/connect', PREFIX + '/config', PREFIX + '/health', PREFIX + '/jobs', PREFIX + '/uploads'} or bool(JOB_RE.fullmatch(path))

    def response(self, data, status=200):
        return web.json_response(data, status=status, headers={'Cache-Control': 'no-store'})

    def cors(self, request, response):
        if request.headers.get('Origin') == 'chrome-extension://' + self.extension_id:
            response.headers.update({'Access-Control-Allow-Origin': 'chrome-extension://' + self.extension_id,
                'Vary': 'Origin', 'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
                'Access-Control-Allow-Headers': 'Authorization,Content-Type,X-Gallery-Extension-ID'})
            if request.headers.get('Access-Control-Request-Private-Network') == 'true':
                response.headers['Access-Control-Allow-Private-Network'] = 'true'
        return response

    def authorize(self, request):
        """Returns an error or None; only recognized extension routes call this."""
        origin = request.headers.get('Origin', '')
        if origin and origin != 'chrome-extension://' + self.extension_id:
            return self.response({'error': 'extension_origin_denied', 'message': '此接口只供配对的画廊扩展使用。'}, 403)
        if request.method == 'OPTIONS':
            return None
        if request.headers.get('X-Gallery-Extension-ID') != self.extension_id:
            return self.response({'error': 'extension_required', 'message': '扩展身份不匹配，请重新安装画廊扩展。'}, 403)
        if request.path == PREFIX + '/pair':
            return None  # A single-use high-entropy code is required by the handler.
        if request.path == PREFIX + '/connect':
            # Origin alone is not authentication against native LAN clients.
            # This convenience endpoint intentionally trusts the local network,
            # but never grants gallery-admin access or accepts website origins.
            try:
                authority = urlsplit('//' + request.host)
                host = authority.hostname or ''
                valid_host = not (authority.username or authority.password) and (
                    host == 'localhost' or is_local_address(host))
            except ValueError:
                valid_host = False
            if (origin != 'chrome-extension://' + self.extension_id
                    or not valid_host or not is_local_address(request.remote or '')):
                return self.response({'error': 'local_extension_only', 'message': '自动连接仅允许本机或可信内网的画廊扩展。'}, 403)
            if not self.config.load().get('auto_connect_enabled', True):
                return self.response({'error': 'extension_disabled', 'message': '画廊已关闭扩展连接，请在画廊设置中重新启用。'}, 403)
            return None
        auth = request.headers.get('Authorization', '')
        token = auth[7:] if auth.startswith('Bearer gxe_') else ''
        state = self.config.load()
        client = (state.get('clients') or {}).get(digest(token)) if token else None
        if (not isinstance(client, dict) or client.get('expires', 0) <= time.time()
                or client.get('password_revision') != self.server._gallery_password_revision()):
            return self.response({'error': 'extension_pair_required', 'message': '扩展连接已失效，请重新连接画廊。'}, 401)
        request['extension_owner'] = client['id']
        return None

    async def options(self, request):
        return self.cors(request, web.Response(status=204))

    async def body(self, request):
        try:
            data = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text='invalid_json')
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text='invalid_json')
        return data

    def settings(self):
        state = self.config.load()
        return {'prompt': str(state.get('prompt') or ''), 'steps': state.get('steps', 25),
                'sync_gallery_prompt': state.get('sync_gallery_prompt', True),
                'model': 'Qwen-Image-2.1 Q8', 'image_to_image': True, 'text_to_image': False,
                'connection_mode': 'local_auto', 'auto_connect_enabled': state.get('auto_connect_enabled', True),
                'version': self.version, 'extension_id': self.extension_id}

    async def manage_get(self, request):
        state = self.config.load()
        clients = [dict(id=c['id'], label=c.get('label', 'Chrome'), created_at=c.get('created_at'))
                   for c in (state.get('clients') or {}).values() if c.get('expires', 0) > time.time()
                   and c.get('password_revision') == self.server._gallery_password_revision()]
        return self.response({**self.settings(), 'clients': clients,
                              'active_jobs': sum(j.get('status') in ACTIVE for j in self.jobs.load().values())})

    def save_settings(self, body, only_sync=False):
        def save(state):
            if only_sync and not state.get('sync_gallery_prompt', True):
                return
            if 'prompt' in body:
                value = body['prompt']
                if not isinstance(value, str) or len(value) > 4000:
                    raise ValueError('提示词不能超过 4000 字。')
                state['prompt'] = value.strip()
            if not only_sync and 'steps' in body:
                state['steps'] = validate_steps(body['steps'])
            if not only_sync and 'sync_gallery_prompt' in body:
                if not isinstance(body['sync_gallery_prompt'], bool):
                    raise ValueError('同步设置必须为布尔值。')
                state['sync_gallery_prompt'] = body['sync_gallery_prompt']
        self.config.update(save)

    async def manage_post(self, request):
        body = await self.body(request)
        operation = body.get('operation', 'save')
        try:
            if operation in {'save', 'sync_prompt'}:
                self.save_settings(body, operation == 'sync_prompt')
                return self.response({'success': True, **self.settings()})
            if operation == 'pair_code':
                code = secrets.token_urlsafe(24)
                self.config.update(lambda s: s.update(pair_hash=digest(code), pair_expires=time.time()+300))
                os.chmod(self.config.path, 0o600)
                return self.response({'code': code, 'expires_in': 300, 'extension_id': self.extension_id})
            if operation == 'revoke':
                self.config.update(lambda s: s.update(clients={}, pair_hash='', pair_expires=0, auto_connect_enabled=False))
                return self.response({'success': True})
            if operation == 'enable_auto_connect':
                self.config.update(lambda s: s.update(auto_connect_enabled=True))
                return self.response({'success': True})
        except ValueError as exc:
            return self.response({'error': 'invalid_settings', 'message': str(exc)}, 400)
        return self.response({'error': 'invalid_operation'}, 400)

    async def connect(self, request):
        """Automatically issue a scoped session; no password/key/code input.

        A stable installation identifier preserves that client's job ownership
        across reconnects. The opaque session remains in trusted extension
        storage and cannot authorize gallery administration.
        """
        now = time.time()
        self.pair_attempts = [t for t in self.pair_attempts if t > now - 60]
        if len(self.pair_attempts) >= 20:
            return self.response({'error': 'rate_limited', 'message': '连接尝试过多，请一分钟后再试。'}, 429)
        self.pair_attempts.append(now)
        body = await self.body(request)
        client_id = str(body.get('client_id') or '').replace('-', '').lower()
        if not re.fullmatch(r'[a-f0-9]{32}', client_id):
            return self.response({'error': 'invalid_client_id', 'message': '扩展安装标识无效，请重新加载扩展。'}, 400)
        owner = digest(self.extension_id + ':' + client_id)[:32]
        token = 'gxe_' + secrets.token_urlsafe(32)
        def register(state):
            if not state.get('auto_connect_enabled', True):
                raise ValueError('画廊已关闭扩展连接。')
            clients = {key: value for key, value in (state.get('clients') or {}).items()
                       if value.get('expires', 0) > now and value.get('id') != owner}
            if len(clients) >= 32:
                raise ValueError('连接客户端过多，请在画廊设置清理旧连接。')
            clients[digest(token)] = {'id': owner, 'label': 'Chrome 自动连接', 'created_at': int(now),
                'expires': now + 90*86400, 'password_revision': self.server._gallery_password_revision()}
            state.update(clients=clients)
        try:
            self.config.update(register)
            os.chmod(self.config.path, 0o600)
        except ValueError as exc:
            return self.response({'error': 'connect_failed', 'message': str(exc)}, 403)
        return self.response({'success': True, 'token': token, 'client_id': owner,
                              'extension_id': self.extension_id, 'expires_in': 90*86400,
                              'connection_mode': 'local_auto'})

    async def pair(self, request):
        now = time.time()
        self.pair_attempts = [t for t in self.pair_attempts if t > now - 60]
        if len(self.pair_attempts) >= 20:
            return self.response({'error': 'rate_limited', 'message': '连接尝试过多，请一分钟后再试。'}, 429)
        self.pair_attempts.append(now)
        body = await self.body(request)
        code = str(body.get('code') or '')
        token = 'gxe_' + secrets.token_urlsafe(32)
        owner = secrets.token_hex(16)
        def exchange(state):
            if (state.get('pair_expires', 0) <= now or not code
                    or not hmac.compare_digest(state.get('pair_hash', ''), digest(code))):
                raise ValueError('连接码无效或已过期，请在画廊设置重新生成。')
            clients = {k:v for k,v in (state.get('clients') or {}).items() if v.get('expires',0)>now}
            if len(clients) >= 10:
                raise ValueError('已连接 10 个客户端，请先撤销旧授权。')
            clients[digest(token)] = {'id': owner, 'label': 'Chrome Qwen X', 'created_at': int(now),
                'expires': now + 90*86400, 'password_revision': self.server._gallery_password_revision()}
            state.update(clients=clients, pair_hash='', pair_expires=0)
        try:
            self.config.update(exchange)
            os.chmod(self.config.path, 0o600)
        except ValueError as exc:
            return self.response({'error': 'pair_failed', 'message': str(exc)}, 401)
        return self.response({'success': True, 'token': token, 'client_id': owner, 'expires_in': 90*86400})

    async def client_config(self, request):
        if request.method == 'POST':
            try:
                body = await self.body(request)
                self.save_settings({k:v for k,v in body.items() if k in {'prompt','steps'}})
            except ValueError as exc:
                return self.response({'error':'invalid_settings','message':str(exc)}, 400)
        return self.response(self.settings())

    async def health(self, request):
        return await self.server.handle_qwen_health(request)

    async def download(self, request):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(self.package_dir.rglob('*')):
                if path.is_file() and path.suffix in {'.json','.js','.css','.html','.png','.md'}:
                    archive.write(path, 'gallery-qwen-x/' + str(path.relative_to(self.package_dir)))
        return web.Response(body=buffer.getvalue(), content_type='application/zip', headers={
            'Content-Disposition': f'attachment; filename="gallery-qwen-x-{self.version}.zip"',
            'Cache-Control': 'no-store'})

    async def recover(self, app):
        records = self.jobs.load()
        if any(j.get('status') in ACTIVE for j in records.values()):
            def update(jobs):
                for job in jobs.values():
                    if job.get('status') in ACTIVE:
                        job.update(status='interrupted', message='画廊曾重启，任务状态需核对；请先查看画廊结果，不自动重复提交。', updated_at=int(time.time()))
            self.jobs.update(update)

    async def cleanup(self, app):
        self.closing = True
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    def public_job(self, job):
        public = {k:v for k,v in job.items() if k in {'id','status','message','created_at','updated_at','result','source_url','source_text','media_url','input_type','source_name'}}
        # Jobs written by versions before local uploads were introduced are X jobs.
        public.setdefault('input_type', 'x')
        public.setdefault('source_name', '')
        public.setdefault('source_text', '')
        return public

    def get_job(self, request):
        job = self.jobs.load().get(request.match_info['job_id'])
        if not job or job.get('owner') != request.get('extension_owner'):
            return None
        return job

    async def submit(self, request):
        if self.closing:
            return self.response({'error':'service_stopping'}, 503)
        body = await self.body(request)
        try:
            media = canonical_media(body.get('media_url'))
            source_text = normalize_source_text(body.get('source_text'))
            settings = self.settings()
            prompt = validate_prompt(settings['prompt'])
            steps = validate_steps(settings['steps'])
            rid = str(body.get('request_id') or '')
            if not re.fullmatch(r'[a-f0-9-]{32,36}', rid):
                raise ValueError('请求编号无效，请刷新 X 页面。')
        except ValueError as exc:
            return self.response({'error':'invalid_edit_request','message':str(exc)}, 400)
        owner = request['extension_owner']
        job_id = digest(owner + ':' + rid)[:32]
        request_digest = digest(media + '\n' + prompt + '\n' + str(steps) + '\n' + source_text)
        async with self.request_lock:
            jobs = self.jobs.load()
            old = jobs.get(job_id)
            if old:
                if old.get('request_digest') != request_digest:
                    return self.response({'error':'request_conflict','message':'请求已存在且参数不同，请先核对原任务。'},409)
                return self.response(self.public_job(old), 200)
            if sum(j.get('status') in ACTIVE for j in jobs.values()) >= 8 or sum(j.get('status') in ACTIVE and j.get('owner')==owner for j in jobs.values()) >= 3:
                return self.response({'error':'queue_full','message':'已有改图任务排队，请等当前任务完成。'},429)
            job = {'id':job_id,'owner':owner,'request_digest':request_digest,'status':'queued',
                'prompt':prompt,'steps':steps,'media_url':media,'source_url':canonical_post(body.get('source_url')),
                'source_text':source_text,
                'created_at':int(time.time()),'updated_at':int(time.time()),'message':'等待 WIND Qwen'}
            self.jobs.update(lambda data:data.update({job_id:job}))
            task = asyncio.create_task(self.run(job))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return self.response(self.public_job(job), 202)

    @staticmethod
    def _safe_source_name(value):
        """Return a display-only filename; never use it as a filesystem path."""
        value = str(value or '').replace('\x00', '').strip()
        value = re.split(r'[\\/]', value)[-1]
        value = ''.join(ch for ch in value if ch.isprintable())
        return value[:120] or '上传图片'

    def _reserve_gallery_result(self, source: Path, filename: str, job_id: str):
        """Copy a result into the gallery without ever overwriting a file.

        The first save keeps the generated filename when it is free.  A retry
        for the same job is idempotent when metadata already points at that
        file; a different job gets a deterministic suffix instead.  The
        exclusive create makes the choice safe if two save requests race.
        """
        gallery_dir = Path(self.server.image_dir)
        gallery_dir.mkdir(parents=True, exist_ok=True)
        metadata = ImageMetadataStore(self.server.data_dir).load()
        original = Path(filename)
        stem, suffix = original.stem, original.suffix or '.png'
        candidates = [filename]
        for index in range(0, 100):
            marker = f'_saved_{job_id[:8]}' if index == 0 else f'_saved_{job_id[:8]}_{index}'
            candidates.append(f'{stem}{marker}{suffix}')
        for candidate in candidates:
            target = gallery_dir / candidate
            if target.exists():
                owner = str((metadata.get(candidate) or {}).get('extension_job_id') or '')
                if owner == job_id:
                    return candidate, target, False
                continue
            descriptor = None
            try:
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with source.open('rb') as src, os.fdopen(descriptor, 'wb') as dst:
                    descriptor = None
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                return candidate, target, True
            except FileExistsError:
                # Another request won the race; try the next unique candidate.
                continue
            except Exception:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                try:
                    target.unlink()
                except OSError:
                    pass
                raise
        raise ValueError('画廊中无法创建唯一图片文件名，请稍后重试。')

    async def _read_upload(self, request):
        """Read one bounded multipart image and its request id.

        The extension sends one image per request.  Keeping the upload one-at-a-time
        makes each job independently retryable and avoids large extension messages.
        """
        content_type = request.headers.get('Content-Type', '').lower()
        if not content_type.startswith('multipart/form-data;'):
            raise ValueError('上传必须使用 multipart/form-data。')
        # aiohttp's application limit is raised for the upload route in web_server;
        # this early check avoids allocating a body that can never be valid.
        length = request.headers.get('Content-Length')
        if length:
            try:
                if int(length) > MAX_BYTES + 512 * 1024:
                    raise ValueError('上传图片不能超过 10 MiB。')
            except ValueError as exc:
                if str(exc) == '上传图片不能超过 10 MiB。':
                    raise
                raise ValueError('上传请求大小无效。') from None
        try:
            reader = await request.multipart()
        except Exception as exc:
            raise ValueError('上传表单无效。') from exc
        raw = None
        upload_name = ''
        request_id = ''
        seen = set()
        try:
            async for field in reader:
                name = field.name or ''
                if name in seen:
                    raise ValueError('上传表单字段重复。')
                seen.add(name)
                if name == 'image':
                    if raw is not None:
                        raise ValueError('每次只能上传一张图片。')
                    upload_name = field.filename or ''
                    part_type = (field.headers.get('Content-Type', '') or '').split(';', 1)[0].strip().lower()
                    if part_type and part_type not in UPLOAD_CONTENT_TYPES and part_type != 'application/octet-stream':
                        raise ValueError('只支持 JPEG、PNG 或 WEBP 图片。')
                    data = bytearray()
                    while True:
                        chunk = await field.read_chunk(64 * 1024)
                        if not chunk:
                            break
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise ValueError('上传图片不能超过 10 MiB。')
                    raw = bytes(data)
                elif name == 'request_id':
                    request_id = (await field.text()).strip()
                    if len(request_id) > 100:
                        raise ValueError('请求编号无效，请重新选择图片。')
                elif name == 'filename':
                    # Older extension builds may send a separate display name.
                    value = (await field.text()).strip()
                    if value:
                        upload_name = value
                else:
                    # Do not accept arbitrary metadata from an extension page.
                    await field.release()
                    raise ValueError('上传表单字段无效。')
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError('读取上传图片失败。') from exc
        if raw is None or not raw:
            raise ValueError('请选择要上传的图片。')
        if not re.fullmatch(r'[a-f0-9-]{32,36}', request_id):
            raise ValueError('请求编号无效，请重新选择图片。')
        return raw, request_id, self._safe_source_name(upload_name)

    async def submit_upload(self, request):
        if self.closing:
            return self.response({'error': 'service_stopping'}, 503)
        try:
            raw, rid, source_name = await self._read_upload(request)
            settings = self.settings()
            prompt = validate_prompt(settings['prompt'])
            steps = validate_steps(settings['steps'])
            owner = request['extension_owner']
            job_id = digest(owner + ':' + rid)[:32]
            request_digest = digest('upload:' + hashlib.sha256(raw).hexdigest() + '\n' + prompt + '\n' + str(steps))
        except ValueError as exc:
            return self.response({'error': 'invalid_upload', 'message': str(exc)}, 400)
        async with self.request_lock:
            jobs = self.jobs.load()
            old = jobs.get(job_id)
            if old:
                if old.get('request_digest') != request_digest:
                    return self.response({'error': 'request_conflict', 'message': '请求已存在且参数不同，请先核对原任务。'}, 409)
                return self.response(self.public_job(old), 200)
            if (sum(j.get('status') in ACTIVE for j in jobs.values()) >= MAX_UPLOAD_QUEUE
                    or sum(j.get('status') in ACTIVE and j.get('owner') == owner for j in jobs.values()) >= MAX_UPLOAD_QUEUE):
                return self.response({'error': 'queue_full', 'message': '已有改图任务排队，请等当前任务完成。'}, 429)
            try:
                reference = await asyncio.to_thread(self.store_reference, raw, job_id)
            except (ValueError, OSError) as exc:
                return self.response({'error': 'invalid_upload', 'message': str(exc) or '图片无法读取。'}, 400)
            now = int(time.time())
            job = {
                'id': job_id, 'owner': owner, 'request_digest': request_digest,
                'status': 'queued', 'input_type': 'upload', 'source_name': source_name,
                'source_url': '', 'media_url': '', 'reference': reference,
                'prompt': prompt, 'steps': steps, 'created_at': now, 'updated_at': now,
                'message': '等待 WIND Qwen',
            }
            self.jobs.update(lambda data: data.update({job_id: job}))
            task = asyncio.create_task(self.run(job))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return self.response(self.public_job(job), 202)

    def update_job(self, job_id, **updates):
        def update(jobs):
            if job_id in jobs:
                jobs[job_id].update(updated_at=int(time.time()), **updates)
        self.jobs.update(update)

    def download_reference(self, job):
        """Fixed CDN only, no redirects, bounded decode. No X cookies are needed."""
        url = canonical_media(job['media_url'])
        candidates = [url]
        # X can serve a WebP thumbnail while the original exists only as JPEG.
        # Retry a format only for 404, never another image, a redirect, or a model.
        if 'format=webp&' in url:
            candidates.append(url.replace('format=webp&', 'format=jpg&'))
        started = time.monotonic()
        for index, candidate in enumerate(candidates):
            with requests.get(candidate, stream=True, allow_redirects=False, timeout=(8,25),
                              headers={'User-Agent':'Gallery-Qwen-X/1.0','Accept':'image/*'}) as response:
                if response.status_code == 404 and index + 1 < len(candidates):
                    continue
                if response.status_code != 200:
                    raise ValueError(f'X 原图下载失败（HTTP {response.status_code}）；未提交 Qwen。')
                if not response.headers.get('Content-Type','').lower().startswith('image/'):
                    raise ValueError('X 返回的不是图片；未提交 Qwen。')
                raw = bytearray()
                for chunk in response.iter_content(65536):
                    if time.monotonic() - started > 60:
                        raise ValueError('X 原图下载超时；未提交 Qwen。')
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        raise ValueError('参考图超过 10 MiB；未提交 Qwen。')
            return self.store_reference(bytes(raw), job['id'])
        raise ValueError('X 原图不可用；未提交 Qwen。')

    def store_reference(self, raw, job_id):
        try:
            with Image.open(io.BytesIO(raw)) as source:
                if source.width*source.height > MAX_PIXELS or getattr(source,'is_animated',False):
                    raise ValueError('请使用不超过 2500 万像素的静态图片。')
                if source.format not in {'JPEG','PNG','WEBP'}:
                    raise ValueError('参考图格式不支持。')
                image = ImageOps.exif_transpose(source).convert('RGB')
                image.load()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError('图片无法读取，请上传 JPEG、PNG 或 WEBP。') from exc
        root = Path(self.server.reference_dir) / 'browser-extension'
        root.mkdir(parents=True, exist_ok=True)
        path = root / (job_id + '.png')
        temp = root / (job_id + '.tmp')
        image.save(temp, format='PNG')
        os.replace(temp,path)
        return str(path)

    async def run(self, job):
        async with self.gate:
            try:
                if job.get('input_type') == 'upload':
                    reference = job.get('reference', '')
                    if not reference or not Path(reference).is_file():
                        raise ValueError('上传参考图不存在；未提交 Qwen。')
                    self.update_job(job['id'], status='generating', message='WIND Qwen 正在改图', reference=reference)
                else:
                    self.update_job(job['id'],status='downloading',message='正在读取点击的 X 原图')
                    reference = await asyncio.to_thread(self.download_reference,job)
                self.update_job(job['id'],status='generating',message='WIND Qwen 正在改图',reference=reference)
                result = await asyncio.to_thread(self.server._run_hermes_image_generation,
                    'qwen',job['prompt'],size='auto',ref_image=reference,ref_images=[reference],steps=job['steps'],
                    source='chrome_extension',filename_prefix='qwen_x_edit',classify_style=False,
                    persist_metadata=False, output_dir=str(self.result_dir))
                if not result or not result.get('success'):
                    raise ValueError('Qwen 未返回有效图片；不会换用其他模型。')
                filename = result['filename']
                public = {k:result[k] for k in ('filename','width','height','elapsed','comfy_prompt_id') if k in result}
                public.update(model_name='Qwen-Image-2.1 Q8',generation_mode='img2img')
                self.update_job(job['id'],status='done',message='改图完成，尚未保存到画廊',result=public)
            except Exception as exc:
                msg = self.server._redact_log_text(str(exc))[:1600]
                log.warning('Chrome Qwen job failed: job=%s error=%s',job['id'],msg)
                self.update_job(job['id'],status='error',message=msg or '改图失败；未自动重试。')

    async def status(self, request):
        job = self.get_job(request)
        return self.response(self.public_job(job)) if job else self.response({'error':'job_not_found'},404)

    async def save(self, request):
        """Explicitly publish a completed extension result to the gallery index."""
        async with self.request_lock:
            job = self.get_job(request)
            if not job or job.get('status') != 'done':
                return self.response({'error': 'image_not_ready'}, 404)
            result = job.get('result') or {}
            if result.get('saved_to_gallery'):
                return self.response({'success': True, 'job': self.public_job(job)})
            filename = result.get('filename', '')
            if not re.fullmatch(r'qwen_x_edit_[A-Za-z0-9_.-]+\.png', filename):
                return self.response({'error': 'image_not_found'}, 404)
            path = self.result_dir / filename
            # Jobs created before the opt-in save flow stored their output directly
            # in the gallery image directory; keep those results readable/savable.
            if not path.is_file():
                path = Path(self.server.image_dir) / filename
            if not path.is_file():
                return self.response({'error': 'image_not_found'}, 404)
            metadata = {
                'category': 'portrait', 'source': 'chrome_extension',
                'source_url': job.get('source_url', ''), 'source_media_url': job.get('media_url', ''),
                'source_text': job.get('source_text', ''),
                'source_name': job.get('source_name', ''), 'input_type': job.get('input_type', 'x'),
                'extension_job_id': job.get('id', ''), 'model': 'Qwen-Image-2.1 Q8',
                'model_name': 'Qwen-Image-2.1 Q8', 'prompt': job.get('prompt', ''),
                'custom_prompt': job.get('prompt', ''), 'user_prompt': job.get('prompt', ''),
                'prompt_mode': 'pure', 'pure_prompt': True, 'custom_ref_mode': 'reference',
                'generation_mode': 'img2img', 'created_at': job.get('created_at', int(time.time())),
                'generation_time': result.get('elapsed', ''), 'width': result.get('width', 0),
                'height': result.get('height', 0), 'ref_image': '', 'ref_image_path': '',
            }
            saved_filename, target, created = self._reserve_gallery_result(path, filename, job['id'])
            metadata['file_size_bytes'] = target.stat().st_size
            metadata['saved_filename'] = saved_filename
            if created:
                self.server._update_image_metadata_entry(saved_filename, metadata)
            updated_result = {**result, 'saved_to_gallery': True, 'saved_filename': saved_filename}
            self.update_job(job['id'], message='已保存到画廊', result=updated_result)
            updated_job = {**job, 'message': '已保存到画廊', 'result': updated_result}
            return self.response({'success': True, 'job': self.public_job(updated_job)})

    async def image(self, request):
        job = self.get_job(request)
        if not job or job.get('status') != 'done':
            return self.response({'error':'image_not_ready'},404)
        if request.match_info['kind'] == 'source':
            path = Path(self.server.reference_dir) / 'browser-extension' / (job['id']+'.png')
        else:
            filename = (job.get('result') or {}).get('filename','')
            if not re.fullmatch(r'qwen_x_edit_[A-Za-z0-9_.-]+\.png',filename):
                return self.response({'error':'image_not_found'},404)
            path = self.result_dir / filename
            if not path.is_file():
                path = Path(self.server.image_dir) / filename
        if not path.is_file():
            return self.response({'error':'image_not_found'},404)
        return web.FileResponse(path, headers={'Cache-Control':'private, no-store'})
