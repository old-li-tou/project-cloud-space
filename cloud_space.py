"""Project Cloud Space: verified Windows archives on Baidu Netdisk."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import msvcrt
import os
import re
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if sys.argv[1:] == ['status']:
    sys.dont_write_bytecode = True

import requests

from baidu_auth import (BaiduClient, BaiduClientError, default_config_path,
                        authorization_url, load_credentials)

APP = Path(__file__).resolve().parent
STATE = Path(os.environ.get('PROJECT_CLOUD_STATE', APP / '.state'))
REGISTRY = STATE / 'projects.json'
MARKER = '.云空间配置.json'
RECORDS = '.云端记录'
AGENT_GUIDE = 'AGENTS.md'
LAUNCH_SHORTCUT = '启动云空间.lnk'
REMOTE_ROOT = '/项目云空间'
INDEX_DIR = '归档索引'
INDEX_MAX_BYTES = 1024 * 1024
CHUNK = 4 * 1024 * 1024
STATUS_FILE = STATE / 'status.json'
EVENT_LOG = STATE / 'events.jsonl'
HEARTBEAT_MAX_AGE = 10


def timestamp():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        return None if ctypes.get_last_error() == 5 else False
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        return kernel.WaitForSingleObject(handle, 0) == 258
    finally:
        kernel.CloseHandle(handle)


def query_status():
    """Read only: no app launch, lock acquisition, network request or file update."""
    try:
        data = read_json(STATUS_FILE)
        if not isinstance(data, dict) or data.get('schema_version') != 1:
            raise ValueError('Invalid status schema')
        beat = data['heartbeat_epoch']
        if not isinstance(beat, (int, float)):
            raise ValueError('Invalid heartbeat')
    except (OSError, ValueError, KeyError):
        return {'schema_version': 1, 'availability': 'unavailable', 'running': None,
                'reason': '尚无可读取的状态文件，请先启动主程序',
                'status_file': str(STATUS_FILE), 'event_log': str(EVENT_LOG)}, 2
    age = time.time() - beat
    data.update(heartbeat_age_seconds=round(age, 2), checked_at=timestamp(),
                status_file=str(STATUS_FILE), event_log=str(EVENT_LOG))
    alive = process_alive(data.get('pid'))
    if data.get('lifecycle') == 'stopped' or alive is False:
        data.update(availability='stopped', running=False)
        code = 3
    elif age < 0 or age > HEARTBEAT_MAX_AGE:
        data.update(availability='stale', running=None,
                    reason='心跳已过期或系统时间变化，不能把快照当成当前运行状态')
        code = 2
    elif alive is None:
        data.update(availability='unknown', running=None, reason='无法确认进程状态')
        code = 2
    else:
        data.update(availability='running', running=True)
        code = 0
    if data['running'] is not True:
        data['accepting_archives'] = False
    return data, code


class AgentMonitor:
    """GUI-thread status snapshots and a bounded, local JSONL event history."""
    def __init__(self):
        STATE.mkdir(exist_ok=True)
        self.session_id = uuid.uuid4().hex
        self.latest = None
        self.transfers = {}
        self.last_error = None
        self.phase_key = None
        self.monitoring_error = None
        self.handler = RotatingFileHandler(EVENT_LOG, maxBytes=1024*1024, backupCount=3,
                                           encoding='utf-8')
        self.logger = logging.Logger('cloud-space-events-' + self.session_id)
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.INFO)
        self.log('app_started', pid=os.getpid())

    def log(self, event, **fields):
        self.logger.info(json.dumps({'time': timestamp(), 'session_id': self.session_id,
                                     'event': event, **fields}, ensure_ascii=False))

    def progress(self, data):
        if self.latest is None or data['task'] != self.latest['task']:
            self.transfers = {}
        self.latest = dict(data)
        if data['stage'] in ('上传', '下载校验', '下载恢复'):
            self.transfers[data['stage']] = dict(data)
        key = (data['task'], data['stage'], data['active'], data['error'], data.get('detail', ''))
        if key != self.phase_key:
            self.phase_key = key
            self.log('transfer_error' if data['error'] else 'transfer_phase', **data)
        if data['error']:
            self.last_error = {'time': timestamp(), 'file': data['file'], 'stage': data['stage'],
                               'message': data['error'], 'method': data['method'],
                               'http': data['http'], 'errno': data['errno']}

    def snapshot(self, data):
        if data is None:
            return None
        result = dict(data)
        if result['active']:
            result['elapsed'] = time.monotonic() - result.get('started', time.monotonic())
            if time.monotonic() - result.get('last_data_at', 0) > 2:
                result['rate'] = 0
        result['percent'] = (round(100 * result['done'] / result['total'], 2)
                             if result['total'] else None)
        result['rate_bytes_per_second'] = result.pop('rate')
        result['done_bytes'] = result.pop('done')
        result['total_bytes'] = result.pop('total')
        result['elapsed_seconds'] = round(result.pop('elapsed'), 2)
        result.pop('started', None)
        result.pop('last_data_at', None)
        return result

    def heartbeat(self, worker, lifecycle='running'):
        data = {'schema_version': 1, 'session_id': self.session_id, 'pid': os.getpid(),
                'heartbeat_at': timestamp(), 'heartbeat_epoch': time.time(),
                'stale_after_seconds': HEARTBEAT_MAX_AGE, 'lifecycle': lifecycle,
                'pause_requested': worker.paused,
                'worker_running': worker.isRunning(),
                'accepting_archives': lifecycle == 'running' and worker.isRunning() and not worker.paused,
                'failed_count': len(worker.failed) + getattr(worker, 'queue_status', {}).get('failed_count', 0),
                'queue': dict(getattr(worker, 'queue_status', {})),
                'recovery': dict(getattr(worker, 'recovery_status', {})),
                'registered_projects': projects(),
                'latest_transfer': self.snapshot(self.latest),
                'transfers': {key: self.snapshot(value) for key, value in self.transfers.items()},
                'last_error': self.last_error, 'event_log': str(EVENT_LOG)}
        if data['queue']:
            data['queue']['cooldown_remaining_seconds'] = max(0, round(
                data['queue'].get('cooldown_until', 0) - time.time()))
            if data['queue']['cooldown_remaining_seconds'] or data['queue'].get('error'):
                data['accepting_archives'] = False
        try:
            save_json(STATUS_FILE, data)
            self.monitoring_error = None
        except OSError as exc:
            self.monitoring_error = 'Agent 状态写入失败：' + type(exc).__name__

    def close(self, worker):
        self.log('app_stopped')
        self.heartbeat(worker, 'stopped')
        self.handler.close()


class ArchiveError(Exception):
    def __init__(self, message, *, method=None, http=None, errno=None):
        super().__init__(message)
        self.method, self.http, self.errno = method, http, errno


class TransferProgress:
    """One worker's current transfer; emit small snapshots to the Qt main thread."""
    def __init__(self, callback=None):
        self.callback = callback or (lambda value: None)
        self.data = {'task': '', 'file': '', 'stage': '等待文件', 'done': 0,
                     'total': 0, 'rate': 0, 'elapsed': 0, 'active': False,
                     'method': '—', 'http': '—', 'errno': '—', 'error': '', 'detail': ''}
        self.started = time.monotonic()
        self.last_emit = 0
        self.samples = []

    def begin(self, filename):
        self.data.update(task=uuid.uuid4().hex, file=str(filename), method='—',
                         http='—', errno='—', error='')
        self.stage('准备处理')

    def stage(self, name, total=0):
        self.started = time.monotonic()
        self.samples = [(self.started, 0)]
        self.data.update(stage=name, done=0, total=total, rate=0, elapsed=0,
                         active=True, started=self.started, last_data_at=self.started, detail='')
        self.emit()

    def note(self, text):
        self.data['detail'] = text
        self.emit()

    def retry_from(self, confirmed_bytes):
        # A failed attempt is not confirmed progress; avoid counting it twice.
        now = time.monotonic()
        self.samples = [(now, confirmed_bytes)]
        self.data.update(done=confirmed_bytes, rate=0, last_data_at=now)
        self.emit()

    def update(self, done, force=False):
        now = time.monotonic()
        if done > self.data['done']:
            self.data['last_data_at'] = now
        self.data['done'] = done
        self.samples.append((now, done))
        while len(self.samples) > 2 and self.samples[1][0] < now - 1:
            self.samples.pop(0)
        then, before = self.samples[0]
        self.data['rate'] = (done - before) / max(now - then, 0.001)
        if force or now - self.last_emit >= 0.2:
            self.emit()

    def finish(self):
        self.data['active'] = False
        self.data['rate'] = self.data['done'] / max(time.monotonic() - self.started, 0.001)
        self.emit()

    def request(self, method, http='—', errno='—'):
        self.data.update(method=method, http=http, errno=errno)
        self.emit()

    def fail(self, exc):
        method = getattr(exc, 'method', None) or getattr(exc, 'stage', None)
        http = getattr(exc, 'http', None) or getattr(exc, 'http_status', None)
        errno = getattr(exc, 'errno', None) if isinstance(exc, (ArchiveError, BaiduClientError)) else None
        api_code = getattr(exc, 'api_code', None) if isinstance(exc, BaiduClientError) else None
        if method:
            self.data['method'] = method
        if http is not None:
            self.data['http'] = http
        if errno is not None or api_code is not None:
            self.data['errno'] = errno if errno is not None else api_code
        self.data.update(active=False, rate=0, error=safe_error(exc))
        self.emit()

    def emit(self):
        self.last_emit = time.monotonic()
        self.data['elapsed'] = self.last_emit - self.started
        self.callback(dict(self.data))


