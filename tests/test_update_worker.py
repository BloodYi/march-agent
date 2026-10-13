"""Exercise cutover and rollback without networking or touching real services."""
import io
import tarfile
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import update_worker as worker
from update_common import UpdateError, read_json, source_path, state_directory, write_json

FILES=('app.py','run.py','project_updates.py','update_common.py','update_worker.py','requirements.txt','detached_chat.py')
def archive(extra=None):
    output=io.BytesIO()
    with tarfile.open(fileobj=output,mode='w') as tar:
        for name in (*FILES,*(extra or {})):
            content=(extra or {}).get(name,b"VALUE = 'new'\n" if name=='app.py' else b'')
            info=tarfile.TarInfo(name);info.size=len(content);tar.addfile(info,io.BytesIO(content))
    return output.getvalue()


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        for name in FILES:(self.root/name).write_text("VALUE = 'old'\n" if name=='app.py' else '')
        (self.root/'.env').write_text('BASIC_AUTH_PASS=synthetic-local-password')
        (self.root/'data').mkdir();(self.root/'data/chat.json').write_text('preserved')
        (self.root/'.venv').mkdir();(self.root/'.venv/old-marker').write_text('old')
        self.head='a'*40;self.code=archive()
        self.job={'id':'1'*32,'root':str(self.root),'target':'b'*40,'previous':'a'*40,'repo':'BloodYi/march-agent','service':'march-agent.service','port':26903}
        (state_directory(self.root)/'jobs').mkdir()
        write_json(state_directory(self.root)/'jobs'/(self.job['id']+'.json'),self.job)

    def tearDown(self):self.temporary.cleanup()

    def git(self,root,*args,**kwargs):
        if args[0]=='status':return ''
        if args[0]=='ls-files':return '\0'.join(FILES)+'\0'
        if args[0]=='rev-parse':return self.job['target']
        if args[0]=='archive':return self.code
        if args[0]=='reset':
            self.head=args[-1]
            (self.root/'app.py').write_text("VALUE = 'new'\n" if self.head=='b'*40 else "VALUE = 'old'\n")
        return ''

    def fake_run(self,command,job,**kwargs):
        if command[1:3]==['-m','venv']:(Path(command[-1])/'bin').mkdir(parents=True)

    def perform(self,health=None,run=None):
        with patch.object(worker,'git',self.git),patch.object(worker,'version',lambda root:self.head),patch.object(worker,'run',run or self.fake_run),patch.object(worker,'wait_health',health or (lambda *a:None)):
            worker.perform(self.job)
        return read_json(state_directory(self.root)/'jobs'/(self.job['id']+'.json'))

    @unittest.skipUnless(sys.platform=='linux', 'Transactional venv links are exercised on Linux')
    def test_success_installs_pinned_code_and_preserves_runtime_files(self):
        result=self.perform()
        self.assertEqual(result['phase'],'succeeded');self.assertEqual(self.head,'b'*40)
        self.assertTrue((self.root/'.venv').is_symlink())
        self.assertEqual((self.root/'data/chat.json').read_text(),'preserved')
        self.assertIn('synthetic-local-password',(self.root/'.env').read_text())

    @unittest.skipUnless(sys.platform=='linux', 'Transactional venv links are exercised on Linux')
    def test_failed_health_restores_old_source_and_environment(self):
        checks=[]
        def health(job,expected):
            checks.append(expected)
            if expected=='b'*40:raise UpdateError('Synthetic startup failure')
        result=self.perform(health=health)
        self.assertEqual(result['phase'],'failed');self.assertTrue(result['rolled_back'])
        self.assertEqual(self.head,'a'*40);self.assertFalse((self.root/'.venv').is_symlink())
        self.assertTrue((self.root/'.venv/old-marker').exists());self.assertEqual(checks,['b'*40,'a'*40])

    def test_dependency_failure_does_not_change_active_code(self):
        result=self.perform(run=lambda *a,**k:(_ for _ in ()).throw(UpdateError('Synthetic install failure')))
        self.assertEqual(result['phase'],'failed');self.assertFalse(result['rolled_back'])
        self.assertEqual(self.head,'a'*40);self.assertTrue((self.root/'.venv/old-marker').exists())

    def test_custom_file_collision_stops_before_cutover(self):
        (self.root/'custom.py').write_text('user-owned')
        self.code=archive({'custom.py':b'new'})
        result=self.perform();self.assertEqual(result['phase'],'failed')
        self.assertEqual((self.root/'custom.py').read_text(),'user-owned');self.assertEqual(self.head,'a'*40)

    def test_protected_data_and_paths_are_rejected(self):
        for name in ('.env','../outside.py','/outside','data/chat.json','.git/config','uploads/image.png','a\\b'):
            with self.subTest(name=name),self.assertRaises(UpdateError):source_path(name)
        self.assertEqual(str(source_path('.env.example')),'.env.example')
