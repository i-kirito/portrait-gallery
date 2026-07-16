import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

APP_DIR = Path(__file__).resolve().parents[1] / "app"
ZHUZHU_DIR = APP_DIR / "zhuzhu"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(ZHUZHU_DIR))
_TEST_LOG_DIR = tempfile.TemporaryDirectory(prefix="portrait-gallery-edit-tests-")
os.environ["HERMES_GALLERY_LOG"] = str(Path(_TEST_LOG_DIR.name) / "gallery.log")

from image_editing import (  # noqa: E402
    build_precision_image_edit_prompt,
    replace_image_schedule_description,
    rewrite_image_edit_schedule_description,
)
from image_gen import ImageGenerator  # noqa: E402
from main import PortraitGalleryApp  # noqa: E402
from store import ScheduleStore  # noqa: E402
from web_server import GalleryServer  # noqa: E402
import generate as unified_generate  # noqa: E402


class PrecisionImageEditPromptTest(unittest.TestCase):
    def test_background_prompt_locks_every_unrequested_element(self):
        prompt = build_precision_image_edit_prompt("background", "换成雨后的东京街道")

        self.assertIn("背景 ONLY", prompt)
        self.assertIn("换成雨后的东京街道", prompt)
        self.assertIn("Preserve the complete subject", prompt)
        self.assertIn("camera angle", prompt)
        self.assertIn("Keep the output dimensions identical", prompt)

    def test_schedule_description_replacement_keeps_original_clock(self):
        updated = replace_image_schedule_description(
            "10:27 坐在咖啡馆画服装草图",
            "09:00",
            "坐在雨后的窗边整理设计稿",
        )

        self.assertEqual("10:27 坐在雨后的窗边整理设计稿", updated)

    def test_background_edit_rewrites_existing_location_in_place(self):
        updated = rewrite_image_edit_schedule_description(
            "10:27 坐在市场旁边的阳光咖啡馆，一边喝拿铁一边在平板上绘制服装设计草图",
            "background",
            "改成繁华的商业街",
        )

        self.assertEqual(
            "坐在繁华的商业街旁边的阳光咖啡馆，一边喝拿铁一边在平板上绘制服装设计草图",
            updated,
        )


class ImageGeneratorPrecisionFlagTest(unittest.IsolatedAsyncioTestCase):
    async def test_precision_edit_flag_reaches_generation_script(self):
        with tempfile.TemporaryDirectory(prefix="portrait-gallery-edit-command-") as temp_dir:
            script_path = Path(temp_dir) / "generate.py"
            script_path.write_text("# test fixture\n", encoding="utf-8")
            source_path = Path(temp_dir) / "source.png"
            source_path.write_bytes(b"source")
            output_path = Path(temp_dir) / "edited.png"
            output_path.write_bytes(b"edited")
            generator = ImageGenerator(temp_dir, temp_dir, default_engine="gptimage")
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=f"SUCCESS:{output_path}\n",
                stderr="",
            )

            with patch("image_gen.subprocess.run", return_value=completed) as run:
                result = await generator.generate(
                    "precision edit",
                    ref_image=str(source_path),
                    prompt_final=True,
                    no_auto_style=True,
                    precise_edit=True,
                    timeout=1,
                )

        self.assertEqual("edited.png", result)
        command = run.call_args.args[0]
        self.assertIn("--precise-edit", command)
        self.assertIn("--ref-image", command)
        self.assertIn("--prompt-final", command)
        self.assertIn("--no-auto-style", command)


class UnifiedGenerationPrecisionFlagTest(unittest.TestCase):
    def test_unified_entrypoint_forwards_precision_mode_to_gpt_image(self):
        with patch.object(
            unified_generate,
            "generate_with_gptimage",
            return_value="/tmp/edited.png",
        ) as gpt_image, patch.object(
            unified_generate,
            "generate_with_gitee",
        ) as gitee:
            result = unified_generate.generate(
                "custom",
                "gptimage",
                prompt_override="change only the background",
                ref_image="/tmp/source.png",
                prompt_final=True,
                no_auto_style=True,
                precise_edit=True,
            )

        self.assertEqual("/tmp/edited.png", result)
        self.assertTrue(gpt_image.call_args.kwargs["precise_edit"])
        self.assertFalse(gpt_image.call_args.kwargs["sync_gallery"])
        gitee.assert_not_called()