class UploadBody(io.BytesIO):
    """Measure payload bytes read by requests, excluding multipart headers."""
    def __init__(self, body, payload_start, payload_size, callback):
        super().__init__(body)
        self.payload_start, self.payload_size = payload_start, payload_size
        self.callback = callback

    def __len__(self):
        return self.getbuffer().nbytes

    def read(self, size=-1):
        data = super().read(size)
        self.callback(max(0, min(self.payload_size, self.tell() - self.payload_start)))
        return data


def format_bytes(value):
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if value < 1024 or unit == 'GiB':
            return f'{value:.1f} {unit}'
        value /= 1024


def safe_error(exc):
    if isinstance(exc, (ArchiveError, BaiduClientError)):
        return str(exc)
    if isinstance(exc, PermissionError):
        return '文件被占用或权限不足，原件保留，请关闭相关文件后重试'
    if isinstance(exc, requests.Timeout):
        return '网络请求超时（未收到可用的百度错误码），原件保留'
    if isinstance(exc, requests.ConnectionError):
        return '网络连接失败（未收到可用的百度错误码），原件保留'
    return f'{type(exc).__name__}，未确认安全时不会释放原件'


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temp.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def is_link(path):
    s = Path(path).lstat()
    return stat.S_ISLNK(s.st_mode) or bool(getattr(s, 'st_file_attributes', 0) & 0x400)


def projects():
    return read_json(REGISTRY) if REGISTRY.exists() else []


def runtime_python(windowed=False):
    executable = Path(sys.executable).resolve()
    candidate = executable.with_name('pythonw.exe' if windowed else 'python.exe')
    return candidate if candidate.exists() else executable


def ensure_launch_shortcut(cloud, force=False):
    shortcut = cloud / LAUNCH_SHORTCUT
    if shortcut.exists() and not force:
        return
    env = os.environ.copy()
    env['CLOUD_SHORTCUT_PATH'] = str(shortcut)
    env['CLOUD_SHORTCUT_TARGET'] = str(runtime_python(windowed=True))
    env['CLOUD_SHORTCUT_SCRIPT'] = str(APP / 'cloud_space.py')
    env['CLOUD_SHORTCUT_WORKDIR'] = str(APP)
    command = '''$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($env:CLOUD_SHORTCUT_PATH)
$link.TargetPath = $env:CLOUD_SHORTCUT_TARGET
$link.Arguments = '"' + $env:CLOUD_SHORTCUT_SCRIPT + '"'
$link.WorkingDirectory = $env:CLOUD_SHORTCUT_WORKDIR
$link.Save()
'''
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
        env=env, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode != 0 or not shortcut.is_file():
        raise ArchiveError('启动快捷方式创建失败')


def ensure_agent_guide(project):
    cloud = Path(project['cloud'])
    if not cloud.is_dir() or is_link(cloud):
        raise ArchiveError('云空间目录不存在或是链接，不能写入使用说明')
    ensure_launch_shortcut(cloud)
    guide = cloud / AGENT_GUIDE
    if guide.exists():
        ensure_agent_query_guide(guide)
        return  # Preserve any user-maintained instructions.
    text = f'''# 云空间使用说明（给 Agent）

本目录是项目的自动云端归档入口，不是普通临时目录。
项目根目录：`{project['project']}`
云空间目录：`{project['cloud']}`
主程序启动入口：`{APP / '启动云空间.cmd'}`
也可双击本目录内的 `{LAUNCH_SHORTCUT}`，该快捷方式不会自动上传。

## 上传前必须先打开主程序

1. 在移动、复制或写入任何待归档文件之前，必须先启动“项目云空间”主程序。
   已运行时使用现有窗口，不要重复启动。可双击上述启动入口。
2. 确认窗口正在运行、当前项目已注册、自动上传未暂停。
   只把文件放进这个目录不等于上传；主程序关闭时文件只会留在本地。
3. 仅归档用户已授权、确认不再被当前项目引用或使用的文件。
   不要把活动数据库、正在写入的文件、仍参与构建的源码或整个活动项目放进来。
4. 将选定文件移动到本目录，可保留子目录结构。跨盘拖放通常是复制，
   外部原件不会自动释放；不要因为执行了复制就声称已节省原位置空间。

## 怎样判断完成

- 文件稳定至少 10 秒后，程序逐个上传，并完整下载回读检查大小和 SHA-256。
- 只有校验成功且归档记录保存后，程序才会自动释放本目录中的本地原件。
- 必须看到窗口“已归档并释放本地原件”的结果及对应归档记录，才能报告完成。
  文件消失、等待了一段时间、开始上传或接口返回成功，都不能单独作为完成证据。
- 失败时保留原件；处理网络或文件占用问题后，点击“重试失败文件”。
  Agent 不得绕过校验手动删除源文件来模拟释放空间。
- 保持主程序运行直到任务结束。关闭窗口会等待当前文件处理结束，随后停止监听。

## 查找与恢复

- 在主程序选中当前项目，从归档列表按原相对路径查找文件。
- 选中文件并点击“恢复选中文件（保留云端副本）”。
- 恢复结果位于项目根目录下的“云空间恢复”，并保留原子目录结构。
- 恢复也会检查 SHA-256；同名本地文件不会覆盖。确认恢复成功后再用于项目。
- 不要把恢复目标设为“云空间”，否则可能再次触发归档。

## 本地记录必须保留

- `AGENTS.md`：本说明，程序不会上传或释放。
- `{MARKER}`：当前项目与云端的映射。
- `{RECORDS}/`：云端路径、文件标识、原相对路径、大小和哈希等恢复记录。
- 不要删除、移动或修改上述记录来清理磁盘，也不要手工清理需要保留的云端副本。
- 普通子目录中的文件按个归档；空文件、链接、目录连接和未完成下载文件会保留。
'''
    with guide.open('x', encoding='utf-8', newline='\n') as f:
        f.write(text)
    ensure_agent_query_guide(guide)


def ensure_agent_query_guide(guide):
    if '## 批量队列与自动重试' not in guide.read_text(encoding='utf-8'):
        with guide.open('a', encoding='utf-8', newline='\n') as f:
            f.write('\n' + QUEUE_HELP)
    if '## 云端索引备份与整项目恢复' not in guide.read_text(encoding='utf-8'):
        with guide.open('a', encoding='utf-8', newline='\n') as f:
            f.write('\n' + INDEX_HELP)
    independent_heading = '## 独立运行版 v0.1'
    if independent_heading not in guide.read_text(encoding='utf-8'):
        with guide.open('a', encoding='utf-8', newline='\n') as f:
            f.write(f'\n{independent_heading}\n\n当前程序已使用自己的 Python 环境和授权，不依赖旧 API 案例。\n'
                    f'如果上文有旧路径，以本节为准。状态查询：\n\n'
                    f'```powershell\n& "{runtime_python()}" "{APP / "cloud_space.py"}" status\n```\n'
                    '新电脑从源码运行 setup.cmd，然后 start.cmd，在“网盘授权…”完成自己的账号授权。\n'
                    '凭据仅保存在当前 Windows 用户的 DPAPI 加密文件，不要提交凭据、项目记录或日志。\n')
    location_heading = '## 网盘目录位置'
    if location_heading not in guide.read_text(encoding='utf-8'):
        with guide.open('a', encoding='utf-8', newline='\n') as f:
            f.write('\n' + location_heading + '\n\n云端项目目录统一放在百度网盘根目录的“项目云空间”内，每个项目仍有独立子目录。\n请勿手工移动或改名云端目录；恢复依赖本地“.云端记录”和项目映射。\n')
    heading = '## Agent 无界面查询运行状态'
    if heading in guide.read_text(encoding='utf-8'):
        return
    text = f'''
{heading}

无需接管桌面。PowerShell 执行以下只读命令（输出 UTF-8 JSON）：

```powershell
& '{runtime_python()}' '{APP / 'cloud_space.py'}' status
```

- 命令只读取本地状态，不启动主程序、不联网、不上传或删除文件。
- 退出码 0：进程存活且心跳新鲜；3：已停止；2：状态缺失、损坏、过期或无法确认。
- `running=true` 且 `accepting_archives=true` 才表示程序可接受自动归档。
  `pause_requested` 为暂停请求；正在处理的文件会先完成。
- `latest_transfer` 包含文件、阶段、完成字节、总字节、百分比、速度（字节/秒）、
  耗时（秒）、HTTP 状态和百度错误码。`transfers` 分别保留上传、下载校验或恢复的进度。
- `last_error` 是本次运行最近的传输错误，后续成功不会清除这条历史错误。
- 状态文件：`{STATUS_FILE}`，主程序每秒刷新，超过 10 秒不更新即视为过期。
  直接读文件必须自行检查心跳与进程，优先使用上面的查询命令。
- 持久化日志：`{EVENT_LOG}`，每行一个 JSON；旧日志保存在 `.1`、`.2`、`.3`。
  每个文件最多约 1 MiB，记录有时间和运行会话 ID；按文件路径查询成功、失败和恢复事件。
- 心跳只证明窗口事件循环仍响应，不保证网络正在传输；用阶段、进度和速度一起判断。
- `上传` 到 100% 不等于归档完成；应看到 `归档完成` 或日志中释放原件成功的记录，
  并核对 `.云端记录` 中对应文件的归档记录。旧日志和过期快照不能证明当前任务成功。
- 状态与日志在主程序目录，不在待上传目录；不要将它们放进云空间或当成待归档数据。
'''
    with guide.open('a', encoding='utf-8', newline='\n') as f:
        f.write(text)


