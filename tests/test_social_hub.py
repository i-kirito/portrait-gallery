import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image, PngImagePlugin

import sys


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from store import ImageMetadataStore  # noqa: E402
from social import SocialStoreCorruptError  # noqa: E402
from social_hub import instance_id_for_client_token, normalize_hub_url  # noqa: E402
from social_hub_main import create_social_hub_server  # noqa: E402
from web_server import GalleryServer, SocialHubRequestError  # noqa: E402


class SocialHubIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def test_public_http_hub_url_is_rejected_but_loopback_is_allowed(self):
        self.assertEqual(
            "http://localhost:18889",
            normalize_hub_url("http://localhost:18889/"),
        )
        with self.assertRaisesRegex(ValueError, "https_hub_url_required"):
            normalize_hub_url("http://gallery.example.com")

    def test_startup_migrates_legacy_records_and_removes_orphan_media(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "hub"
            data_dir = root / "data"
            media_dir = data_dir / "social-media"
            media_dir.mkdir(parents=True)
            orphan = media_dir / "social_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"
            orphan.write_bytes(b"orphan")
            (data_dir / "social.json").write_text(
                json.dumps({
                    "version": 1,
                    "posts": [{
                        "id": "legacy-post",
                        "author_type": "character",
                        "author_id": "private-character-id",
                        "author_snapshot": {"display_name": "Legacy"},
                        "text": "legacy tweet",
                        "source": {"instance_id": "private-instance"},
                        "reactions": {"like": ["private-viewer"]},
                    }],
                }),
                encoding="utf-8",
            )

            server = self._make_server(root)

            self.assertFalse(orphan.exists())
            migrated = Path(server.social_store.path).read_text(encoding="utf-8")
            for forbidden in (
                "author_type",
                "author_id",
                "private-character-id",
                "source",
                "private-instance",
                "reactions",
                "private-viewer",
            ):
                self.assertNotIn(forbidden, migrated)

    def test_corrupt_store_aborts_before_orphan_media_pruning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "data"
            media_dir = data_dir / "social-media"
            media_dir.mkdir(parents=True)
            media = media_dir / "social_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg"
            media.write_bytes(b"must-stay")
            social_path = data_dir / "social.json"
            corrupt_bytes = b'{"posts": ['
            social_path.write_bytes(corrupt_bytes)

            with self.assertRaisesRegex(
                SocialStoreCorruptError,
                "social_store_corrupt",
            ):
                GalleryServer(
                    {
                        "gallery": {"host": "127.0.0.1", "port": 18889},
                        "social": {
                            "server_token": "hub-server-token-with-at-least-24-chars"
                        },
                    },
                    str(data_dir),
                    social_hub_only=True,
                )

            self.assertEqual(corrupt_bytes, social_path.read_bytes())
            self.assertEqual(b"must-stay", media.read_bytes())

    def test_social_image_reencoding_drops_non_exif_metadata(self):
        cases = []

        jpeg = BytesIO()
        jpeg_exif = Image.Exif()
        jpeg_exif[0x010E] = "private-exif"
        Image.new("RGB", (12, 8), (220, 80, 40)).save(
            jpeg,
            format="JPEG",
            exif=jpeg_exif,
            comment=b"private-comment",
            icc_profile=b"private-icc",
        )
        cases.append(("JPEG", jpeg.getvalue()))

        png = BytesIO()
        png_info = PngImagePlugin.PngInfo()
        png_info.add_text("Comment", "private-comment")
        Image.new("RGB", (12, 8), (40, 180, 100)).save(
            png,
            format="PNG",
            pnginfo=png_info,
            icc_profile=b"private-icc",
        )
        cases.append(("PNG", png.getvalue()))

        gif = BytesIO()
        Image.new("RGB", (12, 8), (60, 100, 220)).save(
            gif,
            format="GIF",
            comment=b"private-comment",
        )
        cases.append(("GIF", gif.getvalue()))

        rgba = BytesIO()
        Image.new("RGBA", (12, 8), (40, 180, 100, 128)).save(
            rgba,
            format="PNG",
        )

        for image_format, source in cases:
            with self.subTest(image_format=image_format):
                encoded, extension = GalleryServer._encode_social_image(source)
                self.assertNotIn(b"private", encoded)
                with Image.open(BytesIO(encoded)) as clean:
                    expected_format = "GIF" if image_format == "GIF" else "JPEG"
                    self.assertEqual(expected_format, clean.format, image_format)
                    for key in (
                        "Comment",
                        "comment",
                        "exif",
                        "icc_profile",
                        "xmp",
                        "XML:com.adobe.xmp",
                    ):
                        self.assertNotIn(key, clean.info)

        encoded, extension = GalleryServer._encode_social_image(rgba.getvalue())
        with Image.open(BytesIO(encoded)) as clean:
            self.assertEqual("PNG", clean.format)
        self.assertEqual(".png", extension)

    async def test_hub_only_server_exposes_no_gallery_surface_or_extra_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "data"
            media_dir = data_dir / "social-media"
            media_dir.mkdir(parents=True)
            stale_paths = (
                data_dir / ".social_interrupted.tmp",
                media_dir / ".social_media_interrupted.tmp",
                media_dir / ".social_avatar_interrupted.tmp",
            )
            for stale_path in stale_paths:
                stale_path.write_bytes(b"partial")
            server_token = "hub-server-token-with-at-least-24-chars"
            server = GalleryServer(
                {
                    "gallery": {"host": "127.0.0.1", "port": 18889},
                    "social": {"server_token": server_token},
                },
                str(data_dir),
                social_hub_only=True,
            )
            client = await self._start_client(server)
            try:
                health = await client.get("/api/health")
                self.assertEqual(200, health.status)
                self.assertEqual(
                    {"status": "ok", "service": "social-hub"},
                    await health.json(),
                )

                for unavailable_path in (
                    "/",
                    "/static/index.html",
                    "/api/today",
                    "/api/gallery",
                    "/api/group-chat/rooms",
                    "/api/social/posts",
                    "/images/private.jpg",
                ):
                    response = await client.get(unavailable_path)
                    self.assertEqual(404, response.status, unavailable_path)

                unauthorized = await client.get("/api/social/hub/status")
                self.assertEqual(401, unauthorized.status)

                client_token = "client-token-with-at-least-24-chars"
                authorized = await client.get(
                    "/api/social/hub/status",
                    headers={
                        "X-Social-Hub-Token": server_token,
                        "X-Social-Instance-ID": instance_id_for_client_token(client_token),
                        "X-Social-Client-Token": client_token,
                    },
                )
                self.assertEqual(200, authorized.status)
                self.assertEqual({"ok": True, "hub": True}, await authorized.json())
            finally:
                await client.close()

            self.assertEqual(
                {"social.json", "social.lock", "social-media"},
                {path.name for path in data_dir.iterdir()},
            )
            serialized = (data_dir / "social.json").read_text(encoding="utf-8")
            self.assertNotIn(server_token, serialized)
            self.assertFalse((data_dir / "social_hub.json").exists())
            self.assertFalse((data_dir / "social_hub.lock").exists())
            self.assertTrue(all(not path.exists() for path in stale_paths))

    async def test_remote_redirect_is_rejected_without_following_it(self):
        redirect_app = web.Application()

        async def redirect(_request):
            raise web.HTTPTemporaryRedirect(location="/unexpected-target")

        redirect_app.router.add_get("/api/social/hub/status", redirect)
        redirect_server = TestServer(redirect_app)
        await redirect_server.start_server()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                local = self._make_server(Path(tmpdir) / "local")
                settings = local._social_settings()
                settings.update({
                    "hub_url": str(redirect_server.make_url("/")).rstrip("/"),
                    "hub_token": "redirect-test-token-with-enough-length",
                    "timeout_seconds": 5,
                })
                with self.assertRaises(SocialHubRequestError) as raised:
                    await local._social_remote_request(
                        settings,
                        "GET",
                        "/api/social/hub/status",
                    )
                self.assertEqual(502, raised.exception.status)
                self.assertEqual(
                    "social_hub_redirect_rejected",
                    raised.exception.payload["error"],
                )
        finally:
            await redirect_server.close()

    async def test_remote_publish_rejects_success_without_a_post(self):
        fake_hub = web.Application()

        async def empty_success(_request):
            return web.json_response({})

        fake_hub.router.add_post("/api/social/hub/posts", empty_success)
        fake_server = TestServer(fake_hub)
        await fake_server.start_server()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                local = self._make_server(root / "local")
                self._register_character(local, "alice", "Alice", (190, 90, 120))
                local.social_hub_settings.update_client(
                    hub_url=str(fake_server.make_url("/")).rstrip("/"),
                    hub_token="fake-hub-token-with-enough-length",
                    display_name="Alice",
                )
                client = await self._start_client(local)
                try:
                    response = await client.post("/api/social/posts", json={
                        "author_type": "character",
                        "author_id": "alice",
                        "text": "这条不能被误报为发布成功",
                    })
                    self.assertEqual(502, response.status)
                    self.assertEqual(
                        "invalid_social_hub_response",
                        (await response.json())["error"],
                    )
                finally:
                    await client.close()
        finally:
            await fake_server.close()

    async def test_user_posts_and_replies_use_configured_display_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir) / "local")
            server.social_hub_settings.update_client(
                hub_url="",
                display_name="小林",
            )
            client = await self._start_client(server)
            try:
                create_response = await client.post("/api/social/posts", json={
                    "author_type": "user",
                    "author_id": "user",
                    "author_name": "伪造名称",
                    "text": "真人发布",
                })
                self.assertEqual(201, create_response.status)
                post = (await create_response.json())["post"]
                self.assertEqual("小林", post["author_snapshot"]["display_name"])

                reply_response = await client.post(
                    f"/api/social/posts/{post['id']}/comments",
                    json={
                        "author_type": "user",
                        "author_id": "user",
                        "text": "真人回复",
                    },
                )
                self.assertEqual(201, reply_response.status)
                reply_post = (await reply_response.json())["post"]
                self.assertEqual(
                    "小林",
                    reply_post["comments"][0]["author_snapshot"]["display_name"],
                )
            finally:
                await client.close()

            serialized = Path(server.social_store.path).read_text(encoding="utf-8")
            self.assertNotIn("伪造名称", serialized)
            self.assertNotIn('"display_name": "我"', serialized)

    def test_dedicated_entry_requires_runtime_secret(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(RuntimeError, "at least 24"):
                create_social_hub_server(
                    data_dir=tmpdir,
                    server_token="too-short",
                )

            server = create_social_hub_server(
                data_dir=tmpdir,
                host="127.0.0.1",
                port="18999",
                server_token="dedicated-hub-token-with-enough-length",
            )
            self.assertTrue(server.social_hub_only)
            self.assertEqual("127.0.0.1", server.host)
            self.assertEqual(18999, server.port)
            self.assertEqual(
                "dedicated-hub-token-with-enough-length",
                server._social_settings()["server_token"],
            )

            with self.assertRaisesRegex(RuntimeError, "printable ASCII"):
                create_social_hub_server(
                    data_dir=tmpdir,
                    server_token="中文令牌" * 12,
                )

    def test_dedicated_entry_reads_runtime_secret_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            token_path = Path(tmpdir) / "social_server_token"
            token_path.write_text(
                "file-backed-token-with-enough-length\n",
                encoding="ascii",
            )
            with patch.dict(
                "os.environ",
                {
                    "SOCIAL_SERVER_TOKEN": "",
                    "SOCIAL_SERVER_TOKEN_FILE": str(token_path),
                },
                clear=False,
            ):
                server = create_social_hub_server(data_dir=tmpdir)
            self.assertEqual(
                "file-backed-token-with-enough-length",
                server._social_settings()["server_token"],
            )

    @staticmethod
    def _make_server(root: Path) -> GalleryServer:
        data_dir = root / "data"
        config_path = root / "config" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("gallery:\n  port: 18889\n", encoding="utf-8")
        (root / "app" / "references").mkdir(parents=True, exist_ok=True)
        return GalleryServer(
            {
                "paths": {"project_root": str(root)},
                "gallery": {"port": 18889, "host": "127.0.0.1"},
            },
            str(data_dir),
            str(config_path),
        )

    @staticmethod
    async def _start_client(server: GalleryServer) -> TestClient:
        client = TestClient(TestServer(server.app))
        await client.start_server()
        return client

    @staticmethod
    def _register_gallery_image(server: GalleryServer, filename: str) -> Path:
        path = Path(server.image_dir) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        exif = Image.Exif()
        exif[0x010E] = "private local generation metadata"
        Image.new("RGB", (32, 24), (64, 128, 192)).save(
            path,
            format="JPEG",
            exif=exif,
        )
        ImageMetadataStore(server.data_dir).save({filename: {"favorite": False}})
        return path

    @staticmethod
    def _register_character(
        server: GalleryServer,
        character_id: str,
        name: str,
        color: tuple[int, int, int],
    ) -> Path:
        path = Path(server.reference_dir) / f"{character_id}-avatar.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        exif = Image.Exif()
        exif[0x010E] = "private local avatar metadata"
        Image.new("RGB", (360, 480), color).save(path, format="JPEG", exif=exif)
        server.config["characters"] = [{
            "id": character_id,
            "name": name,
            "reference_image": str(path),
        }]
        return path

    async def test_two_galleries_share_uploaded_post_and_keep_central_copy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            hub = create_social_hub_server(
                data_dir=str(root / "hub" / "data"),
                host="127.0.0.1",
                port=18889,
                server_token="integration-hub-token-with-enough-length",
            )
            alice = self._make_server(root / "alice")
            bob = self._make_server(root / "bob")
            alice_avatar_path = self._register_character(
                alice,
                "alice",
                "Alice",
                (196, 92, 122),
            )
            self._register_character(
                bob,
                "bob",
                "Bob",
                (52, 132, 176),
            )
            hub_client = await self._start_client(hub)
            alice_client = await self._start_client(alice)
            bob_client = await self._start_client(bob)
            try:
                hub_url = str(hub_client.make_url("/")).rstrip("/")
                hub_token = hub._social_settings()["server_token"]
                alice.social_hub_settings.update_client(
                    hub_url=hub_url,
                    hub_token=hub_token,
                    display_name="Alice 的画廊",
                )
                bob.social_hub_settings.update_client(
                    hub_url=hub_url,
                    hub_token=hub_token,
                    display_name="Bob 的画廊",
                )
                source_path = self._register_gallery_image(alice, "alice-post.jpg")
                source_bytes = source_path.read_bytes()

                with patch.object(
                    alice,
                    "_social_picgo_settings",
                    return_value={"repo": "", "branch": "master", "path": "img", "token": ""},
                ):
                    create_response = await alice_client.post("/api/social/posts", json={
                        "author_type": "character",
                        "author_id": "alice",
                        "text": "来自 Alice 本地画廊的第一张图",
                        "media": [{
                            "image_filename": "alice-post.jpg",
                            "alt": "不应传到中心的本地提示词",
                        }],
                        "source": {"type": "private-local-source"},
                    })
                self.assertEqual(201, create_response.status)
                created = await create_response.json()
                post = created["post"]
                self.assertEqual("Alice", post["author_snapshot"]["display_name"])
                self.assertTrue(
                    post["author_snapshot"]["avatar"].startswith(
                        "/api/social/media/avatar_"
                    )
                )
                self.assertNotIn("source", post)
                self.assertEqual("/api/social/media/", post["media"][0]["image_url"][:18])
                social_filename = post["media"][0]["image_filename"]
                self.assertTrue((Path(hub.social_media_dir) / social_filename).is_file())
                avatar_filename = post["author_snapshot"]["avatar"].rsplit("/", 1)[-1]
                self.assertTrue((Path(hub.social_media_dir) / avatar_filename).is_file())

                bob_feed_response = await bob_client.get("/api/social/posts")
                self.assertEqual(200, bob_feed_response.status)
                bob_feed = await bob_feed_response.json()
                self.assertEqual([post["id"]], [item["id"] for item in bob_feed["posts"]])
                bob_post = bob_feed["posts"][0]
                self.assertEqual("Alice", bob_post["author_snapshot"]["display_name"])
                self.assertFalse(bob_post["can_delete"])
                self.assertTrue(bob_post["media"][0]["image_url"].startswith("/api/social/media/"))

                image_response = await bob_client.get(bob_post["media"][0]["image_url"])
                self.assertEqual(200, image_response.status)
                image_bytes = await image_response.read()
                self.assertGreater(len(image_bytes), 20)
                self.assertEqual(b"\xff\xd8", image_bytes[:2])
                with Image.open(BytesIO(image_bytes)) as shared_image:
                    self.assertEqual({}, dict(shared_image.getexif()))

                avatar_response = await bob_client.get(
                    bob_post["author_snapshot"]["avatar"]
                )
                self.assertEqual(200, avatar_response.status)
                avatar_bytes = await avatar_response.read()
                with Image.open(BytesIO(avatar_bytes)) as shared_avatar:
                    self.assertEqual((256, 256), shared_avatar.size)
                    self.assertEqual({}, dict(shared_avatar.getexif()))

                source_path.unlink()
                alice_avatar_path.unlink()
                still_available = await bob_client.get(bob_post["media"][0]["image_url"])
                self.assertEqual(200, still_available.status)
                self.assertEqual(b"\xff\xd8", (await still_available.read())[:2])
                avatar_still_available = await bob_client.get(
                    bob_post["author_snapshot"]["avatar"]
                )
                self.assertEqual(200, avatar_still_available.status)

                comment_response = await bob_client.post(
                    f"/api/social/posts/{post['id']}/comments",
                    json={
                        "author_type": "character",
                        "author_id": "bob",
                        "text": "我在另一台画廊看到了这张图。",
                    },
                )
                self.assertEqual(201, comment_response.status)
                comment_post = (await comment_response.json())["post"]
                self.assertEqual(1, comment_post["comment_count"])
                self.assertTrue(comment_post["comments"][0]["can_delete"])
                self.assertEqual(
                    "Bob",
                    comment_post["comments"][0]["author_snapshot"]["display_name"],
                )
                self.assertTrue(
                    comment_post["comments"][0]["author_snapshot"]["avatar"].startswith(
                        "/api/social/media/avatar_"
                    )
                )

                social_before_reaction = Path(hub.social_store.path).read_bytes()
                reaction_response = await bob_client.post(
                    f"/api/social/posts/{post['id']}/reactions",
                    json={"kind": "like"},
                )
                self.assertEqual(200, reaction_response.status)
                reaction_data = await reaction_response.json()
                self.assertTrue(reaction_data["local_only"])
                self.assertEqual(
                    social_before_reaction,
                    Path(hub.social_store.path).read_bytes(),
                )

                forbidden_delete = await bob_client.delete(f"/api/social/posts/{post['id']}")
                self.assertEqual(403, forbidden_delete.status)
                self.assertEqual("social_owner_required", (await forbidden_delete.json())["error"])

                alice_feed = await alice_client.get("/api/social/posts")
                alice_post = (await alice_feed.json())["posts"][0]
                self.assertTrue(alice_post["can_delete"])
                self.assertNotIn("like", alice_post["viewer_reactions"])
                self.assertFalse(alice_post["comments"][0]["can_delete"])

                raw_social = json.loads(
                    Path(hub.social_store.path).read_text(encoding="utf-8")
                )
                stored_post = raw_social["posts"][0]
                self.assertEqual(
                    {"id", "author_snapshot", "text", "media", "comments", "created_at"},
                    set(stored_post),
                )
                self.assertEqual(
                    {"display_name", "avatar"},
                    set(stored_post["author_snapshot"]),
                )
                self.assertEqual(
                    {"type", "image_filename", "image_url"},
                    set(stored_post["media"][0]),
                )
                self.assertEqual(
                    {"id", "author_snapshot", "text", "created_at"},
                    set(stored_post["comments"][0]),
                )
                serialized_social = json.dumps(raw_social, ensure_ascii=False)
                self.assertNotIn(
                    alice._social_settings()["instance_id"],
                    serialized_social,
                )
                self.assertNotIn(
                    bob._social_settings()["instance_id"],
                    serialized_social,
                )
                for forbidden in (
                    "author_type",
                    "author_id",
                    "source",
                    "reactions",
                    "instance_id",
                    "local_character_id",
                    "private-local-source",
                    "不应传到中心的本地提示词",
                    "Alice 的画廊",
                    "Bob 的画廊",
                ):
                    self.assertNotIn(forbidden, serialized_social)

                self.assertFalse(Path(hub.social_hub_settings.store.path).exists())
                self.assertFalse(Path(hub.social_hub_settings.store.lock_path).exists())

                spoofed_delete = await hub_client.delete(
                    f"/api/social/hub/posts/{post['id']}",
                    headers={
                        "X-Social-Hub-Token": hub_token,
                        "X-Social-Instance-ID": alice._social_settings()["instance_id"],
                        "X-Social-Client-Token": bob._social_settings()["client_token"],
                    },
                )
                self.assertEqual(401, spoofed_delete.status)
                self.assertIsNotNone(hub.social_store.get_post(post["id"]))

                existing_media = set(Path(hub.social_media_dir).iterdir())
                failed_upload = FormData()
                failed_upload.add_field(
                    "payload",
                    json.dumps({
                        "author_snapshot": {"display_name": "Rollback"},
                        "text": "这条不应保存",
                        "media": [
                            {"upload_key": "media_0"},
                            {"upload_key": "media_1"},
                        ],
                    }),
                    content_type="application/json",
                )
                failed_upload.add_field(
                    "media_0",
                    source_bytes,
                    filename="valid.jpg",
                    content_type="image/jpeg",
                )
                failed_upload.add_field(
                    "media_1",
                    b"not-an-image",
                    filename="invalid.jpg",
                    content_type="image/jpeg",
                )
                failed_upload_response = await hub_client.post(
                    "/api/social/hub/posts",
                    data=failed_upload,
                    headers={
                        "X-Social-Hub-Token": hub_token,
                        "X-Social-Instance-ID": alice._social_settings()["instance_id"],
                        "X-Social-Client-Token": alice._social_settings()["client_token"],
                    },
                )
                self.assertEqual(400, failed_upload_response.status)
                self.assertEqual(
                    existing_media,
                    set(Path(hub.social_media_dir).iterdir()),
                )

                unexpected_field = FormData()
                unexpected_field.add_field(
                    "payload",
                    json.dumps({"text": "should not be accepted"}),
                    content_type="application/json",
                )
                unexpected_field.add_field(
                    "untrusted_blob",
                    b"not-an-upload",
                    filename="untrusted.bin",
                    content_type="application/octet-stream",
                )
                unexpected_field_response = await hub_client.post(
                    "/api/social/hub/posts",
                    data=unexpected_field,
                    headers={
                        "X-Social-Hub-Token": hub_token,
                        "X-Social-Instance-ID": alice._social_settings()["instance_id"],
                        "X-Social-Client-Token": alice._social_settings()["client_token"],
                    },
                )
                self.assertEqual(400, unexpected_field_response.status)
                self.assertEqual(
                    "invalid_social_field",
                    (await unexpected_field_response.json())["error"],
                )

                config_response = await alice_client.get("/api/social/config")
                self.assertEqual(200, config_response.status)
                config = await config_response.json()
                self.assertNotIn("hub_token", config)
                self.assertNotIn("client_token", config)
                self.assertTrue(config["hub_token_configured"])
                self.assertTrue(config["server_token"])

                bad_hub_status = await hub_client.get(
                    "/api/social/hub/status",
                    headers={
                        "X-Social-Hub-Token": "not-the-token",
                        "X-Social-Instance-ID": alice._social_settings()["instance_id"],
                    },
                )
                self.assertEqual(401, bad_hub_status.status)
                self.assertEqual(
                    {"social.json", "social.lock", "social-media"},
                    {path.name for path in Path(hub.data_dir).iterdir()},
                )
            finally:
                await bob_client.close()
                await alice_client.close()
                await hub_client.close()


    async def test_social_config_persists_github_picgo_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir) / "local")
            with patch.object(
                GalleryServer,
                "_picgo_config_path",
                return_value="/nonexistent/picgo-config.json",
            ):
                client = await self._start_client(server)
                try:
                    save_response = await client.post("/api/social/config", json={
                        "display_name": "小林",
                        "hub_url": "",
                        "github_repo": "test-owner/test-repo",
                        "github_branch": "dev",
                        "github_image_path": "images",
                        "github_token": "github_pat_abcdefghijklmnopqrstuvwx",
                    })
                    self.assertEqual(200, save_response.status)
                    saved = await save_response.json()
                    self.assertTrue(saved["saved"])
                    self.assertEqual("test-owner/test-repo", saved["github_repo"])
                    self.assertEqual("dev", saved["github_branch"])
                    self.assertEqual("images", saved["github_image_path"])
                    self.assertTrue(saved["github_token_configured"])
                    self.assertNotIn("github_token", saved)

                    load_response = await client.get("/api/social/config")
                    self.assertEqual(200, load_response.status)
                    loaded = await load_response.json()
                    self.assertEqual("test-owner/test-repo", loaded["github_repo"])
                    self.assertEqual("dev", loaded["github_branch"])
                    self.assertEqual("images", loaded["github_image_path"])
                    self.assertTrue(loaded["github_token_configured"])
                    self.assertNotIn("github_token", loaded)

                    bad_response = await client.post("/api/social/config", json={
                        "display_name": "小林",
                        "hub_url": "",
                        "github_repo": "not-a-repo",
                    })
                    self.assertEqual(400, bad_response.status)
                    self.assertEqual(
                        "invalid_github_repo",
                        (await bad_response.json())["error"],
                    )

                    clear_response = await client.post("/api/social/config", json={
                        "display_name": "小林",
                        "hub_url": "",
                        "github_repo": "",
                        "github_token": "",
                    })
                    self.assertEqual(200, clear_response.status)
                    cleared = await clear_response.json()
                    self.assertEqual("", cleared["github_repo"])
                    self.assertFalse(cleared["github_token_configured"])
                finally:
                    await client.close()

    async def test_hub_media_urls_endpoint_persists_remote_urls(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = create_social_hub_server(
                data_dir=str(Path(tmpdir) / "hub" / "data"),
                host="127.0.0.1",
                port=18889,
                server_token="integration-hub-token-with-enough-length",
            )
            client = await self._start_client(server)
            try:
                server_token = server._social_settings()["server_token"]
                client_token = "client-token-with-at-least-24-chars"
                instance_id = instance_id_for_client_token(client_token)
                headers = {
                    "X-Social-Hub-Token": server_token,
                    "X-Social-Instance-ID": instance_id,
                    "X-Social-Client-Token": client_token,
                }
                image_bytes = BytesIO()
                Image.new("RGB", (16, 12), (10, 200, 90)).save(
                    image_bytes,
                    format="JPEG",
                )
                form = FormData()
                form.add_field(
                    "payload",
                    json.dumps({
                        "author_snapshot": {"display_name": "Alice"},
                        "text": "hub attach test",
                        "media": [{"upload_key": "media_0"}],
                    }),
                    content_type="application/json",
                )
                form.add_field(
                    "media_0",
                    image_bytes.getvalue(),
                    filename="social_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg",
                    content_type="application/octet-stream",
                )
                create_response = await client.post(
                    "/api/social/hub/posts",
                    data=form,
                    headers=headers,
                )
                self.assertEqual(201, create_response.status)
                post = (await create_response.json())["post"]
                media = post["media"][0]
                remote = (
                    "https://raw.githubusercontent.com/i-kirito/picx-images-hosting/"
                    f"master/img/{media['image_filename']}"
                )

                attach_response = await client.post(
                    f"/api/social/hub/posts/{post['id']}/media-urls",
                    json={"media_urls": {media["image_filename"]: remote}},
                    headers=headers,
                )
                self.assertEqual(200, attach_response.status)
                attached = (await attach_response.json())["post"]
                self.assertEqual(remote, attached["media"][0]["remote_url"])
                self.assertEqual(remote, attached["media"][0]["image_url"])

                other_client_token = "other-client-token-with-at-least-24-chars"
                other_headers = {
                    "X-Social-Hub-Token": server_token,
                    "X-Social-Instance-ID": instance_id_for_client_token(
                        other_client_token
                    ),
                    "X-Social-Client-Token": other_client_token,
                }
                overwrite_attempt = await client.post(
                    f"/api/social/hub/posts/{post['id']}/media-urls",
                    json={
                        "media_urls": {
                            media["image_filename"]: "https://example.com/other.jpg"
                        }
                    },
                    headers=other_headers,
                )
                self.assertEqual(403, overwrite_attempt.status)
                self.assertEqual(
                    "social_owner_required",
                    (await overwrite_attempt.json())["error"],
                )
                preserved = server.social_store.get_post(
                    post["id"],
                    viewer_instance_id=instance_id,
                )
                self.assertEqual(remote, preserved["media"][0]["remote_url"])

                denied = await client.post(
                    f"/api/social/hub/posts/{post['id']}/media-urls",
                    json={"media_urls": {media["image_filename"]: remote}},
                )
                self.assertEqual(401, denied.status)

                bad = await client.post(
                    f"/api/social/hub/posts/{post['id']}/media-urls",
                    json={"media_urls": {"nope.txt": "https://example.com/x.png"}},
                    headers=headers,
                )
                self.assertEqual(400, bad.status)

                missing = await client.post(
                    "/api/social/hub/posts/does-not-exist/media-urls",
                    json={"media_urls": {media["image_filename"]: remote}},
                    headers=headers,
                )
                self.assertEqual(404, missing.status)
            finally:
                await client.close()

    async def test_remote_publish_uploads_media_to_github_and_attaches_on_hub(self):
        received: dict = {}

        async def fake_create(_request):
            return web.json_response({
                "post": {
                    "id": "hub-post-123",
                    "author_snapshot": {"display_name": "Alice", "avatar": ""},
                    "text": "GitHub 图床发布",
                    "media": [{
                        "type": "image",
                        "image_filename": (
                            "social_11111111111111111111111111111111.jpg"
                        ),
                        "image_url": (
                            "/api/social/media/"
                            "social_11111111111111111111111111111111.jpg"
                        ),
                    }],
                },
            }, status=201)

        async def fake_attach(request):
            body = await request.json()
            received["media_urls"] = body.get("media_urls") or {}
            remote = (
                "https://raw.githubusercontent.com/i-kirito/picx-images-hosting/"
                "master/img/social_11111111111111111111111111111111.jpg"
            )
            return web.json_response({
                "post": {
                    "id": "hub-post-123",
                    "author_snapshot": {"display_name": "Alice", "avatar": ""},
                    "text": "GitHub 图床发布",
                    "media": [{
                        "type": "image",
                        "image_filename": (
                            "social_11111111111111111111111111111111.jpg"
                        ),
                        "image_url": remote,
                        "remote_url": remote,
                    }],
                },
            })

        fake_hub = web.Application()
        fake_hub.router.add_post("/api/social/hub/posts", fake_create)
        fake_hub.router.add_post(
            "/api/social/hub/posts/hub-post-123/media-urls",
            fake_attach,
        )
        fake_server = TestServer(fake_hub)
        await fake_server.start_server()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                local = self._make_server(root / "local")
                self._register_character(local, "alice", "Alice", (190, 90, 120))
                self._register_gallery_image(local, "alice-post.jpg")
                local.social_hub_settings.update_client(
                    hub_url=str(fake_server.make_url("/")).rstrip("/"),
                    hub_token="fake-hub-token-with-enough-length",
                    display_name="Alice",
                )
                with patch.object(
                    local,
                    "_social_picgo_settings",
                    return_value={
                        "repo": "i-kirito/picx-images-hosting",
                        "branch": "master",
                        "path": "img",
                        "token": "fake-token",
                    },
                ), patch.object(
                    local,
                    "_github_api_upload_file",
                    return_value=(
                        "https://raw.githubusercontent.com/i-kirito/"
                        "picx-images-hosting/master/img/"
                        "social_11111111111111111111111111111111.jpg"
                    ),
                ):
                    client = await self._start_client(local)
                    try:
                        response = await client.post("/api/social/posts", json={
                            "author_type": "character",
                            "author_id": "alice",
                            "text": "GitHub 图床发布",
                            "media": [{"image_filename": "alice-post.jpg"}],
                        })
                        self.assertEqual(201, response.status)
                        post = (await response.json())["post"]
                    finally:
                        await client.close()
                self.assertEqual(
                    {
                        "social_11111111111111111111111111111111.jpg": (
                            "https://raw.githubusercontent.com/i-kirito/"
                            "picx-images-hosting/master/img/"
                            "social_11111111111111111111111111111111.jpg"
                        )
                    },
                    received.get("media_urls"),
                )
                self.assertTrue(
                    post["media"][0]["image_url"].startswith(
                        "https://raw.githubusercontent.com/"
                    )
                )
        finally:
            await fake_server.close()


    async def test_schedule_tweet_endpoint_uses_llm_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir) / "local")
            client = await self._start_client(server)
            try:
                payload = {
                    "theme_day": "霍格沃兹魔法体验日",
                    "outfit_style": "优雅风",
                    "caption": "今天先按这个节奏来：早上整理笔记…",
                    "schedule": [
                        {"time": "08:15", "activity": "在图书馆整理笔记"},
                        {"time": "12:35", "activity": "享用慢炖牛肉午餐"},
                    ],
                    "image_count": 6,
                }
                with patch.object(
                    server,
                    "_call_schedule_tweet_llm",
                    new=AsyncMock(return_value=("今天的推文文案，自然又有生活感。", "gemini-3.5-flash")),
                ):
                    response = await client.post("/api/social/schedule-tweet", json=payload)
                    self.assertEqual(200, response.status)
                    data = await response.json()
                    self.assertEqual("今天的推文文案，自然又有生活感。", data["text"])
                    self.assertEqual("gemini-3.5-flash", data["model"])
                    call_prompt = server._call_schedule_tweet_llm.await_args.args[0]
                    self.assertIn("霍格沃兹魔法体验日", call_prompt)
                    self.assertIn("优雅风", call_prompt)
                    self.assertIn("08:15 在图书馆整理笔记", call_prompt)
                    self.assertIn("配 6 张图片", call_prompt)

                with patch.object(
                    server,
                    "_call_schedule_tweet_llm",
                    new=AsyncMock(side_effect=RuntimeError("llm_unavailable")),
                ):
                    failed = await client.post("/api/social/schedule-tweet", json=payload)
                    self.assertEqual(502, failed.status)
                    self.assertEqual(
                        "schedule_tweet_llm_failed",
                        (await failed.json())["error"],
                    )

                invalid = await client.post(
                    "/api/social/schedule-tweet",
                    data="not-json",
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(400, invalid.status)
            finally:
                await client.close()


if __name__ == "__main__":
    unittest.main()