class PortraitGalleryImageEditTest(unittest.IsolatedAsyncioTestCase):
    async def test_edit_replaces_card_and_auto_updates_background_schedule_context(self):
        with tempfile.TemporaryDirectory(prefix="portrait-gallery-edit-main-") as temp_dir:
            data_dir = Path(temp_dir) / "data"
            image_dir = data_dir / "images"
            image_dir.mkdir(parents=True)
            original_filename = "original.png"
            original_path = image_dir / original_filename
            Image.new("RGB", (48, 64), (240, 220, 230)).save(original_path)
            ScheduleStore(str(data_dir)).save({
                original_filename: {
                    "id": original_filename,
                    "date": "2026-07-13",
                    "time": "10:27",
                    "image_filename": original_filename,
                    "image_path": f"/images/{original_filename}",
                    "outfit_style": "清新风",
                    "outfit": "风格：清新风 穿搭：白衬衫搭配藏青色百褶裙",
                    "favorite": True,
                    "status": "ok",
                    "source": "cron",
                    "schedule_time": "10:27 坐在市场旁边的阳光咖啡馆，一边喝拿铁一边在平板上绘制服装设计草图",
                },
            })
            (data_dir / "image_metadata.json").write_text(
                json.dumps({original_filename: {"model": "gpt-image-2"}}),
                encoding="utf-8",
            )
            saved_metadata = {}
            delete_image_files = Mock(return_value=([original_filename], []))
            server = SimpleNamespace(
                _image_file_path=lambda filename: str(original_path) if filename == original_filename else "",
                _update_image_metadata_entry=lambda filename, entry: saved_metadata.update({filename: entry}),
                _delete_image_files=delete_image_files,
            )
            app = PortraitGalleryApp.__new__(PortraitGalleryApp)
            app.data_dir = str(data_dir)
            app.config = {}
            app.web_server = server
            app.image_gen = SimpleNamespace(generate=AsyncMock(return_value="edited.png"))

            result = await app.edit_image(
                original_filename,
                "background",
                "改成繁华的商业街",
            )

            stored = ScheduleStore(str(data_dir)).load()
            self.assertEqual("ok", result["status"])
            self.assertIn(original_filename, stored)
            self.assertNotIn("edited.png", stored)
            self.assertEqual(1, len(stored))
            replacement = stored[original_filename]
            self.assertEqual("edited.png", replacement["image_filename"])
            self.assertEqual("cron", replacement["source"])
            self.assertEqual(original_filename, replacement["edited_from"])
            self.assertEqual(original_filename, replacement["replaced_image_filename"])
            self.assertEqual("2026-07-13", replacement["date"])
            self.assertEqual("10:27", replacement["time"])
            self.assertTrue(replacement["favorite"])
            self.assertEqual("清新风", replacement["outfit_style"])
            self.assertEqual("繁华的商业街", replacement["edit_instruction"].replace("改成", ""))
            self.assertEqual(
                "10:27 坐在繁华的商业街旁边的阳光咖啡馆，一边喝拿铁一边在平板上绘制服装设计草图",
                replacement["schedule_time"],
            )
            self.assertEqual(
                "坐在繁华的商业街旁边的阳光咖啡馆，一边喝拿铁一边在平板上绘制服装设计草图",
                replacement["schedule_description"],
            )
            self.assertEqual("image_edit", replacement["schedule_edit_source"])
            self.assertEqual(1, len(replacement["edit_history"]))
            self.assertEqual(
                "10:27 坐在市场旁边的阳光咖啡馆，一边喝拿铁一边在平板上绘制服装设计草图",
                replacement["edit_history"][0]["previous_schedule_time"],
            )
            call = app.image_gen.generate.await_args
            self.assertEqual(str(original_path), call.kwargs["ref_image"])
            self.assertEqual("48x64", call.kwargs["size"])
            self.assertTrue(call.kwargs["precise_edit"])
            self.assertTrue(call.kwargs["prompt_final"])
            self.assertEqual(original_filename, saved_metadata["edited.png"]["edited_from"])
            self.assertEqual(original_filename, saved_metadata["edited.png"]["replaced_image_filename"])
            self.assertEqual(
                "10:27 坐在繁华的商业街旁边的阳光咖啡馆，一边喝拿铁一边在平板上绘制服装设计草图",
                saved_metadata["edited.png"]["schedule_time"],
            )
            delete_image_files.assert_called_once_with(original_filename)

    async def test_reroll_precision_edit_uses_current_image_as_edit_source(self):
        with tempfile.TemporaryDirectory(prefix="portrait-gallery-edit-reroll-") as temp_dir:
            data_dir = Path(temp_dir) / "data"
            image_dir = data_dir / "images"
            image_dir.mkdir(parents=True)
            current_filename = "edited.png"
            current_path = image_dir / current_filename
            Image.new("RGB", (48, 64), (220, 230, 240)).save(current_path)
            edit_history = [{
                "from_image": "original.png",
                "target": "background",
                "instruction": "换成雨后街道",
            }]
            ScheduleStore(str(data_dir)).save({
                "card": {
                    "id": current_filename,
                    "date": "2026-07-13",
                    "time": "10:27",
                    "image_filename": current_filename,
                    "image_path": f"/images/{current_filename}",
                    "prompt": "precision edit prompt",
                    "outfit_style": "清新风",
                    "outfit": "风格：清新风 穿搭：白衬衫",
                    "status": "ok",
                    "source": "cron",
                    "schedule_time": "08:00 上班",
                    "generation_type": "image_edit",
                    "prompt_mode": "precision_edit",
                    "pure_prompt": True,
                    "custom_ref_mode": "precision_edit",
                    "edit_target": "background",
                    "edit_target_label": "背景",
                    "edit_instruction": "换成雨后街道",
                    "edit_history": edit_history,
                    "edited_from": "original.png",
                    "original_image_filename": "original.png",
                },
            })
            (data_dir / "image_metadata.json").write_text(
                json.dumps({current_filename: {"model": "gpt-image-2"}}),
                encoding="utf-8",
            )
            saved_metadata = {}
            delete_image_files = Mock(return_value=([current_filename], []))
            app = PortraitGalleryApp.__new__(PortraitGalleryApp)
            app.data_dir = str(data_dir)
            app.config = {}
            app.web_server = SimpleNamespace(
                _image_file_path=lambda filename: str(current_path) if filename == current_filename else "",
                _update_image_metadata_entry=lambda filename, entry: saved_metadata.update({filename: entry}),
                _delete_image_files=delete_image_files,
            )
            app.image_gen = SimpleNamespace(
                default_engine="gitee",
                generate=AsyncMock(return_value="rerolled.png"),
            )

            result = await app.reroll_image(current_filename)

            call = app.image_gen.generate.await_args
            self.assertEqual("gptimage", call.kwargs["engine"])
            self.assertEqual(str(current_path), call.kwargs["ref_image"])
            self.assertEqual("48x64", call.kwargs["size"])
            self.assertEqual("", call.kwargs["schedule_time"])
            self.assertTrue(call.kwargs["prompt_final"])
            self.assertTrue(call.kwargs["no_auto_style"])
            self.assertTrue(call.kwargs["precise_edit"])
            self.assertEqual("image_edit", result["generation_type"])
            self.assertEqual("precision_edit", result["prompt_mode"])
            self.assertEqual("background", result["edit_target"])
            self.assertEqual(edit_history, result["edit_history"])
            self.assertEqual(current_filename, result["requested_ref_image"])
            self.assertEqual(current_filename, result["rerolled_from"])
            self.assertTrue(saved_metadata["rerolled.png"]["precise_edit"])
            delete_image_files.assert_called_once_with(current_filename)


class ImageEditEndpointTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_server(root: Path) -> GalleryServer:
        data_dir = root / "data"
        config_path = root / "config" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("gallery:\n  port: 18889\n", encoding="utf-8")
        (root / "app" / "references").mkdir(parents=True, exist_ok=True)
        return GalleryServer(
            {"paths": {"project_root": str(root)}, "gallery": {"port": 18889}},
            str(data_dir),
            str(config_path),
        )

    async def test_endpoint_validates_and_forwards_precision_edit(self):
        with tempfile.TemporaryDirectory(prefix="portrait-gallery-edit-api-") as temp_dir:
            server = self._make_server(Path(temp_dir))
            server.on_edit_image = AsyncMock(return_value={
                "id": "edited.png",
                "date": "2026-07-15",
                "time": "11:30",
                "image_filename": "edited.png",
                "image_path": "/images/edited.png",
                "status": "ok",
                "source": "image_edit",
                "replaced_image_filename": "original.png",
                "schedule_time": "11:30 在雨后的窗边整理设计稿",
            })
            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                response = await client.post(
                    "/api/images/original.png/edit",
                    json={
                        "target": "background",
                        "instruction": "换成雨后街道",
                        "schedule_description": "在雨后的窗边整理设计稿",
                    },
                )
                payload = await response.json()
                invalid = await client.post(
                    "/api/images/original.png/edit",
                    json={"target": "unknown", "instruction": "测试"},
                )
                too_long = await client.post(
                    "/api/images/original.png/edit",
                    json={
                        "target": "background",
                        "instruction": "测试",
                        "schedule_description": "日" * 161,
                    },
                )
            finally:
                await client.close()

        self.assertEqual(200, response.status)
        self.assertEqual("edited.png", payload["image_filename"])
        self.assertEqual("original.png", payload["replaced_image_filename"])
        server.on_edit_image.assert_awaited_once_with(
            "original.png",
            "background",
            "换成雨后街道",
            "在雨后的窗边整理设计稿",
        )
        self.assertEqual(400, invalid.status)
        self.assertEqual(400, too_long.status)


