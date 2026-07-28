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
    normalize_image_edit_target,
    replace_image_schedule_description,
    rewrite_image_edit_schedule_description,
)
from image_gen import ImageGenerator  # noqa: E402
from image_versions import archive_image_version, image_version_path  # noqa: E402
from main import PortraitGalleryApp  # noqa: E402
from store import ImageMetadataStore, ScheduleStore  # noqa: E402
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

    def test_schedule_only_prompt_changes_visible_activity(self):
        prompt = build_precision_image_edit_prompt(
            "background",
            "",
            previous_schedule_description="在晨光中进行全身拉伸唤醒肌肉",
            schedule_description="坐在窗边喝咖啡并阅读杂志",
        )

        self.assertIn("UPDATED SCHEDULE ACTIVITY: 坐在窗边喝咖啡并阅读杂志", prompt)
        self.assertIn("Change the subject's action and pose", prompt)
        self.assertIn("scene/background", prompt)
        self.assertIn("Preserve the person's identity", prompt)
        self.assertNotIn("EDIT SCOPE: 背景 ONLY", prompt)

    def test_schedule_target_is_internal_only(self):
        self.assertEqual("", normalize_image_edit_target("schedule"))
        self.assertEqual(
            "schedule",
            normalize_image_edit_target("schedule", allow_internal=True),
        )

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
            self.assertEqual(1, replacement["version_count"])
            self.assertEqual(1, len(replacement["image_versions"]))
            archived_path = image_version_path(
                str(data_dir),
                replacement["image_versions"][0],
            )
            self.assertIsNotNone(archived_path)
            self.assertTrue(archived_path.is_file())
            self.assertEqual(original_path.read_bytes(), archived_path.read_bytes())
            self.assertEqual(
                "10:27 坐在市场旁边的阳光咖啡馆，一边喝拿铁一边在平板上绘制服装设计草图",
                replacement["edit_history"][0]["previous_schedule_time"],
            )
            call = app.image_gen.generate.await_args
            self.assertEqual(str(original_path), call.kwargs["ref_image"])
            self.assertEqual("48x64", call.kwargs["size"])
            self.assertTrue(call.kwargs["precise_edit"])
            self.assertTrue(call.kwargs["prompt_final"])
            self.assertIn("UPDATED SCHEDULE ACTIVITY", call.args[0])
            self.assertIn("坐在繁华的商业街旁边的阳光咖啡馆", call.args[0])
            self.assertEqual(original_filename, saved_metadata["edited.png"]["edited_from"])
            self.assertEqual(original_filename, saved_metadata["edited.png"]["replaced_image_filename"])
            self.assertEqual(
                "10:27 坐在繁华的商业街旁边的阳光咖啡馆，一边喝拿铁一边在平板上绘制服装设计草图",
                saved_metadata["edited.png"]["schedule_time"],
            )
            delete_image_files.assert_called_once_with(original_filename)

    async def test_schedule_only_edit_regenerates_image_and_updates_card(self):
        with tempfile.TemporaryDirectory(prefix="portrait-gallery-schedule-edit-main-") as temp_dir:
            data_dir = Path(temp_dir) / "data"
            image_dir = data_dir / "images"
            image_dir.mkdir(parents=True)
            original_filename = "original.png"
            original_path = image_dir / original_filename
            Image.new("RGB", (48, 64), (235, 225, 215)).save(original_path)
            ScheduleStore(str(data_dir)).save({
                original_filename: {
                    "id": original_filename,
                    "date": "2026-07-16",
                    "time": "07:12",
                    "image_filename": original_filename,
                    "image_path": f"/images/{original_filename}",
                    "outfit": "风格：酷飒风 穿搭：黑色工装造型",
                    "status": "ok",
                    "source": "cron",
                    "schedule_time": "07:12 在晨光中进行全身拉伸唤醒肌肉",
                },
            })
            (data_dir / "image_metadata.json").write_text(
                json.dumps({original_filename: {"model": "gpt-image-2"}}),
                encoding="utf-8",
            )
            saved_metadata = {}
            delete_image_files = Mock(return_value=([original_filename], []))
            app = PortraitGalleryApp.__new__(PortraitGalleryApp)
            app.data_dir = str(data_dir)
            app.config = {}
            app.web_server = SimpleNamespace(
                _image_file_path=lambda filename: str(original_path) if filename == original_filename else "",
                _update_image_metadata_entry=lambda filename, entry: saved_metadata.update({filename: entry}),
                _delete_image_files=delete_image_files,
            )
            app.image_gen = SimpleNamespace(generate=AsyncMock(return_value="edited.png"))

            result = await app.edit_image(
                original_filename,
                "background",
                "",
                "坐在窗边喝咖啡并阅读杂志",
            )

            prompt = app.image_gen.generate.await_args.args[0]
            self.assertEqual("ok", result["status"])
            self.assertEqual("schedule", result["edit_target"])
            self.assertEqual("日程", result["edit_target_label"])
            self.assertEqual("坐在窗边喝咖啡并阅读杂志", result["edit_instruction"])
            self.assertEqual("07:12 坐在窗边喝咖啡并阅读杂志", result["schedule_time"])
            self.assertIn("UPDATED SCHEDULE ACTIVITY: 坐在窗边喝咖啡并阅读杂志", prompt)
            self.assertIn("Change the subject's action and pose", prompt)
            self.assertEqual("schedule", saved_metadata["edited.png"]["edit_target"])
            delete_image_files.assert_called_once_with(original_filename)

    async def test_edit_discards_result_when_source_is_replaced_during_generation(self):
        with tempfile.TemporaryDirectory(prefix="portrait-gallery-edit-conflict-") as temp_dir:
            data_dir = Path(temp_dir) / "data"
            image_dir = data_dir / "images"
            image_dir.mkdir(parents=True)
            original_filename = "original.png"
            original_path = image_dir / original_filename
            Image.new("RGB", (48, 64), (225, 215, 205)).save(original_path)
            store = ScheduleStore(str(data_dir))
            store.save({
                "card": {
                    "id": original_filename,
                    "date": "2026-07-16",
                    "time": "10:27",
                    "image_filename": original_filename,
                    "image_path": f"/images/{original_filename}",
                    "status": "ok",
                    "source": "cron",
                    "schedule_time": "10:27 原始活动",
                },
            })
            (data_dir / "image_metadata.json").write_text("{}", encoding="utf-8")
            update_metadata = Mock()
            delete_image_files = Mock(return_value=(["edited.png"], []))

            async def replace_source_during_generate(*_args, **_kwargs):
                store.update(lambda entries: {
                    **entries,
                    "card": {
                        **entries["card"],
                        "id": "rerolled.png",
                        "image_filename": "rerolled.png",
                        "image_path": "/images/rerolled.png",
                    },
                })
                return "edited.png"

            app = PortraitGalleryApp.__new__(PortraitGalleryApp)
            app.data_dir = str(data_dir)
            app.config = {}
            app.web_server = SimpleNamespace(
                _image_file_path=lambda filename: str(original_path) if filename == original_filename else "",
                _update_image_metadata_entry=update_metadata,
                _delete_image_files=delete_image_files,
            )
            app.image_gen = SimpleNamespace(generate=AsyncMock(side_effect=replace_source_during_generate))

            result = await app.edit_image(original_filename, "background", "改成雨后街道")

            self.assertEqual("failed", result["status"])
            self.assertEqual("edit_source_changed", result["error"])
            self.assertEqual("rerolled.png", store.load()["card"]["image_filename"])
            update_metadata.assert_not_called()
            delete_image_files.assert_called_once_with("edited.png")
            version_dir = data_dir / "image_versions"
            self.assertFalse(version_dir.exists() and any(version_dir.iterdir()))

    async def test_reroll_precision_edit_uses_current_image_as_edit_source(self):
        with tempfile.TemporaryDirectory(prefix="portrait-gallery-edit-reroll-") as temp_dir:
            data_dir = Path(temp_dir) / "data"
            image_dir = data_dir / "images"
            image_dir.mkdir(parents=True)
            current_filename = "edited.png"
            current_path = image_dir / current_filename
            Image.new("RGB", (48, 64), (220, 230, 240)).save(current_path)
            generated_filename = "rerolled.png"
            generated_path = image_dir / generated_filename
            Image.new("RGB", (48, 64), (80, 100, 120)).save(generated_path)
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
            delete_image_files = Mock(return_value=([generated_filename], []))
            app = PortraitGalleryApp.__new__(PortraitGalleryApp)
            app.data_dir = str(data_dir)
            app.config = {}
            app.web_server = SimpleNamespace(
                _image_file_path=lambda filename: str(current_path) if filename == current_filename else "",
                _delete_image_files=delete_image_files,
            )
            app.image_gen = SimpleNamespace(
                default_engine="gitee",
                output_dir=str(image_dir),
                generate=AsyncMock(return_value=generated_filename),
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
            self.assertEqual(1, result["version_count"])
            self.assertEqual(1, len(result["image_versions"]))
            self.assertTrue(
                image_version_path(str(data_dir), result["image_versions"][0]).is_file()
            )
            self.assertEqual(current_filename, result["requested_ref_image"])
            self.assertEqual(current_filename, result["rerolled_from"])
            self.assertEqual(current_filename, result["image_filename"])
            self.assertEqual(current_filename, ScheduleStore(str(data_dir)).load()["card"]["image_filename"])
            self.assertEqual((80, 100, 120), Image.open(current_path).getpixel((0, 0)))
            saved_metadata = ImageMetadataStore(str(data_dir)).load()
            self.assertTrue(saved_metadata[current_filename]["precise_edit"])
            self.assertNotIn(generated_filename, saved_metadata)
            delete_image_files.assert_called_once_with(generated_filename)

    async def test_regular_reroll_archives_current_image_and_preserves_history(self):
        with tempfile.TemporaryDirectory(prefix="portrait-gallery-reroll-history-") as temp_dir:
            data_dir = Path(temp_dir) / "data"
            image_dir = data_dir / "images"
            image_dir.mkdir(parents=True)
            current_filename = "current.png"
            current_path = image_dir / current_filename
            Image.new("RGB", (48, 64), (210, 220, 230)).save(current_path)
            generated_filename = "rerolled.png"
            generated_path = image_dir / generated_filename
            Image.new("RGB", (48, 64), (60, 80, 100)).save(generated_path)
            previous_path = image_dir / "previous.png"
            Image.new("RGB", (48, 64), (180, 190, 200)).save(previous_path)
            previous_version = archive_image_version(
                str(data_dir),
                str(previous_path),
                original_image_filename=current_filename,
                target="background",
                target_label="背景",
                instruction="上一版背景",
            )
            ScheduleStore(str(data_dir)).save({
                "card": {
                    "id": current_filename,
                    "date": "2026-07-19",
                    "time": "10:27",
                    "image_filename": current_filename,
                    "image_path": f"/images/{current_filename}",
                    "prompt": "adult portrait in a cafe",
                    "outfit_style": "清新风",
                    "outfit": "风格：清新风 穿搭：白衬衫",
                    "status": "ok",
                    "source": "custom",
                    "prompt_mode": "pure",
                    "pure_prompt": True,
                    "image_versions": [previous_version],
                    "version_count": 1,
                },
            })
            (data_dir / "image_metadata.json").write_text(
                json.dumps({
                    current_filename: {
                        "model": "gpt-image-2",
                        "prompt": "adult portrait in a cafe",
                    },
                    generated_filename: {
                        "model": "gpt-image-2",
                        "prompt": "rerolled adult portrait in a cafe",
                    },
                }),
                encoding="utf-8",
            )
            delete_image_files = Mock(return_value=([generated_filename], []))
            app = PortraitGalleryApp.__new__(PortraitGalleryApp)
            app.data_dir = str(data_dir)
            app.config = {}
            app.web_server = SimpleNamespace(
                _image_file_path=lambda filename: (
                    str(current_path) if filename == current_filename else ""
                ),
                _delete_image_files=delete_image_files,
            )
            app.image_gen = SimpleNamespace(
                default_engine="gptimage",
                output_dir=str(image_dir),
                generate=AsyncMock(return_value=generated_filename),
            )

            result = await app.reroll_image(current_filename)

            self.assertEqual("ok", result["status"])
            self.assertEqual(2, result["version_count"])
            self.assertEqual(previous_version["id"], result["image_versions"][0]["id"])
            reroll_version = result["image_versions"][1]
            self.assertEqual("reroll", reroll_version["target"])
            self.assertEqual("重抽", reroll_version["target_label"])
            self.assertFalse(str(reroll_version.get("instruction") or "").strip())
            self.assertTrue(image_version_path(str(data_dir), reroll_version).is_file())
            saved = ScheduleStore(str(data_dir)).load()["card"]
            self.assertEqual(current_filename, result["image_filename"])
            self.assertEqual(current_filename, saved["image_filename"])
            self.assertEqual(2, saved["version_count"])
            self.assertEqual(1, len(ScheduleStore(str(data_dir)).load()))
            self.assertEqual((60, 80, 100), Image.open(current_path).getpixel((0, 0)))
            self.assertEqual((210, 220, 230), Image.open(image_version_path(str(data_dir), reroll_version)).getpixel((0, 0)))
            saved_metadata = ImageMetadataStore(str(data_dir)).load()
            self.assertIn(current_filename, saved_metadata)
            self.assertNotIn(generated_filename, saved_metadata)
            self.assertEqual("rerolled adult portrait in a cafe", saved_metadata[current_filename]["prompt"])
            delete_image_files.assert_called_once_with(generated_filename)

    async def test_scheduled_reroll_also_creates_a_history_version(self):
        with tempfile.TemporaryDirectory(prefix="portrait-gallery-scheduled-reroll-history-") as temp_dir:
            data_dir = Path(temp_dir) / "data"
            image_dir = data_dir / "images"
            image_dir.mkdir(parents=True)
            current_filename = "scheduled.png"
            current_path = image_dir / current_filename
            Image.new("RGB", (48, 64), (200, 210, 220)).save(current_path)
            generated_filename = "scheduled-rerolled.png"
            generated_path = image_dir / generated_filename
            Image.new("RGB", (48, 64), (40, 60, 80)).save(generated_path)
            ScheduleStore(str(data_dir)).save({
                "card": {
                    "id": current_filename,
                    "date": "2026-07-18",
                    "time": "10:27",
                    "image_filename": current_filename,
                    "image_path": f"/images/{current_filename}",
                    "prompt": "stored scheduled portrait prompt",
                    "outfit_style": "清新风",
                    "outfit": "风格：清新风 穿搭：白衬衫",
                    "status": "ok",
                    "source": "cron",
                    "schedule_time": "10:27 在咖啡馆阅读",
                },
            })
            (data_dir / "image_metadata.json").write_text(
                json.dumps({
                    current_filename: {
                        "model": "gpt-image-2",
                        "prompt": "stored scheduled portrait prompt",
                    },
                }),
                encoding="utf-8",
            )
            delete_image_files = Mock(return_value=([generated_filename], []))
            app = PortraitGalleryApp.__new__(PortraitGalleryApp)
            app.data_dir = str(data_dir)
            app.config = {}
            app.web_server = SimpleNamespace(
                _image_file_path=lambda filename: (
                    str(current_path) if filename == current_filename else ""
                ),
                _delete_image_files=delete_image_files,
            )
            app.image_gen = SimpleNamespace(
                default_engine="gptimage",
                output_dir=str(image_dir),
                generate=AsyncMock(return_value=generated_filename),
            )

            result = await app.reroll_image(current_filename)

            call = app.image_gen.generate.await_args
            self.assertEqual("10:27 在咖啡馆阅读", call.kwargs["schedule_time"])
            self.assertEqual(1, result["version_count"])
            self.assertEqual("reroll", result["image_versions"][0]["target"])
            self.assertEqual("重抽", result["image_versions"][0]["target_label"])
            self.assertTrue(
                image_version_path(str(data_dir), result["image_versions"][0]).is_file()
            )
            self.assertEqual("10:27 在咖啡馆阅读", result["schedule_time"])
            self.assertEqual(current_filename, result["image_filename"])
            self.assertEqual(current_filename, ScheduleStore(str(data_dir)).load()["card"]["image_filename"])
            self.assertEqual((40, 60, 80), Image.open(current_path).getpixel((0, 0)))
            delete_image_files.assert_called_once_with(generated_filename)


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

    async def test_endpoint_allows_schedule_only_image_edit(self):
        with tempfile.TemporaryDirectory(prefix="portrait-gallery-schedule-edit-api-") as temp_dir:
            server = self._make_server(Path(temp_dir))
            server.on_edit_image = AsyncMock(return_value={
                "id": "edited.png",
                "date": "2026-07-16",
                "time": "07:12",
                "image_filename": "edited.png",
                "image_path": "/images/edited.png",
                "status": "ok",
                "source": "image_edit",
                "replaced_image_filename": "original.png",
                "schedule_time": "07:12 坐在窗边喝咖啡并阅读杂志",
            })
            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                response = await client.post(
                    "/api/images/original.png/edit",
                    json={
                        "target": "background",
                        "instruction": "",
                        "schedule_description": "坐在窗边喝咖啡并阅读杂志",
                    },
                )
                missing_both = await client.post(
                    "/api/images/original.png/edit",
                    json={"target": "background", "instruction": ""},
                )
                direct_schedule_target = await client.post(
                    "/api/images/original.png/edit",
                    json={"target": "schedule", "instruction": "坐在窗边喝咖啡"},
                )
            finally:
                await client.close()

        self.assertEqual(200, response.status)
        self.assertEqual(400, missing_both.status)
        self.assertEqual(400, direct_schedule_target.status)
        server.on_edit_image.assert_awaited_once_with(
            "original.png",
            "background",
            "",
            "坐在窗边喝咖啡并阅读杂志",
        )

    async def test_reroll_endpoint_returns_same_card_filename_with_revision(self):
        with tempfile.TemporaryDirectory(prefix="portrait-gallery-reroll-api-") as temp_dir:
            server = self._make_server(Path(temp_dir))
            server.on_reroll_image = AsyncMock(return_value={
                "id": "original.png",
                "date": "2026-07-23",
                "time": "10:27",
                "image_filename": "original.png",
                "image_path": "/images/original.png",
                "status": "ok",
                "source": "cron",
                "replaced_image_filename": "original.png",
                "version_count": 1,
            })
            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                response = await client.post(
                    "/api/images/original.png/reroll",
                    json={},
                )
                payload = await response.json()
            finally:
                await client.close()

        self.assertEqual(200, response.status)
        self.assertEqual("original.png", payload["image_filename"])
        self.assertEqual("original.png", payload["replaced_image_filename"])
        self.assertTrue(payload["image_revision"])
        server.on_reroll_image.assert_awaited_once_with("original.png")


class ImageEditFrontendContractTest(unittest.TestCase):
    def test_long_running_image_actions_have_bounded_frontend_requests(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("async function fetchWithTimeout(", html)
        self.assertIn("const IMAGE_OPERATION_TIMEOUT_MS = 31 * 60 * 1000;", html)
        self.assertIn("controller.abort()", html)
        for function_name, next_marker in (
            ("async function triggerGenerate()", "// ===== Parse schedule ====="),
            ("async function submitImageEdit()", "function wardrobeEscapeRegExp"),
            ("async function rerollImage(", "async function rerollModalImage"),
            ("async function submitCustomGen()", "async function quickCustomImg2ImgFromModal"),
            ("async function quickCustomImg2ImgFromModal", "// ===== Load ====="),
            ("async function generateWardrobeImage", "document.addEventListener('click'"),
            ("async function activateWardrobeVersion", "async function openWardrobeVersions"),
        ):
            with self.subTest(function=function_name):
                start = html.index(function_name)
                end = html.index(next_marker, start)
                self.assertIn("fetchWithTimeout(", html[start:end])

    def test_modal_uses_edit_button_and_precision_endpoint(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="modalEditBtn"', html)
        self.assertIn("openImageEdit()", html)
        self.assertIn("/edit`", html)
        self.assertIn('data-image-edit-target="background"', html)
        self.assertIn('id="imageEditScheduleDescription"', html)
        self.assertIn('id="modalVersionBtn"', html)
        self.assertIn('fa-clock-rotate-left', html)
        self.assertNotIn('id="modalVersionCount"', html)
        self.assertIn('imageVersionOverlay.id = "imageVersionOverlay"', html)
        self.assertIn('async function openImageVersions(event)', html)
        self.assertIn('/versions`', html)
        self.assertIn('data-version-activate-index=', html)
        self.assertIn('/activate`', html)
        self.assertIn('async function activateImageVersion(item, button)', html)
        self.assertIn(
            '.modal-content .today-card-info { flex:0 0 auto;min-height:auto;',
            html,
        )
        self.assertIn(
            '.modal-content .today-card-info > * { flex-shrink:0; }',
            html,
        )
        self.assertNotIn("function modalShare()", html)
        submit_start = html.index("async function submitImageEdit()")
        submit_end = html.index("function wardrobeEscapeRegExp", submit_start)
        submit = html[submit_start:submit_end]
        self.assertIn("replaceLocalImage(replacedImage, data)", submit)
        self.assertIn("scheduleManuallyChanged ? {schedule_description:scheduleDescription} : {}", submit)
        self.assertIn("if (!instruction && !scheduleManuallyChanged)", submit)
        self.assertIn("请输入修改内容或调整日程说明", submit)
        self.assertIn("图片与日程说明已更新", submit)
        self.assertIn("已替换当前图片", submit)
        self.assertIn("已开始后台编辑，可关闭窗口继续浏览", submit)
        self.assertIn("const editDialogStillOpen", submit)
        self.assertIn("if (editDialogStillOpen)", submit)
        self.assertNotIn("原图已保留", submit)
        self.assertNotIn("revealGeneratedGalleryEntry", submit)
        self.assertNotIn("closeImageEdit(true)", submit)
        self.assertIn("function isPrecisionImageEditEntry(e)", html)
        self.assertIn('generationType === "image_edit"', html)
        self.assertIn('if (isPrecisionImageEditEntry(e)) return "";', html)
        self.assertIn("renderScheduleItemHtml(e.schedule_time, e.image_filename) + editContextHtml", html)
        self.assertIn("grid-template-columns:minmax(330px,350px) minmax(0,1fr)", html)
        self.assertIn('class="image-edit-form-scroll"', html)
        self.assertIn("grid-template-rows:minmax(0,1fr) auto", html)
        self.assertIn("min-width:0;min-height:0;overflow-y:auto", html)
        self.assertIn("border-top:1px solid #f0e8f1", html)
        self.assertIn("display:flex;flex-direction:column;padding:15px", html)
        self.assertIn("max-height:none;object-fit:cover;object-position:center", html)
        self.assertIn("grid-template-rows:auto clamp(300px,52vh,500px) auto", html)
        self.assertNotIn("if (imageEditBusy && !force) return", html)
        self.assertIn("cancel.textContent = imageEditBusy ? '关闭窗口' : '取消';", html)
        self.assertIn("document.getElementById('imageEditCloseBtn').disabled = false;", html)

    def test_reroll_replaces_same_card_and_cache_busts_image(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")
        reroll_start = html.index("async function rerollImage(")
        reroll_end = html.index("async function rerollModalImage", reroll_start)
        reroll = html[reroll_start:reroll_end]

        self.assertIn("const oldImgId = data.replaced_image_filename || imgId", reroll)
        self.assertIn("const revision = imageRevisionToken(data.image_revision) || String(Date.now())", reroll)
        self.assertIn("withImageRevision(", reroll)
        self.assertIn("image_filename: oldImgId", reroll)
        self.assertIn("replaceLocalImage(oldImgId, fresh)", reroll)
        self.assertNotIn("upsertGeneratedGalleryEntry(fresh)", reroll)
        self.assertIn("<span>后台编辑中</span>", html)

    def test_gallery_image_urls_keep_revision_after_full_reload(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")
        url_start = html.index("function imageRevisionToken(value)")
        url_end = html.index("function truncateGroupChatMetadataText", url_start)
        image_url_helpers = html[url_start:url_end]

        self.assertIn("function withImageRevision(url, revision)", image_url_helpers)
        self.assertIn("entry.image_revision", image_url_helpers)
        self.assertIn('base.includes("?") ? "&" : "?"', image_url_helpers)
        self.assertIn("encodeURIComponent(token)", image_url_helpers)

    def test_gallery_card_shows_favorite_reroll_edit_and_delete_actions(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")
        grid_start = html.index(': `<div class="gallery-grid">')
        grid_end = html.index("content.insertAdjacentHTML", grid_start)
        gallery_grid = html[grid_start:grid_end]

        self.assertIn('<span class="ca-label">收藏</span>', gallery_grid)
        self.assertIn('<span class="ca-label">编辑</span>', gallery_grid)
        self.assertIn('<span class="ca-label">删除</span>', gallery_grid)
        self.assertNotIn('<span class="ca-label">分享</span>', gallery_grid)
        self.assertNotIn('<span class="ca-label">详情</span>', gallery_grid)
        self.assertIn("renderRerollButton(e, {card: true})", gallery_grid)
        self.assertIn('<span class="ca-label">重抽</span>', html)
        self.assertIn("openImageEditForImage(${imageFilenameArg})", gallery_grid)
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr))", html)

    def test_character_style_is_not_relabelled_as_group_chat(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")
        start = html.index("function displayOutfitStyle(e)")
        end = html.index("function compareStyleFilterLabels", start)
        display_style = html[start:end]

        self.assertNotIn('style === "角色"', display_style)
        self.assertIn('return isBaseModelStyle(style) ? "自定义" : style;', display_style)


if __name__ == "__main__":
    unittest.main()
