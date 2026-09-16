import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_image_url_config as fixtures
from aiohttp.test_utils import TestClient, TestServer
from zhuzhu import core as zhuzhu_core


class ModelSelectionTest(unittest.IsolatedAsyncioTestCase):
    _make_server = fixtures.ImageUrlConfigTest._make_server

    async def test_models_roundtrip_and_generator_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = self._make_server(root)
            server.config['image_gen']['gpt_model'] = 'gpt-image-2'
            keys = root / 'data' / 'api_keys_config.json'
            async with TestClient(TestServer(server.app)) as client:
                for model in ('gpt-image-2.5-sunburst', 'gpt-image-2.5-flare', 'gpt-image-2'):
                    response = await client.post('/api/config/keys', json={'gpt_model': model})
                    self.assertEqual(response.status, 200, await response.text())
                    response = await client.get('/api/config/keys')
                    self.assertEqual((await response.json())['gpt_model'], model)
                    with patch.object(zhuzhu_core, '_API_KEYS_CONFIG_PATH', str(keys)), patch.dict(os.environ, {'GPT_IMAGE_MODEL': ''}):
                        self.assertEqual(zhuzhu_core.get_image_model('gpt_model'), model)
                        with patch.dict(os.environ, {'GPT_IMAGE_MODEL': 'single-request-model'}):
                            self.assertEqual(zhuzhu_core.get_image_model('gpt_model'), 'single-request-model')
                before = keys.read_text()
                response = await client.post('/api/config/keys', json={'gpt_model': 'bad model!'})
                self.assertEqual(response.status, 400)
                self.assertEqual(keys.read_text(), before)
                response = await client.post('/api/config/keys', json={'gpt_model': ''})
                self.assertEqual(response.status, 200)
                response = await client.get('/api/config/keys')
                self.assertEqual((await response.json())['gpt_model'], 'gpt-image-2')


if __name__ == '__main__':
    unittest.main()
