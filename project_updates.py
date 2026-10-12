"""Version checks and deliberate update scheduling; never execute request commands."""
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from update_common import (BUSY, SHA, UpdateError, acquire_lock, active_job, git,
                           public_job, read_json, repository, state_directory, version, write_json)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ProjectUpdater:
    def __init__(self, root, port):
        self.root, self.port = Path(root).resolve(), port
        self.running_version = version(self.root)
        self.repo = os.getenv('AGY_UPDATE_REPO', 'BloodYi/march-agent')
        self.service = os.getenv('AGY_UPDATE_SERVICE', 'march-agent.service')
        self.enabled = os.getenv('AGY_WEB_UPDATE_ENABLED', '1').lower() not in ('0', 'false', '')
        self.latest, self.checked_at, self.error = None, 0, None
        self.next_check, self.guard = 0, threading.Lock()

    def job(self):
        try:
            job = active_job(self.root)
        except (OSError, UpdateError):
            return {}
        if job.get('phase') in BUSY and time.time()-job.get('updated_at', 0) > 1800:
            job.update(phase='failed', message='更新任务已中断，请检查后台服务后重试。')
        return job

    def capability(self):
        if not self.enabled:
            return '管理员已关闭网页更新。'
        if sys.platform != 'linux' or not shutil.which('systemd-run'):
            return '一键更新需要 Linux 的 systemd 用户服务；仍可检查版本。'
        if not os.access(self.root, os.W_OK):
            return '安装目录只读，可以检查版本；一键更新需要写入权限。'
        try:
            repository(self.repo)
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,100}\.service', self.service):
                return '请配置正确的更新服务名称。'
            if not self.running_version:
                return '此安装没有 Git 版本记录，请按部署说明安装 GitHub 项目。'
            remote = git(self.root, 'remote', 'get-url', 'origin')
            allowed = {'https://github.com/'+self.repo+'.git', 'https://github.com/'+self.repo,
                       'git@github.com:'+self.repo+'.git'}
            if remote.lower() not in {item.lower() for item in allowed}:
                return 'Git 来源与更新来源不一致，请先核对部署配置。'
            if git(self.root, 'status', '--porcelain', '--untracked-files=no'):
                return '本地代码有改动，已暂停一键更新以保留你的修改。'
            if version(self.root) != self.running_version:
                return '磁盘代码已改变，请先重启服务再更新。'
            service = subprocess.run(['systemctl', '--user', 'show', self.service, '-p', 'LoadState', '-p', 'MainPID'],
                                     capture_output=True, text=True, timeout=3)
            values = dict(line.split('=',1) for line in service.stdout.splitlines() if '=' in line)
            if service.returncode or values.get('LoadState')!='loaded':
                return '请先配置 systemd 用户服务，再使用一键更新。'
            if values.get('MainPID')!=str(os.getpid()):
                return '更新服务与当前网页进程不一致，请核对服务名称。'
        except (UpdateError, OSError, subprocess.TimeoutExpired) as error:
            return str(error) if isinstance(error, UpdateError) else '无法读取安装信息。'
        return None

    def info(self):
        reason = self.capability()
        job = self.job()
        available = bool(self.latest and self.latest != self.running_version)
        return {'current':self.running_version, 'latest':self.latest, 'checked_at':self.checked_at,
                'available':available, 'error':self.error, 'reason':reason,
                'can_update':available and not reason and not self.error and job.get('phase') not in BUSY,
                'repo':self.repo, 'job':public_job(job) if job else None}

    def check(self, force=False):
        with self.guard:
            now = time.time()
            if now < self.next_check and (not force or now-self.checked_at < 30):
                return self.info()
            try:
                repo = repository(self.repo)
                request = urllib.request.Request('https://api.github.com/repos/'+repo+'/commits/main',
                    headers={'Accept':'application/vnd.github.sha', 'User-Agent':'March-Agent-Updater',
                             'X-GitHub-Api-Version':'2022-11-28'})
                with urllib.request.build_opener(NoRedirect()).open(request, timeout=12) as response:
                    value = response.read(512).decode().strip()
                if not SHA.fullmatch(value):
                    raise UpdateError('GitHub 没有返回可验证的版本号。')
                self.latest, self.error = value, None
                self.next_check = now+900
            except (OSError, ValueError, UpdateError):
                self.error = '暂时无法检查 GitHub 更新，请稍后重试。'
                self.next_check = now+300
            self.checked_at = now
            return self.info()

    def acquire_turn(self):
        if not self.enabled or sys.platform != 'linux' or not os.access(self.root, os.W_OK):
            return None
        handle = acquire_lock(self.root)
        if self.job().get('phase') in BUSY:
            handle.close()
            raise UpdateError('项目正在更新，请完成后再发送消息。')
        return handle

    def start(self, target):
        if not SHA.fullmatch(target or '') or target != self.latest or self.error:
            raise UpdateError('版本信息已失效，请重新检查更新。')
        if target == self.running_version:
            raise UpdateError('当前已经是最新版。')
        reason = self.capability()
        if reason:
            raise UpdateError(reason)
        with acquire_lock(self.root, exclusive=True):
            if self.job().get('phase') in BUSY:
                raise UpdateError('更新任务正在进行。')
            directory = state_directory(self.root)
            jobs = directory/'jobs'; jobs.mkdir(mode=0o700, exist_ok=True)
            job_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
            now = time.time()
            job = {'id':job_id, 'root':str(self.root), 'repo':self.repo, 'service':self.service,
                   'port':self.port, 'target':target, 'previous':self.running_version,
                   'phase':'queued', 'message':'等待后台更新…', 'updated_at':now,
                   'expires_at':now+3600, 'token_hash':hashlib.sha256(token.encode()).hexdigest()}
            job_file = jobs/(job_id+'.json')
            write_json(job_file, job); write_json(directory/'active.json', {'id':job_id})
            try:
                command = ['systemd-run', '--user', '--quiet', '--collect', '--no-block',
                           '--unit=march-agent-update-'+job_id, '--property=Type=oneshot',
                           '--property=RuntimeMaxSec=1500', '--working-directory='+str(self.root),
                           str(Path(getattr(sys, '_base_executable', sys.executable)).resolve()),
                           str(self.root/'update_worker.py'), str(job_file)]
                result = subprocess.run(command, capture_output=True, timeout=15)
                if result.returncode:
                    raise UpdateError('无法启动后台更新，请检查 systemd 用户服务。')
            except (OSError, subprocess.TimeoutExpired, UpdateError):
                job.update(phase='failed', message='无法启动后台更新，请检查 systemd 用户服务。')
                write_json(job_file, job)
                raise UpdateError(job['message'])
            return {'job':public_job(job), 'token':token}

    def read_job(self, job_id, token):
        if not re.fullmatch(r'[a-f0-9]{32}', job_id):
            raise UpdateError('更新任务不存在。')
        job = read_json(state_directory(self.root)/'jobs'/(job_id+'.json'))
        if (not job or time.time()>job.get('expires_at', 0) or not token
                or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), job.get('token_hash', ''))):
            raise UpdateError('更新状态访问已失效，请重新登录查看。')
        if job.get('phase') in BUSY and time.time()-job.get('updated_at', 0)>1800:
            job.update(phase='failed', message='更新任务已中断，请刷新后检查后台服务。')
        return public_job(job)
