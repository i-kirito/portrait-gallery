import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
ZHUZHU_DIR = APP_DIR / "zhuzhu"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(ZHUZHU_DIR))

from zhuzhu import generate_gitee  # noqa: E402


class GiteeTlsTest(unittest.TestCase):
    def test_gitee_request_keeps_certificate_verification_enabled(self):
        response = SimpleNamespace(status_code=400, text="bad request")
        with (
            patch.object(generate_gitee, "ENGINE_URL", "https://example.test/images/generations"),
            patch.object(generate_gitee, "MODEL_NAME", "test-model"),
            patch.object(generate_gitee, "get_gitee_key", return_value="secret"),
            patch.object(generate_gitee.REQUEST_SESSION, "post", return_value=response) as post,
        ):
            self.assertIsNone(generate_gitee.generate_image_bytes("portrait"))

        self.assertNotEqual(False, post.call_args.kwargs.get("verify", True))


if __name__ == "__main__":
    unittest.main()
