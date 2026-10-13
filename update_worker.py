"""Independent systemd worker: prepare, install a pinned revision, or roll back."""
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from update_common import (SHA, UpdateError, acquire_lock, git, read_json, repository,
                           source_path, state_directory, version, write_json)


def progress(job, phase, message, **extra):
    job.update(phase=phase, message=message, updated_at=time.time(), **extra)
    write_json(state_directory(job['root'])/'jobs'/(job['id']+'.json'), job)


def run(command, job, *, env=None, cwd=None):
    result = subprocess.run(command, env=env, cwd=cwd, capture_output=True, timeout=600)
    log = state_directory(job['root'])/'jobs'/(job['id']+'.log')
    with log.open('ab') as handle:
        handle.write(result.stdout); handle.write(result.stderr)
    log.chmod(0o600)
    if result.returncode:
        raise UpdateError('依赖安装或新版检查未完成，已停止更新。')


def prepare_source(root, stage, target):
    data = git(root, 'archive', '--format=tar', target, binary=True)
    if len(data)>128*1024*1024:
        raise UpdateError('新版代码包过大，已停止更新。')
    current = set(git(root, 'ls-files', '-z').split('\0'))
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = archive.getmembers()
        if len(members)>4096:
            raise UpdateError('新版代码包文件数量异常。')
        for member in members:
            path = source_path(member.name.rstrip('/'))
            destination = root/str(path)
            if not destination.resolve().is_relative_to(root.resolve()):
                raise UpdateError('更新路径指向项目目录外，已停止更新。')
            if not member.isfile() and not member.isdir():
                raise UpdateError('新版包含链接或特殊文件，已停止更新。')
            if member.isfile() and member.name not in current and destination.exists():
                raise UpdateError('新版文件与本机自定义文件冲突，已保留本机文件。')
        archive.extractall(stage)
    for name in ('app.py', 'run.py', 'project_updates.py', 'update_worker.py', 'update_common.py', 'requirements.txt', 'detached_chat.py'):
        if not (stage/name).is_file():
            raise UpdateError('新版缺少必需的项目文件。')
    for file in stage.rglob('*.py'):
        compile(file.read_bytes(), str(file), 'exec')


def wait_health(job, expected, attempts=30):
    url = 'http://127.0.0.1:'+str(job['port'])+'/healthz'
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                health = json.load(response)
            if health.get('status')=='ok' and health.get('version')==expected:
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise UpdateError('服务重启后未通过新版检查。')


