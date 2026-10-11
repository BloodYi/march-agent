"""Run tests against a clean copy with a temporary HOME; never read user tokens."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='march-agent-tests-') as temporary:
    copy=Path(temporary)/'project';copy.mkdir()
    for name in ('app.py','agy_metadata.py','agy_client_profile.py','run.py'):
        shutil.copy2(root/name,copy/name)
    for name in ('static','tests'):
        shutil.copytree(root/name,copy/name,ignore=shutil.ignore_patterns('__pycache__'))
    task_home=Path(temporary)/'home';task_home.mkdir()
    environment=os.environ.copy()
    for key in list(environment):
        if key.startswith(('AGY_','BASIC_AUTH_')) or key=='CLOUD_CODE_URL':environment.pop(key)
    environment.update(HOME=str(task_home),USERPROFILE=str(task_home),
        BASIC_AUTH_USER='synthetic-test-user',BASIC_AUTH_PASS='synthetic-test-password',
        AGY_ACCOUNTS_DIR=str(task_home/'accounts'),AGY_ALLOWED_ROOTS=str(copy/'scratch'),
        AGY_UPSTREAM_PROFILE='native')
    result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'],
        cwd=copy,env=environment)
    sys.exit(result.returncode)
