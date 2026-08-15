import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from store import ScheduleStore  # noqa: E402
from web_server import GalleryServer  # noqa: E402


class GalleryPaginationTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_server(root: Path) -> GalleryServer:
        config_path = root / "config" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("gallery:\n  port: 18889\n", encoding="utf-8")
        (root / "app" / "references").mkdir(parents=True, exist_ok=True)
        return GalleryServer(
            {"paths": {"project_root": str(root)}, "gallery": {"port": 18889}},
            str(root / "data"),
            str(config_path),
        )

    @staticmethod
    def _seed_entries(server: GalleryServer, count: int = 3) -> None:
        image_dir = Path(server.image_dir)
        image_dir.mkdir(parents=True, exist_ok=True)
        entries = {}
        styles = ("清新风", "元气风", "甜妹风")
        for index in range(count):
            filename = f"gallery-{index}.png"
            Image.new("RGB", (24, 32), (80 + index, 90, 100)).save(image_dir / filename)
            entries[filename] = {
                "date": f"2026-07-{14 + index:02d}",
                "time": f"1{index}:20",
                "image_filename": filename,
                "prompt": f"private prompt {index}",
                "caption": f"caption {index}",
                "outfit_style": styles[index % len(styles)],
                "favorite": index % 2 == 0,
                "status": "ok",
                "source": "cron",
            }
        ScheduleStore(server.data_dir).save(entries)

    async def test_paginated_gallery_omits_prompts_and_detail_restores_them(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            self._seed_entries(server)

            first_response = await server.handle_gallery(SimpleNamespace(query={
                "limit": "2",
                "include_prompt": "false",
            }))
            first = json.loads(first_response.text)

            self.assertEqual(3, first.get("total"))
            self.assertEqual(3, first.get("total_all"))
            self.assertEqual(2, first.get("favorite_total"))
            self.assertEqual(["元气风", "清新风", "甜妹风"], first.get("styles"))
            self.assertEqual(2, len(first.get("items", [])))
            self.assertTrue(first.get("has_more"))
            self.assertTrue(first.get("next_cursor"))
            self.assertNotIn("prompt", first["items"][0])
            self.assertTrue(first["items"][0].get("has_prompt"))

            filename = first["items"][0]["image_filename"]
            detail_response = await server.handle_image_detail(
                SimpleNamespace(match_info={"img_id": filename})
            )
            detail = json.loads(detail_response.text)

            self.assertEqual(200, detail_response.status)
            self.assertTrue(detail.get("prompt", "").startswith("private prompt"))

            second_response = await server.handle_gallery(SimpleNamespace(query={
                "limit": "2",
                "cursor": first["next_cursor"],
                "include_prompt": "false",
            }))
            second = json.loads(second_response.text)
            self.assertEqual(1, len(second.get("items", [])))
            self.assertFalse(second.get("has_more"))

    async def test_stable_cursor_does_not_skip_after_loaded_image_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            self._seed_entries(server, count=5)

            first_response = await server.handle_gallery(SimpleNamespace(query={
                "limit": "2",
                "include_prompt": "false",
            }))
            first = json.loads(first_response.text)
            first_filenames = [entry["image_filename"] for entry in first["items"]]
            deleted_filename = first_filenames[0]
            ScheduleStore(server.data_dir).update(
                lambda entries: {key: value for key, value in entries.items() if key != deleted_filename}
            )

            second_response = await server.handle_gallery(SimpleNamespace(query={
                "limit": "2",
                "cursor": first["next_cursor"],
                "include_prompt": "false",
            }))
            second = json.loads(second_response.text)
            second_filenames = [entry["image_filename"] for entry in second["items"]]

            self.assertEqual(["gallery-2.png", "gallery-1.png"], second_filenames)

    async def test_favorites_filter_reports_full_favorite_total(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            self._seed_entries(server, count=7)

            response = await server.handle_gallery(SimpleNamespace(query={
                "favorites": "true",
                "limit": "2",
                "include_prompt": "false",
            }))
            payload = json.loads(response.text)

            self.assertEqual(4, payload["total"])
            self.assertEqual(4, payload["favorite_total"])
            self.assertEqual(7, payload["total_all"])
            self.assertTrue(all(entry["favorite"] for entry in payload["items"]))

    async def test_style_facets_and_filter_cover_entries_outside_first_page(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            self._seed_entries(server, count=5)

            first_response = await server.handle_gallery(SimpleNamespace(query={
                "limit": "1",
                "include_prompt": "false",
            }))
            first = json.loads(first_response.text)
            filtered_response = await server.handle_gallery(SimpleNamespace(query={
                "limit": "1",
                "style": "清新风",
                "include_prompt": "false",
            }))
            filtered = json.loads(filtered_response.text)

            self.assertEqual(1, len(first["items"]))
            self.assertEqual(5, first["total_all"])
            self.assertEqual(["元气风", "清新风", "甜妹风"], first["styles"])
            self.assertEqual(2, filtered["total"])
            self.assertEqual(5, filtered["total_all"])
            self.assertEqual("清新风", filtered["style"])
            self.assertEqual("清新风", filtered["items"][0]["outfit_style"])
            self.assertTrue(filtered["has_more"])

    async def test_legacy_gallery_contract_remains_a_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            self._seed_entries(server, count=1)

            response = await server.handle_gallery(SimpleNamespace(query={}))

            self.assertIsInstance(json.loads(response.text), list)

    def test_untrusted_external_image_directory_falls_back_to_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "data"
            data_dir.mkdir()
            outside = root / "outside-images"
            outside.mkdir()
            (data_dir / "api_keys_config.json").write_text(
                json.dumps({"image_dir": str(outside)}),
                encoding="utf-8",
            )

            server = self._make_server(root)

            self.assertEqual(str((data_dir / "images").resolve()), server.image_dir)

    def test_wardrobe_generation_state_recovers_after_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "data"
            data_dir.mkdir()
            (data_dir / "favorite_outfits.json").write_text(
                json.dumps({
                    "items": [{
                        "id": "outfit-one",
                        "wardrobe_image_status": {
                            "status": "generating",
                            "started_at": 123,
                        },
                    }],
                }),
                encoding="utf-8",
            )

            server = self._make_server(root)
            item = server._load_favorite_outfits()[0]

            self.assertEqual("failed", item["wardrobe_image_status"]["status"])
            self.assertEqual("generation_interrupted", item["wardrobe_image_status"]["error"])


class GalleryPaginationFrontendContractTest(unittest.TestCase):
    def test_all_badge_uses_gallery_total_instead_of_loaded_page_size(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("Number.isFinite(galleryTotal) ? galleryTotal : 0", html)
        self.assertNotIn("const allCount = imageEntries.length;", html)
        self.assertIn("previousTotal > 0 ? previousTotal - 1 : 0", html)
        self.assertIn("galleryTotal = Math.max(loaded, currentTotal + 1);", html)

    def test_favorites_badge_and_tab_use_complete_server_result(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("let galleryFavoriteTotal = null;", html)
        self.assertIn("favorites: 'true'", html)
        self.assertIn("while (hasMore)", html)
        self.assertIn("galleryFavoriteTotal = Number.isFinite(favoriteTotal)", html)
        self.assertIn("if (showFavoritesOnly || !galleryHasMore) return '';", html)

    def test_gallery_cards_render_the_supported_reroll_action(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("${renderRerollButton(e, {card: true})}", html)
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr));", html)
        self.assertIn("暂无提示词，无法重抽", html)

    def test_style_filters_use_server_facets_and_filtered_pagination(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("let galleryViewTotal = 0;", html)
        self.assertIn("let galleryStyles = [];", html)
        self.assertIn("galleryParams.set('style', requestedTagFilter)", html)
        self.assertIn("Array.isArray(galleryPayload.styles)", html)
        self.assertIn("const styleSource = galleryStyles.length", html)
        self.assertIn("galleryViewTotal || loaded", html)
        self.assertIn("galleryLoadedFavoritesOnly || galleryLoadedTagFilter !== currentTagFilter", html)
        self.assertIn("loadGallery({ skipTabSwitch: true });", html)
        self.assertNotIn("galleryReloadPending = true;", html)

    def test_gallery_automatically_loads_more_at_the_scroll_boundary(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="galleryLoadMoreSentinel"', html)
        self.assertIn("new IntersectionObserver", html)
        self.assertIn("rootMargin: '320px 0px'", html)
        self.assertIn("loadMoreGallery(entry.target)", html)
        self.assertIn("getGalleryPageSize(4)", html)
        self.assertIn("window.addEventListener('scroll', maybeAutoLoadGallery", html)
        self.assertIn("function scheduleGalleryLoadMoreRetry()", html)
        self.assertIn("galleryNextCursor !== previousCursor", html)
        self.assertNotIn('onclick="loadMoreGallery(this)">加载更多</button>', html)


if __name__ == "__main__":
    unittest.main()