def register(project):
    project = Path(project).resolve(strict=True)
    if not project.is_dir():
        raise ArchiveError('请选择项目文件夹')
    for p in projects():
        existing = Path(p['project'])
        if project == existing:
            ensure_agent_guide(p)
            return p
        if project.is_relative_to(existing / '云空间') or existing.is_relative_to(project / '云空间'):
            raise ArchiveError('不能在已监听的云空间内部再注册项目')
    cloud = project / '云空间'
    # Never enroll a pre-existing folder: its contents have not been selected for archiving.
    if cloud.exists():
        raise ArchiveError('这个项目已经有“云空间”目录；为避免误处理，请先另选空测试项目')
    cloud.mkdir()
    ident = uuid.uuid4().hex
    p = {'id': ident, 'project': str(project), 'cloud': str(cloud),
         'remote': REMOTE_ROOT + '/项目云空间-' + project.name[:50] + '-' + ident[:12],
         'remote_ready': False}
    (cloud / RECORDS).mkdir()
    save_json(cloud / MARKER, p)
    ensure_agent_guide(p)
    save_json(REGISTRY, projects() + [p])
    return p


class LockedSource:
    """Open by handle; deny concurrent writers/deleters until verified deletion."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        handle = kernel.CreateFileW(str(self.path), 0x80000000 | 0x10000, 1, None, 3,
                                    0x00200000 | 0x08000000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except Exception:
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(handle)
            raise
        self.file = os.fdopen(fd, 'rb')
        if is_link(self.path) or not stat.S_ISREG(os.fstat(self.file.fileno()).st_mode):
            self.file.close()
            raise ArchiveError('不归档链接、目录连接或特殊文件')
        return self

    def release(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD]
        kernel.SetFileInformationByHandle.restype = wintypes.BOOL
        delete = wintypes.BOOL(True)
        if not kernel.SetFileInformationByHandle(msvcrt.get_osfhandle(self.file.fileno()),
                                                  4, ctypes.byref(delete), ctypes.sizeof(delete)):
            raise ctypes.WinError(ctypes.get_last_error())

    def __exit__(self, *args):
        self.file.close()


def safe_relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ArchiveError('恢复相对路径无效')
    parts = value.split('/')
    for part in parts:
        if (not part or part in ('.', '..') or part.endswith((' ', '.'))
                or any(ord(ch) < 32 or ch in '<>:"|?*' for ch in part)
                or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I)):
            raise ArchiveError('恢复相对路径不安全')
    return Path(*parts)


def file_matches(path, record):
    if not path.is_file() or is_link(path) or path.stat().st_size != record['size']:
        return False
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest() == record['sha256']


def valid_project_remote(remote):
    return (isinstance(remote, str) and remote.startswith(REMOTE_ROOT + '/')
            and len(remote.split('/')) == 3 and remote.rsplit('/', 1)[1] not in ('', '.', '..'))


def validate_index(envelope, remote):
    try:
        if envelope['schema_version'] != 1 or not valid_project_remote(remote):
            raise ValueError()
        project, record = envelope['project'], envelope['record']
        if project['remote'] != remote or project['id'] != record['project_id']:
            raise ValueError()
        for ident in (record['id'], record['project_id']):
            if not re.fullmatch('[0-9a-f]{32}', ident):
                raise ValueError()
        safe_relative(record['relative_path'])
        if (not record['remote_path'].startswith(remote + '/')
                or '/' in record['remote_path'][len(remote)+1:]
                or not record['remote_path'][len(remote)+1:]):
            raise ValueError()
        if (type(record['fs_id']) is not int or record['fs_id'] <= 0
                or type(record['size']) is not int or record['size'] <= 0
                or not re.fullmatch('[0-9a-f]{64}', record['sha256'])
                or not isinstance(record['mtime'], (int, float)) or not 0 <= record['mtime'] <= 32503680000
                or not isinstance(record['archived_at'], str)
                or record['verification'] != 'full-download-sha256'):
            raise ValueError()
        return record
    except (KeyError, TypeError, ValueError):
        raise ArchiveError('云端索引格式或字段无效') from None


class Cloud:
    def __init__(self, progress=None):
        self.auth = BaiduClient(default_config_path())
        self.session = self.auth.session
        self.progress = progress or TransferProgress()
        self.upload_route = None

    def close(self):
        self.auth.close()

    def api(self, method, data=None, extra=None, files=None, multimedia=False, upload=False,
            upload_callback=None, upload_origin=None):
        if upload and not upload_origin:
            raise ArchiveError('上传前必须先获取当前任务的上传域名')
        url = (upload_origin + '/rest/2.0/pcs/superfile2' if upload else
               'https://d.pcs.baidu.com/rest/2.0/pcs/file' if method == 'locateupload' else
               'https://pan.baidu.com/rest/2.0/xpan/' + ('multimedia' if multimedia else 'file'))
        self.progress.request(method)
        params = {'method': method, 'access_token': self.auth.get_access_token(), **(extra or {})}
        headers = None
        body = None
        if files and upload_callback is not None:
            # Let requests generate its normal multipart body, then observe its reads.
            prepared = requests.Request('POST', url, files=files).prepare()
            payload_size = len(files['file'][1])
            payload_start = prepared.body.index(b'\r\n\r\n') + 4
            body = UploadBody(prepared.body, payload_start, payload_size, upload_callback)
            data = body
            files = None
            headers = {'Content-Type': prepared.headers['Content-Type']}
        with self.session.request('POST' if data is not None or files else 'GET', url,
                                  params=params, data=data, files=files, headers=headers,
                                  timeout=(10, 60)) as r:
            self.progress.request(method, r.status_code)
            try:
                result = r.json()
            except ValueError:
                raise ArchiveError(f'{method}: HTTP {r.status_code}，响应不是有效 JSON',
                                   method=method, http=r.status_code) from None
            if not isinstance(result, dict):
                raise ArchiveError(f'{method}: 响应格式异常', method=method, http=r.status_code)
            code = result.get('errno', result.get('error_code'))
            self.progress.request(method, r.status_code, code if code is not None else '未提供')
            if r.status_code != 200:
                raise ArchiveError(f'{method}: HTTP {r.status_code}，百度错误码 {code if code is not None else "未提供"}',
                                   method=method, http=r.status_code, errno=code)
        if code not in (None, 0):
            hint = '（接口限流）' if code in (20012, 31034) else ''
            raise ArchiveError(f'{method}: 百度错误码 {code}{hint}',
                               method=method, http=200, errno=code)
        return result

    def locate_upload(self, remote, uploadid):
        self.progress.note('获取当前任务的上传地址')
        data = self.api('locateupload', extra={'appid': 250528, 'path': remote,
                                              'uploadid': uploadid, 'upload_version': '2.0'})
        # Only use HTTPS hosts returned by the official locator; never copy example hosts.
        for item in data.get('servers', []):
            if not isinstance(item, dict) or not isinstance(item.get('server'), str):
                continue
            address = urlsplit(item['server'].strip())
            hostname = address.hostname or ''
            if (address.scheme != 'https' or not hostname.endswith('.pcs.baidu.com')
                    or address.username or address.password or address.port not in (None, 443)
                    or address.path not in ('', '/') or address.query or address.fragment):
                continue
            expiry = data.get('expire')
            if not isinstance(expiry, (int, float)) or isinstance(expiry, bool) or expiry <= 0:
                raise ArchiveError('上传域名有效期异常，未上传分片')
            self.upload_route = {'origin': 'https://' + address.netloc, 'path': remote,
                                 'uploadid': uploadid, 'expires_at': time.monotonic() + expiry}
            return
        raise ArchiveError('百度未返回可用的 HTTPS 上传域名')

    @staticmethod
    def retryable_upload_error(exc):
        if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
            return True
        if isinstance(exc, ArchiveError):
            if exc.errno in (20012, 31034, 9013) or exc.http == 429:
                return False
            return exc.errno == -11015 or exc.http in (408, 500, 502, 503, 504)
        return False

    def upload_part(self, remote, uploadid, index, chunk, expected_md5, confirmed_bytes):
        for attempt in range(3):
            try:
                route = self.upload_route
                if (route is None or route['path'] != remote or route['uploadid'] != uploadid
                        or time.monotonic() >= route['expires_at']):
                    self.locate_upload(remote, uploadid)
                self.progress.note(f'第 {index + 1} 个分片，第 {attempt + 1}/3 次尝试')
                part = self.api('upload', extra={'path': remote, 'type': 'tmpfile',
                                                'uploadid': uploadid, 'partseq': index},
                                files={'file': ('chunk', chunk, 'application/octet-stream')}, upload=True,
                                upload_origin=self.upload_route['origin'],
                                upload_callback=lambda count: self.progress.update(confirmed_bytes + count))
                if part.get('md5') != expected_md5:
                    raise ArchiveError('上传分片校验失败')
                return part
            except (requests.RequestException, ArchiveError) as exc:
                if attempt == 2 or not self.retryable_upload_error(exc):
                    raise
                delay = (2, 5)[attempt]
                self.upload_route = None
                self.progress.retry_from(confirmed_bytes)
                self.progress.note(f'分片 {index + 1} 失败：{safe_error(exc)}；'
                                   f'{delay} 秒后重新获取地址并重试（{attempt + 2}/3）')
                time.sleep(delay)

    def ensure_remote_directory(self, path):
        parent, name = path.rsplit('/', 1)
        offset = 0
        while True:
            entries = self.api('list', extra={'dir': parent or '/', 'start': offset, 'limit': 1000})['list']
            for entry in entries:
                if entry['path'] == path:
                    if entry.get('isdir') != 1:
                        raise ArchiveError('云端目标位置已被同名文件占用：' + path)
                    return entry
            if len(entries) < 1000:
                break
            offset += len(entries)
        created = self.api('create', {'path': path, 'isdir': 1, 'rtype': 0})
        if created.get('path') != path or created.get('isdir') != 1:
            raise ArchiveError('云端目录创建结果不匹配，停止上传')
        return created

    def ensure_remote(self, project):
        marker = Path(project['cloud']) / MARKER
        current = read_json(marker)
        if current['remote_ready']:
            return
        if current['remote'].startswith(REMOTE_ROOT + '/'):
            self.ensure_remote_directory(REMOTE_ROOT)
        self.ensure_remote_directory(current['remote'])
        current['remote_ready'] = True
        save_json(marker, current)

    def list_all(self, remote):
        entries, offset = [], 0
        while True:
            batch = self.api('list', extra={'dir': remote, 'start': offset, 'limit': 1000})['list']
            entries.extend(batch)
            if len(batch) < 1000:
                return entries
            offset += len(batch)

    def backup_index(self, project, record):
        """Upload deterministic, immutable metadata and verify every byte before release."""
        fields = ('id', 'project_id', 'relative_path', 'remote_path', 'fs_id', 'size',
                  'sha256', 'mtime', 'archived_at', 'verification')
        envelope = {'schema_version': 1,
                    'project': {'id': project['id'], 'name': Path(project['project']).name,
                                'remote': project['remote']},
                    'record': {key: record[key] for key in fields}}
        validate_index(envelope, project['remote'])
        payload = json.dumps(envelope, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':')).encode('utf-8')
        if len(payload) > INDEX_MAX_BYTES:
            raise ArchiveError('归档索引过大')
        digest = hashlib.sha256(payload).hexdigest()
        folder = project['remote'] + '/' + INDEX_DIR
        self.progress.stage('备份云端索引')
        self.ensure_remote_directory(folder)
        remote = folder + '/' + record['id'] + '-' + digest + '.json'
        created = next((e for e in self.list_all(folder) if e['path'] == remote), None)
        if created is None:
            md5 = hashlib.md5(payload).hexdigest()
            base = {'path': remote, 'size': len(payload), 'isdir': 0, 'rtype': 0,
                    'block_list': json.dumps([md5])}
            pre = self.api('precreate', {**base, 'autoinit': 1})
            if pre.get('return_type') == 2:
                created = next((e for e in self.list_all(folder) if e['path'] == remote), None)
            else:
                indexes = pre.get('block_list')
                if indexes not in ([], [0]):
                    raise ArchiveError('索引分片响应异常')
                if indexes:
                    self.upload_part(remote, pre['uploadid'], 0, payload, md5, 0)
                created = self.api('create', {**base, 'uploadid': pre['uploadid']})
        if not created or created.get('path') != remote or created.get('size') != len(payload):
            raise ArchiveError('云端索引创建结果不匹配')
        index = {'remote_path': remote, 'fs_id': created['fs_id'], 'size': len(payload), 'sha256': digest}
        self.download(index, stage='云端索引回读校验')
        return index

    def backfill_indexes(self, project, notify=lambda text: None):
        records = sorted((Path(project['cloud']) / RECORDS).glob('*.json'))
        done = 0
        for path in records:
            record = read_json(path)
            self.progress.begin(record['relative_path'])
            # Old archives were fully verified when created. Recheck identity before publishing metadata.
            result = self.api('filemetas', extra={'fsids': json.dumps([record['fs_id']])}, multimedia=True)
            entries = result.get('list', [])
            if (len(entries) != 1 or entries[0].get('path') != record['remote_path']
                    or entries[0].get('size') != record['size'] or entries[0].get('fs_id') != record['fs_id']):
                raise ArchiveError('旧归档云端标识或大小不匹配：' + record['relative_path'])
            record['cloud_index'] = self.backup_index(project, record)
            save_json(path, record)
            done += 1
            notify(f'索引备份已核对：{done}/{len(records)} {record["relative_path"]}')
        return {'project': project['remote'], 'verified_indexes': done}

    def cloud_projects(self):
        return [{'remote': e['path'], 'name': e['path'].rsplit('/', 1)[1]}
                for e in self.list_all(REMOTE_ROOT) if e.get('isdir') == 1]

    def read_project_indexes(self, remote):
        if not valid_project_remote(remote):
            raise ArchiveError('项目必须位于 /项目云空间 的直接子目录')
        folder = remote + '/' + INDEX_DIR
        records, errors = {}, []
        for entry in self.list_all(folder):
            try:
                name = entry['path'].rsplit('/', 1)[1]
                match = re.fullmatch(r'([0-9a-f]{32})-([0-9a-f]{64})\.json', name)
                if (not match or entry.get('isdir') != 0 or entry['size'] > INDEX_MAX_BYTES
                        or entry['path'] != folder + '/' + name):
                    raise ArchiveError('索引文件名、类型或大小不合法')
                buffer = io.BytesIO()
                self.download({'remote_path': entry['path'], 'fs_id': entry['fs_id'],
                               'size': entry['size'], 'sha256': match[2]}, buffer, stage='读取云端索引')
                envelope = json.loads(buffer.getvalue())
                record = validate_index(envelope, remote)
                if record['id'] != match[1]:
                    raise ArchiveError('索引标识与文件名不匹配')
                if record['id'] in records and records[record['id']] != record:
                    raise ArchiveError('同一归档标识存在冲突索引')
                records[record['id']] = record
            except Exception as exc:
                if ArchiveQueue.limited(exc) or Cloud.retryable_upload_error(exc):
                    raise
                errors.append({'index': entry.get('path'), 'error': safe_error(exc)})
        return list(records.values()), errors

    def recover_project(self, remote, destination, notify=lambda text: None,
                        should_continue=lambda: True, status=lambda data: None):
        destination = Path(destination).absolute()
        for project in projects():
            cloud = Path(project['cloud']).resolve()
            if destination.resolve().is_relative_to(cloud) or cloud.is_relative_to(destination.resolve()):
                raise ArchiveError('请将恢复位置选在所有云空间监听目录之外')
        for path in [destination, *destination.parents]:
            if path.exists() and is_link(path):
                raise ArchiveError('恢复位置包含链接')
        self.progress.begin(remote)
        records, errors = self.read_project_indexes(remote)
        if not records and not errors:
            raise ArchiveError('云端项目没有可用归档索引')
        report = {'remote': remote, 'destination': str(destination), 'total': len(records),
                  'completed': [], 'errors': errors, 'state': 'recovering', 'started_at': timestamp()}
        report_dir = STATE / 'recovery'
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / (hashlib.sha256((remote + str(destination)).encode()).hexdigest()[:24] + '.json')
        def checkpoint():
            save_json(report_path, report)
            status({'remote': remote, 'destination': str(destination), 'total': len(records),
                    'completed': len(report['completed']), 'failed': len(report['errors']),
                    'state': report['state'], 'report': str(report_path)})
        checkpoint()
        # Latest version keeps original relative path; earlier versions get a stable recovery suffix.
        for record in sorted(records, key=lambda r: (r['archived_at'], r['id']), reverse=True):
            if not should_continue():
                report['state'] = 'stopped'
                break
            try:
                for attempt in range(3):
                    try:
                        target = self.restore({}, record, root=destination, resume=True)
                        break
                    except Exception as exc:
                        if not Cloud.retryable_upload_error(exc) or attempt == 2:
                            raise
                        time.sleep((2, 5)[attempt])
                report['completed'].append({'id': record['id'], 'path': str(target), 'sha256': record['sha256']})
                notify(f'项目恢复：{len(report["completed"])}/{len(records)} {record["relative_path"]}')
            except Exception as exc:
                report['errors'].append({'file': record['relative_path'], 'error': safe_error(exc)})
                notify('恢复失败：' + record['relative_path'] + '；' + safe_error(exc))
                if ArchiveQueue.limited(exc):
                    report['state'] = 'rate_limited'
                    checkpoint()
                    raise
            checkpoint()
        if report['state'] == 'recovering':
            report['state'] = 'partial' if report['errors'] else 'complete'
        checkpoint()
        return report

    def download(self, record, output=None, *, stage=None):
        self.progress.stage('获取恢复下载地址' if output is not None else '获取校验下载地址')
        data = self.api('filemetas', extra={'fsids': json.dumps([record['fs_id']]), 'dlink': 1},
                        multimedia=True)
        entries = data.get('list', [])
        if len(entries) != 1 or entries[0].get('fs_id') != record['fs_id']:
            raise ArchiveError('云端文件信息不完整')
        meta = entries[0]
        if meta.get('path') != record['remote_path']:
            raise ArchiveError('云端文件路径与记录不匹配')
        if meta.get('size') != record['size']:
            raise ArchiveError('云端文件大小不匹配')
        link = urlsplit(meta['dlink'])
        if link.scheme != 'https':
            raise ArchiveError('下载地址不是 HTTPS，停止校验')
        query = dict(parse_qsl(link.query))
        query['access_token'] = self.auth.get_access_token()
        url = urlunsplit((link.scheme, link.netloc, link.path, urlencode(query), link.fragment))
        sha = hashlib.sha256()
        size = 0
        self.progress.stage(stage or ('下载恢复' if output is not None else '下载校验'), record['size'])
        self.progress.request('download')
        with self.session.get(url, stream=True, timeout=(10, 60),
                              headers={'User-Agent': 'pan.baidu.com', 'Accept-Encoding': 'identity'}) as r:
            if r.status_code != 200:
                code = None
                if 'json' in r.headers.get('Content-Type', '').lower():
                    try:
                        error_data = r.json()
                        code = error_data.get('errno', error_data.get('error_code'))
                    except (ValueError, AttributeError):
                        pass
                self.progress.request('download', r.status_code, code if code is not None else '—')
                raise ArchiveError(f'下载失败：HTTP {r.status_code}，百度错误码 {code if code is not None else "未提供"}',
                                   method='download', http=r.status_code, errno=code)
            self.progress.request('download', r.status_code, '不适用（文件流）')
            for chunk in r.iter_content(64 * 1024):
                size += len(chunk)
                if size > record['size']:
                    raise ArchiveError('下载大小超过记录值')
                sha.update(chunk)
                if output is not None:
                    output.write(chunk)
                self.progress.update(size)
        if size != record['size'] or sha.hexdigest() != record['sha256']:
            raise ArchiveError('下载回读内容不一致，原件保留')
        self.progress.update(size, force=True)
        self.progress.finish()

    def archive(self, project, path):
        self.progress.begin(path)
        cloud = Path(project['cloud'])
        relative = path.relative_to(cloud)
        if any(is_link(parent) for parent in [path, *path.parents] if parent.is_relative_to(cloud)):
            raise ArchiveError('不处理链接路径')
        self.ensure_remote(project)
        with LockedSource(path) as source:
            stream = source.file
            size = os.fstat(stream.fileno()).st_size
            if size == 0:
                raise ArchiveError('第一版暂保留空文件，不释放')
            sha = hashlib.sha256()
            blocks = []
            self.progress.stage('计算本地哈希', size)
            hashed = 0
            while chunk := stream.read(CHUNK):
                sha.update(chunk)
                blocks.append(hashlib.md5(chunk).hexdigest())
                hashed += len(chunk)
                self.progress.update(hashed)
            self.progress.finish()
            for pending_path in sorted((cloud / RECORDS).glob('*.json')):
                pending = read_json(pending_path)
                if (pending['relative_path'] == relative.as_posix()
                        and pending['size'] == size and pending['sha256'] == sha.hexdigest()):
                    self.download(pending)
                    pending['cloud_index'] = self.backup_index(project, pending)
                    save_json(pending_path, pending)
                    self.progress.stage('云端索引已核对，释放本地原件')
                    source.release()
                    self.progress.stage('归档完成')
                    self.progress.finish()
                    return pending
            ident = uuid.uuid4().hex
            remote = project['remote'] + '/' + ident[:12] + '-' + path.name
            base = {'path': remote, 'size': size, 'isdir': 0, 'rtype': 0,
                    'block_list': json.dumps(blocks)}
            self.progress.stage('准备上传')
            pre = self.api('precreate', {**base, 'autoinit': 1})
            if pre.get('return_type') == 2:
                self.progress.stage('秒传成功（无需传输）')
                self.progress.finish()
                listing = self.api('list', extra={'dir': project['remote'], 'order': 'time',
                                                 'desc': 1, 'limit': 1000})
                created = next((x for x in listing['list'] if x['path'] == remote), None)
                if created is None:
                    raise ArchiveError('秒传后未定位到文件；本地保留')
            else:
                indexes = pre.get('block_list')
                if not isinstance(indexes, list):
                    raise ArchiveError('缺少待上传分片列表')
                if any(type(index) is not int or not 0 <= index < len(blocks) for index in indexes):
                    raise ArchiveError('上传分片序号异常')
                required = sum(min(CHUNK, size - index * CHUNK) for index in indexes)
                self.progress.stage('上传', required)
                uploaded = 0
                self.upload_route = None
                for index in indexes:
                    if type(index) is not int or not 0 <= index < len(blocks):
                        raise ArchiveError('上传分片序号异常')
                    stream.seek(index * CHUNK)
                    chunk = stream.read(CHUNK)
                    self.upload_part(remote, pre['uploadid'], index, chunk, blocks[index], uploaded)
                    uploaded += len(chunk)
                    self.progress.update(uploaded, force=True)
                self.progress.finish()
                self.progress.stage('云端合并文件')
                created = self.api('create', {**base, 'uploadid': pre['uploadid']})
            if created.get('path') != remote or created.get('size') != size:
                raise ArchiveError('云端创建结果不匹配，本地保留')
            record = {'id': ident, 'project_id': project['id'], 'relative_path': relative.as_posix(),
                      'remote_path': remote, 'fs_id': created['fs_id'], 'size': size,
                      'sha256': sha.hexdigest(), 'mtime': os.fstat(stream.fileno()).st_mtime,
                      'archived_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                      'verification': 'full-download-sha256'}
            # Hash download in memory chunks; do not allocate a second local file.
            self.download(record)
            self.progress.stage('保存本地索引')
            record_path = cloud / RECORDS / (ident + '.json')
            save_json(record_path, record)
            record['cloud_index'] = self.backup_index(project, record)
            save_json(record_path, record)
            self.progress.stage('云端索引已核对，释放本地原件')
            # Delete this same locked handle, never a later path replacement.
            source.release()
        self.progress.stage('归档完成')
        self.progress.finish()
        return record

    def restore(self, project, record, *, root=None, resume=False):
        self.progress.begin(record['relative_path'])
        relative = safe_relative(record['relative_path'])
        root = Path(root) if root is not None else Path(project['project']) / '云空间恢复'
        target = root / relative
        if not target.resolve().is_relative_to(root.resolve()):
            raise ArchiveError('恢复路径越界')
        for parent in [target, *target.parents]:
            if parent.exists() and is_link(parent):
                raise ArchiveError('恢复目录包含链接')
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if resume and file_matches(target, record):
                self.progress.stage('已恢复且校验一致，跳过')
                self.progress.finish()
                return target
            suffix = record['id'] if resume else uuid.uuid4().hex[:8]
            target = target.with_name(target.stem + '-恢复-' + suffix + target.suffix)
            if resume and target.exists():
                if not is_link(target) and file_matches(target, record):
                    self.progress.stage('已恢复且校验一致，跳过')
                    self.progress.finish()
                    return target
                raise ArchiveError('恢复目标存在不同内容，保留现有文件：' + str(target))
        temp = target.with_name(target.name + '.part-' + uuid.uuid4().hex[:8])
        try:
            with temp.open('xb') as f:
                self.download(record, f)
                f.flush()
                os.fsync(f.fileno())
            os.rename(temp, target)  # Windows fails rather than overwriting existing destination.
            os.utime(target, (time.time(), record['mtime']))
        finally:
            temp.unlink(missing_ok=True)
        self.progress.stage('恢复完成')
        self.progress.finish()
        return target


def candidates(project):
    cloud = Path(project['cloud'])
    if is_link(cloud):
        raise ArchiveError('云空间目录已变成链接，停止处理')
    marker = read_json(cloud / MARKER)
    if marker['id'] != project['id'] or marker['remote'] != project['remote']:
        raise ArchiveError('云空间配置不匹配')
    for root, dirs, files in os.walk(cloud, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(root) == cloud and d == RECORDS)
                   and not is_link(Path(root) / d)]
        for name in files:
            path = Path(root) / name
            if (path.parent == cloud and name.casefold() in
                    (MARKER.casefold(), AGENT_GUIDE.casefold(), LAUNCH_SHORTCUT.casefold())) or is_link(path):
                continue
            if path.suffix.lower() in ('.part', '.tmp', '.crdownload'):
                continue
            if path.is_file():
                yield path


INDEX_HELP = '## 云端索引备份与整项目恢复\n\n每个云端项目目录内的“归档索引”保存独立 JSON 索引（格式版本 1），含项目名称/标识、\n原始相对路径、云端路径/文件标识、大小、SHA-256、归档时间及修改时间。\n索引不包含本机绝对路径或网盘授权令牌；文件名带内容 SHA-256，读取时会核对。\n完整文件回读校验、本地索引保存、云端索引上传及回读核对全部成功后，才释放本地原件。\n索引备份失败保留原件；相同路径、大小和哈希的后续尝试会复用已有云端文件并重新核对。\n不要删除云端“归档索引”或手工修改索引。文件名中的哈希用于发现损坏，不是数字签名。\n\n新电脑恢复：\n1. 新电脑安装 Python 3.12，解压本工具源码后运行 setup.cmd，再运行 start.cmd。\n   点击“网盘授权…”填写自己的百度应用 App Key 和 Secret Key，打开百度页面授权同一账号，\n   将授权码粘贴回窗口并提交。无需原电脑的 projects.json 或 .云端记录，也无需旧 API 案例。\n   账号凭据由 Windows DPAPI 加密保存在当前用户目录；换电脑需要重新授权。\n2. 点击“从云端恢复整个项目…”，选择云端项目，再选择本地恢复父目录。\n   文件恢复到该父目录下的项目子目录，按原相对路径还原，不需要先添加本地项目。\n3. 每个文件下载后核对大小和 SHA-256。再次选择同一位置恢复，会重新核对已存在文件；\n   内容一致则跳过，不同则使用带归档 ID 的后缀保存，绝不覆盖已有文件。\n   同一路径的多个归档版本：较新的优先使用原名，其他版本保留后缀。\n4. 普通网络失败每个恢复文件最多尝试 3 次；损坏索引、权限和校验错误会记录失败。\n   遇限流停止本次整项目恢复；待恢复后再次选择同一项目和同一位置即可继续。\n   关闭窗口会完成当前文件后停止整项目恢复；恢复目录不能位于任何正在监听的云空间内。\n5. 结果会明确区分 complete / partial / stopped / rate_limited；只认 complete 为全部成功。\n   恢复报告保存在程序 .state/recovery，包含成功文件、失败原因及实际保存路径。\n   恢复范围仅包括已归档且有有效索引的文件，不包含未归档资料或空目录；压缩包按文件恢复，不自动解压。\n\n既有索引补齐：点击“补齐选中项目的云端索引”，核对文件标识和大小后上传索引、回读核对。\n重复执行可核对已有索引，不重复上传大文件。云端索引与数据都在同一网盘账号，不是异地第二副本。\n\nAgent 无需接管桌面，也可用 CLI（这些联网命令需要先正常关闭 GUI，避免并发操作）：\n  python cloud_space.py cloud-projects\n  python cloud_space.py backup-indexes\n  python cloud_space.py recover-project --remote "/项目云空间/具体项目目录" --destination "E:\\找回项目"\n用实际 Python 和脚本绝对路径替换示例。recover-project 的 destination 是最终项目目录。\nstdout 输出 JSON，stderr 输出进度；退出码 0 表示完成，1 表示失败/部分完成。\n上述命令需要网盘授权；status 仍是只读本地状态，不联网。\nGUI 的 status.recovery 显示整项目恢复总数、完成数、失败数、状态及报告路径。\n命令行恢复结果以退出码和最终 JSON/恢复报告为准，不以 GUI 心跳判断。\n'


QUEUE_HELP = """## 批量队列与自动重试