def perform(job):
    root, target = Path(job['root']).resolve(), job['target']
    repository(job['repo'])
    if not SHA.fullmatch(target) or not SHA.fullmatch(job['previous']):
        raise UpdateError('更新版本号无效。')
    if not 1<=int(job['port'])<=65535:
        raise UpdateError('服务监听配置无效。')
    directory = state_directory(root)
    backup = directory/'backups'/job['id']; backup.mkdir(parents=True, mode=0o700)
    stage = directory/'stages'/job['id']; stage.mkdir(parents=True, mode=0o700)
    environment_dir = directory/'venvs'/job['id']; environment_dir.parent.mkdir(mode=0o700, exist_ok=True)
    venv = root/'.venv'
    switched, changed = False, False
    old_link = os.readlink(venv) if venv.is_symlink() else None
    try:
        if version(root)!=job['previous'] or git(root, 'status', '--porcelain', '--untracked-files=no'):
            raise UpdateError('本机代码已改变，已停止更新并保留修改。')
        progress(job, 'fetching', '正在获取 GitHub 新版本…')
        git(root, 'fetch', '--no-tags', 'https://github.com/'+job['repo']+'.git', target)
        if git(root, 'rev-parse', 'FETCH_HEAD')!=target:
            raise UpdateError('下载版本与所选版本不一致。')
        # Refuse replacing an unrelated local branch or a rewritten upstream.
        git(root, 'merge-base', '--is-ancestor', job['previous'], target)
        prepare_source(root, stage, target)
        (backup/'source.tar').write_bytes(git(root, 'archive', '--format=tar', job['previous'], binary=True))
        progress(job, 'preparing', '正在准备新版运行环境…')
        base_python = str(Path(getattr(sys, '_base_executable', sys.executable)).resolve())
        run([base_python, '-m', 'venv', str(environment_dir)], job)
        python = str(environment_dir/'bin/python')
        progress(job, 'installing', '正在安装依赖并检查新版…')
        run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--index-url',
             'https://pypi.org/simple', '-r', str(stage/'requirements.txt')], job)
        with tempfile.TemporaryDirectory(prefix='march-update-smoke-') as temporary:
            env = {key:value for key,value in os.environ.items()
                   if not key.startswith(('AGY_', 'BASIC_AUTH_', 'PYTHON')) and key!='CLOUD_CODE_URL'}
            env.update(HOME=temporary, USERPROFILE=temporary, AGY_SCRATCH=temporary+'/scratch',
                       AGY_UPLOADS=temporary+'/uploads', AGY_MESSAGES_DIR=temporary+'/messages',
                       AGY_ACCOUNTS_DIR=temporary+'/accounts', AGY_CONVERSATIONS_FILE=temporary+'/conversations.json',
                       BASIC_AUTH_USER='update-smoke', BASIC_AUTH_PASS='update-smoke-password', AGY_WEB_UPDATE_ENABLED='0')
            run([python, '-c', 'import asyncio,app; assert asyncio.run(app.healthz())["status"]=="ok"'],
                job, env=env, cwd=stage)
        progress(job, 'restarting', '检查通过，正在切换版本并重启…')
        if version(root)!=job['previous'] or git(root, 'status', '--porcelain', '--untracked-files=no'):
            raise UpdateError('准备期间本机代码已改变，已保留修改并停止更新。')
        prepare_source(root, stage, target)
        changed = True
        git(root, 'reset', '--hard', target)
        if old_link is None:
            os.replace(venv, backup/'venv')
        else:
            venv.unlink()
        switched = True
        venv.symlink_to(environment_dir, target_is_directory=True)
        run(['systemctl', '--user', 'restart', job['service']], job)
        progress(job, 'verifying', '正在确认新版已正常启动…')
        wait_health(job, target)
        progress(job, 'succeeded', '更新完成，刷新页面并重新登录即可使用新版。', rolled_back=False)
    except Exception as error:
        if changed:
            progress(job, 'rolling_back', '更新未完成，正在恢复原版本…')
            try:
                git(root, 'reset', '--hard', job['previous'])
                if switched:
                    if venv.is_symlink():venv.unlink()
                    if old_link is not None:venv.symlink_to(old_link, target_is_directory=True)
                    else:os.replace(backup/'venv', venv)
                run(['systemctl', '--user', 'restart', job['service']], job)
                wait_health(job, job['previous'])
            except Exception:
                progress(job, 'failed', '自动恢复未完成，请管理员检查后台服务；备份已保留。', rolled_back=False)
                return
        message = str(error) if isinstance(error, UpdateError) else '更新未完成，请稍后重试或检查后台日志。'
        progress(job, 'failed', message + (' 已恢复原版本。' if changed else ''), rolled_back=changed)


def main():
    root = Path(__file__).resolve().parent
    path = Path(sys.argv[1]).resolve()
    if path.parent != state_directory(root)/'jobs':
        raise UpdateError('更新任务路径无效。')
    job = read_json(path)
    if str(root)!=job.get('root') or path.name!=job.get('id','')+'.json':
        raise UpdateError('更新任务与安装目录不一致。')
    def interrupted(*_):raise UpdateError('后台更新被中断。')
    signal.signal(signal.SIGTERM, interrupted)
    with acquire_lock(root, exclusive=True, wait=True):
        try:
            perform(job)
        except Exception:
            progress(job, 'failed', '后台更新未完成，请检查服务器目录权限或后台日志。')


if __name__ == '__main__':
    main()