class ImageEditFrontendContractTest(unittest.TestCase):
    def test_modal_uses_edit_button_and_precision_endpoint(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="modalEditBtn"', html)
        self.assertIn("openImageEdit()", html)
        self.assertIn("/edit`", html)
        self.assertIn('data-image-edit-target="background"', html)
        self.assertIn('id="imageEditScheduleDescription"', html)
        self.assertNotIn("function modalShare()", html)
        submit_start = html.index("async function submitImageEdit()")
        submit_end = html.index("function wardrobeEscapeRegExp", submit_start)
        submit = html[submit_start:submit_end]
        self.assertIn("replaceLocalImage(replacedImage, data)", submit)
        self.assertIn("scheduleManuallyChanged ? {schedule_description:scheduleDescription} : {}", submit)
        self.assertIn("图片与日程说明已更新", submit)
        self.assertIn("已替换当前图片", submit)
        self.assertNotIn("原图已保留", submit)
        self.assertNotIn("revealGeneratedGalleryEntry", submit)
        self.assertIn("function isPrecisionImageEditEntry(e)", html)
        self.assertIn('generationType === "image_edit"', html)
        self.assertIn('if (isPrecisionImageEditEntry(e)) return "";', html)
        self.assertIn("renderScheduleItemHtml(e.schedule_time, e.image_filename) + editContextHtml", html)
        self.assertIn("grid-template-columns:minmax(330px,350px) minmax(0,1fr)", html)
        self.assertIn('class="image-edit-form-scroll"', html)
        self.assertIn("grid-template-rows:minmax(0,1fr) auto", html)
        self.assertIn("min-width:0;min-height:0;overflow-y:auto", html)
        self.assertIn("border-top:1px solid #f0e8f1", html)
        self.assertIn("max-height:none;object-fit:cover;object-position:center", html)
        self.assertIn("grid-template-rows:auto clamp(300px,52vh,500px) auto", html)

    def test_character_style_is_not_relabelled_as_group_chat(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")
        start = html.index("function displayOutfitStyle(e)")
        end = html.index("function compareStyleFilterLabels", start)
        display_style = html[start:end]

        self.assertNotIn('style === "角色"', display_style)
        self.assertIn('return isBaseModelStyle(style) ? "自定义" : style;', display_style)


if __name__ == "__main__":
    unittest.main()
