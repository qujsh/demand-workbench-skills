#!/usr/bin/env python3
"""MCP标准库客户端：无重定向，核验使用有总时限的独立进程。"""
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


MESSAGES = {'S048105012': '图片存储核验超时', 'S048105013': '图片存储核验暂不可用',
            'S048105003': '图片不存在或不可访问', 'NETWORK_TIMEOUT': '请求超时',
            'NETWORK_ERROR': '网络请求失败', '-32603': '服务端内部错误'}


class McpError(ValueError):
    def __init__(self, code, request_no='', retryable=False, offline=False):
        self.code = str(code) if (re.fullmatch(r'(?:S[0-9]{9}|-[0-9]{5}|HTTP_[1-5][0-9]{2})', str(code)) or str(code) in {'NETWORK_TIMEOUT', 'NETWORK_ERROR', 'CONFIRM_WORKER_FAILED', 'INVALID_RESPONSE'}) else 'REMOTE_ERROR'
        self.request_no = request_no if isinstance(request_no, str) and re.fullmatch(r'CALL-[A-Za-z0-9-]{1,80}', request_no) else ''
        self.retryable = retryable is True
        self.offline = offline is True
        super().__init__(MESSAGES.get(self.code, '工具调用失败'))

    def detail(self):
        return {'code': self.code, 'message': str(self), 'requestNo': self.request_no,
                'retryable': self.retryable, 'offline': self.offline}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect())


def check_url(url, local=False):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' and not (local and parsed.scheme == 'http' and
            parsed.hostname in ('localhost', '127.0.0.1', '::1'))):
        raise ValueError('服务地址需要 HTTPS，本机 MCP 允许 HTTP')
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('服务地址不能含凭据、查询参数或片段')


def request_json(url, data, headers, timeout=60):
    try:
        with OPENER.open(Request(url, data=data, headers=headers), timeout=timeout) as response:
            raw = response.read()
            value = json.loads(raw) if raw else {}
            value['_requestNo'] = response.headers.get('X-Request-Id', '')
            return value
    except HTTPError as error:
        raise McpError('HTTP_' + str(error.code), error.headers.get('X-Request-Id', ''),
                       retryable=error.code in (408, 429, 500, 502, 503, 504), offline=error.code in (502, 503, 504)) from error
    except (TimeoutError, URLError, OSError) as error:
        code = 'NETWORK_TIMEOUT' if isinstance(error, TimeoutError) or isinstance(getattr(error, 'reason', None), TimeoutError) else 'NETWORK_ERROR'
        raise McpError(code, retryable=True, offline=True) from error


def rpc(url, secret, method, params, timeout=60):
    body = {'jsonrpc': '2.0', 'method': method, 'params': params}
    if not method.startswith('notifications/'):
        body['id'] = str(uuid.uuid4())
    value = request_json(url, json.dumps(body).encode(), {
        'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25'}, timeout=timeout)
    if 'error' in value:
        error = value['error']
        raise McpError(error.get('code', 'REMOTE_ERROR'),
                       error.get('data', {}).get('requestNo') or value.get('_requestNo', ''))
    if method.startswith('notifications/'):
        return {}
    result = value['result']
    if result.get('isError'):
        error = result.get('structuredContent', {})
        code = str(error.get('errorCode', 'REMOTE_ERROR'))
        raise McpError(code, error.get('requestNo') or value.get('_requestNo', ''),
                       retryable=error.get('retryable') is True and code in ('S048105012', 'S048105013'))
    return result


def tool(url, secret, name, args):
    return rpc(url, secret, 'tools/call', {'name': name, 'arguments': args})['structuredContent']


def bounded_confirm(url, secret, media_no):
    """三秒覆盖DNS/连接/响应整体时间；凭据仅通过子进程标准输入传递。"""
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), 'confirm-worker'],
            input=json.dumps({'url': url, 'secret': secret, 'mediaNo': media_no}),
            text=True, capture_output=True, timeout=3)
    except subprocess.TimeoutExpired as error:
        raise McpError('NETWORK_TIMEOUT', retryable=True, offline=True) from error
    if result.returncode:
        raise McpError('CONFIRM_WORKER_FAILED')
    try:
        value = json.loads(result.stdout)
        if 'error' in value:
            failure = value['error']
            raise McpError(failure.get('code'), failure.get('requestNo', ''),
                           failure.get('retryable'), failure.get('offline'))
        return value['result']
    except (KeyError, json.JSONDecodeError) as error:
        raise McpError('INVALID_RESPONSE') from error


if __name__ == '__main__':
    try:
        value = json.loads(sys.stdin.read())
        check_url(value['url'], local=True)
        result = rpc(value['url'], value['secret'], 'tools/call',
                     {'name': 'complete_note_image_upload', 'arguments': {'mediaNo': value['mediaNo']}}, timeout=3)
        print(json.dumps({'result': result['structuredContent']}))
    except McpError as error:
        print(json.dumps({'error': error.detail()}))
    except Exception:
        print(json.dumps({'error': {'code': 'INVALID_RESPONSE'}}))