可以一次拖入 34 个或更多文件；全部登记后逐个上传、完整下载校验，再释放原件。
文件稳定 10 秒才开始。网络类失败每个文件最多尝试 3 次（首次 + 2 次重试），
整文件重试至少间隔 30 秒，期间可处理其他文件；每个分片原有的 3 次尝试仍保留。
达到上限后保留原件，标记失败并继续其他文件；权限、授权、完整性错误不自动重试。
收到 HTTP 429 或百度 20012、31034、9013 后，整个上传队列等待 70 分钟。
限流不消耗文件尝试次数；到期试传，如仍限流，再等待 70 分钟。等待时间不是解封保证。
队列和等待截止时间保存在主程序 .state/upload_queue.json，重启后继续；不要手工编辑。
关闭程序不会自动唤醒，需保持主程序运行或到时重新打开。电脑休眠期间也不会上传。
重启后恢复的是文件队列，不是跨重启的分片断点续传；未完成文件可能重新上传。
“重试失败文件”可重置失败文件的尝试次数，但不会跳过限流等待。
Agent 的 status 输出含 queue：items（路径、状态、attempts、last_error、next_attempt_at）、
waiting_count、failed_count、cooldown_until、cooldown_remaining_seconds、cooldown_reason。
items 状态为 pending（排队/待重试）、uploading（正在处理）、failed（需人工处理）。
限流期间 accepting_archives=false，但仍会登记新拖入的文件；不要据此重复搬运文件。
上传 100% 不代表完成；只有完整回读校验成功并保存归档记录，才会释放原件。
"""


class ArchiveQueue:
    """Single worker owns mutations; GUI receives immutable snapshots."""
    def __init__(self):
        self.path = STATE / 'upload_queue.json'
        self.data = (read_json(self.path) if self.path.exists() else
                     {'version': 1, 'items': {}, 'cooldown_until': 0, 'cooldown_reason': ''})
        if self.data.get('version') != 1 or not isinstance(self.data.get('items'), dict):
            raise ArchiveError('队列文件损坏，请保留文件并检查，已停止自动上传')
        for item in self.data['items'].values():
            if item['state'] == 'uploading':
                item['state'] = 'failed' if item['attempts'] >= 3 else 'pending'
                item['last_error'] = '上次运行中断，原件仍在时重新处理'
        self.save()

    def save(self):
        save_json(self.path, self.data)

    def snapshot(self):
        items = [dict(i) for i in self.data['items'].values()]
        return {'items': items,
                'waiting_count': sum(i['state'] == 'pending' for i in items),
                'failed_count': sum(i['state'] == 'failed' for i in items),
                'active_count': sum(i['state'] == 'uploading' for i in items),
                'cooldown_until': self.data['cooldown_until'],
                'cooldown_remaining_seconds': max(0, round(self.data['cooldown_until'] - time.time())),
                'cooldown_reason': self.data['cooldown_reason']}

    def discover(self, found):
        # Only remove absent files from projects whose scan completed successfully.
        changed = False
        for project_id, entries in found.items():
            for key, item in list(self.data['items'].items()):
                if item['project_id'] == project_id and key not in entries:
                    del self.data['items'][key]
                    changed = True
            for key, signature in entries.items():
                old = self.data['items'].get(key)
                if old is None or old['signature'] != signature:
                    self.data['items'][key] = {'path': key, 'project_id': project_id,
                        'signature': signature, 'state': 'pending', 'attempts': 0,
                        'next_attempt_at': 0, 'last_error': ''}
                    changed = True
        if changed:
            self.save()

    def ready(self, eligible):
        if self.data['cooldown_until'] > time.time():
            return None
        return next((i for i in self.data['items'].values()
                     if i['path'] in eligible and i['state'] == 'pending'
                     and i['next_attempt_at'] <= time.time()), None)

    def start(self, item):
        item['attempts'] += 1
        item['state'] = 'uploading'
        self.save()  # Never start an unrecorded attempt.

    def complete(self, item):
        del self.data['items'][item['path']]
        self.save()

    @staticmethod
    def limited(exc):
        return (isinstance(exc, (ArchiveError, BaiduClientError)) and
                (getattr(exc, 'http', None) == 429 or
                 getattr(exc, 'errno', None) in (20012, 31034, 9013)))

    def cooldown(self, exc):
        self.data['cooldown_until'] = time.time() + 70 * 60
        self.data['cooldown_reason'] = safe_error(exc)

    def fail(self, item, exc):
        item['last_error'] = safe_error(exc)
        if self.limited(exc):
            self.cooldown(exc)
            item['attempts'] = max(0, item['attempts'] - 1)
            item['state'] = 'pending'
            item['next_attempt_at'] = self.data['cooldown_until']
        elif Cloud.retryable_upload_error(exc) and item['attempts'] < 3:
            item['state'] = 'pending'
            item['next_attempt_at'] = time.time() + 30
        else:
            item['state'] = 'failed'
        self.save()

    def retry_failed(self):
        for item in self.data['items'].values():
            if item['state'] == 'failed':
                item.update(state='pending', attempts=0, next_attempt_at=0, last_error='')
        self.save()


def run_gui():
    from PySide6.QtCore import QThread, Signal, Qt, QTimer
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                                   QPushButton, QLabel, QListWidget, QTreeWidget, QTreeWidgetItem,
                                   QFileDialog, QMessageBox, QPlainTextEdit, QProgressBar, QInputDialog, QDialog, QFormLayout, QLineEdit)
    import queue

    class Worker(QThread):
        message = Signal(str)
        changed = Signal()
        progress = Signal(dict)
        scanned = Signal(list)
        def __init__(self):
            super().__init__()
            self.commands = queue.Queue()
            self.running = True
            self.paused = False
            self.seen = {}
            self.failed = {}
            self.queue_status = {}
            self.recovery_status = {}

        def run(self):
            client = Cloud(TransferProgress(self.progress.emit))
            try:
                archive_queue = ArchiveQueue()
                self.queue_status = archive_queue.snapshot()
                while self.running:
                    try:
                        command = self.commands.get_nowait()
                    except queue.Empty:
                        command = None
                    if command == 'retry':
                        archive_queue.retry_failed()
                        self.failed.clear()
                        self.message.emit('已重置失败文件的尝试次数；限流等待仍保留')
                    elif isinstance(command, dict):
                        try:
                            if command['kind'] == 'authorize':
                                client.auth.authorize(**command['credentials'])
                                self.message.emit('网盘授权成功，凭据已加密保存在当前 Windows 用户目录')
                                command['credentials'].clear()
                            elif command['kind'] == 'scan':
                                client.progress.begin('云端项目')
                                client.progress.stage('扫描云端项目')
                                self.scanned.emit(client.cloud_projects())
                                client.progress.finish()
                            elif command['kind'] == 'recover':
                                self.recovery_status = {'state': 'reading_indexes', 'remote': command['remote']}
                                def recovery_status(data):
                                    self.recovery_status = data
                                report = client.recover_project(command['remote'], command['destination'],
                                    self.message.emit, lambda: self.running, recovery_status)
                                self.message.emit(f"整项目恢复结束：{report['state']}；成功 {len(report['completed'])}，失败 {len(report['errors'])}")
                            elif command['kind'] == 'backup_indexes':
                                result = client.backfill_indexes(command['project'], self.message.emit)
                                self.message.emit(f"项目索引备份完成：{result['verified_indexes']} 条已回读核对")
                        except Exception as exc:
                            client.progress.fail(exc)
                            self.message.emit('操作未完成：' + safe_error(exc))
                            if command['kind'] == 'authorize':
                                command['credentials'].clear()
                            if command['kind'] == 'recover':
                                self.recovery_status = {**self.recovery_status, 'state': 'failed', 'error': safe_error(exc)}
                            if archive_queue.limited(exc):
                                archive_queue.cooldown(exc)
                                archive_queue.save()
                    elif command:
                        project, record = command
                        try:
                            self.message.emit('正在恢复：' + record['relative_path'])
                            target = client.restore(project, record)
                            self.message.emit('恢复完成：' + str(target))
                        except Exception as exc:
                            client.progress.fail(exc)
                            self.message.emit('恢复失败：' + safe_error(exc))
                            if archive_queue.limited(exc):
                                archive_queue.cooldown(exc)
                                archive_queue.save()
                    found, eligible = {}, set()
                    registered = {p['id']: p for p in projects()}
                    for project in registered.values():
                        try:
                            entries = {}
                            for path in candidates(project):
                                if not self.running:
                                    break
                                st = path.stat()
                                key = str(path)
                                signature = [st.st_size, st.st_mtime_ns]
                                entries[key] = signature
                                prior = self.seen.get(key)
                                if prior is None or prior[0] != signature:
                                    self.seen[key] = (signature, time.monotonic())
                                elif time.monotonic() - prior[1] >= 10:
                                    eligible.add(key)
                            if self.running:
                                found[project['id']] = entries
                        except Exception as exc:
                            eligible.difference_update(entries)
                            if project['cloud'] not in self.failed:
                                self.failed[project['cloud']] = True
                                self.message.emit('项目暂不可用：' + project['cloud'] + '；' + safe_error(exc))
                    archive_queue.discover(found)
                    self.queue_status = archive_queue.snapshot()
                    item = archive_queue.ready(eligible) if not self.paused and self.running else None
                    if item:
                        archive_queue.start(item)
                        self.queue_status = archive_queue.snapshot()
                        path = Path(item['path'])
                        self.message.emit(f"开始归档（第 {item['attempts']}/3 次）：{path}")
                        try:
                            client.archive(registered[item['project_id']], path)
                        except Exception as exc:
                            if client.progress.data['file'] != str(path):
                                client.progress.begin(path)
                            client.progress.fail(exc)
                            archive_queue.fail(item, exc)
                            self.message.emit('保留原件：' + str(path) + '；' + safe_error(exc))
                            if archive_queue.limited(exc):
                                self.message.emit('接口限流：整个上传队列暂停 70 分钟，到期自动尝试')
                            elif item['state'] == 'pending':
                                self.message.emit('网络故障：30 秒后可自动重试，先处理其他文件')
                            else:
                                self.message.emit('文件停止自动重试，请处理问题后点击“重试失败文件”')
                        else:
                            archive_queue.complete(item)
                            self.seen.pop(str(path), None)
                            self.message.emit('已归档并释放本地原件：' + str(path))
                            self.changed.emit()
                        self.queue_status = archive_queue.snapshot()
                    for _ in range(20):
                        if not self.running:
                            break
                        self.msleep(100)
            except Exception as exc:
                self.queue_status = {**self.queue_status, 'error': safe_error(exc)}
                self.message.emit('队列已停止，保留原件：' + safe_error(exc))
            finally:
                client.close()

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle('项目云空间 · 上传校验后释放本地')
            self.resize(1100, 850)
            central = QWidget()
            self.setCentralWidget(central)
            layout = QVBoxLayout(central)
            title = QLabel('每个项目一个云空间')
            title.setStyleSheet('font-size:24px;font-weight:600;padding:10px 0')
            layout.addWidget(title)
            notice = QLabel('把文件拖入项目内的“云空间”。稳定 10 秒后自动上传；完整下载校验及云端索引备份核对成功后释放原件。\n'
                            '恢复到项目的“云空间恢复”目录。窗口运行时才会监听；关闭窗口后停止。')
            notice.setWordWrap(True)
            layout.addWidget(notice)
            row = QHBoxLayout()
            layout.addLayout(row)
            self.add_button(row, '网盘授权…', self.configure_auth)
            self.add_button(row, '添加项目…', self.add_project)
            self.add_button(row, '打开云空间', self.open_cloud)
            self.add_button(row, '打开恢复目录', self.open_restore)
            self.pause = self.add_button(row, '暂停自动上传', self.toggle_pause)
            self.add_button(row, '重试失败文件', self.retry)
            self.list = QListWidget()
            self.list.setMaximumHeight(120)
            self.list.currentRowChanged.connect(self.refresh_records)
            layout.addWidget(self.list)
            self.queue_label = QLabel('队列：正在读取…')
            self.queue_label.setWordWrap(True)
            layout.addWidget(self.queue_label)
            self.task_label = QLabel('当前文件：—')
            self.task_label.setWordWrap(True)
            layout.addWidget(self.task_label)
            self.stage_label = QLabel('当前阶段：等待文件')
            layout.addWidget(self.stage_label)
            self.transfer_rows = {}
            for key, title in [('upload', '上传'), ('download', '下载校验 / 恢复')]:
                label = QLabel(title + '：等待开始')
                bar = QProgressBar()
                bar.setRange(0, 1000)
                bar.setValue(0)
                layout.addWidget(label)
                layout.addWidget(bar)
                self.transfer_rows[key] = (label, bar)
            detail = QLabel('上传进度按实际待传分片计算；100% 表示数据已发送，仍需云端确认和完整回读校验。')
            detail.setStyleSheet('color:#666;font-size:12px')
            layout.addWidget(detail)
            self.api_label = QLabel('最近接口：—  |  HTTP：—  |  百度错误码：—')
            self.api_label.setWordWrap(True)
            layout.addWidget(self.api_label)
            self.latest_progress = None
            self.progress_task = None
            self.phase_logged = None
            self.finished_logged = set()
            self.detail_logged = None
            self.progress_timer = QTimer(self)
            self.progress_timer.timeout.connect(self.render_progress)
            self.progress_timer.start(500)
            self.table = QTreeWidget()
            self.table.setHeaderLabels(['原相对路径', '大小', '归档时间', '校验'])
            self.table.setColumnWidth(0, 440)
            self.table.setColumnWidth(1, 100)
            self.table.setColumnWidth(2, 170)
            layout.addWidget(self.table, 1)
            self.add_button(layout, '恢复选中文件（保留云端副本）', self.restore)
            recovery_row = QHBoxLayout()
            layout.addLayout(recovery_row)
            self.add_button(recovery_row, '从云端恢复整个项目…', self.scan_cloud_projects)
            self.add_button(recovery_row, '补齐选中项目的云端索引', self.backup_project_indexes)
            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setMaximumBlockCount(300)
            self.log.setMaximumHeight(150)
            layout.addWidget(self.log)
            self.monitor = AgentMonitor()
            self.closing = False
            self.worker = Worker()
            self.worker.message.connect(self.append_log)
            self.worker.changed.connect(self.refresh_records)
            self.worker.progress.connect(self.on_progress)
            self.worker.scanned.connect(self.choose_cloud_project)
            for project in projects():
                try:
                    ensure_agent_guide(project)
                except Exception as exc:
                    self.append_log('使用说明未补齐：' + project['cloud'] + '；' + safe_error(exc))
            self.refresh_projects()
            self.worker.start()
            self.heartbeat_timer = QTimer(self)
            self.heartbeat_timer.timeout.connect(self.write_heartbeat)
            self.heartbeat_timer.start(1000)
            self.write_heartbeat()

        def append_log(self, message):
            self.log.appendPlainText(message)
            self.monitor.log('message', message=message)

        def write_heartbeat(self):
            q = self.worker.queue_status
            remaining = max(0, int(q.get('cooldown_until', 0) - time.time()))
            text = f"队列：待处理 {q.get('waiting_count', 0)} | 正在处理 {q.get('active_count', 0)} | 失败 {q.get('failed_count', 0)}"
            if remaining:
                until = datetime.fromtimestamp(q['cooldown_until']).strftime('%m-%d %H:%M:%S')
                text += f' | 限流等待 {remaining // 60} 分 {remaining % 60} 秒，预计 {until} 继续'
            if self.worker.paused:
                text += ' | 已请求手动暂停'
            if q.get('error'):
                text += ' | 队列停止：' + q['error']
            self.queue_label.setText(text)
            self.monitor.heartbeat(self.worker, 'stopping' if self.closing else 'running')
            self.statusBar().showMessage(self.monitor.monitoring_error or 'Agent 状态与心跳已启用（每秒刷新）')

        def on_progress(self, data):
            self.monitor.progress(data)
            detail_key = (data['task'], data.get('detail', ''))
            if data.get('detail') and detail_key != self.detail_logged:
                if '失败' in data['detail']:
                    self.append_log(data['detail'])
                self.detail_logged = detail_key
            if data['task'] != self.progress_task:
                self.progress_task = data['task']
                self.finished_logged.clear()
                for key, (label, bar) in self.transfer_rows.items():
                    label.setText(('上传' if key == 'upload' else '下载校验 / 恢复') + '：等待开始')
                    bar.setValue(0)
            phase = (data['task'], data['stage'])
            if phase != self.phase_logged:
                self.append_log(time.strftime('%H:%M:%S') + '  阶段：' + data['stage'])
                self.phase_logged = phase
            if (not data['active'] and not data['error'] and phase not in self.finished_logged
                    and data['stage'] in ('上传', '下载校验', '下载恢复')):
                self.finished_logged.add(phase)
                self.append_log(f"{data['stage']}完成：{format_bytes(data['done'])}，"
                                         f"用时 {data['elapsed']:.1f} 秒，平均 {format_bytes(data['rate'])}/s")
            self.latest_progress = data
            self.render_progress()

        def render_progress(self):
            d = self.latest_progress
            if not d:
                return
            elapsed = time.monotonic() - d.get('started', time.monotonic()) if d['active'] else d['elapsed']
            state = d['stage'] + ('（失败，原件保留）' if d['error'] else '')
            self.task_label.setText('当前文件：' + d['file'])
            self.stage_label.setText(f'当前阶段：{state}  |  阶段耗时：{elapsed:.1f} 秒'
                                    + ('\n' + d['detail'] if d.get('detail') else ''))
            key = 'upload' if d['stage'] == '上传' else 'download' if d['stage'] in ('下载校验', '下载恢复') else None
            if key:
                label, bar = self.transfer_rows[key]
                rate = d['rate']
                if d['active'] and time.monotonic() - d.get('last_data_at', 0) > 2:
                    rate = 0
                speed_label = '实时' if d['active'] else '平均'
                label.setText(f"{d['stage']}：{format_bytes(d['done'])} / {format_bytes(d['total'])}"
                              f"  |  {speed_label} {format_bytes(rate)}/s  |  {elapsed:.1f} 秒"
                              + ('  |  失败' if d['error'] else ''))
                bar.setValue(min(1000, int(d['done'] * 1000 / d['total'])) if d['total']
                             else 1000 if not d['active'] and not d['error'] else 0)
            if d['stage'] == '秒传成功（无需传输）':
                label, bar = self.transfer_rows['upload']
                label.setText('上传：秒传成功，无需发送文件数据')
                bar.setValue(1000)
            self.api_label.setText(f"最近接口：{d['method']}  |  HTTP：{d['http']}  |  百度错误码：{d['errno']}"
                                   + ('\n' + d['error'] if d['error'] else ''))
            self.api_label.setStyleSheet('color:#b42318' if d['error'] else '')

        def add_button(self, layout, label, callback):
            button = QPushButton(label)
            button.clicked.connect(callback)
            layout.addWidget(button)
            return button

        def selected(self):
            index = self.list.currentRow()
            return self.items[index] if 0 <= index < len(self.items) else None

        def refresh_projects(self):
            self.items = projects()
            self.list.clear()
            self.list.addItems([p['project'] for p in self.items])
            if self.items:
                self.list.setCurrentRow(len(self.items) - 1)

        def refresh_records(self, *args):
            self.table.clear()
            project = self.selected()
            if not project:
                return
            for path in sorted((Path(project['cloud']) / RECORDS).glob('*.json')):
                try:
                    r = read_json(path)
                    item = QTreeWidgetItem([r['relative_path'], f"{r['size']:,} B", r['archived_at'], 'SHA-256 一致 / 云端索引已核对' if r.get('cloud_index') else 'SHA-256 一致 / 索引待备份'])
                    item.setData(0, Qt.ItemDataRole.UserRole, r)
                    self.table.addTopLevelItem(item)
                except Exception:
                    self.append_log('归档记录读取失败：' + path.name)

        def configure_auth(self):
            import webbrowser
            dialog = QDialog(self)
            dialog.setWindowTitle('使用自己的百度应用授权')
            dialog.setMinimumWidth(550)
            form = QFormLayout(dialog)
            tip = QLabel('先在百度网盘开放平台创建自己的应用。填写应用密钥后，打开百度页面授权，\n再粘贴页面给出的授权码。不要提供百度账号密码。')
            tip.setWordWrap(True)
            form.addRow(tip)
            app_key, secret, code = QLineEdit(), QLineEdit(), QLineEdit()
            secret.setEchoMode(QLineEdit.EchoMode.Password)
            code.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow('App Key', app_key)
            form.addRow('Secret Key', secret)
            form.addRow('授权码', code)
            browse, submit = QPushButton('打开百度授权页面'), QPushButton('提交授权')
            form.addRow(browse, submit)
            def open_auth():
                try:
                    webbrowser.open(authorization_url(app_key.text()))
                except Exception as exc:
                    QMessageBox.warning(dialog, '授权', safe_error(exc))
            def accept_auth():
                if not all(x.text().strip() for x in (app_key, secret, code)):
                    QMessageBox.warning(dialog, '授权', '请填写全部三个字段')
                    return
                self.worker.commands.put({'kind': 'authorize', 'credentials': {
                    'client_id': app_key.text().strip(), 'client_secret': secret.text().strip(),
                    'code': code.text().strip()}})
                secret.clear()
                code.clear()
                self.append_log('已提交授权请求，当前任务结束后处理；请留意授权结果')
                dialog.accept()
            browse.clicked.connect(open_auth)
            submit.clicked.connect(accept_auth)
            dialog.exec()
            dialog.deleteLater()

        def add_project(self):
            folder = QFileDialog.getExistingDirectory(self, '选择项目根目录；将新建“云空间”并开始监听')
            if folder:
                try:
                    register(folder)
                    self.refresh_projects()
                except Exception as exc:
                    QMessageBox.warning(self, '添加失败', safe_error(exc))

        def open_cloud(self):
            if p := self.selected():
                os.startfile(p['cloud'])

        def open_restore(self):
            if p := self.selected():
                path = Path(p['project']) / '云空间恢复'
                path.mkdir(exist_ok=True)
                os.startfile(str(path))

        def toggle_pause(self):
            self.worker.paused = not self.worker.paused
            self.pause.setText('继续自动上传' if self.worker.paused else '暂停自动上传')
            self.append_log('暂停将在当前文件完成后生效' if self.worker.paused else '已继续监听')
            self.write_heartbeat()

        def retry(self):
            self.worker.commands.put('retry')
            self.append_log('已请求重试失败文件，当前文件结束后处理')

        def restore(self):
            item = self.table.currentItem()
            project = self.selected()
            if item and project:
                self.worker.commands.put((project, item.data(0, Qt.ItemDataRole.UserRole)))

        def scan_cloud_projects(self):
            self.worker.commands.put({'kind': 'scan'})
            self.append_log('已请求扫描云端项目，当前任务完成后处理')

        def choose_cloud_project(self, entries):
            if self.closing:
                return
            if not entries:
                QMessageBox.information(self, '云端项目', '未找到云端项目目录')
                return
            names = [e['name'] for e in entries]
            name, ok = QInputDialog.getItem(self, '从云端恢复', '选择项目（无需本机项目记录）', names, 0, False)
            if not ok:
                return
            remote = entries[names.index(name)]['remote']
            folder = QFileDialog.getExistingDirectory(self, '选择恢复父目录；将在其中新建项目目录，保留同名文件')
            if folder:
                destination = str(Path(folder) / name)
                self.worker.commands.put({'kind': 'recover', 'remote': remote, 'destination': destination})
                self.append_log('已请求整项目恢复至：' + destination)

        def backup_project_indexes(self):
            if project := self.selected():
                self.worker.commands.put({'kind': 'backup_indexes', 'project': project})
                self.append_log('已请求核对并补齐选中项目的云端索引')

        def closeEvent(self, event):
            if self.worker.isRunning():
                self.worker.running = False
                self.setWindowTitle('正在完成当前文件并停止，请稍候…')
                if not self.closing:
                    self.closing = True
                    self.monitor.log('app_stopping')
                    self.worker.finished.connect(self.close)
                self.write_heartbeat()
                event.ignore()
            else:
                self.heartbeat_timer.stop()
                self.monitor.close(self.worker)
                event.accept()

    app = QApplication(sys.argv)
    font_path = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts' / 'msyh.ttc'
    if font_path.exists():
        QFontDatabase.addApplicationFont(str(font_path))
    app.setFont(QFont('Microsoft YaHei', 10))
    app.setStyle('Fusion')
    window = Window()
    window.show()
    return app.exec()


def acquire_single_instance():
    STATE.mkdir(exist_ok=True)
    lock = (STATE / 'instance.lock').open('a+b')
    lock.seek(0)
    if lock.read(1) == b'':
        lock.write(b'0'); lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        raise ArchiveError('项目云空间已经运行，请查看现有窗口')
    return lock


def cli():
    import argparse
    parser = argparse.ArgumentParser(description='项目云空间：status / 云端索引 / 整项目恢复')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='只读本地状态，不联网')
    sub.add_parser('authorize', help='交互式授权；密钥不会出现在命令行参数中')
    sub.add_parser('cloud-projects', help='联网扫描可选择的云端项目目录')
    sub.add_parser('backup-indexes', help='核对并备份所有本机登记项目的索引')
    recover = sub.add_parser('recover-project', help='仅从云端索引恢复，无需本机项目记录')
    recover.add_argument('--remote', required=True)
    recover.add_argument('--destination', required=True, help='恢复项目的目标目录，不能位于云空间内')
    args = parser.parse_args()
    if args.command == 'status':
        data, code = query_status()
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return code
    instance = acquire_single_instance()
    client = Cloud()
    try:
        def notify(message):
            print(message, file=sys.stderr, flush=True)
        if args.command == 'authorize':
            import getpass
            import webbrowser
            client_id = input('百度应用 App Key: ').strip()
            secret = getpass.getpass('百度应用 Secret Key（不回显）: ').strip()
            url = authorization_url(client_id)
            print('请在浏览器授权自己的百度网盘账号。若没有自动打开，请访问：' + url, file=sys.stderr)
            webbrowser.open(url)
            code = getpass.getpass('百度页面给出的授权码（不回显）: ').strip()
            client.auth.authorize(client_id, secret, code)
            result = {'authorized': True}
        elif args.command == 'cloud-projects':
            result = client.cloud_projects()
        elif args.command == 'backup-indexes':
            result = [client.backfill_indexes(p, notify) for p in projects()]
        else:
            result = client.recover_project(args.remote, args.destination, notify)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not isinstance(result, dict) or result.get('authorized') or result.get('state') == 'complete' else 1
    finally:
        client.close()
        instance.close()


if __name__ == '__main__':
    if len(sys.argv) > 1:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        try:
            sys.exit(cli())
        except Exception as exc:
            print(json.dumps({'error': safe_error(exc)}, ensure_ascii=False))
            sys.exit(1)
    try:
        instance_lock = acquire_single_instance()
        sys.exit(run_gui())
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(None, safe_error(exc), '项目云空间', 0x10)
        sys.exit(1)
