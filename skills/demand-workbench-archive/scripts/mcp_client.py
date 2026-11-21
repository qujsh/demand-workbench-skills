#!/usr/bin/env python3
"""MCP标准库客户端：禁止自动重定向，不依赖业务仓库。"""
import json
import uuid
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect())


def check_url(url, local=False):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' and not (local and parsed.scheme == 'http' and
            parsed.hostname in ('localhost', '127.0.0.1', '::1'))):
        raise ValueError('服务地址需要 HTTPS，本机 MCP 允许 HTTP')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('服务地址不能含凭据、查询参数或片段')


def request_json(url, data, headers):
    with OPENER.open(Request(url, data=data, headers=headers), timeout=60) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def rpc(url, secret, method, params):
    body = {'jsonrpc': '2.0', 'method': method, 'params': params}
    if not method.startswith('notifications/'):
        body['id'] = str(uuid.uuid4())
    value = request_json(url, json.dumps(body).encode(), {
        'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25'})
    if 'error' in value:
        raise ValueError('MCP 请求失败：' + value['error'].get('message', '未知错误'))
    if method.startswith('notifications/'):
        return {}
    result = value['result']
    if result.get('isError'):
        raise ValueError('工具调用失败：' + json.dumps(result.get('structuredContent', {})))
    return result


def tool(url, secret, name, args):
    return rpc(url, secret, 'tools/call', {'name': name, 'arguments': args})['structuredContent']

