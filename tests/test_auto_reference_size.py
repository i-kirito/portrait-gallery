"""Automatic output sizes must come from the source image, not its thumbnail."""
import io
import base64
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / 'app', ROOT / 'app/zhuzhu'):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from image_editing import reference_image_dimensions, resolve_reference_output_size
import generate_gptimage as gpt
from web_server import GalleryServer


class ReferenceSizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original = Path(self.temp.name) / 'original.png'
        self.secondary = Path(self.temp.name) / 'secondary.png'
        Image.new('RGB', (1536, 2048), 'white').save(self.original)
        Image.new('RGB', (512, 512), 'white').save(self.secondary)

    def test_auto_uses_original_not_thumbnail(self):
        for value in ('', 'auto', '自动', None):
            self.assertEqual(resolve_reference_output_size(value, str(self.original)), '1536x2048')

    def test_text_to_image_remains_automatic(self):
        self.assertEqual(resolve_reference_output_size('auto', None), '')

    def test_explicit_dimensions_win_without_reading_reference(self):
        with patch('image_editing.reference_image_dimensions') as read:
            self.assertEqual(resolve_reference_output_size('768x1024', 'missing.png'), '768x1024')
        read.assert_not_called()

    def test_odd_dimensions_are_not_silently_rounded(self):
        path = Path(self.temp.name) / 'odd.png'
        Image.new('RGB', (777, 1111), 'white').save(path)
        self.assertEqual(resolve_reference_output_size('auto', str(path)), '777x1111')

    def test_exif_rotation_uses_displayed_dimensions(self):
        path = Path(self.temp.name) / 'rotated.jpg'
        exif = Image.Exif()
        exif[274] = 6
        Image.new('RGB', (1600, 1200), 'white').save(path, exif=exif)
        self.assertEqual(reference_image_dimensions(str(path)), (1200, 1600))
        with Image.open(io.BytesIO(gpt._image_bytes_for_edit(str(path), max_size=2000))) as upload:
            self.assertEqual(upload.size, (1200, 1600))
        with Image.open(path) as original:
            self.assertEqual(original.size, (1600, 1200))

    def test_invalid_reference_never_falls_back_to_square(self):
        with self.assertRaises(ValueError):
            resolve_reference_output_size('auto', str(Path(self.temp.name) / 'missing.png'))

    def test_gpt_transport_receives_original_size_for_multi_reference(self):
        with patch.object(gpt, '_direct_gpt_image_endpoints', return_value=[{'base_url': 'http://upstream.test/v1', 'api_key': ''}]), patch.object(gpt, 'GPTIMAGE_DIRECT_MODEL', 'gpt-image-2'), patch.object(gpt, '_compact_request_prompt', side_effect=lambda text: text), patch.object(gpt, '_generate_via_images_api', return_value=(b'example', 1.0)) as transport:
            gpt._generate_via_direct_gpt('Change the background color.', str(self.original), 'auto', ref_images=[str(self.original), str(self.secondary)])
        self.assertEqual(transport.call_args.args[2], '1536x2048')

    def test_hermes_runner_records_actual_requested_dimensions(self):
        output = io.BytesIO()
        Image.new('RGB', (1536, 2048), 'white').save(output, format='PNG')
        captured = {}
        server = GalleryServer.__new__(GalleryServer)
        server.image_dir = self.temp.name
        server._wardrobe_reference_for_value = lambda _: {}
        server._update_image_metadata_entry = lambda filename, data: captured.update(data)
        with patch.object(gpt, '_generate_via_direct_gpt', return_value=(output.getvalue(), 1.0)) as transport:
            result = server._run_hermes_image_generation('gptimage', 'Change the background color.', ref_image=str(self.original), size='auto', classify_style=False)
        self.assertEqual(transport.call_args.kwargs['size'], '1536x2048')
        self.assertEqual(captured['requested_size'], '1536x2048')
        self.assertEqual((result['width'], result['height']), (1536, 2048))

    def test_outgoing_multipart_size_is_not_compressed_input_size(self):
        response = SimpleNamespace(status_code=200, text='', json=lambda: {'data': [{'b64_json': base64.b64encode(self.original.read_bytes()).decode()}]})
        with patch.object(gpt, '_direct_gpt_image_endpoints', return_value=[{'base_url': 'http://upstream.test/v1', 'api_key': ''}]), patch.object(gpt, 'GPTIMAGE_DIRECT_MODEL', 'gpt-image-2'), patch.object(gpt, '_compact_request_prompt', side_effect=lambda text: text), patch.object(gpt.REQUEST_SESSION, 'post', return_value=response) as post:
            result = gpt._generate_via_direct_gpt('Change the background color.', str(self.original), 'auto')
        self.assertIsNotNone(result)
        self.assertEqual(post.call_args.kwargs['data']['size'], '1536x2048')
        with Image.open(io.BytesIO(post.call_args.kwargs['files'][0][1][1])) as uploaded:
            self.assertLessEqual(max(uploaded.size), gpt.IMG2IMG_MAX_SIZE)


class ReferenceSizeRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original = Path(self.temp.name) / 'source.png'
        self.secondary = Path(self.temp.name) / 'face.png'
        Image.new('RGB', (1536, 2048), 'white').save(self.original)
        Image.new('RGB', (512, 512), 'white').save(self.secondary)
        self.server = GalleryServer.__new__(GalleryServer)
        self.server.on_generate_custom = AsyncMock(return_value=SimpleNamespace(status='ok', to_dict=lambda: {'status': 'ok'}))
        refs = {'/local-refs/source.png': str(self.original), '/local-refs/face.png': str(self.secondary)}
        self.server._resolve_reference_image = lambda value, **kwargs: refs.get(value, '')
        self.server._xiaohongshu_reference_filenames_for_paths = lambda paths: []
        self.server._is_xiaohongshu_reference = lambda *args: False
        self.server._reference_profile_for_value = lambda value: {}
        self.server._wardrobe_reference_for_value = lambda value: {}
        self.server._xiaohongshu_reference_for_value = lambda value: {}
        self.server._select_default_custom_reference_sync = lambda: {}
        self.server._normalize_entry_display = lambda entry, metadata: entry
        self.server._load_image_metadata = lambda: {}
        app = web.Application()
        app.router.add_get('/api/reference-size', self.server.handle_reference_size)
        app.router.add_post('/api/generate-custom', self.server.handle_generate_custom)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_preview_endpoint_reports_dimensions(self):
        response = await self.client.get('/api/reference-size', params={'ref': '/local-refs/source.png'})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['size'], '1536x2048')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    async def test_preview_does_not_expose_arbitrary_paths(self):
        response = await self.client.get('/api/reference-size', params={'ref': '/etc/passwd'})
        self.assertEqual(response.status, 400)

    async def submit(self, **kwargs):
        body = {'prompt': 'Change the background color.', 'pure': True, 'source': 'custom_ui', 'size_mode': 'auto', 'size': 'auto', **kwargs}
        response = await self.client.post('/api/generate-custom', json=body)
        self.assertEqual(response.status, 200, await response.text())
        return self.server.on_generate_custom.call_args.args[1]

    async def test_auto_custom_request_resolves_first_source(self):
        size = await self.submit(ref_image='/local-refs/source.png', ref_images=['/local-refs/source.png', '/local-refs/face.png'])
        self.assertEqual(size, '1536x2048')

    async def test_auto_ignores_stale_manual_dimensions(self):
        size = await self.submit(ref_image='/local-refs/source.png', size='1024x1024', aspect='1:1', resolution='1k')
        self.assertEqual(size, '1536x2048')

    async def test_manual_size_is_unchanged(self):
        size = await self.submit(ref_image='/local-refs/source.png', size_mode='custom', size='768x1024')
        self.assertEqual(size, '768x1024')

    async def test_no_reference_stays_automatic(self):
        self.assertEqual(await self.submit(), '')


if __name__ == '__main__':
    unittest.main()
