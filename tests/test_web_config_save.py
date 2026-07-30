import json
import os
import sys
import tempfile
import unittest


APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from web_server import GalleryServer  # noqa: E402


class ConfigRequest:
    async def json(self):
        return {
            "validate_required_config": True,
            "gitee_url": "https://api.gitee.com/v1",
            "gpt_base_url": "https://images.example/v1",
            "cpa_url": "http://llm.example/v1",
            "github_proxy": "http://proxy.example",
        }


class WebConfigSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_base_urls_satisfy_required_validation(self):
        with tempfile.TemporaryDirectory() as data_dir:
            api_keys_path = os.path.join(data_dir, "api_keys_config.json")
            with open(api_keys_path, "w", encoding="utf-8") as handle:
                json.dump({"gpt_key": "gpt-secret", "cpa_key": "cpa-secret"}, handle)

            server = GalleryServer.__new__(GalleryServer)
            server.data_dir = data_dir
            server.config_path = ""
            server.image_dir = os.path.join(data_dir, "images")
            server.config = {
                "image_gen": {
                    "gpt_base_url": "https://images.example/v1",
                    "gitee_url": "https://api.gitee.com/v1",
                },
                "llm": {"base_url": "http://llm.example/v1"},
                "update": {},
            }
            server._load_plugin_config = lambda: {
                "gitee_config": {"api_keys": ["gitee-secret"]}
            }

            response = await server.handle_save_keys(ConfigRequest())

            self.assertEqual(response.status, 200, response.text)


if __name__ == "__main__":
    unittest.main()
