import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'app'))
for directory in (APP_DIR, os.path.join(APP_DIR, 'zhuzhu')):
    if directory not in sys.path:
        sys.path.insert(0, directory)

import generate_qwen as qwen
from web_server import GalleryServer
from main import PortraitGalleryApp


class QwenProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ref = Path(self.temp.name) / 'ref.png'
        self.ref.write_bytes(self.png((512, 768)))

    @staticmethod
    def png(size=(512, 768)):
        output = io.BytesIO()
        Image.new('RGB', size, 'white').save(output, format='PNG')
        return output.getvalue()

    def graph(self, **kwargs):
        return qwen.build_workflow('Make it blue', reference_images=['test/ref.png'], reference_size=(512, 768), **kwargs)

    def test_default_size_uses_reference(self):
        self.assertEqual(qwen.resolve_size('auto', (512, 768)), (512, 768))
        self.assertEqual(qwen.resolve_size('1024x1024', (512, 768)), (512, 768))
        with self.assertRaises(ValueError):
            qwen.resolve_size('auto')

    def test_large_reference_is_bounded(self):
        result = qwen.resolve_size('auto', (1536, 2048))
        self.assertEqual(result, (864, 1152))
        self.assertLessEqual(result[0] * result[1], qwen.MAX_PIXELS)

    def test_bad_sizes(self):
        for size in ['0x0', '1920', '-1x1024', '99999x512']:
            with self.subTest(size=size), self.assertRaises(ValueError):
                qwen.resolve_size(size, (512, 768))

    def test_graph_requires_reference(self):
        with self.assertRaises(ValueError):
            qwen.build_workflow('test', reference_size=(512, 768))

    def test_real_image_condition_and_latent(self):
        graph, width, height = self.graph(seed=42)
        self.assertEqual(graph['1']['class_type'], 'UnetLoaderGGUF')
        self.assertEqual(graph['1']['inputs']['unet_name'], qwen.UNET_NAME)
        self.assertEqual(graph['20'], {'class_type': 'LoadImage', 'inputs': {'image': 'test/ref.png'}})
        self.assertEqual(graph['4']['inputs']['images.image_1'], ['20', 0])
        self.assertEqual(graph['4']['inputs']['vae'], ['3', 0])
        self.assertEqual(graph['4']['inputs']['resolution'], 0)
        self.assertEqual(graph['5']['inputs']['latent_image'], ['4', 2])
        self.assertEqual(graph['5']['inputs']['seed'], 42)
        self.assertNotIn('EmptyLatentImage', [n['class_type'] for n in graph.values()])
        self.assertEqual((width, height), (512, 768))

    def test_two_references_keep_order(self):
        graph, _, _ = qwen.build_workflow('edit', reference_images=['first.png', 'second.png'], reference_size=(512, 768))
        self.assertEqual(graph['21']['inputs']['image'], 'second.png')
        self.assertEqual(graph['4']['inputs']['images.image_2'], ['21', 0])
        with self.assertRaises(ValueError):
            qwen.build_workflow('edit', reference_images=['a', 'b', 'c'], reference_size=(512, 768))

    def test_preparation_keeps_aspect_and_pixels(self):
        data, dimensions = qwen.prepare_reference(self.ref, '1024x1024')
        self.assertEqual(dimensions, (512, 768))
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size, dimensions)

    def test_no_reference_means_no_network(self):
        with patch.object(qwen, '_json') as request, patch.object(qwen, 'upload_reference') as upload:
            with self.assertRaises(ValueError):
                qwen.generate_image_bytes('edit')
        request.assert_not_called()
        upload.assert_not_called()

    def test_bad_reference_means_no_network(self):
        invalid = Path(self.temp.name) / 'invalid.png'
        invalid.write_text('not a picture')
        with patch.object(qwen, '_json') as request:
            with self.assertRaises(OSError):
                qwen.generate_image_bytes('edit', ref_image=invalid)
        request.assert_not_called()

    def test_offline_does_not_upload_or_submit(self):
        with patch.object(qwen, '_base_url', return_value='http://127.0.0.1:8188'), patch.object(qwen, '_json', side_effect=qwen.QwenError('offline')) as request, patch.object(qwen, 'upload_reference') as upload:
            with self.assertRaises(qwen.QwenError):
                qwen.generate_image_bytes('edit', ref_image=self.ref)
        self.assertEqual(request.call_count, 1)
        upload.assert_not_called()

    def test_completed_result_checks_image_and_records_references(self):
        completed = {'job': {'status': {'completed': True, 'status_str': 'success'}, 'outputs': {'7': {'images': [{'filename': 'edit.png', 'subfolder': 'test', 'type': 'output'}]}}}}
        info = {}
        with patch.object(qwen, '_base_url', return_value='http://127.0.0.1:8188'), patch.object(qwen, '_json', side_effect=[{}, {'prompt_id': 'job', 'node_errors': {}}, completed]) as request, patch.object(qwen, 'upload_reference', return_value='test/ref.png'), patch.object(qwen, '_open', return_value=io.BytesIO(self.png())):
            data, _ = qwen.generate_image_bytes('edit', ref_image=self.ref, request_info=info)
        self.assertTrue(data.startswith(b'\x89PNG'))
        self.assertEqual(info['generation_mode'], 'img2img')
        self.assertEqual(info['reference_count'], 1)
        self.assertEqual(info['upstream_references'], ['test/ref.png'])
        self.assertEqual(info['resolved_size'], '512x768')
        self.assertEqual(request.call_args_list[1].args[2]['prompt']['4']['inputs']['images.image_1'], ['20', 0])

    def test_generation_error_is_not_success_or_retry(self):
        error = {'job': {'status': {'completed': False, 'status_str': 'error', 'messages': [['execution_error', {'exception_message': 'out of memory'}]]}}}
        with patch.object(qwen, '_base_url', return_value='http://127.0.0.1:8188'), patch.object(qwen, '_json', side_effect=[{}, {'prompt_id': 'job'}, error]) as request, patch.object(qwen, 'upload_reference', return_value='test/ref.png'):
            with self.assertRaisesRegex(qwen.QwenError, 'out of memory'):
                qwen.generate_image_bytes('edit', ref_image=self.ref)
        self.assertEqual(sum(call.args[1] == '/prompt' for call in request.call_args_list), 1)

    def test_hermes_saves_img2img_metadata(self):
        metadata = {}
        def fake_generate(prompt, **kwargs):
            self.assertEqual(kwargs['ref_image'], str(self.ref))
            kwargs['request_info'].update(comfy_prompt_id='job', resolved_size='512x768', seed=42, steps=25, reference_count=1, ref_images=[str(self.ref)])
            return self.png(), 2.0
        server = GalleryServer.__new__(GalleryServer)
        server.image_dir = self.temp.name
        server._wardrobe_reference_for_value = lambda _: {}
        server._update_image_metadata_entry = lambda filename, entry: metadata.update(entry)
        with patch.object(qwen, 'generate_image_bytes', side_effect=fake_generate):
            result = server._run_hermes_image_generation('qwen', 'make it blue', ref_image=str(self.ref), seed=42)
        self.assertTrue(os.path.isfile(result['path']))
        self.assertEqual(result['generation_mode'], 'img2img')
        self.assertEqual(result['reference_count'], 1)
        self.assertEqual(metadata['model'], qwen.MODEL_NAME)
        self.assertEqual(metadata['generation_mode'], 'img2img')
        self.assertEqual(metadata['ref_image_path'], str(self.ref))
        self.assertFalse(metadata['fallback_used'])

    def test_shared_runner_rejects_missing_reference(self):
        server = GalleryServer.__new__(GalleryServer)
        with patch.object(qwen, 'generate_image_bytes') as provider:
            with self.assertRaises(ValueError):
                server._run_hermes_image_generation('qwen', 'edit')
        provider.assert_not_called()

    def test_reroll_engine_unchanged(self):
        self.assertEqual(PortraitGalleryApp._engine_from_model_name(qwen.MODEL_NAME), 'qwen')
        self.assertEqual(PortraitGalleryApp._engine_from_model_name('gpt-image-2'), 'gptimage')


