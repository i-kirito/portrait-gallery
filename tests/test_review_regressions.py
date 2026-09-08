import asyncio
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from store import LockedJsonDictStore, ScheduleStore
from web_server import GalleryServer


class StorageFailures(unittest.TestCase):
    def test_failed_reads_never_overwrite_existing_data(self):
        for cls in (LockedJsonDictStore, ScheduleStore):
            for error in (OSError("read failed"), json.JSONDecodeError("bad", "", 0)):
                with self.subTest(store=cls.__name__, error=type(error).__name__), tempfile.TemporaryDirectory() as folder:
                    path = Path(folder) / "schedule_data.json"
                    original = '{"existing": {"favorite": true}}'
                    path.write_text(original)
                    store = cls(folder) if cls is ScheduleStore else cls(str(path))
                    callback = Mock(return_value={"new": {}})
                    with patch("store.json.load", side_effect=error):
                        with self.assertRaises(type(error)):
                            store.update(callback)
                    callback.assert_not_called()
                    self.assertEqual(original, path.read_text())


class LoginLimits(unittest.IsolatedAsyncioTestCase):
    async def test_attempt_limit_expires_and_hash_runs_off_loop(self):
        server = GalleryServer.__new__(GalleryServer)
        server._gallery_password_configured = Mock(return_value=True)
        server._read_json_body = AsyncMock(return_value={"password": "incorrect"})
        loop_thread = threading.get_ident()
        worker_threads = []
        def verify(password):
            worker_threads.append(threading.get_ident())
            return False
        server._verify_gallery_password = verify
        for _ in range(10):
            response = await server.handle_auth_login(Mock())
            self.assertEqual(401, response.status)
        response = await server.handle_auth_login(Mock())
        self.assertEqual(429, response.status)
        self.assertEqual(10, len(worker_threads))
        self.assertTrue(all(t != loop_thread for t in worker_threads))
        server._login_attempts = [t - 61 for t in server._login_attempts]
        self.assertEqual(401, (await server.handle_auth_login(Mock())).status)


if __name__ == "__main__":
    unittest.main()
