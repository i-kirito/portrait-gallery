"""History deletion integration tests. All files are in temporary galleries."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import image_version_delete as deletion
from image_versions import archive_image_version, image_version_path
from store import ScheduleStore, ImageMetadataStore
from web_server import GalleryServer


class HistoryDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = self.root / 'config/config.yaml'
        config.parent.mkdir(parents=True)
        config.write_text('gallery:\n  port: 18889\n')
        (self.root / 'app/references').mkdir(parents=True)
        self.server = GalleryServer({'paths': {'project_root': str(self.root)}, 'gallery': {'port': 18889}}, str(self.root/'data'), str(config))
        self.current = Path(self.server.image_dir) / 'current.png'
        self.other = Path(self.server.image_dir) / 'other.png'
        Image.new('RGB', (48,64), 'white').save(self.current)
        Image.new('RGB', (40,50), 'gray').save(self.other)
        self.records = []
        for color in ('blue', 'green'):
            source = self.root / f'{color}.png'
            Image.new('RGB', (36,54), color).save(source)
            self.records.append(archive_image_version(self.server.data_dir, str(source), original_image_filename='current.png', target='version_switch'))
        self.paths = [image_version_path(self.server.data_dir, r) for r in self.records]
        self.initial_current = self.current.read_bytes()
        self.initial_other = self.other.read_bytes()
        self.initial_archives = [p.read_bytes() for p in self.paths]
        self.store = ScheduleStore(self.server.data_dir)
        self.store.save({'card': {'id':'current.png','image_filename':'current.png','image_path':'/images/current.png','date':'2026-09-23','time':'12:43','status':'ok','favorite':True,'image_versions':self.records,'edit_history':[{'target':'custom'},{'target':'custom'}]}, 'other-card': {'id':'other.png','image_filename':'other.png','image_path':'/images/other.png','status':'ok'}})
        ImageMetadataStore(self.server.data_dir).save({'current.png':{'model':'gpt-image-2'}})
        self.client = TestClient(TestServer(self.server.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def route(self, index=0, filename='current.png'):
        return f'/api/images/{filename}/versions/{self.records[index]["id"]}'

    def assert_preserved(self):
        self.assertEqual(self.current.read_bytes(), self.initial_current)
        self.assertEqual(self.other.read_bytes(), self.initial_other)
        self.assertTrue(self.store.load()['card']['favorite'])
        self.assertEqual(ImageMetadataStore(self.server.data_dir).load(), {'current.png':{'model':'gpt-image-2'}})

    async def test_delete_one_keeps_current_sibling_and_card(self):
        response = await self.client.delete(self.route())
        data = await response.json()
        self.assertEqual(response.status, 200, data)
        self.assertTrue(data['success'])
        self.assertEqual(data['deleted_version_id'], self.records[0]['id'])
        self.assertEqual(data['version_count'], 1)
        self.assertEqual(data['deleted_count'], 1)
        self.assertEqual(data['unavailable_count'], 0)
        self.assertFalse(self.paths[0].exists())
        self.assertEqual(self.paths[1].read_bytes(), self.initial_archives[1])
        self.assertEqual(len(self.store.load()), 2)
        self.assertEqual((await self.client.get(self.route())).status, 404)
        self.assert_preserved()

    async def test_delete_last_shows_empty_without_false_legacy_warning(self):
        self.assertEqual((await self.client.delete(self.route(0))).status, 200)
        response = await self.client.delete(self.route(1))
        data = await response.json()
        self.assertEqual((data['version_count'], data['unavailable_count'], data['deleted_count']), (0,0,2))
        self.assertEqual(data['items'], [])
        detail = await (await self.client.get('/api/images/current.png')).json()
        self.assertFalse(detail['has_image_history'])
        self.assert_preserved()

    async def test_duplicate_delete_is_safe(self):
        await self.client.delete(self.route())
        response = await self.client.delete(self.route())
        self.assertEqual(response.status, 404)
        self.assertEqual(self.store.load()['card']['deleted_version_count'], 1)
        self.assert_preserved()

    async def test_other_images_version_is_rejected(self):
        response = await self.client.delete(self.route(filename='other.png'))
        self.assertEqual(response.status, 404)
        self.assertEqual(self.paths[0].read_bytes(), self.initial_archives[0])
        self.assert_preserved()

    async def test_invalid_version_id_is_rejected(self):
        response = await self.client.delete('/api/images/current.png/versions/not-an-id')
        self.assertEqual(response.status, 400)
        self.assert_preserved()

    async def test_busy_image_is_not_deleted(self):
        lock = self.server._reserve_image_mutation_lock('current.png')
        await lock.acquire()
        try:
            response = await self.client.delete(self.route())
            self.assertEqual(response.status, 409)
            self.assertEqual((await response.json())['error'], 'image_busy')
        finally:
            lock.release()
            self.server._release_image_mutation_lock('current.png', lock)
        self.assertTrue(self.paths[0].exists())
        self.assert_preserved()

    async def test_store_write_failure_restores_archive(self):
        before = self.store.load()
        def fail_write(store, callback):
            callback(store.load())
            raise OSError('simulated disk failure')
        with patch.object(ScheduleStore, 'update', fail_write):
            response = await self.client.delete(self.route())
        self.assertEqual(response.status, 500)
        self.assertEqual(self.store.load(), before)
        self.assertEqual(self.paths[0].read_bytes(), self.initial_archives[0])
        self.assertEqual(list(self.paths[0].parent.glob('.deleting-*')), [])
        self.assert_preserved()

    async def test_permission_failure_does_not_drop_record(self):
        with patch.object(deletion.os, 'replace', side_effect=PermissionError('simulated denied')):
            response = await self.client.delete(self.route())
        self.assertEqual(response.status, 500)
        self.assertEqual(len(self.store.load()['card']['image_versions']), 2)
        self.assertTrue(self.paths[0].exists())
        self.assert_preserved()

    async def test_symlink_to_current_is_rejected(self):
        self.paths[0].unlink()
        self.paths[0].symlink_to(self.current)
        response = await self.client.delete(self.route())
        self.assertEqual(response.status, 404)
        self.assert_preserved()

    async def test_shared_archive_is_protected(self):
        def share(data):
            data['other-card']['image_versions'] = [self.records[0]]
            return data
        self.store.update(share)
        response = await self.client.delete(self.route())
        self.assertEqual(response.status, 409)
        self.assertTrue(self.paths[0].exists())
        self.assert_preserved()

    async def test_symlink_to_another_archive_is_rejected(self):
        self.paths[0].unlink()
        self.paths[0].symlink_to(self.paths[1])
        response = await self.client.delete(self.route())
        self.assertEqual(response.status, 409)
        self.assertEqual(self.paths[1].read_bytes(), self.initial_archives[1])
        self.assert_preserved()

    async def test_unauthenticated_lan_host_is_rejected(self):
        response = await self.client.delete(self.route(), headers={'Host':'192.168.31.216:18889'})
        self.assertEqual(response.status, 401)
        self.assert_preserved()

    async def test_deletion_does_not_break_remaining_version_activation(self):
        await self.client.delete(self.route(0))
        response = await self.client.post(self.route(1) + '/activate')
        self.assertEqual(response.status, 200)
        self.assertEqual(self.current.read_bytes(), self.initial_archives[1])
        new_record = self.store.load()['card']['image_versions'][0]
        self.assertEqual(image_version_path(self.server.data_dir, new_record).read_bytes(), self.initial_current)


if __name__ == '__main__':
    unittest.main()
