import asyncio
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
_TEST_LOG_DIR = tempfile.TemporaryDirectory(prefix="portrait-gallery-xhs-favorites-")
os.environ["HERMES_GALLERY_LOG"] = str(Path(_TEST_LOG_DIR.name) / "gallery.log")

from main import PortraitGalleryApp  # noqa: E402
from store import ScheduleStore  # noqa: E402
from web_server import GalleryServer  # noqa: E402
from xiaohongshu_favorites import (  # noqa: E402
    COLLECTION_FAVORITES,
    COLLECTION_HISTORY,
    XiaohongshuFavoriteLibrary,
    build_post_url,
    make_outfit_key,
)

OCTOBER = datetime(2026, 10, 7, 12, 0)
POST_A = "6a057b86000000003502961e"
POST_B = "6a057b86000000003502962f"


def _record(suffix: str, **overrides) -> dict:
    post_id = overrides.pop("post_id", POST_A)
    record = {
        "outfit_key": make_outfit_key(post_id, f"img{suffix}"),
        "image_sha256": hashlib.sha256(suffix.encode()).hexdigest(),
        "post_id": post_id,
        "post_url": build_post_url(post_id, "secret-token"),
        "xsec_token": "secret-token",
        "title": "秋日通勤穿搭",
        "author": "测试博主",
        "images": [{"index": 1, "filename": f"fav_{suffix}.png", "width": 90, "height": 120}],
    }
    record.update(overrides)
    return record


class FavoriteLibraryLifecycleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = self._tmp.name
        self.now = OCTOBER
        self.library = self._library()

    def _library(self) -> XiaohongshuFavoriteLibrary:
        return XiaohongshuFavoriteLibrary(
            self.data_dir,
            os.path.join(self.data_dir, "img"),
            now=lambda: self.now,
        )

    def _save(self, suffix: str, **overrides) -> dict:
        item, _created = self.library.save(_record(suffix, **overrides))
        return item

    # --- save / deduplication -------------------------------------------------
    def test_repeated_save_is_deduplicated_by_outfit_identity(self):
        first, created = self.library.save(_record("a"))
        again, created_again = self.library.save(_record("a", title="另一个标题"))

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(2, again["save_count"])
        self.assertEqual("秋日通勤穿搭", again["title"])  # existing data is not overwritten
        self.assertEqual(1, self.library.counts()["favorites"])

    def test_same_image_bytes_from_another_post_is_a_duplicate(self):
        self.library.save(_record("a"))
        repost, created = self.library.save(
            _record("a", post_id=POST_B, outfit_key=make_outfit_key(POST_B, "other"))
        )

        self.assertFalse(created)
        self.assertEqual(1, self.library.counts()["favorites"])

    def test_different_images_of_one_post_stay_separate_outfits(self):
        one = self._save("a")
        two = self._save("b")

        self.assertNotEqual(one["id"], two["id"])
        self.assertEqual(one["post_id"], two["post_id"])
        self.assertEqual(2, self.library.counts()["favorites"])

    def test_public_item_never_exposes_the_stored_token(self):
        item = self._save("a")
        public = self.library.public_item(item)

        self.assertNotIn("xsec_token", public)
        self.assertNotIn("secret-token", json.dumps({k: v for k, v in public.items() if k != "post_url"}))
        self.assertTrue(public["post_url"].startswith("https://www.xiaohongshu.com/explore/"))

    # --- persistence -----------------------------------------------------------
    def test_favorites_history_and_settings_survive_restart(self):
        keep = self._save("a")
        used = self._save("b")
        self.library.reserve_for_date("2026-10-08", context_text="通勤")
        self.library.set_auto_schedule(False)
        self.library.mark_used(used["id"], "2026-10-08", "img_1.png")

        reloaded = self._library()

        self.assertEqual(COLLECTION_FAVORITES, reloaded.get(keep["id"])["collection"])
        self.assertEqual(COLLECTION_HISTORY, reloaded.get(used["id"])["collection"])
        self.assertEqual(1, len(reloaded.get(used["id"])["usage"]))
        self.assertFalse(reloaded.settings()["auto_schedule"])

    # --- automatic selection ---------------------------------------------------
    def test_auto_selection_reserves_an_unused_favorite(self):
        item = self._save("a")

        picked = self.library.reserve_for_date("2026-10-08", context_text="秋季通勤穿搭")

        self.assertEqual(item["id"], picked["id"])
        self.assertEqual("reserved", self.library.item_state(self.library.get(item["id"])))
        self.assertEqual(COLLECTION_FAVORITES, self.library.get(item["id"])["collection"])

    def test_season_mismatch_is_not_selected_and_context_prefers_relevant(self):
        summer = self._save("a", title="夏日海边度假穿搭")
        general = self._save("b", title="简约日常穿搭")

        # October: the summer-only outfit is unsuitable, the untagged one is fine.
        picked = self.library.reserve_for_date("2026-10-08", context_text="日常")
        self.assertEqual(general["id"], picked["id"])
        self.assertEqual("summer", self.library.get(summer["id"])["seasons"][0])
        # July: the summer outfit becomes eligible.
        self.library.release("2026-10-08")
        july = self.library.reserve_for_date("2026-07-08", context_text="夏日海边")
        self.assertEqual(summer["id"], july["id"])

    def test_no_suitable_favorite_returns_empty_so_caller_keeps_fallback(self):
        self._save("a", title="夏日海边度假穿搭")

        self.assertEqual({}, self.library.reserve_for_date("2026-12-20", context_text="通勤"))
        self.assertEqual({}, XiaohongshuFavoriteLibrary(
            self.data_dir, os.path.join(self.data_dir, "img")
        ).reserve_for_date("2026-12-20"))

    def test_auto_schedule_switch_disables_pool_without_touching_items(self):
        self._save("a")
        self.library.set_auto_schedule(False)

        self.assertEqual({}, self.library.reserve_for_date("2026-10-08"))
        self.assertEqual(1, self.library.counts()["available"])

    # --- reservations: concurrency, retries, restarts, cancel ------------------
    def test_same_favorite_is_never_reserved_for_two_dates_concurrently(self):
        self._save("a")
        winners = []
        errors = []

        def reserve(day: int):
            try:
                picked = self._library().reserve_for_date(f"2026-10-{day:02d}")
                if picked:
                    winners.append(day)
            except Exception as exc:  # pragma: no cover - surfaced by assertion
                errors.append(exc)

        threads = [threading.Thread(target=reserve, args=(day,)) for day in range(8, 16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], errors)
        self.assertEqual(1, len(winners))

    def test_reservation_is_idempotent_per_date_across_retry_and_restart(self):
        item = self._save("a")
        self._save("b", title="第二套")

        first = self.library.reserve_for_date("2026-10-08")
        retry = self.library.reserve_for_date("2026-10-08")
        after_restart = self._library().reserve_for_date("2026-10-08")

        self.assertEqual(item["id"], first["id"])  # oldest suitable favorite wins ties
        self.assertEqual(first["id"], retry["id"])
        self.assertEqual(first["id"], after_restart["id"])
        self.assertEqual(1, self.library.counts()["reserved"])  # the second outfit stays free
        self.assertEqual(1, self.library.counts()["available"])

    def test_release_returns_unused_favorite_to_the_pool(self):
        item = self._save("a")
        self.library.reserve_for_date("2026-10-08")

        released = self.library.release("2026-10-08", favorite_id=item["id"])

        self.assertEqual([item["id"]], released)
        self.assertEqual(1, self.library.counts()["available"])
        self.assertEqual(item["id"], self.library.reserve_for_date("2026-10-09")["id"])

    def test_stale_reservations_are_released_so_favorites_are_not_stranded(self):
        item = self._save("a")
        self.library.reserve_for_date("2026-10-08")
        self.now = datetime(2026, 10, 12, 9, 0)

        released = self.library.reconcile()

        self.assertEqual([item["id"]], released)
        self.assertEqual(1, self.library.counts()["available"])

    # --- success archival / failure --------------------------------------------
    def test_drafting_a_schedule_does_not_consume_the_outfit(self):
        item = self._save("a")
        self.library.reserve_for_date("2026-10-08")

        self.assertEqual(COLLECTION_FAVORITES, self.library.get(item["id"])["collection"])
        self.assertEqual(0, self.library.counts()["history"])

    def test_mark_used_requires_a_recorded_image_and_moves_to_history(self):
        item = self._save("a")
        self.library.reserve_for_date("2026-10-08")

        self.assertEqual({}, self.library.mark_used(item["id"], "2026-10-08", ""))
        self.assertEqual(COLLECTION_FAVORITES, self.library.get(item["id"])["collection"])

        used = self.library.mark_used(item["id"], "2026-10-08", "schedule_1.png")

        self.assertEqual(COLLECTION_HISTORY, used["collection"])
        self.assertEqual({}, used["reservations"])
        self.assertEqual("schedule_1.png", used["usage"][0]["image_filename"])
        self.assertEqual("2026-10-08", used["usage"][0]["schedule_date"])
        # idempotent for the same (date, image)
        again = self.library.mark_used(item["id"], "2026-10-08", "schedule_1.png")
        self.assertEqual(1, len(again["usage"]))

    def test_generation_failure_keeps_reservation_for_retry_then_archives(self):
        item = self._save("a")
        self.library.reserve_for_date("2026-10-08")
        # generation failed: nothing is archived and the reservation survives
        self.assertEqual(COLLECTION_FAVORITES, self.library.get(item["id"])["collection"])
        # retry (even after a restart) resolves to the same outfit
        retried = self._library().reserve_for_date("2026-10-08")
        self.assertEqual(item["id"], retried["id"])
        self.library.mark_used(item["id"], "2026-10-08", "schedule_2.png")
        self.assertEqual(COLLECTION_HISTORY, self.library.get(item["id"])["collection"])

    # --- history exclusion & explicit reuse ------------------------------------
    def test_history_is_excluded_from_every_automatic_path(self):
        item = self._save("a")
        self.library.reserve_for_date("2026-10-08")
        self.library.mark_used(item["id"], "2026-10-08", "schedule_1.png")

        self.assertEqual({}, self.library.reserve_for_date("2026-10-09", context_text="通勤"))
        self.assertTrue(self.library.known_outfit(post_id=POST_A, image_identity="imga"))
        saved_again, created = self.library.save(_record("a"))
        self.assertFalse(created)
        self.assertEqual(COLLECTION_HISTORY, saved_again["collection"])
        self.assertEqual({}, self.library.reserve_for_date("2026-10-10"))

    def test_explicit_wear_of_history_item_keeps_it_in_history_and_appends_usage(self):
        item = self._save("a")
        self.library.reserve_for_date("2026-10-08")
        original = self.library.mark_used(item["id"], "2026-10-08", "schedule_1.png")

        reserved = self.library.reserve_manual(item["id"], "2026-10-09", reference_filename="xhs_ref.png")
        self.assertEqual(COLLECTION_HISTORY, reserved["collection"])
        self.assertEqual({}, self.library.reserve_for_date("2026-10-11"))  # still not auto-selectable
        reused = self.library.mark_used(item["id"], "2026-10-09", "schedule_9.png")

        self.assertEqual(COLLECTION_HISTORY, reused["collection"])
        self.assertEqual(
            ["schedule_1.png", "schedule_9.png"],
            [entry["image_filename"] for entry in reused["usage"]],
        )
        self.assertEqual("manual", reused["usage"][1]["mode"])
        self.assertEqual(original["first_used_at"], reused["first_used_at"])

    def test_canceling_explicit_wear_leaves_history_item_in_history(self):
        item = self._save("a")
        self.library.reserve_for_date("2026-10-08")
        self.library.mark_used(item["id"], "2026-10-08", "schedule_1.png")
        self.library.reserve_manual(item["id"], "2026-10-09")

        self.library.release("2026-10-09", favorite_id=item["id"])

        self.assertEqual(COLLECTION_HISTORY, self.library.get(item["id"])["collection"])
        self.assertEqual({}, self.library.reserve_for_date("2026-10-10"))

    def test_delete_protects_history_and_reserved_outfits(self):
        free = self._save("a")
        reserved = self._save("b", title="第二套")
        used = self._save("c", title="第三套")
        self.library.reserve_manual(reserved["id"], "2026-10-08")
        self.library.mark_used(used["id"], "2026-10-07", "schedule_1.png")

        self.assertEqual((False, "history_protected", []), self.library.delete(used["id"]))
        self.assertEqual((False, "reserved", []), self.library.delete(reserved["id"]))
        self.assertEqual((False, "not_found", []), self.library.delete("missing"))
        self.assertEqual((True, "", ["fav_a.png"]), self.library.delete(free["id"]))
        self.assertEqual({}, self.library.get(free["id"]))
        self.assertEqual(2, len(self.library.list_items()))


class FavoriteWebIntegrationTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_server(root: Path) -> GalleryServer:
        data_dir = root / "data"
        config_path = root / "config" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("gallery:\n  port: 18899\n", encoding="utf-8")
        (root / "app" / "references").mkdir(parents=True, exist_ok=True)
        config = {"paths": {"project_root": str(root)}, "gallery": {"port": 18899}}
        server = GalleryServer(config, str(data_dir), str(config_path))
        server._now = lambda: OCTOBER
        server.xiaohongshu_schedule_store.update(lambda state: {**state, "enabled": True})
        return server

    async def _start_client(self, server: GalleryServer) -> TestClient:
        test_server = TestServer(server.app)
        await test_server.start_server(access_log=None)
        client = TestClient(test_server)
        await client.start_server()
        return client

    @staticmethod
    def _seed(server: GalleryServer, suffix: str, **overrides) -> dict:
        os.makedirs(server.xiaohongshu_favorite_dir, exist_ok=True)
        filename = f"fav_{suffix}.png"
        Image.new("RGB", (90, 120), "white").save(os.path.join(server.xiaohongshu_favorite_dir, filename))
        record = _record(suffix, **overrides)
        record["images"] = [{"index": 1, "filename": filename, "width": 90, "height": 120}]
        item, _created = server.xiaohongshu_favorites.save(record)
        return item

    @staticmethod
    def _fake_import(server: GalleryServer):
        async def _import(url: str, output_dir: str) -> dict:
            os.makedirs(output_dir, exist_ok=True)
            digest = hashlib.sha256(url.encode()).hexdigest()[:24]
            path = os.path.join(output_dir, f"xhs_{digest}.png")
            shade = int(digest[:2], 16)
            Image.new("RGB", (90, 120), (shade, 80, 120)).save(path)
            return {"filename": os.path.basename(path), "path": path, "size_bytes": os.path.getsize(path), "source_url": url}

        return _import

    def _no_live_search(self, server: GalleryServer) -> None:
        server.xiaohongshu_client.status = AsyncMock(return_value={"is_logged_in": False})
        server.xiaohongshu_client.search = AsyncMock(return_value=[])

    # --- save endpoints ----------------------------------------------------------
    async def test_save_persists_dedupes_and_never_mixes_images_of_one_post(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            server.xiaohongshu_client.import_image = AsyncMock(side_effect=self._fake_import(server))
            client = await self._start_client(server)
            try:
                base = {
                    "feed_id": POST_A,
                    "xsec_token": "tok123",
                    "title": "秋日通勤穿搭",
                    "author": "博主",
                }
                url_one = "https://sns-webpic-qc.xhscdn.com/202610/abc123!nd_dft_wlteh_webp_3"
                url_two = "https://sns-webpic-qc.xhscdn.com/202610/def456!nd_dft_wlteh_webp_3"
                first = await client.post("/api/xiaohongshu/favorites", json={**base, "image_url": url_one, "image_index": 1})
                first_body = await first.json()
                repeat = await client.post("/api/xiaohongshu/favorites", json={**base, "image_url": url_one, "image_index": 1})
                repeat_body = await repeat.json()
                other = await client.post("/api/xiaohongshu/favorites", json={**base, "image_url": url_two, "image_index": 2})
                other_body = await other.json()
                listing = await (await client.get("/api/xiaohongshu/favorites?collection=favorites")).json()
                history = await (await client.get("/api/xiaohongshu/favorites?collection=history")).json()
                invalid = await client.get("/api/xiaohongshu/favorites?collection=nope")
            finally:
                await client.close()

            self.assertEqual(201, first.status, first_body)
            self.assertTrue(first_body["created"])
            self.assertEqual("available", first_body["item"]["state"])
            self.assertNotIn("xsec_token", first_body["item"])
            self.assertTrue(first_body["item"]["post_url"].startswith(f"https://www.xiaohongshu.com/explore/{POST_A}"))
            self.assertEqual(200, repeat.status)
            self.assertTrue(repeat_body["duplicate"])
            self.assertEqual(first_body["item"]["id"], repeat_body["item"]["id"])
            # the repeat save must not re-download; only two distinct images were fetched
            self.assertEqual(2, server.xiaohongshu_client.import_image.await_count)
            self.assertEqual(201, other.status)
            self.assertNotEqual(first_body["item"]["id"], other_body["item"]["id"])
            self.assertEqual(2, listing["count"])
            self.assertEqual(0, history["count"])
            self.assertEqual(400, invalid.status)
            self.assertTrue(Path(server.xiaohongshu_favorite_dir, first_body["item"]["images"][0]["filename"]).is_file())

    async def test_save_from_share_link_requires_choosing_one_outfit_of_a_multi_image_post(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            server.xiaohongshu_client.parse_note_link = AsyncMock(return_value=(POST_A, "tokX"))
            server.xiaohongshu_client.detail = AsyncMock(return_value={
                "id": POST_A,
                "title": "一周穿搭",
                "author": "博主",
                "images": [
                    {"index": 0, "url": "https://sns-webpic-qc.xhscdn.com/a/one!x"},
                    {"index": 1, "url": "https://sns-webpic-qc.xhscdn.com/a/two!x"},
                ],
            })
            server.xiaohongshu_client.import_image = AsyncMock(side_effect=self._fake_import(server))
            client = await self._start_client(server)
            link = f"https://www.xiaohongshu.com/explore/{POST_A}?xsec_token=tokX"
            try:
                ambiguous = await client.post("/api/xiaohongshu/favorites", json={"note_url": link})
                ambiguous_body = await ambiguous.json()
                chosen = await client.post("/api/xiaohongshu/favorites", json={"note_url": link, "image_index": 1})
                chosen_body = await chosen.json()
            finally:
                await client.close()

            self.assertEqual(409, ambiguous.status)
            self.assertEqual("image_selection_required", ambiguous_body["error"])
            server.xiaohongshu_client.import_image.assert_awaited_once()
            self.assertEqual("https://sns-webpic-qc.xhscdn.com/a/two!x", server.xiaohongshu_client.import_image.await_args.args[0])
            self.assertEqual(201, chosen.status, chosen_body)
            self.assertEqual("一周穿搭", chosen_body["item"]["title"])

    async def test_save_with_share_link_and_known_image_skips_the_second_note_fetch(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            server.xiaohongshu_client.parse_note_link = AsyncMock(return_value=(POST_A, "tokX"))
            server.xiaohongshu_client.detail = AsyncMock()
            server.xiaohongshu_client.import_image = AsyncMock(side_effect=self._fake_import(server))
            client = await self._start_client(server)
            try:
                response = await client.post("/api/xiaohongshu/favorites", json={
                    "note_url": f"https://www.xiaohongshu.com/explore/{POST_A}?xsec_token=tokX",
                    "image_url": "https://sns-webpic-qc.xhscdn.com/a/three!x",
                    "image_index": 3,
                    "title": "一周穿搭",
                    "author": "博主",
                })
                body = await response.json()
            finally:
                await client.close()

            self.assertEqual(201, response.status, body)
            server.xiaohongshu_client.detail.assert_not_awaited()
            self.assertEqual(POST_A, body["item"]["post_id"])
            self.assertIn("xsec_token=tokX", body["item"]["post_url"])  # reopenable original link
            self.assertEqual(3, body["item"]["image_index"])
            self.assertNotIn("xsec_token", body["item"])

    # --- scheduling ---------------------------------------------------------------
    async def test_new_schedule_uses_a_favorite_reference_without_live_search_and_is_stable_on_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            self._no_live_search(server)
            item = self._seed(server, "a")

            first = await server.ensure_xiaohongshu_schedule_reference(
                "2026-10-08",
                {"xiaohongshu_search_query": "秋季通勤穿搭"},
                force=True,
            )
            retry = await server.ensure_xiaohongshu_schedule_reference(
                "2026-10-08",
                {"xiaohongshu_search_query": "秋季通勤穿搭"},
                force=True,
            )

            self.assertEqual(item["id"], first["favorite_id"])
            self.assertEqual("favorite_library", first["selection_source"])
            self.assertTrue(Path(first["path"]).is_file())  # real reference image linkage
            self.assertEqual(first["filename"], retry["filename"])
            server.xiaohongshu_client.search.assert_not_awaited()
            server.xiaohongshu_client.status.assert_not_awaited()
            library_item = server.xiaohongshu_favorites.get(item["id"])
            self.assertEqual(["2026-10-08"], list(library_item["reservations"]))
            self.assertEqual(COLLECTION_FAVORITES, library_item["collection"])

    async def test_schedule_keeps_live_search_fallback_when_no_favorite_is_suitable(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            self._no_live_search(server)
            self._seed(server, "a", title="夏日海边度假穿搭")  # wrong season for October

            result = await server.ensure_xiaohongshu_schedule_reference(
                "2026-10-08",
                {"xiaohongshu_search_query": "秋季通勤穿搭"},
                force=True,
            )

            self.assertEqual({}, result)
            server.xiaohongshu_client.status.assert_awaited()  # existing live path still runs
            self.assertEqual(1, server.xiaohongshu_favorites.counts()["available"])

    async def test_manual_assignment_is_never_overridden_by_the_favorites_pool(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            self._no_live_search(server)
            self._seed(server, "a")
            manual = Path(server.xiaohongshu_reference_dir) / "xhs_manual.png"
            os.makedirs(server.xiaohongshu_reference_dir, exist_ok=True)
            Image.new("RGB", (64, 96), "black").save(manual)
            server.xiaohongshu_reference_store.update(lambda records: {
                **records,
                manual.name: {"filename": manual.name, "title": "手动穿搭", "source": "xiaohongshu"},
            })
            server._bind_manual_xiaohongshu_schedule_reference(
                "2026-10-08", f"/local-refs/xiaohongshu/{manual.name}"
            )

            picked = server._select_favorite_schedule_reference(
                "2026-10-08",
                {},
                "秋季通勤穿搭",
                server._xiaohongshu_schedule_reference("2026-10-08"),
            )

            self.assertEqual({}, picked)
            self.assertEqual(1, server.xiaohongshu_favorites.counts()["available"])

    async def test_forced_schedule_regeneration_never_selects_a_history_outfit(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            self._no_live_search(server)
            item = self._seed(server, "a")  # suitable for October: would be picked if unused
            library = server.xiaohongshu_favorites
            library.reserve_for_date("2026-10-06")
            library.mark_used(item["id"], "2026-10-06", "schedule_1.png")

            for _attempt in range(2):  # first generation and a forced regeneration
                result = await server.ensure_xiaohongshu_schedule_reference(
                    "2026-10-08",
                    {"xiaohongshu_search_query": "秋季通勤穿搭"},
                    force=True,
                )
                self.assertNotEqual("favorite_library", result.get("selection_source"))
                self.assertNotEqual(item["id"], result.get("favorite_id"))

            server.xiaohongshu_client.status.assert_awaited()  # fell through to the normal live path
            after = library.get(item["id"])
            self.assertEqual(COLLECTION_HISTORY, after["collection"])
            self.assertEqual({}, after["reservations"])

    def _mock_live_search(self, server: GalleryServer) -> None:
        server.xiaohongshu_client.status = AsyncMock(return_value={"service_running": True, "is_logged_in": True})
        server.xiaohongshu_client.search = AsyncMock(return_value=[{
            "id": "note-1", "xsec_token": "token-1", "title": "秋季通勤穿搭 OOTD", "author": "作者",
            "cover_url": "https://sns-webpic-qc.xhscdn.com/cover.webp", "width": 900, "height": 1200,
        }])
        server.xiaohongshu_client.detail = AsyncMock(return_value={
            "id": "note-1", "title": "秋季通勤穿搭 OOTD", "author": "作者",
            "images": [
                {"index": 0, "url": "https://sns-webpic-qc.xhscdn.com/cover.webp", "width": 900, "height": 1200},
                {"index": 1, "url": "https://sns-webpic-qc.xhscdn.com/full-body.webp", "width": 900, "height": 1200},
            ],
        })

        async def fake_import(_url, output_dir):
            path = Path(output_dir) / "xhs_live.png"
            Image.new("RGB", (900, 1200), "white").save(path)
            return {"filename": path.name, "path": str(path), "size_bytes": path.stat().st_size}

        server.xiaohongshu_client.import_image = AsyncMock(side_effect=fake_import)
        server.on_validate_xiaohongshu_outfit = AsyncMock(return_value={
            "accepted": True, "selected_index": 1, "quality_score": 92, "reason": "ok",
            "person_count": 1, "is_real_photo": True, "is_collage": False, "single_outfit": True,
            "full_body_visible": True, "clothing_clear": True, "quality_sufficient": True,
            "keyword_match": True,
        })

    async def test_live_search_never_reselects_an_outfit_already_in_the_library(self):
        live_query = {"xiaohongshu_search_query": "秋季通勤穿搭"}
        # control: nothing in the library, the mocked search flow downloads the image
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            control = self._make_server(Path(tmpdir))
            self._mock_live_search(control)
            result = await control.ensure_xiaohongshu_schedule_reference("2026-10-08", live_query, force=True)
            self.assertEqual("keyword_search", result.get("selection_source"))
            control.xiaohongshu_client.import_image.assert_awaited()

        # the very same post image is already a used (history) library outfit
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            self._mock_live_search(server)
            item = self._seed(
                server, "a", post_id="note-1", outfit_key=make_outfit_key("note-1", "full-body"),
                title="夏日海边度假穿搭",  # not suitable now, so the pool cannot pick it either
            )
            library = server.xiaohongshu_favorites
            library.reserve_for_date("2026-07-07")
            library.mark_used(item["id"], "2026-07-07", "schedule_1.png")

            result = await server.ensure_xiaohongshu_schedule_reference("2026-10-08", live_query, force=True)

            self.assertEqual({}, result)
            server.xiaohongshu_client.search.assert_awaited()
            server.xiaohongshu_client.import_image.assert_not_awaited()  # skipped before any download
            self.assertEqual(COLLECTION_HISTORY, library.get(item["id"])["collection"])

    # --- replacement / cancel ------------------------------------------------------
    async def test_replacing_or_clearing_an_assignment_releases_the_reservation(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            self._no_live_search(server)
            item = self._seed(server, "a")
            await server.ensure_xiaohongshu_schedule_reference(
                "2026-10-08", {"xiaohongshu_search_query": "秋季通勤穿搭"}, force=True,
            )
            self.assertEqual(1, server.xiaohongshu_favorites.counts()["reserved"])

            # explicit refresh style cancel
            self.assertTrue(server._clear_manual_xiaohongshu_schedule_reference("2026-10-08", include_favorite=True))
            self.assertEqual(1, server.xiaohongshu_favorites.counts()["available"])

            # replaced by an unrelated manual choice
            await server.ensure_xiaohongshu_schedule_reference(
                "2026-10-08", {"xiaohongshu_search_query": "秋季通勤穿搭"}, force=True,
            )
            manual = Path(server.xiaohongshu_reference_dir) / "xhs_other.png"
            Image.new("RGB", (64, 96), "black").save(manual)
            server.xiaohongshu_reference_store.update(lambda records: {
                **records, manual.name: {"filename": manual.name, "title": "别的穿搭", "source": "xiaohongshu"},
            })
            server._bind_manual_xiaohongshu_schedule_reference(
                "2026-10-08", f"/local-refs/xiaohongshu/{manual.name}",
            )

            self.assertEqual(1, server.xiaohongshu_favorites.counts()["available"])
            self.assertEqual({}, server.xiaohongshu_favorites.get(item["id"])["reservations"])

    # --- explicit wear (history reuse) ---------------------------------------------
    async def test_wear_history_outfit_reuses_manual_flow_without_returning_it_to_the_pool(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            self._no_live_search(server)
            item = self._seed(server, "a")
            server.xiaohongshu_favorites.reserve_for_date("2026-10-07")
            server.xiaohongshu_favorites.mark_used(item["id"], "2026-10-07", "schedule_1.png")
            client = await self._start_client(server)
            try:
                worn = await client.post(
                    f"/api/xiaohongshu/favorites/{item['id']}/wear",
                    json={"schedule_date": "2026-10-08"},
                )
                body = await worn.json()
                missing = await client.post(
                    "/api/xiaohongshu/favorites/nope/wear", json={"schedule_date": "2026-10-08"},
                )
                too_far = await client.post(
                    f"/api/xiaohongshu/favorites/{item['id']}/wear", json={"schedule_date": "2026-10-20"},
                )
            finally:
                await client.close()

            self.assertEqual(200, worn.status, body)
            reference = body["schedule"]["today_reference"]
            self.assertEqual("manual", reference["selection_source"])
            self.assertEqual(item["id"], reference["favorite_id"])
            self.assertEqual(COLLECTION_HISTORY, body["item"]["collection"])
            self.assertEqual("history", body["item"]["state"])
            self.assertEqual("manual", body["item"]["reservations"][0]["mode"])
            self.assertEqual(404, missing.status)
            self.assertEqual(400, too_far.status)
            # still excluded from automatic scheduling while assigned
            self.assertEqual({}, server.xiaohongshu_favorites.reserve_for_date("2026-10-09"))
            # the temporary import copy never lingers in the reference list
            self.assertEqual([], [r for r in server._xiaohongshu_reference_items() if r["filename"].startswith("xhs_fav_")])

            # explicit wear survives routine forced schedule regeneration
            kept = await server.ensure_xiaohongshu_schedule_reference(
                "2026-10-08", {"xiaohongshu_search_query": "别的"}, force=True,
            )
            self.assertEqual(item["id"], kept["favorite_id"])
            server.xiaohongshu_client.search.assert_not_awaited()

            # canceling it leaves the outfit in history
            self.assertTrue(server._clear_manual_xiaohongshu_schedule_reference("2026-10-08"))
            after = server.xiaohongshu_favorites.get(item["id"])
            self.assertEqual(COLLECTION_HISTORY, after["collection"])
            self.assertEqual({}, after["reservations"])
            self.assertEqual(1, len(after["usage"]))

    async def test_wear_unused_favorite_then_cancel_returns_it_to_the_pool(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            item = self._seed(server, "a")
            client = await self._start_client(server)
            try:
                worn = await client.post(
                    f"/api/xiaohongshu/favorites/{item['id']}/wear", json={"schedule_date": "2026-10-07"},
                )
                body = await worn.json()
                blocked = await client.delete(f"/api/xiaohongshu/favorites/{item['id']}")
            finally:
                await client.close()

            self.assertEqual(200, worn.status, body)
            self.assertEqual("reserved", body["item"]["state"])
            self.assertEqual(409, blocked.status)  # reserved outfits cannot be deleted
            self.assertTrue(server._clear_manual_xiaohongshu_schedule_reference("2026-10-07"))
            self.assertEqual(1, server.xiaohongshu_favorites.counts()["available"])

    async def test_favorites_api_delete_and_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            item = self._seed(server, "a")
            image_path = Path(server.xiaohongshu_favorite_dir, item["images"][0]["filename"])
            client = await self._start_client(server)
            try:
                toggled = await (await client.post("/api/xiaohongshu/favorites/settings", json={"auto_schedule": False})).json()
                removed = await client.delete(f"/api/xiaohongshu/favorites/{item['id']}")
                missing = await client.delete(f"/api/xiaohongshu/favorites/{item['id']}")
            finally:
                await client.close()

            self.assertFalse(toggled["auto_schedule"])
            self.assertEqual(200, removed.status)
            self.assertFalse(image_path.exists())
            self.assertEqual(404, missing.status)

    # --- generation hook in the scheduler app ---------------------------------------
    def _app_stub(self, server: GalleryServer):
        stub = SimpleNamespace(data_dir=server.data_dir, web_server=server)
        stub._gallery_image_recorded = lambda name: PortraitGalleryApp._gallery_image_recorded(stub, name)
        return stub

    async def test_outfit_moves_to_history_only_after_the_image_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            item = self._seed(server, "a")
            server.xiaohongshu_favorites.reserve_for_date("2026-10-08")
            stub = self._app_stub(server)
            reference = {"favorite_id": item["id"], "filename": "xhs_schedule_20261008_fav_x.png"}
            library = server.xiaohongshu_favorites

            # drafting / failed generation: no image was recorded
            PortraitGalleryApp._record_favorite_outfit_use(stub, "2026-10-08", reference, "")
            PortraitGalleryApp._record_favorite_outfit_use(stub, "2026-10-08", reference, "never_saved.png")
            self.assertEqual(COLLECTION_FAVORITES, library.get(item["id"])["collection"])
            self.assertEqual(["2026-10-08"], list(library.get(item["id"])["reservations"]))

            # retry succeeds and the image lands in the gallery store
            ScheduleStore(server.data_dir).update(
                lambda data: {**data, "schedule_1.png": {"image_filename": "schedule_1.png"}}
            )
            PortraitGalleryApp._record_favorite_outfit_use(stub, "2026-10-08", reference, "schedule_1.png")
            PortraitGalleryApp._record_favorite_outfit_use(stub, "2026-10-08", reference, "schedule_1.png")

            archived = library.get(item["id"])
            self.assertEqual(COLLECTION_HISTORY, archived["collection"])
            self.assertEqual({}, archived["reservations"])
            self.assertEqual(1, len(archived["usage"]))
            self.assertEqual("schedule_1.png", archived["usage"][0]["image_filename"])

            # explicit manual reuse appends instead of erasing the first use
            library.reserve_manual(item["id"], "2026-10-09")
            ScheduleStore(server.data_dir).update(
                lambda data: {**data, "schedule_2.png": {"image_filename": "schedule_2.png"}}
            )
            PortraitGalleryApp._record_favorite_outfit_use(stub, "2026-10-09", reference, "schedule_2.png")
            self.assertEqual(
                ["schedule_1.png", "schedule_2.png"],
                [e["image_filename"] for e in library.get(item["id"])["usage"]],
            )
            self.assertEqual(COLLECTION_HISTORY, library.get(item["id"])["collection"])

    async def test_non_library_references_are_ignored_by_the_archival_hook(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            item = self._seed(server, "a")
            stub = self._app_stub(server)
            ScheduleStore(server.data_dir).update(
                lambda data: {**data, "schedule_1.png": {"image_filename": "schedule_1.png"}}
            )

            PortraitGalleryApp._record_favorite_outfit_use(stub, "2026-10-08", {"filename": "plain.png"}, "schedule_1.png")

            self.assertEqual(COLLECTION_FAVORITES, server.xiaohongshu_favorites.get(item["id"])["collection"])

    async def test_wardrobe_favorites_file_is_never_touched(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            wardrobe = Path(server.data_dir) / "favorite_outfits.json"
            wardrobe.write_text(json.dumps({"items": [{"id": "keep"}]}), encoding="utf-8")
            before = wardrobe.read_bytes()
            item = self._seed(server, "a")
            server.xiaohongshu_favorites.reserve_for_date("2026-10-08")
            server.xiaohongshu_favorites.mark_used(item["id"], "2026-10-08", "schedule_1.png")

            self.assertEqual(before, wardrobe.read_bytes())
            self.assertTrue((Path(server.data_dir) / "xiaohongshu_favorites.json").is_file())


class FavoriteUiContractTest(unittest.TestCase):
    """Static checks: the single-file UI exposes the favorites workflow."""

    @classmethod
    def setUpClass(cls):
        cls.html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

    def test_dedicated_tab_section_and_badge_exist(self):
        self.assertIn('data-tab="xhs-favorites"', self.html)
        self.assertIn('id="xhsFavSection"', self.html)
        self.assertIn('id="badge-xhs-favorites"', self.html)
        self.assertIn('"wardrobe", "xhs-favorites", "group-chat"', self.html)  # switchTab allow-list
        self.assertIn('"xhs-favorites", "character"', self.html)  # early tab restore

    def test_tab_bar_grid_was_widened_for_the_sixth_tab(self):
        self.assertGreaterEqual(self.html.count("grid-template-columns: repeat(6, minmax(0, 1fr));"), 2)

    def test_favorites_and_history_views_and_actions(self):
        for needle in (
            "setXhsFavView('favorites')",
            "setXhsFavView('history')",
            "async function saveXiaohongshuFavorite",
            "async function wearXhsFavorite",
            "async function openXhsFavLink",
            "/api/xiaohongshu/favorites/${encodeURIComponent(favoriteId)}/wear",
            "再穿",  # explicit reuse of history items
        ):
            self.assertIn(needle, self.html, needle)

    def test_save_action_is_available_in_both_existing_browsing_workflows(self):
        self.assertIn('xhsFavSaveButtonHtml("search", data, imageIndex)', self.html)
        self.assertIn("xhsFavSaveButtonHtml('themeday', data, imageIndex)", self.html)
        # the direct assignment actions must keep working next to the new heart
        self.assertIn('onclick="importXiaohongshuOutfit(${imageIndex})"', self.html)
        self.assertIn("onclick=\"chooseThemeDayXhsImage(' + imageIndex + ')\"", self.html)

    def test_saved_state_is_derived_from_the_library_not_only_page_memory(self):
        self.assertIn("function xhsFavIsSaved", self.html)
        # one definition plus the two render sites (heart buttons and link-box tiles)
        self.assertEqual(3, self.html.count("xhsFavIsSaved("))
        self.assertIn("xhsFavItems.some(item => item.post_id === postId", self.html)

    def test_saving_inside_the_favorites_tab_refreshes_the_grid(self):
        start = self.html.index("async function saveXiaohongshuFavorite")
        body = self.html[start:self.html.index("function xhsFavActiveNote", start)]
        self.assertIn("loadXhsFavorites({ render: true, silent: true, force: true })", body)

    def test_hidden_toolbar_is_not_overridden_by_its_display_rule(self):
        # `display: grid` beats the UA [hidden] rule, so the History view needs this
        # explicit override or the link-paste box would stay visible there.
        self.assertIn(".xfav-toolbar[hidden] { display: none; }", self.html)

    def test_original_post_link_and_history_usage_links_are_rendered(self):
        self.assertIn("打开小红书原帖", self.html)
        self.assertIn("查看成图", self.html)


if __name__ == "__main__":
    unittest.main()