class Request:
    def __init__(self, body):
        self.body = body
    async def json(self):
        return self.body


class QwenRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_route_rejects_qwen(self):
        server = GalleryServer.__new__(GalleryServer)
        with patch.object(server, '_run_hermes_image_generation') as generate:
            result = await server.handle_hermes_text_to_image(Request({'engine': 'qwen', 'prompt': 'edit'}))
        self.assertEqual(result.status, 400)
        self.assertEqual(json.loads(result.text)['error'], 'qwen_img2img_only')
        generate.assert_not_called()

    async def test_image_route_requires_reference(self):
        server = GalleryServer.__new__(GalleryServer)
        result = await server.handle_hermes_image_to_image(Request({'engine': 'qwen', 'prompt': 'edit'}))
        self.assertEqual(result.status, 400)
        self.assertEqual(json.loads(result.text)['error'], 'qwen_reference_required')

    async def test_custom_ui_route_requires_reference(self):
        server = GalleryServer.__new__(GalleryServer)
        server.on_generate_custom = lambda *args: None
        result = await server.handle_generate_custom(Request({'model': qwen.MODEL_NAME, 'prompt': 'edit', 'source': 'custom_ui'}))
        self.assertEqual(result.status, 400)
        self.assertEqual(json.loads(result.text)['error'], 'qwen_reference_required')


if __name__ == '__main__':
    unittest.main()
