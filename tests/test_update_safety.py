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
            current_head = subprocess.CompletedProcess([], 0, "a" * 40 + "\n", "")

            with (
                patch.object(
                    server,
                    "_git_run",
                    side_effect=[completed, completed, current_head],
                ) as git_run,
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
            self.assertEqual(3, git_run.call_count)
            self.assertFalse(any(call.args[0][0] == "checkout" for call in git_run.call_args_list))

    async def test_merge_base_execution_error_is_not_reported_as_conflict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self.make_server(Path(tmpdir))
            completed = subprocess.CompletedProcess([], 0, "", "")
            fatal = subprocess.CompletedProcess([], 128, "", "fatal: bad revision")

            with patch.object(server, "_git_run", side_effect=[completed, fatal]):
                payload, status = await server._perform_safe_update(
                    dry_run=False,
                    restart=False,
                )

            self.assertEqual(500, status)
            self.assertEqual("update_git_check_failed", payload.get("error"))

    async def test_safe_update_moves_head_and_can_run_twice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            source = base / "source"
            target = base / "target"
            (source / "app" / "references").mkdir(parents=True)
            (source / "app" / "references" / ".keep").write_text("", encoding="utf-8")
            (source / "config").mkdir()
            (source / "app" / "main.py").write_text("VERSION = 1\n", encoding="utf-8")
            (source / "README.md").write_text("release one\n", encoding="utf-8")
            (source / "config" / "config.yaml").write_text("gallery: {}\n", encoding="utf-8")

            def git(cwd: Path, *args: str) -> str:
                result = subprocess.run(
                    ["git", *args],
                    cwd=str(cwd),
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return result.stdout.strip()

            git(source, "init", "-b", "main")
            git(source, "config", "user.email", "tests@example.com")
            git(source, "config", "user.name", "Tests")
            git(source, "add", ".")
            git(source, "commit", "-m", "base")
            subprocess.run(
                ["git", "clone", str(source), str(target)],
                check=True,
                capture_output=True,
                text=True,
            )

            (source / "app" / "main.py").write_text("VERSION = 2\n", encoding="utf-8")
            git(source, "add", "app/main.py")
            git(source, "commit", "-m", "second")

            server = GalleryServer(
                {"paths": {"project_root": str(target)}, "gallery": {}},
                str(target / "data"),
                str(target / "config" / "config.yaml"),
            )
            first_payload, first_status = await server._perform_safe_update(
                dry_run=False,
                restart=False,
            )

            self.assertEqual(200, first_status, first_payload)
            self.assertEqual(git(source, "rev-parse", "HEAD"), git(target, "rev-parse", "HEAD"))
            self.assertEqual("VERSION = 2\n", (target / "app" / "main.py").read_text(encoding="utf-8"))

            (source / "README.md").write_text("release three\n", encoding="utf-8")
            git(source, "add", "README.md")
            git(source, "commit", "-m", "third")
            second_payload, second_status = await server._perform_safe_update(
                dry_run=False,
                restart=False,
            )

            self.assertEqual(200, second_status, second_payload)
            self.assertEqual(git(source, "rev-parse", "HEAD"), git(target, "rev-parse", "HEAD"))
            self.assertEqual("release three\n", (target / "README.md").read_text(encoding="utf-8"))

    async def test_safe_update_preserves_unrelated_staged_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            source = base / "source"
            target = base / "target"
            (source / "app" / "references").mkdir(parents=True)
            (source / "app" / "references" / ".keep").write_text("", encoding="utf-8")
            (source / "config").mkdir()
            (source / "app" / "main.py").write_text("VERSION = 1\n", encoding="utf-8")
            (source / "README.md").write_text("release one\n", encoding="utf-8")
            (source / "config" / "config.yaml").write_text("gallery: {}\n", encoding="utf-8")

            def git(cwd: Path, *args: str) -> str:
                result = subprocess.run(
                    ["git", *args],
                    cwd=str(cwd),
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return result.stdout.strip()

            git(source, "init", "-b", "main")
            git(source, "config", "user.email", "tests@example.com")
            git(source, "config", "user.name", "Tests")
            git(source, "add", ".")
            git(source, "commit", "-m", "base")
            subprocess.run(
                ["git", "clone", str(source), str(target)],
                check=True,
                capture_output=True,
                text=True,
            )

            (source / "app" / "main.py").write_text("VERSION = 2\n", encoding="utf-8")
            git(source, "add", "app/main.py")
            git(source, "commit", "-m", "remote update")
            (target / "README.md").write_text("locally staged notes\n", encoding="utf-8")
            git(target, "add", "README.md")

            server = GalleryServer(
                {"paths": {"project_root": str(target)}, "gallery": {}},
                str(target / "data"),
                str(target / "config" / "config.yaml"),
            )
            payload, status = await server._perform_safe_update(
                dry_run=False,
                restart=False,
            )

            self.assertEqual(200, status, payload)
            self.assertEqual("README.md", git(target, "diff", "--cached", "--name-only"))
            self.assertEqual("", git(target, "diff", "--name-only", "--", "README.md"))
            self.assertEqual("VERSION = 2\n", (target / "app" / "main.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
