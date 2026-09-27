"""Automatic extension connection contracts; temporary gallery and fake images only."""
from types import SimpleNamespace
import unittest
import uuid
import test_browser_extension as fixtures
from browser_extension import PREFIX, is_local_address


class AutoConnectionTests(fixtures.ExtensionTests):
    async def auto(self, client_id=None, headers=None):
        client_id = client_id or uuid.uuid4().hex
        response = await self.client.post(PREFIX+'/connect', headers=headers or self.headers, json={'client_id':client_id})
        return response, await response.json(), client_id

    async def test_auto_connection_needs_no_code_and_stays_scoped(self):
        r,d,_=await self.auto();self.assertEqual(r.status,200);self.assertEqual(d['connection_mode'],'local_auto')
        self.assertNotIn(d['token'],str(self.ext.config.load()))
        h={**self.headers,'Authorization':'Bearer '+d['token']}
        r=await self.client.get(PREFIX+'/config',headers=h);self.assertEqual(r.status,200)
        r=await self.client.get('/api/config/keys',headers={**h,'Host':'192.168.31.216:18889'});self.assertEqual(r.status,401)

    async def test_auto_reconnect_retains_owner_and_old_jobs(self):
        _,d,cid=await self.auto();h={**self.headers,'Authorization':'Bearer '+d['token']}
        await self.client.post(PREFIX+'/config',headers=h,json={'prompt':'Change the background to blue.'})
        r,job=await self.submit(h);await self.drain()
        r,new,_=await self.auto(cid);self.assertEqual(new['client_id'],d['client_id']);self.assertEqual(len(self.ext.config.load()['clients']),1)
        r=await self.client.get(PREFIX+'/jobs/'+job['id'],headers={**self.headers,'Authorization':'Bearer '+new['token']});self.assertEqual(r.status,200)
        _,other,_=await self.auto();r=await self.client.get(PREFIX+'/jobs/'+job['id'],headers={**self.headers,'Authorization':'Bearer '+other['token']});self.assertEqual(r.status,404)

    async def test_auto_wrong_or_missing_origin_denied(self):
        for origin in ['','null','https://x.com','chrome-extension://'+'a'*32]:
            r,_,_=await self.auto(headers={**self.headers,'Origin':origin});self.assertEqual(r.status,403)

    async def test_auto_public_hostname_denied(self):
        r,_,_=await self.auto(headers={**self.headers,'Host':'public.example:18889'});self.assertEqual(r.status,403)

    async def test_auto_public_peer_denied(self):
        req=SimpleNamespace(headers=self.headers,host='192.168.31.216:18889',remote='8.8.8.8',method='POST',path=PREFIX+'/connect')
        self.assertEqual(self.ext.authorize(req).status,403)

    async def test_auto_disable_is_persistent_until_admin_enables(self):
        _,d,_=await self.auto();await self.client.post(PREFIX+'/manage',json={'operation':'revoke'})
        r,_,_=await self.auto();self.assertEqual(r.status,403)
        r=await self.client.get(PREFIX+'/config',headers={**self.headers,'Authorization':'Bearer '+d['token']});self.assertEqual(r.status,401)
        await self.client.post(PREFIX+'/manage',json={'operation':'enable_auto_connect'});r,_,_=await self.auto();self.assertEqual(r.status,200)

    async def test_auto_invalid_install_id_denied(self):
        for value in ['','abc','../../etc/passwd','x'*32]:
            r=await self.client.post(PREFIX+'/connect',headers=self.headers,json={'client_id':value});self.assertEqual(r.status,400)

    async def test_auto_cors_private_network_only_extension(self):
        r=await self.client.options(PREFIX+'/connect',headers={**self.headers,'Access-Control-Request-Private-Network':'true'})
        self.assertEqual(r.status,204);self.assertEqual(r.headers['Access-Control-Allow-Origin'],self.headers['Origin'])
        self.assertEqual(r.headers.get('Access-Control-Allow-Private-Network'),'true')


class LocalAddressTests(unittest.TestCase):
    def test_only_explicit_loopback_or_rfc1918(self):
        for ip in ['127.0.0.1','::1','10.1.2.3','172.16.0.1','192.168.31.216']:self.assertTrue(is_local_address(ip))
        for ip in ['8.8.8.8','0.0.0.0','169.254.169.254','100.64.0.1','172.32.1.1','localhost','public.example']:self.assertFalse(is_local_address(ip))
