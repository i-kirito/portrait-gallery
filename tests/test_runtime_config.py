import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import settings as settings_module  # noqa: E402
from settings import load_config, runtime_config_path, service_today  # noqa: E402


class FixedUtcDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        current = cls(2026, 7, 15, 16, 30, tzinfo=timezone.utc)
        return current if tz is None else current.astimezone(tz)


class RuntimeConfigTest(unittest.TestCase):
    def test_runtime_overrides_load_with_read_only_base_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_dir = root / "config"
            data_dir = root / "data"
            config_dir.mkdir()
            data_dir.mkdir()
            config_path = config_dir / "config.yaml"
            config_path.write_text(
                "paths:\n  data_dir: data\nllm:\n  model: base-model\n",
                encoding="utf-8",
            )
            Path(runtime_config_path(str(data_dir))).write_text(
                json.dumps({
                    "llm": {"model": "runtime-model", "models": ["runtime-model"], "stream": True},
                    "integrations": {"hermes_cli": "/runtime/hermes"},
                    "gallery": {"allowed_image_roots": ["/"]},
                    "paths": {"image_dir": "/tmp/untrusted"},
                }),
                encoding="utf-8",
            )
            os.chmod(config_path, 0o444)

            loaded = load_config(str(config_path))

            self.assertEqual("runtime-model", loaded["llm"]["model"])
            self.assertEqual(["runtime-model"], loaded["llm"]["models"])
            self.assertTrue(loaded["llm"]["stream"])
            self.assertEqual("/runtime/hermes", loaded["integrations"]["hermes_cli"])
            self.assertNotIn("allowed_image_roots", loaded.get("gallery", {}))
            self.assertNotEqual("/tmp/untrusted", loaded.get("paths", {}).get("image_dir"))

    def test_service_date_uses_configured_timezone(self):
        with patch.object(settings_module, "datetime", FixedUtcDateTime):
            shanghai = service_today({"config": {"timezone": "Asia/Shanghai"}})
            utc = service_today({"config": {"timezone": "UTC"}})

        self.assertEqual("2026-07-16", shanghai.isoformat())
        self.assertEqual("2026-07-15", utc.isoformat())


if __name__ == "__main__":
    unittest.main()
