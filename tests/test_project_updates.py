"""Offline version, scheduling and job-capability checks."""
import hashlib
import io
import os
import tempfile
import types
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
from project_updates import ProjectUpdater
from update_common import UpdateError, read_json, state_directory


class ProjectUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name)
        self.updater=ProjectUpdater(self.root,26903)
        self.updater.running_version='a'*40
        self.updater.capability=lambda:None

    def tearDown(self):self.temporary.cleanup()

    def test_remote_sha_is_cached_and_compared_to_running_revision(self):
        opener=types.SimpleNamespace(open=lambda *a,**k:io.BytesIO(('b'*40).encode()))
        with patch('project_updates.urllib.request.build_opener',return_value=opener),patch('project_updates.time.time',return_value=100):
            info=self.updater.check()
        self.assertTrue(info['available']);self.assertTrue(info['can_update'])
        with patch('project_updates.urllib.request.build_opener',side_effect=AssertionError('Must use cache')),patch('project_updates.time.time',return_value=105):
            self.assertEqual(self.updater.check(True)['latest'],'b'*40)

    def test_failed_or_malformed_check_never_claims_latest(self):
        for body in (b'bad',b'{"sha":"unverified"}'):
            self.updater.next_check=0
            with patch('project_updates.urllib.request.build_opener',return_value=types.SimpleNamespace(open=lambda *a,**k:io.BytesIO(body))):
                info=self.updater.check(True)
            self.assertIsNotNone(info['error']);self.assertFalse(info['can_update'])

    def test_unchecked_commit_and_already_latest_cannot_start(self):
        for target in ('../anything','b'*40,'a'*40):
            with self.subTest(target=target),self.assertRaises(UpdateError):self.updater.start(target)

    def test_job_token_survives_service_restart_and_is_read_only(self):
        self.updater.latest='b'*40
        with patch('project_updates.acquire_lock',return_value=nullcontext()),patch('project_updates.subprocess.run',return_value=types.SimpleNamespace(returncode=0)) as run:
            result=self.updater.start('b'*40)
        command=run.call_args.args[0]
        self.assertIn('--property=Type=oneshot',command)
        self.assertNotIn(result['token'],str(command))
        new=ProjectUpdater(self.root,26903)
        status=new.read_job(result['job']['id'],result['token'])
        self.assertEqual(status['phase'],'queued')
        self.assertNotIn('token_hash',status);self.assertNotIn('root',status)
        file=state_directory(self.root)/'jobs'/(status['id']+'.json')
        self.assertEqual(read_json(file)['token_hash'],hashlib.sha256(result['token'].encode()).hexdigest())
        for identifier,token in ((status['id'],'bad'),('../path',result['token'])):
            with self.assertRaises(UpdateError):new.read_job(identifier,token)

    def test_failed_dispatch_is_recorded_and_does_not_leave_busy_job(self):
        self.updater.latest='b'*40
        with patch('project_updates.acquire_lock',return_value=nullcontext()),patch('project_updates.subprocess.run',return_value=types.SimpleNamespace(returncode=1)):
            with self.assertRaises(UpdateError):self.updater.start('b'*40)
        self.assertEqual(self.updater.job()['phase'],'failed')

    def test_locked_turn_prevents_update_dispatch(self):
        self.updater.latest='b'*40
        with patch('project_updates.acquire_lock',side_effect=UpdateError('busy')),patch('project_updates.subprocess.run') as run:
            with self.assertRaises(UpdateError):self.updater.start('b'*40)
        run.assert_not_called()

    def test_running_job_rejects_new_turn(self):
        self.updater.enabled=True
        handle=types.SimpleNamespace(close=lambda:None)
        with patch('project_updates.sys.platform','linux'),patch('project_updates.acquire_lock',return_value=handle),patch.object(self.updater,'job',return_value={'phase':'installing'}):
            with self.assertRaises(UpdateError):self.updater.acquire_turn()

    def test_install_requires_service_to_own_the_current_web_process(self):
        del self.updater.capability
        self.updater.enabled=True
        responses=['https://github.com/'+self.updater.repo+'.git', '']
        for pid, allowed in ((0, False), (os.getpid(), True)):
            service=types.SimpleNamespace(returncode=0, stdout=f'LoadState=loaded\nMainPID={pid}\n')
            with patch('project_updates.sys.platform','linux'),patch('project_updates.shutil.which',return_value='/bin/systemd-run'),patch('project_updates.git',side_effect=responses),patch('project_updates.version',return_value='a'*40),patch('project_updates.subprocess.run',return_value=service):
                self.assertEqual(self.updater.capability() is None, allowed)

    @unittest.skipUnless(sys.platform=='linux', 'File leases are exercised on Linux')
    def test_real_file_lease_keeps_an_active_reply_safe(self):
        self.updater.enabled=True;self.updater.latest='b'*40
        handle=self.updater.acquire_turn()
        try:
            with patch('project_updates.subprocess.run') as dispatch:
                with self.assertRaises(UpdateError):self.updater.start('b'*40)
                dispatch.assert_not_called()
        finally:handle.close()
