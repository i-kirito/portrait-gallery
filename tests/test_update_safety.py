import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from web_server import GalleryServer  # noqa: E402


class SafeUpdateTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_server(root: Path) -> GalleryServer:
        config_path = root / "config" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("gallery: {}\n", encoding="utf-8")
        (root / "app" / "references").mkdir(parents=True)
        (root / ".git").mkdir()
        return GalleryServer(
            {"paths": {"project_root": str(root)}, "gallery": {}},
            str(root / "data"),
            str(config_path),
        )

    async def test_update_stops_before_checkout_when_local_file_conflicts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self.make_server(root)
            plan = {
                "all_changed_files": ["app/main.py", "README.md"],
                "updated_files": ["app/main.py", "README.md"],
                "checkout_files": ["app/main.py", "README.md"],
                "deleted_files": [],
                "skipped_files": [],
                "safe_update": True,
                "protection": server._update_protection_summary(),
            }
            completed = subprocess.CompletedProcess([], 0, "", "")

            with (
                patch.object(server, "_git_run", return_value=completed) as git_run,
                patch.object(server, "_safe_update_plan", return_value=plan),
                patch.object(
                    server,
                    "_local_update_changed_files",
                    return_value=["app/main.py", "tests/local_test.py"],
                ),
            ):
                payload, status = await server._perform_safe_update(
                    dry_run=False,
                    restart=False,
                )

            self.assertEqual(409, status)
            self.assertEqual("local_changes_conflict", payload.get("error"))
            self.assertEqual(["app/main.py"], payload.get("conflicting_files"))
            self.assertFalse(payload.get("safe_update"))
            self.assertEqual(1, git_run.call_count)


if __name__ == "__main__":
    unittest.main()
