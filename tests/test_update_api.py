"""Authentication and same-origin checks for the update endpoints."""
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import app
from update_common import UpdateError
import types


class UpdateApiTests(unittest.TestCase):
    def setUp(self):self.client=TestClient(app.app,base_url='https://testserver')
    def tearDown(self):self.client.close()
    def login(self):self.client.post('/api/login',json={'username':app.BASIC_AUTH_USER,'password':app.BASIC_AUTH_PASS})

    def test_start_and_version_checks_require_login(self):
        self.assertEqual(self.client.get('/api/updates').status_code,401)
        self.assertEqual(self.client.post('/api/updates/install',json={'commit':'b'*40}).status_code,401)

    def test_cross_origin_start_is_refused(self):
        self.login()
        with patch.object(app.project_updater,'start') as start:
            response=self.client.post('/api/updates/install',json={'commit':'b'*40},headers={'Origin':'https://elsewhere.test','X-Requested-With':'MarchAgent'})
        self.assertEqual(response.status_code,403);start.assert_not_called()

    def test_background_turn_blocks_install_even_before_cli_lease_is_acquired(self):
        self.login()
        with patch.object(app.web_turns, 'turns', {'synthetic': types.SimpleNamespace(status='running')}), patch.object(app.project_updater,'start') as start:
            response=self.client.post('/api/updates/install',json={'commit':'b'*40},headers={'Origin':'https://testserver','X-Requested-With':'MarchAgent'})
        self.assertEqual(response.status_code,409);start.assert_not_called()

    def test_deliberate_same_origin_start_uses_only_expected_commit(self):
        self.login()
        with patch.object(app.project_updater,'start',return_value={'job':{'id':'1'*32},'token':'synthetic'}) as start:
            response=self.client.post('/api/updates/install',json={'commit':'b'*40},headers={'Origin':'https://testserver','X-Requested-With':'MarchAgent'})
        self.assertEqual(response.status_code,202);start.assert_called_once_with('b'*40)

    def test_job_read_requires_its_capability_even_after_login_reset(self):
        with patch.object(app.project_updater,'read_job',side_effect=UpdateError('invalid')):
            self.assertEqual(self.client.get('/api/updates/jobs/'+'1'*32).status_code,403)
        with patch.object(app.project_updater,'read_job',return_value={'phase':'succeeded'}) as reader:
            response=self.client.get('/api/updates/jobs/'+'1'*32,headers={'X-Update-Token':'synthetic'})
        self.assertEqual(response.status_code,200);reader.assert_called_with('1'*32,'synthetic')
