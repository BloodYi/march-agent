"""Small, shared primitives for authenticated GitHub project updates."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

SHA = re.compile(r'^[0-9a-f]{40}$')
BUSY = {'queued', 'fetching', 'preparing', 'installing', 'restarting', 'verifying', 'rolling_back'}


class UpdateError(RuntimeError):
    pass


def repository(value):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value or ''):
        raise UpdateError('更新来源必须是 GitHub 的 owner/repository。')
    return value


def git(root, *args, binary=False):
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1')
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(root), *args],
                            env=env, capture_output=True, timeout=120)
    if result.returncode:
        raise UpdateError('Git 操作未完成，请检查网络或服务器目录权限。')
    return result.stdout if binary else result.stdout.decode(errors='replace').strip()


def version(root):
    try:
        if Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != Path(root).resolve():
            return None
        value = git(root, 'rev-parse', 'HEAD')
        return value if SHA.fullmatch(value) else None
    except (UpdateError, OSError, subprocess.TimeoutExpired):
        return None


def state_directory(root):
    path = Path(root)/'.march-agent-updater'
    if path.is_symlink():
        raise UpdateError('更新状态目录不能是符号链接。')
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def acquire_lock(root, exclusive=False, wait=False):
    import fcntl
    handle = (state_directory(root)/'update.lock').open('a+b')
    try:
        fcntl.flock(handle.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                    | (0 if wait else fcntl.LOCK_NB))
        return handle
    except BlockingIOError:
        handle.close()
        raise UpdateError('正在回复或更新，请稍后再试。')


def source_path(name):
    path = PurePosixPath(name)
    protected = {'.git', '.venv', 'venv', '.march-agent-updater', '.gemini', 'accounts',
                 'data', 'uploads', 'scratch', '__pycache__', '.access'}
    if (not name or '\\' in name or path.is_absolute() or '..' in path.parts
            or any(part in protected for part in path.parts)
            or path.name == '.env' or (path.name.startswith('.env.') and path.name != '.env.example')
            or path.name.startswith('.antigravity_conversations.json')):
        raise UpdateError('新版包含受保护的本机数据路径，已停止更新。')
    return path


def active_job(root):
    directory = state_directory(root)
    job_id = read_json(directory/'active.json').get('id', '')
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        return {}
    return read_json(directory/'jobs'/(job_id+'.json'))


def public_job(job):
    return {key:job.get(key) for key in ('id', 'phase', 'message', 'target', 'updated_at', 'rolled_back')}
