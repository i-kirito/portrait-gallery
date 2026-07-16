import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from reference_profiles import load_reference_profiles, upsert_reference_profile  # noqa: E402


class ReferenceProfileStoreTest(unittest.TestCase):
    @staticmethod
    def _dirs(root: Path) -> tuple[Path, Path, Path]:
        reference_dir = root / "references"
        app_reference_dir = root / "app-references"
        uploaded_dir = reference_dir / "uploads"
        uploaded_dir.mkdir(parents=True)
        app_reference_dir.mkdir(parents=True)
        return reference_dir, app_reference_dir, uploaded_dir

    def test_legacy_top_level_list_is_preserved_and_migrated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reference_dir, app_reference_dir, uploaded_dir = self._dirs(root)
            path = root / "reference_profiles.json"
            path.write_text(
                json.dumps([{
                    "id": "legacy-one",
                    "filename": "legacy.jpg",
                    "label": "旧参考图",
                    "source": "upload",
                }], ensure_ascii=False),
                encoding="utf-8",
            )

            loaded = load_reference_profiles(
                str(root),
                str(reference_dir),
                str(app_reference_dir),
                str(uploaded_dir),
            )
            upsert_reference_profile(str(root), {
                "id": "new-one",
                "filename": "new.jpg",
                "label": "新参考图",
                "source": "upload",
            })
            persisted = json.loads(path.read_text(encoding="utf-8"))

            self.assertIn("legacy-one", {item.get("id") for item in loaded})
            self.assertIsInstance(persisted, dict)
            self.assertEqual(
                {"legacy-one", "new-one"},
                {item.get("id") for item in persisted.get("items", [])},
            )

    def test_concurrent_upserts_do_not_drop_profiles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            def save(index: int):
                return upsert_reference_profile(str(root), {
                    "id": f"profile-{index}",
                    "filename": f"profile-{index}.jpg",
                    "label": f"参考图 {index}",
                    "source": "upload",
                })

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(save, range(20)))

            persisted = json.loads(
                (root / "reference_profiles.json").read_text(encoding="utf-8")
            )

            self.assertEqual(20, len(persisted.get("items", [])))


if __name__ == "__main__":
    unittest.main()
