"""Regression checks for files required by the published runtime image."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_runtime_image_contains_chrome_extension_package(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"(?m)^COPY\s+extensions/\s+\./extensions/\s*$")

    def test_release_script_updates_static_ui_version_labels(self):
        with tempfile.TemporaryDirectory(prefix="portrait-release-test-") as temp:
            worktree = Path(temp)
            for relative in ("README.md", "AGENTS.md", "SKILL.md", "scripts/release.sh"):
                target = worktree / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            index = worktree / "app/web/index.html"
            index.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "app/web/index.html", index)
            (worktree / "VERSION").write_text("1.4.7\n", encoding="utf-8")

            env = os.environ.copy()
            env.update({"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                       "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})
            subprocess.run(["git", "init", "-b", "main"], cwd=worktree, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "add", "."], cwd=worktree, check=True,
                           stdout=subprocess.DEVNULL)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=worktree, check=True,
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(
                [str(worktree / "scripts/release.sh"), "9.9.9", "--no-commit", "--skip-tests",
                 "--skip-docker", "--skip-github", "--skip-push", "--yes", "--notes", "- test"],
                cwd=worktree, check=True, env=env, stdout=subprocess.DEVNULL,
            )
            html = index.read_text(encoding="utf-8")
            self.assertIn('id="versionTag">v9.9.9</span>', html)
            self.assertIn('id="settingsVersion">v9.9.9</strong>', html)

    def test_release_commit_includes_static_ui_version_file(self):
        release = (ROOT / "scripts/release.sh").read_text(encoding="utf-8")
        self.assertRegex(release, r"git add .*app/web/index\.html")


if __name__ == "__main__":
    unittest.main()
