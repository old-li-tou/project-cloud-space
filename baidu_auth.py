"""Baidu OAuth and Windows-user credential storage; no viewer project dependency."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import time
import uuid
from urllib.parse import urlencode

import requests

TOKEN_URL = 'https://openapi.baidu.com/oauth/2.0/token'


class BaiduClientError(RuntimeError):
    """Messages are deliberately redacted: never include requests, URLs or tokens."""
    def __init__(self, message, *, http_status=None, errno=None, api_code=None):
        super().__init__(message)
        self.http_status = http_status
        self.http = http_status
        self.errno = errno
        self.api_code = api_code


def default_config_path():
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    return base / 'ProjectCloudSpace' / 'credentials.dpapi'


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def protect(data, decrypt=False):
    """DPAPI binds secrets to this Windows user; UI prompts are disallowed."""
    source_buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    func = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    func.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                     ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    func.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not func(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise BaiduClientError('无法读取或保存当前 Windows 用户的加密授权，请重新授权')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


def load_credentials(path=None):
    path = Path(path or default_config_path())
    try:
        value = json.loads(protect(path.read_bytes(), decrypt=True))
        if value.get('version') != 1:
            raise ValueError()
        if not all(isinstance(value.get(k), str) and value[k]
                   for k in ('client_id', 'client_secret', 'refresh_token')):
            raise ValueError()
        return value
    except (OSError, ValueError, AttributeError):
        raise BaiduClientError('尚未完成授权或授权文件损坏，请点击“网盘授权…”') from None


def save_credentials(value, path=None):
    path = Path(path or default_config_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = protect(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except OSError:
        raise BaiduClientError('无法保存加密授权文件，请检查用户目录权限') from None
    finally:
        temp.unlink(missing_ok=True)


def authorization_url(client_id):
    if not client_id.strip():
        raise BaiduClientError('请先填写自己的百度应用 App Key')
    return 'https://openapi.baidu.com/oauth/2.0/authorize?' + urlencode({
        'response_type': 'code', 'client_id': client_id.strip(),
        'redirect_uri': 'oob', 'scope': 'basic,netdisk', 'display': 'popup'})


class BaiduClient:
    def __init__(self, config_path=None, *, session=None, now=time.time):
        self.config_path = Path(config_path or default_config_path())
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.now = now

    def close(self):
        self.session.close()

    def _exchange(self, client_id, client_secret, **grant):
        # The documented token endpoint uses GET. Never log its prepared URL.
        try:
            with self.session.get(TOKEN_URL, params={
                    'client_id': client_id, 'client_secret': client_secret, **grant},
                    timeout=(10, 30)) as response:
                status = response.status_code
                try:
                    data = response.json()
                except ValueError:
                    raise BaiduClientError('百度授权响应不是有效 JSON', http_status=status) from None
                if status != 200 or not isinstance(data, dict) or data.get('error'):
                    # Server-provided text can contain request values, so do not echo it.
                    raise BaiduClientError('百度授权失败，请检查应用设置、授权码或重新授权', http_status=status)
        except requests.RequestException:
            raise BaiduClientError('连接百度授权服务失败，请稍后重试') from None
        if (not all(isinstance(data.get(k), str) and data[k] for k in ('access_token', 'refresh_token'))
                or type(data.get('expires_in')) is not int or data['expires_in'] <= 0):
            raise BaiduClientError('百度授权响应字段不完整')
        return {'version': 1, 'client_id': client_id, 'client_secret': client_secret,
                'access_token': data['access_token'], 'refresh_token': data['refresh_token'],
                'expires_at': int(self.now()) + data['expires_in']}

    def authorize(self, client_id, client_secret, code):
        if not all(isinstance(s, str) and s.strip() for s in (client_id, client_secret, code)):
            raise BaiduClientError('请填写 App Key、Secret Key 和百度页面给出的授权码')
        value = self._exchange(client_id.strip(), client_secret.strip(),
                               grant_type='authorization_code', code=code.strip(), redirect_uri='oob')
        save_credentials(value, self.config_path)

    def get_access_token(self, *, force_refresh=False):
        current = load_credentials(self.config_path)
        expires = current.get('expires_at', 0)
        if (not force_refresh and isinstance(expires, (int, float))
                and expires > self.now() + 90 and current.get('access_token')):
            return current['access_token']
        updated = self._exchange(current['client_id'], current['client_secret'],
                                 grant_type='refresh_token', refresh_token=current['refresh_token'])
        if load_credentials(self.config_path) != current:
            raise BaiduClientError('授权设置在刷新期间已变化，请重试')
        save_credentials(updated, self.config_path)
        return updated['access_token']
