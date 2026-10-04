#!/usr/bin/env python3
import base64
import hashlib
import http.server
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request

PORT = int(os.environ.get('KAZUMI_PORT', '8866'))
DANDAN_APP_ID = os.environ.get('DANDAN_APP_ID', '')
DANDAN_APP_SECRET = os.environ.get('DANDAN_APP_SECRET', '')
DANDAN_DOMAIN = 'https://api.dandanplay.net'
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kazumi_player.html')

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.1',
]

DANMAKU_TIMEOUT = 20
MEDIA_TIMEOUT = 30
CHUNK_SIZE = 65536


def dandan_headers(path):
    headers = {
        'User-Agent': random.choice(USER_AGENTS),
        'Referer': '',
        'Accept': 'application/json',
    }
    if DANDAN_APP_ID and DANDAN_APP_SECRET:
        ts = str(int(time.time()))
        raw = (DANDAN_APP_ID + ts + path + DANDAN_APP_SECRET).encode('utf-8')
        sig = base64.b64encode(hashlib.sha256(raw).digest()).decode('ascii')
        headers.update({
            'X-Auth': '1',
            'X-AppId': DANDAN_APP_ID,
            'X-Timestamp': ts,
            'X-Signature': sig,
        })
    return headers


def proxied_url(target):
    return '/proxy?url=' + urllib.parse.quote(target, safe='')


def rewrite_m3u8(text, base_url):
    lines = text.splitlines()
    out = []

    def absu(uri):
        return urllib.parse.urljoin(base_url, uri)

    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith('#'):
            if 'URI="' in line:
                line = re.sub(
                    r'URI="([^"]+)"',
                    lambda m: 'URI="%s"' % proxied_url(absu(m.group(1))),
                    line,
                )
            out.append(line)
        else:
            out.append(proxied_url(absu(stripped)))
    return '\n'.join(out) + '\n'


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'KazumiWeb/1.0'

    def log_message(self, fmt, *args):
        sys.stderr.write('[%s] %s\n' % (time.strftime('%H:%M:%S'), fmt % args))

    def _send_json(self, obj, status=200):
        data = json_bytes(obj)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self):
        try:
            with open(HTML_FILE, 'rb') as f:
                data = f.read()
        except OSError:
            self._send_json({'error': 'kazumi_player.html not found'}, 500)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        if path == '/' or path == '/index.html':
            self._send_html()
        elif path == '/favicon.ico':
            self.send_response(204)
            self.send_header('Content-Length', '0')
            self.end_headers()
        elif path.startswith('/api/dd/'):
            self.proxy_dandanplay(path[len('/api/dd'):], parsed.query)
        elif path == '/proxy':
            self.proxy_media(parsed.query)
        else:
            self._send_json({'error': 'not found'}, 404)

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith('/proxy'):
            self.proxy_media(parsed.query, head_only=True)
        else:
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()

    def proxy_dandanplay(self, sub_path, query):
        if not sub_path.startswith('/'):
            sub_path = '/' + sub_path
        target = DANDAN_DOMAIN + sub_path
        if query:
            target += '?' + query
        req = urllib.request.Request(target, headers=dandan_headers(sub_path))
        try:
            with urllib.request.urlopen(req, timeout=DANMAKU_TIMEOUT) as resp:
                data = resp.read()
                self.send_response(resp.status)
                self.send_header('Content-Type', resp.headers.get('Content-Type', 'application/json; charset=utf-8'))
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            try:
                data = e.read()
            except Exception:
                data = b'{}'
            self.send_response(e.code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._send_json({'error': 'dandanplay unreachable', 'detail': str(e)}, 502)

    def proxy_media(self, query, head_only=False):
        params = urllib.parse.parse_qs(query)
        urls = params.get('url')
        if not urls or not urls[0]:
            self._send_json({'error': 'missing url param'}, 400)
            return
        target = urls[0]
        referer = params.get('referer', [''])[0]
        if not (target.startswith('http://') or target.startswith('https://')):
            self._send_json({'error': 'only http(s) urls supported'}, 400)
            return
        req_headers = {
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': '*/*',
        }
        if referer:
            req_headers['Referer'] = referer
        range_value = self.headers.get('Range')
        if range_value:
            req_headers['Range'] = range_value
        req = urllib.request.Request(target, headers=req_headers, method='HEAD' if head_only else 'GET')
        try:
            resp = urllib.request.urlopen(req, timeout=MEDIA_TIMEOUT)
        except Exception as e:
            self._send_json({'error': 'upstream error', 'detail': str(e)}, 502)
            return
        try:
            ctype = resp.headers.get('Content-Type') or ''
            is_playlist = ('mpegurl' in ctype.lower()
                           or target.split('?')[0].lower().endswith('.m3u8'))
            self.send_response(resp.status)
            for h in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
                v = resp.headers.get(h)
                if v:
                    self.send_header(h, v)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-store')
            if head_only:
                self.send_header('Content-Length', resp.headers.get('Content-Length', '0'))
                self.end_headers()
                resp.close()
                return
            if is_playlist:
                data = resp.read()
                resp.close()
                text = data.decode('utf-8', 'replace')
                if '#EXTM3U' in text:
                    body = rewrite_m3u8(text, target).encode('utf-8')
                else:
                    body = data
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.end_headers()
            while True:
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
            resp.close()
        except Exception as e:
            self.log_message('proxy stream aborted: %s', e)


import json


def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode('utf-8')


def lan_addresses():
    result = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(('8.8.8.8', 80))
        result.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith('127.') and ip not in result:
                result.append(ip)
    except Exception:
        pass
    return result or ['127.0.0.1']


def main():
    server = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    server.daemon_threads = True
    print('Kazumi Web Player')
    print('Python %s' % sys.version.split()[0])
    if DANDAN_APP_ID:
        print('dandanplay signature: enabled (%s)' % DANDAN_APP_ID)
    else:
        print('dandanplay signature: off (set DANDAN_APP_ID / DANDAN_APP_SECRET to enable)')
    print('local:   http://127.0.0.1:%d' % PORT)
    for ip in lan_addresses():
        print('lan:     http://%s:%d' % (ip, PORT))
    print('press Ctrl+C to stop')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nbye')
        server.server_close()


if __name__ == '__main__':
    main()
