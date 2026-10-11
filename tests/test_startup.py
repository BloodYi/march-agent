"""HTTP smoke checks against a clean temporary installation with synthetic data."""
from pathlib import Path
import unittest
import runpy
from unittest.mock import patch
from fastapi.testclient import TestClient
import app

class StartupTests(unittest.TestCase):
    def setUp(self):self.client=TestClient(app.app,base_url='https://testserver')
    def tearDown(self):self.client.close()
    def login(self):
        response=self.client.post('/api/login',json={'username':app.BASIC_AUTH_USER,'password':app.BASIC_AUTH_PASS})
        self.assertEqual(response.status_code,200)
    def test_private_chat_requires_authentication(self):
        self.assertEqual(self.client.get('/api/conversations').status_code,401)
        self.assertEqual(self.client.post('/api/login',json={'username':'wrong','password':'wrong'}).status_code,401)
    def test_health_and_login_art_are_available(self):
        self.assertEqual(self.client.get('/healthz').json()['status'],'ok')
        self.assertEqual(self.client.get('/static/bg-login.jpg').status_code,200)
    def test_signed_in_page_has_both_themes_and_march_avatar(self):
        self.login()
        response=self.client.get('/')
        self.assertEqual(response.status_code,200)
        self.assertIn('小三月',response.text)
        for name in ('ui-settings.js','march-status.js','scene-theme.css'):
            self.assertIn(name,response.text)
        self.assertEqual(self.client.get('/static/ui/march-avatar-v2.png').status_code,200)
        self.assertEqual(self.client.get('/static/ui/castorice-chibi.png').status_code,200)
    def test_uploaded_file_can_be_downloaded_on_same_authenticated_site(self):
        self.login()
        response=self.client.post('/api/upload',files={'file':('sample.txt',b'synthetic attachment','text/plain')})
        self.assertEqual(response.status_code,200)
        path=response.json()['path']
        self.assertTrue(Path(path).is_relative_to(app.UPLOADS.resolve()))
        download=self.client.get('/api/download',params={'path':path})
        self.assertEqual(download.status_code,200)
        self.assertEqual(download.content,b'synthetic attachment')
        self.assertIn('attachment',download.headers['content-disposition'])
    def test_download_outside_configured_directories_is_rejected(self):
        self.login()
        self.assertEqual(self.client.get('/api/download',params={'path':str(Path(app.HOME)/'outside.txt')}).status_code,403)
    def test_startup_requires_the_deployers_own_website_credentials(self):
        with patch.object(app,'BASIC_AUTH_PASS',''),self.assertRaises(SystemExit) as caught:
            runpy.run_path(str(Path(app.__file__).with_name('run.py')),run_name='__main__')
        self.assertIn('BASIC_AUTH_PASS',str(caught.exception))

if __name__=='__main__':unittest.main()
