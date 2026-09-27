"""Chrome extension contract tests: temporary files, fake CDN and fake Qwen only."""
import asyncio
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid
import zipfile

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import FormData
from PIL import Image

APP = Path(__file__).resolve().parents[1] / 'app'
sys.path.insert(0, str(APP))
from browser_extension import canonical_media, canonical_post, normalize_source_text, validate_prompt, validate_steps, PREFIX, digest
from web_server import GalleryServer
from store import ImageMetadataStore

MEDIA = 'https://pbs.twimg.com/media/fixture_photo?format=png&name=small'
POST = 'https://x.com/example/status/12345/photo/1'

class ValidationTests(unittest.TestCase):
    def test_original_media_only(self):
        self.assertEqual(canonical_media(MEDIA), 'https://pbs.twimg.com/media/fixture_photo?format=png&name=orig')
        for u in ['http://pbs.twimg.com/media/a','https://pbs.twimg.com.evil.test/media/a','https://pbs.twimg.com@evil.test/media/a','file:///etc/passwd','http://127.0.0.1:8188/','https://pbs.twimg.com/profile_images/a','https://pbs.twimg.com/media/a?format=svg','https://pbs.twimg.com:444/media/a','https://pbs.twimg.com/media/../a']:
            with self.subTest(url=u), self.assertRaises(ValueError): canonical_media(u)
    def test_post_not_tweet_text(self):
        self.assertEqual(canonical_post(POST),'https://x.com/example/status/12345')
        self.assertEqual(canonical_post('https://evil.test/example/status/1'),'')
    def test_prompt_required(self):
        for p in ['',None,[], 'x'*4001]:
            with self.subTest(p=type(p).__name__), self.assertRaises(ValueError):validate_prompt(p)
        self.assertEqual(validate_prompt(' change color '),'change color')
    def test_steps(self):
        for v in [True,0,51,'25',2.5]:
            with self.subTest(v=v),self.assertRaises(ValueError):validate_steps(v)
        self.assertEqual(validate_steps(25),25)
    def test_source_text_is_bounded_and_normalized(self):
        self.assertEqual(normalize_source_text('  第一行\r\n\r\n\r\n第二行  '), '第一行\n\n第二行')
        with self.assertRaises(ValueError):
            normalize_source_text('x' * 20001)

class ExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);config=self.root/'config/config.yaml';config.parent.mkdir();config.write_text('gallery:\n  port: 18889\n');(self.root/'app/references').mkdir(parents=True)
        self.server=GalleryServer({'paths':{'project_root':str(self.root)},'gallery':{'port':18889}},str(self.root/'data'),str(config))
        self.ext=self.server.browser_extension
        self.client=TestClient(TestServer(self.server.app));await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.headers={'X-Gallery-Extension-ID':self.ext.extension_id,'Origin':'chrome-extension://'+self.ext.extension_id}
        self.calls=[]
        self.ext.download_reference=lambda job:self.ext.store_reference(self.png(),job['id'])
        def fake(engine,prompt,**kwargs):
            self.calls.append((engine,prompt,kwargs));filename='qwen_x_edit_'+uuid.uuid4().hex+'.png'
            output_dir=Path(kwargs.get('output_dir') or self.server.image_dir);output_dir.mkdir(parents=True,exist_ok=True);p=output_dir/filename;p.write_bytes(self.png())
            return {'success':True,'filename':filename,'width':64,'height':80,'elapsed':0.1,'comfy_prompt_id':'fake-prompt'}
        self.server._run_hermes_image_generation=fake
    @staticmethod
    def png():
        out=io.BytesIO();Image.new('RGB',(64,80),'#336699').save(out,format='PNG');return out.getvalue()
    async def pair(self):
        r=await self.client.post(PREFIX+'/manage',json={'operation':'pair_code'});self.assertEqual(r.status,200);code=(await r.json())['code']
        r=await self.client.post(PREFIX+'/pair',headers=self.headers,json={'code':code});self.assertEqual(r.status,200);data=await r.json()
        return {**self.headers,'Authorization':'Bearer '+data['token']},code,data
    async def configured(self):
        h,_,_=await self.pair();r=await self.client.post(PREFIX+'/config',headers=h,json={'prompt':'Change the blue circle to green.','steps':25});self.assertEqual(r.status,200);return h
    async def submit(self,h,rid=None,source_text=''):
        r=await self.client.post(PREFIX+'/jobs',headers=h,json={'media_url':MEDIA,'source_url':POST,'source_text':source_text,'request_id':rid or uuid.uuid4().hex});return r,await r.json()
    async def upload(self,h,rid=None,raw=None,name='portrait.png'):
        form=FormData()
        form.add_field('image',raw or self.png(),filename=name,content_type='image/png')
        form.add_field('request_id',rid or uuid.uuid4().hex)
        return await self.client.post(PREFIX+'/uploads',headers=h,data=form)
    async def drain(self):
        if self.ext.tasks:await asyncio.gather(*list(self.ext.tasks))
    async def test_pair_single_use_and_hash_only(self):
        h,code,data=await self.pair();state=self.ext.config.load();self.assertNotIn(data['token'],json.dumps(state));self.assertNotIn(code,json.dumps(state));self.assertIn(digest(data['token']),state['clients'])
        r=await self.client.post(PREFIX+'/pair',headers=self.headers,json={'code':code});self.assertEqual(r.status,401)
    async def test_pair_denies_bad_origin_or_no_id(self):
        r=await self.client.post(PREFIX+'/pair',headers={**self.headers,'Origin':'https://x.com'},json={'code':'x'*32});self.assertEqual(r.status,403)
        r=await self.client.post(PREFIX+'/pair',json={'code':'x'*32});self.assertEqual(r.status,403)
    async def test_unpaired_even_on_loopback_denied(self):
        r=await self.client.get(PREFIX+'/config',headers=self.headers);self.assertEqual(r.status,401)
    async def test_admin_endpoints_keep_lan_auth(self):
        h,_,_=await self.pair();h['Host']='192.168.31.216:18889'
        for path in [PREFIX+'/manage',PREFIX+'/download','/api/config/keys']:
            r=await self.client.get(path,headers=h);self.assertEqual(r.status,401)
    async def test_revoke_invalidates_scope(self):
        h,_,_=await self.pair();await self.client.post(PREFIX+'/manage',json={'operation':'revoke'});r=await self.client.get(PREFIX+'/config',headers=h);self.assertEqual(r.status,401)
    async def test_password_revision_revokes_scope(self):
        h,_,_=await self.pair()
        with patch.object(self.server,'_gallery_password_revision',return_value='changed'):
            r=await self.client.get(PREFIX+'/config',headers=h);self.assertEqual(r.status,401)
    async def test_missing_prompt_does_not_start(self):
        h,_,_=await self.pair();r,data=await self.submit(h);self.assertEqual(r.status,400);self.assertFalse(self.calls)
    async def test_job_img2img_output_and_owner_scope(self):
        h=await self.configured();r,j=await self.submit(h);self.assertEqual(r.status,202);await self.drain()
        r=await self.client.get(PREFIX+'/jobs/'+j['id'],headers=h);j=await r.json();self.assertEqual(j['status'],'done');self.assertNotIn('prompt',j);self.assertNotIn('reference',j)
        self.assertEqual(self.calls[0][0],'qwen');self.assertEqual(self.calls[0][2]['size'],'auto');self.assertTrue(Path(self.calls[0][2]['ref_image']).is_file());self.assertEqual(self.calls[0][2]['source'],'chrome_extension')
        self.assertFalse((Path(self.server.image_dir)/j['result']['filename']).exists())
        for kind in ['image','source']:
            r=await self.client.get(PREFIX+'/jobs/'+j['id']+'/'+kind,headers=h);self.assertEqual(r.status,200);self.assertTrue((await r.read()).startswith(b'\x89PNG'))
        other,_,_=await self.pair();r=await self.client.get(PREFIX+'/jobs/'+j['id'],headers=other);self.assertEqual(r.status,404)

    async def test_save_result_is_explicit_and_publishes_metadata(self):
        h=await self.configured();r,j=await self.submit(h,source_text='X 原帖中的正文内容');self.assertEqual(r.status,202);await self.drain()
        r=await self.client.get(PREFIX+'/jobs/'+j['id'],headers=h);job=await r.json();filename=job['result']['filename']
        self.assertFalse((Path(self.server.image_dir)/filename).exists())
        r=await self.client.post(PREFIX+'/jobs/'+j['id']+'/save',headers=h);self.assertEqual(r.status,200);saved=await r.json();self.assertTrue(saved['job']['result']['saved_to_gallery'])
        self.assertTrue((Path(self.server.image_dir)/filename).is_file())
        metadata=ImageMetadataStore(self.server.data_dir).load();self.assertEqual(metadata[filename]['source'],'chrome_extension');self.assertEqual(metadata[filename]['source_url'],canonical_post(POST));self.assertEqual(metadata[filename]['source_text'],'X 原帖中的正文内容')
        entry=self.server._metadata_gallery_entry(filename,metadata[filename]);entry=self.server._normalize_entry_display(entry,metadata);self.assertEqual(entry['source_text'],'X 原帖中的正文内容')
        r=await self.client.get(PREFIX+'/jobs/'+j['id']+'/image',headers=h);self.assertEqual(r.status,200)

    async def test_upload_job_is_independent_and_opt_in_saved(self):
        h=await self.configured();r=await self.upload(h);self.assertEqual(r.status,202);j=await r.json()
        self.assertEqual(j['input_type'],'upload');self.assertEqual(j['source_name'],'portrait.png');self.assertEqual(j['source_url'],'')
        await self.drain();job=await (await self.client.get(PREFIX+'/jobs/'+j['id'],headers=h)).json()
        self.assertEqual(job['status'],'done');self.assertFalse((Path(self.server.image_dir)/job['result']['filename']).exists())
        self.assertEqual(self.calls[0][2]['source'],'chrome_extension');self.assertTrue(Path(self.calls[0][2]['ref_image']).is_file())
        for kind in ['image','source']:
            r=await self.client.get(PREFIX+'/jobs/'+j['id']+'/'+kind,headers=h);self.assertEqual(r.status,200)
        filename=job['result']['filename'];r=await self.client.post(PREFIX+'/jobs/'+j['id']+'/save',headers=h);self.assertEqual(r.status,200)
        self.assertTrue((Path(self.server.image_dir)/filename).is_file())
        metadata=ImageMetadataStore(self.server.data_dir).load();self.assertEqual(metadata[filename]['input_type'],'upload');self.assertEqual(metadata[filename]['source_name'],'portrait.png')

    async def test_upload_validation_and_duplicate_request(self):
        h=await self.configured();rid=uuid.uuid4().hex
        r=await self.upload(h,rid,raw=b'not-an-image');self.assertEqual(r.status,400)
        r=await self.upload(h,rid);self.assertEqual(r.status,202);first=await r.json()
        r=await self.upload(h,rid);self.assertEqual(r.status,200);second=await r.json();self.assertEqual(first['id'],second['id'])
        await self.drain();self.assertEqual(len(self.calls),1)
        form=FormData();form.add_field('image',self.png(),filename='x.png',content_type='image/png')
        r=await self.client.post(PREFIX+'/uploads',headers=h,data=form);self.assertEqual(r.status,400)

    async def test_legacy_gallery_result_fallback_is_readable_and_savable(self):
        """Results from the pre-opt-in build remain usable after an upgrade."""
        h=await self.configured();r,j=await self.submit(h);self.assertEqual(r.status,202);await self.drain()
        job=await (await self.client.get(PREFIX+'/jobs/'+j['id'],headers=h)).json()
        filename=job['result']['filename']
        temporary=self.ext.result_dir/filename
        legacy=Path(self.server.image_dir)/filename
        legacy.write_bytes(temporary.read_bytes())
        temporary.unlink()

        r=await self.client.get(PREFIX+'/jobs/'+j['id']+'/image',headers=h)
        self.assertEqual(r.status,200)
        self.assertTrue((await r.read()).startswith(b'\x89PNG'))
        r=await self.client.post(PREFIX+'/jobs/'+j['id']+'/save',headers=h)
        self.assertEqual(r.status,200)
        self.assertTrue((await r.json())['job']['result']['saved_to_gallery'])
    async def test_duplicate_request_one_generation(self):
        h=await self.configured();rid=uuid.uuid4().hex;r,a=await self.submit(h,rid);r,b=await self.submit(h,rid);self.assertEqual(a['id'],b['id']);await self.drain();self.assertEqual(len(self.calls),1)
    async def test_request_conflict(self):
        h=await self.configured();rid=uuid.uuid4().hex;await self.submit(h,rid);await self.client.post(PREFIX+'/config',headers=h,json={'prompt':'another edit'});r,_=await self.submit(h,rid);self.assertEqual(r.status,409)
    async def test_download_failure_no_generation_or_fallback(self):
        h=await self.configured();self.ext.download_reference=Mock(side_effect=ValueError('fixture download failed'));_,j=await self.submit(h);await self.drain();r=await self.client.get(PREFIX+'/jobs/'+j['id'],headers=h);self.assertEqual((await r.json())['status'],'error');self.assertFalse(self.calls)
    async def test_recover_marks_not_retries(self):
        self.ext.jobs.save({'a':{'status':'generating'}});await self.ext.recover(self.server.app);self.assertEqual(self.ext.jobs.load()['a']['status'],'interrupted');self.assertFalse(self.calls)
    async def test_sync_can_be_disabled(self):
        await self.client.post(PREFIX+'/manage',json={'operation':'save','prompt':'original','sync_gallery_prompt':False});await self.client.post(PREFIX+'/manage',json={'operation':'sync_prompt','prompt':'new'});self.assertEqual(self.ext.settings()['prompt'],'original')
    async def test_webp_original_404_uses_same_image_jpeg(self):
        from contextlib import nullcontext
        from browser_extension import BrowserExtension
        bad=Mock(status_code=404)
        good=Mock(status_code=200,headers={'Content-Type':'image/png'})
        good.iter_content.return_value=[self.png()]
        with patch('browser_extension.requests.get',side_effect=[nullcontext(bad),nullcontext(good)]) as get:
            result=BrowserExtension.download_reference(self.ext,{'id':'c'*32,'media_url':MEDIA.replace('format=png','format=webp')})
        self.assertTrue(Path(result).is_file())
        self.assertIn('format=webp&name=orig',get.call_args_list[0].args[0])
        self.assertIn('format=jpg&name=orig',get.call_args_list[1].args[0])
        self.assertFalse(get.call_args.kwargs['allow_redirects'])
    async def test_cdn_forbidden_does_not_retry_or_redirect(self):
        from contextlib import nullcontext
        from browser_extension import BrowserExtension
        with patch('browser_extension.requests.get',return_value=nullcontext(Mock(status_code=403))) as get:
            with self.assertRaises(ValueError):BrowserExtension.download_reference(self.ext,{'id':'c'*32,'media_url':MEDIA.replace('format=png','format=webp')})
        self.assertEqual(get.call_count,1)
    async def test_bad_image_rejected(self):
        with self.assertRaises(Exception):self.ext.store_reference(b'not image','a'*32)
    async def test_package_is_self_contained_without_secrets(self):
        r=await self.client.get(PREFIX+'/download');self.assertEqual(r.status,200)
        with zipfile.ZipFile(io.BytesIO(await r.read())) as z:
            names=z.namelist();self.assertIn('gallery-qwen-x/manifest.json',names);self.assertIn('gallery-qwen-x/icons/128.png',names)
            m=json.loads(z.read('gallery-qwen-x/manifest.json'));self.assertEqual(m['manifest_version'],3);self.assertNotIn('<all_urls>',m['host_permissions']);self.assertNotIn('cookies',m['permissions'])
            self.assertFalse(any('config.yaml' in n or 'browser_extension_config' in n for n in names))
    async def test_cors_only_extension(self):
        r=await self.client.options(PREFIX+'/jobs',headers=self.headers);self.assertEqual(r.status,204);self.assertEqual(r.headers['Access-Control-Allow-Origin'],self.headers['Origin'])
        r=await self.client.options(PREFIX+'/jobs',headers={'Origin':'https://evil.test'});self.assertEqual(r.status,403);self.assertNotIn('Access-Control-Allow-Origin',r.headers)

if __name__=='__main__':unittest.main()
