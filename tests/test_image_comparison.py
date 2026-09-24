"""Qwen before/after grouping must never destroy or mis-associate images."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from PIL import Image

APP = Path(__file__).resolve().parents[1] / 'app'
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
from image_comparison import group_qwen_edits
from store import ScheduleStore, ImageMetadataStore
from web_server import GalleryServer


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.entries = []
        self.meta = {}
        for name, model, timestamp in [('original.png','gpt-image-2',100), ('edited.png','qwen-image-2.1-Q8_0',200), ('other.png','gpt-image-2',300)]:
            Image.new('RGB',(24,32),'white').save(self.base/name)
            self.entries.append({'image_filename':name,'image_path':'/images/'+name,'date':'2026-09-23','time':'12:43','status':'ok','model_name':model,'source':'custom','favorite':False})
            self.meta[name] = {'model':model,'created_at':timestamp,'generation_mode':'img2img','width':24,'height':32}
        self.meta['edited.png']['ref_images'] = [str(self.base/'original.png')]

    def group(self):
        return group_qwen_edits(self.entries,self.meta,str(self.base))

    def test_original_card_has_two_layers_and_edited_card_is_hidden(self):
        result=self.group()
        self.assertEqual([x['image_filename'] for x in result],['original.png','other.png'])
        c=result[0]['image_comparison']
        self.assertEqual(c['before']['filename'],'original.png')
        self.assertEqual(c['after']['filename'],'edited.png')
        self.assertEqual(c['after']['model_name'],'Qwen-Image-2.1 Q8')
        self.assertEqual(result[0]['time'],'12:43')
        self.assertEqual(result[0]['image_path'],'/images/original.png')

    def test_never_mutates_input_or_files(self):
        entries,meta=copy.deepcopy(self.entries),copy.deepcopy(self.meta)
        before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.base.iterdir()}
        self.group()
        self.assertEqual(entries,self.entries);self.assertEqual(meta,self.meta)
        self.assertEqual(before,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.base.iterdir()})

    def test_second_reference_does_not_control_grouping(self):
        self.meta['edited.png']['ref_images']=['/local-refs/face.png','/images/original.png']
        self.assertEqual(len(self.group()),3)

    def test_missing_source_or_after_never_hides_an_image(self):
        (self.base/'original.png').unlink()
        self.assertEqual(len(self.group()),3)
        Image.new('RGB',(24,32)).save(self.base/'original.png')
        (self.base/'edited.png').unlink()
        self.assertEqual(len(self.group()),3)

    def test_missing_parent_entry_preserves_orphan(self):
        self.entries=self.entries[1:]
        self.assertEqual(len(self.group()),2)

    def test_non_qwen_is_not_grouped(self):
        self.meta['edited.png']['model']='gpt-image-2'
        self.assertEqual(len(self.group()),3)

    def test_unsafe_or_external_reference_does_not_group(self):
        for value in ['https://other.test/images/original.png','/etc/original.png','/local-refs/original.png','/images/../original.png','../original.png']:
            with self.subTest(value=value):
                self.meta['edited.png']['ref_images']=[value]
                self.assertEqual(len(self.group()),3)

    def test_url_and_legacy_basename_reference_work(self):
        for value in ['/images/original.png?v=123','original.png']:
            self.meta['edited.png']['ref_images']=[value]
            self.assertEqual(len(self.group()),2)
        self.meta['edited.png'].pop('ref_images')
        self.meta['edited.png']['ref_image_path']='original.png'
        self.assertEqual(len(self.group()),2)

    def test_chain_is_one_card_and_all_edit_versions_are_kept(self):
        self.entries[2]['model_name']='qwen-image-2.1-Q8_0'
        self.meta['other.png'].update(model='qwen-image-2.1-Q8_0',ref_images=['/images/edited.png'])
        result=self.group()
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['image_comparison']['edit_count'],2)
        self.assertEqual(result[0]['image_comparison']['after']['filename'],'other.png')

    def test_cycle_does_not_hide_entries(self):
        self.meta['original.png'].update(model='qwen-image-2.1-Q8_0',ref_images=['/images/edited.png'])
        self.assertEqual(len(self.group()),3)


class ComparisonApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        config=self.root/'config/config.yaml';config.parent.mkdir();config.write_text('gallery:\n  port: 18889\n')
        (self.root/'app/references').mkdir(parents=True)
        self.server=GalleryServer({'paths':{'project_root':str(self.root)},'gallery':{'port':18889}},str(self.root/'data'),str(config))
        self.image_dir=Path(self.server.image_dir)
        entries={};meta={}
        for i,name in enumerate(['original.png','edited.png']):
            Image.new('RGB',(24,32),'white').save(self.image_dir/name)
            entries[name]={'image_filename':name,'date':'2026-09-23','time':'12:43','status':'ok','source':'custom'}
            meta[name]={'model':'qwen-image-2.1-Q8_0' if i else 'gpt-image-2','generation_mode':'img2img','created_at':100+i,'width':24,'height':32}
        meta['edited.png']['ref_images']=[str(self.image_dir/'original.png')]
        ScheduleStore(self.server.data_dir).save(entries)
        ImageMetadataStore(self.server.data_dir).save(meta)

    async def test_grouping_precedes_pagination_and_detail_keeps_comparison(self):
        response=await self.server.handle_gallery(SimpleNamespace(query={'limit':'1'}))
        page=json.loads(response.text)
        self.assertEqual(page['total_all'],1)
        self.assertFalse(page['has_more'])
        self.assertEqual(page['items'][0]['image_filename'],'original.png')
        detail=await self.server.handle_image_detail(SimpleNamespace(match_info={'img_id':'original.png'}))
        self.assertEqual(json.loads(detail.text)['image_comparison']['after']['filename'],'edited.png')
        self.assertIn('edited.png',self.server._registered_image_filenames())
        self.assertTrue((self.image_dir/'edited.png').is_file())

    async def test_future_qwen_output_is_grouped_without_migration(self):
        Image.new('RGB',(24,32),'blue').save(self.image_dir/'next.png')
        ImageMetadataStore(self.server.data_dir).update(lambda m: {**m,'next.png':{'model':'qwen-image-2.1-Q8_0','source':'hermes_api','created_at':200,'generation_mode':'img2img','ref_image_path':str(self.image_dir/'original.png')}})
        items=self.server._load_all_entries()
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['image_comparison']['after']['filename'],'next.png')
        self.assertEqual(items[0]['image_comparison']['edit_count'],2)
        self.assertEqual(len(self.server._load_all_entries(group_edits=False)),3)


if __name__=='__main__':
    unittest.main()
