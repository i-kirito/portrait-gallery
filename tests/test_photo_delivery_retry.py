import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import main as main_module  # noqa: E402
from main import PhotoDeliveryError, PortraitGalleryApp  # noqa: E402
from store import ImageMetadataStore, ScheduleStore  # noqa: E402
from web_server import GalleryServer  # noqa: E402


class PhotoDeliveryRetryTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_app(root: Path) -> PortraitGalleryApp:
        data_dir = root / "data"
        image_dir = data_dir / "images"
        image_dir.mkdir(parents=True)
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app.data_dir = str(data_dir)
        app.config = {"config": {"timezone": "Asia/Shanghai"}, "integrations": {}}
        app.web_server = SimpleNamespace(
            image_dir=str(image_dir),
            _image_search_dirs=lambda: [str(image_dir)],
        )
        app._failed_photo_jobs = {}
        app._photo_job_schedule_meta = {}
        app._photo_jobs_inflight = set()
        app._photo_jobs_inflight_started = {}
        app._inflight_lock = asyncio.Lock()
        app._hermes_send_lock = asyncio.Lock()
        app._hermes_send_cooldown_until = 0.0
        app._last_delivery_error = ""
        return app

    @staticmethod
    def _seed_image(app: PortraitGalleryApp, filename: str, time_text: str) -> Path:
        image_path = Path(app.web_server.image_dir) / filename
        image_path.write_bytes(b"generated-image")
        ScheduleStore(app.data_dir).save({
            filename: {
                "date": app._today().isoformat(),
                "time": time_text,
                "schedule_time": f"{time_text} 测试活动",
                "image_filename": filename,
                "caption": "原图文案",
                "status": "ok",
                "source": "cron",
            },
        })
        return image_path

    async def test_delivery_retry_resends_existing_image_and_marks_sent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(Path(tmpdir))
            image_path = self._seed_image(app, "existing.png", "15:12")
            slot_key = f"{app._today().isoformat()} 15:12"
            failed = {
                "reason": "delivery_failed",
                "image_filename": image_path.name,
                "image_path": str(image_path),
                "caption": "原图文案",
            }
            app._failed_photo_jobs[slot_key] = dict(failed)
            app._mark_photo_job_inflight(slot_key)
            app._send_generated_photo = AsyncMock(return_value=True)

            await app._retry_photo_delivery(slot_key, dict(failed))

            app._send_generated_photo.assert_awaited_once_with(str(image_path), "原图文案")
            self.assertNotIn(slot_key, app._failed_photo_jobs)
            self.assertNotIn(slot_key, app._photo_jobs_inflight)
            entry = ScheduleStore(app.data_dir).load()[image_path.name]
            self.assertEqual("sent", entry.get("delivery_status"))

    async def test_retry_detects_delivery_failure_before_already_done(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(Path(tmpdir))
            image_path = self._seed_image(app, "existing.png", "15:12")
            slot_key = f"{app._today().isoformat()} 15:12"
            app._failed_photo_jobs[slot_key] = {
                "reason": "delivery_failed",
                "image_filename": image_path.name,
                "image_path": str(image_path),
                "caption": "原图文案",
            }
            app._check_photo_exists_for_slot = lambda *_args: True

            def close_task(coro):
                coro.close()
                return None

            with patch.object(main_module.asyncio, "create_task", side_effect=close_task):
                result = await app.retry_photo_job("15:12")

            self.assertEqual("queued", result.get("status"))
            self.assertEqual("resend", result.get("action"))
            self.assertEqual(image_path.name, result.get("image_filename"))

    async def test_scheduled_delivery_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app = self._make_app(root)
            image_path = self._seed_image(app, "scheduled.png", "15:12")
            app.image_gen = SimpleNamespace(
                python_executable=sys.executable,
                generate_script=str(root / "generate.py"),
                script_dir=str(root),
                build_env=lambda: dict(os.environ),
            )
            app._is_photo_quiet_now = lambda: False
            app._check_photo_exists_for_slot = lambda *_args: False
            app._photo_quota_snapshot = lambda *_args, **_kwargs: (4, 0, 0, 0, 0, 0, 4)
            app._today_schedule_entry = lambda: {}
            app._select_reference_for_generation = AsyncMock(return_value={})

            async def fail_delivery(_image_path, _caption):
                app._last_delivery_error = "wechat rate limited"
                return False

            app._send_generated_photo = fail_delivery
            process = subprocess.CompletedProcess(
                [],
                0,
                stdout=f"SUCCESS:{image_path}\nCAPTION:原图文案\n",
                stderr="[caption] llm caption rejected: reason=generic_template\n",
            )

            with self.assertLogs("portrait_gallery", level="WARNING") as logs:
                with patch.object(main_module.subprocess, "run", return_value=process):
                    with self.assertRaises(PhotoDeliveryError):
                        await app.photo_job("noon", "15:12 测试活动")

            self.assertTrue(any("reason=generic_template" in line for line in logs.output))

            slot_key = f"{app._today().isoformat()} 15:12"
            self.assertEqual("delivery_failed", app._failed_photo_jobs[slot_key].get("reason"))
            self.assertEqual("scheduled.png", app._failed_photo_jobs[slot_key].get("image_filename"))
            persisted = json.loads(
                Path(app._failed_photo_jobs_path()).read_text(encoding="utf-8")
            )
            self.assertIn(slot_key, persisted)
            entry = ScheduleStore(app.data_dir).load()["scheduled.png"]
            self.assertEqual("failed", entry.get("delivery_status"))

    async def test_startup_recovery_turns_pending_delivery_into_manual_resend(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(Path(tmpdir))
            image_path = self._seed_image(app, "pending.png", "15:12")
            ScheduleStore(app.data_dir).update(lambda data: {
                **data,
                image_path.name: {
                    **data[image_path.name],
                    "delivery_status": "pending",
                },
            })

            app._recover_orphaned_generated_photos()

            slot_key = f"{app._today().isoformat()} 15:12"
            failed = app._failed_photo_jobs[slot_key]
            self.assertEqual("delivery_failed", failed.get("reason"))
            self.assertEqual(image_path.name, failed.get("image_filename"))
            entry = ScheduleStore(app.data_dir).load()[image_path.name]
            self.assertEqual("failed", entry.get("delivery_status"))
            self.assertEqual("delivery_interrupted_before_send", entry.get("delivery_error"))

    async def test_startup_recovery_reuses_metadata_only_generated_image(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(Path(tmpdir))
            time_text = "15:12"
            filename = f"zhuzhu_schedule_{time_text.replace(':', '')}_test_{int(app._now().timestamp())}.png"
            image_path = Path(app.web_server.image_dir) / filename
            image_path.write_bytes(b"generated-image")
            ImageMetadataStore(app.data_dir).save({
                filename: {
                    "source": "cron",
                    "created_at": int(app._now().timestamp()),
                    "prompt": "recovered prompt",
                    "model": "gpt-image-test",
                },
            })
            slot_key = f"{app._today().isoformat()} {time_text}"
            app._failed_photo_jobs[slot_key] = {
                "theme": "noon",
                "time": time_text,
                "activity": "测试活动",
                "error": "gallery sync crashed",
            }
            app._check_photo_exists_for_slot = lambda *_args: False

            app._recover_orphaned_generated_photos()

            recovered = app._failed_photo_jobs[slot_key]
            self.assertEqual("delivery_failed", recovered.get("reason"))
            self.assertEqual(filename, recovered.get("image_filename"))
            entry = ScheduleStore(app.data_dir).load()[filename]
            self.assertTrue(entry.get("recovered_from_metadata"))
            self.assertEqual("", entry.get("caption"))
            self.assertEqual("failed", entry.get("delivery_status"))

    async def test_startup_recovery_turns_interrupted_send_into_manual_resend(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self._make_app(Path(tmpdir))
            image_path = self._seed_image(app, "sending.png", "15:12")
            ScheduleStore(app.data_dir).update(lambda data: {
                **data,
                image_path.name: {
                    **data[image_path.name],
                    "delivery_status": "sending",
                },
            })

            app._recover_orphaned_generated_photos()

            slot_key = f"{app._today().isoformat()} 15:12"
            failed = app._failed_photo_jobs[slot_key]
            self.assertEqual("delivery_failed", failed.get("reason"))
            self.assertEqual(image_path.name, failed.get("image_filename"))
            entry = ScheduleStore(app.data_dir).load()[image_path.name]
            self.assertEqual("failed", entry.get("delivery_status"))
            self.assertEqual("delivery_interrupted_by_restart", entry.get("delivery_error"))

    async def test_photo_job_counters_do_not_double_count_delivery_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config" / "config.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("gallery:\n  port: 18889\n", encoding="utf-8")
            (root / "app" / "references").mkdir(parents=True)
            server = GalleryServer(
                {"paths": {"project_root": str(root)}, "gallery": {"port": 18889}},
                str(root / "data"),
                str(config_path),
            )
            image_dir = Path(server.image_dir)
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / "sent.png").write_bytes(b"sent")
            (image_dir / "delivery-failed.png").write_bytes(b"failed")
            (image_dir / "sending.png").write_bytes(b"sending")
            today = server._today().isoformat()
            ScheduleStore(server.data_dir).save({
                "sent.png": {
                    "date": today,
                    "schedule_time": "09:24 已发送",
                    "image_filename": "sent.png",
                    "status": "ok",
                    "source": "cron",
                    "delivery_status": "sent",
                },
                "delivery-failed.png": {
                    "date": today,
                    "schedule_time": "12:36 待重发",
                    "image_filename": "delivery-failed.png",
                    "status": "ok",
                    "source": "cron",
                    "delivery_status": "failed",
                },
                "sending.png": {
                    "date": today,
                    "schedule_time": "14:12 正在发送",
                    "image_filename": "sending.png",
                    "status": "ok",
                    "source": "cron",
                    "delivery_status": "sending",
                },
            })
            server.on_list_photo_jobs = lambda: [
                {"status": "delivery_failed", "time": "12:36"},
                {"status": "sending", "time": "14:12"},
                {"status": "scheduled", "time": "15:12"},
            ]

            response = await server.handle_photo_jobs(SimpleNamespace())
            payload = json.loads(response.text)

            self.assertEqual(1, payload["completed_today"])
            self.assertEqual(1, payload["failed_today"])
            self.assertEqual(2, payload["active_today"])
            self.assertEqual(4, payload["planned_today"])


if __name__ == "__main__":
    unittest.main()
