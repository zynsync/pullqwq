import argparse
import base64
import hashlib
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DANDAN_DOMAIN = "https://api.dandanplay.net"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(BASE_DIR, "player.html")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
CONFIG = {"app_id": "", "app_secret": ""}
CHUNK = 262144
TIMEOUT = 30


def dandan_signature(path, ts):
    raw = (CONFIG["app_id"] + str(ts) + path + CONFIG["app_secret"]).encode("utf-8")
    return base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii")


def dandan_headers(path):
    headers = {"User-Agent": UA, "Accept": "application/json", "Referer": ""}
    if CONFIG["app_id"] and CONFIG["app_secret"]:
        ts = int(time.time())
        headers["X-Auth"] = "1"
        headers["X-AppId"] = CONFIG["app_id"]
        headers["X-Timestamp"] = str(ts)
        headers["X-Signature"] = dandan_signature(path, ts)
    return headers


def lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def guess_content_type(url, header_value):
    if header_value:
        return header_value.split(";")[0].strip()
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if path.endswith(".mpd"):
        return "application/dash+xml"
    if path.endswith(".ts"):
        return "video/mp2t"
    if path.endswith(".mp4"):
        return "video/mp4"
    return "application/octet-stream"


def is_playlist(url, content_type):
    lowered = content_type.lower()
    if "mpegurl" in lowered or "dash+xml" in lowered or "vnd.apple" in lowered:
        return True
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith(".m3u8") or path.endswith(".mpd")


def proxy_url(absolute, referer):
    params = {"url": absolute}
    if referer:
        params["referer"] = referer
    return "/api/proxy?" + urllib.parse.urlencode(params)


def rewrite_playlist(text, base_url, referer):
    def fix_uri(match):
        return 'URI="%s"' % proxy_url(urllib.parse.urljoin(base_url, match.group(1)), referer)

    text = re.sub(r'URI="([^"]+)"', fix_uri, text)
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append(proxy_url(urllib.parse.urljoin(base_url, stripped), referer))
        else:
            out.append(line)
    return "\n".join(out)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KazumiWeb/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_bytes(self, status, body, content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, body)

    def send_text(self, status, text, content_type="text/plain; charset=utf-8"):
        self.send_bytes(status, text.encode("utf-8"), content_type)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                self.serve_html()
            elif path == "/api/dandan/search":
                self.dandan_search(query)
            elif path.startswith("/api/dandan/bangumi/"):
                self.dandan_bangumi(path.rsplit("/", 1)[-1])
            elif path.startswith("/api/dandan/bgmtv/"):
                self.dandan_bgmtv(path.rsplit("/", 1)[-1])
            elif path.startswith("/api/dandan/comment/"):
                self.dandan_comment(path.rsplit("/", 1)[-1], query)
            elif path == "/api/proxy":
                self.proxy_stream(query)
            else:
                self.send_json(404, {"error": "not found"})
        except BrokenPipeError:
            pass
        except Exception as exc:
            try:
                self.send_json(500, {"error": str(exc)})
            except Exception:
                pass

    def serve_html(self):
        if not os.path.exists(HTML_FILE):
            self.send_text(500, "player.html not found")
            return
        with open(HTML_FILE, "rb") as handle:
            body = handle.read()
        self.send_bytes(200, body, "text/html; charset=utf-8")

    def dandan_search(self, query):
        keyword = (query.get("anime") or [""])[0]
        if not keyword:
            self.send_json(400, {"error": "missing anime"})
            return
        target = DANDAN_DOMAIN + "/api/v2/search/episodes"
        params = urllib.parse.urlencode({"anime": keyword, "v2": "true"})
        self.forward_json(target + "?" + params, "/api/v2/search/episodes")

    def dandan_bangumi(self, anime_id):
        target = DANDAN_DOMAIN + "/api/v2/bangumi/" + urllib.parse.quote(anime_id)
        self.forward_json(target, "/api/v2/bangumi/" + anime_id)

    def dandan_bgmtv(self, bgm_id):
        target = DANDAN_DOMAIN + "/api/v2/bangumi/bgmtv/" + urllib.parse.quote(bgm_id)
        self.forward_json(target, "/api/v2/bangumi/bgmtv/" + bgm_id)

    def dandan_comment(self, episode_id, query):
        ch_convert = (query.get("chConvert") or ["0"])[0]
        with_related = (query.get("withRelated") or ["true"])[0]
        path = "/api/v2/comment/" + episode_id
        params = urllib.parse.urlencode({"withRelated": with_related, "chConvert": ch_convert})
        self.forward_json(DANDAN_DOMAIN + path + "?" + params, path)

    def forward_json(self, url, sign_path):
        request = urllib.request.Request(url, headers=dandan_headers(sign_path), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                body = response.read()
            self.send_bytes(200, body)
        except urllib.error.HTTPError as exc:
            detail = exc.read()
            if not detail:
                detail = json.dumps({"errorCode": exc.code, "errorMessage": "弹幕源请求失败"}).encode("utf-8")
            self.send_bytes(exc.code if exc.code >= 400 else 502, detail)
        except Exception as exc:
            self.send_json(502, {"error": str(exc)})

    def proxy_stream(self, query):
        raw_url = (query.get("url") or [""])[0]
        referer = (query.get("referer") or [""])[0]
        if not raw_url:
            self.send_json(400, {"error": "missing url"})
            return
        headers = {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": referer or raw_url,
            "Origin": "",
        }
        range_header = self.headers.get("Range")
        if range_header:
            headers["Range"] = range_header
        request = urllib.request.Request(raw_url, headers=headers, method="GET")
        try:
            upstream = urllib.request.urlopen(request, timeout=TIMEOUT)
        except urllib.error.HTTPError as exc:
            self.send_response(exc.code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        except Exception as exc:
            self.send_json(502, {"error": str(exc)})
            return
        with upstream:
            content_type = guess_content_type(raw_url, upstream.headers.get("Content-Type"))
            status = upstream.status
            if is_playlist(raw_url, content_type) and not range_header:
                data = upstream.read().decode("utf-8", "replace")
                body = rewrite_playlist(data, upstream.geturl(), referer).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Accept-Ranges", upstream.headers.get("Accept-Ranges", "bytes"))
            for key in ("Content-Length", "Content-Range"):
                value = upstream.headers.get(key)
                if value:
                    self.send_header(key, value)
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:
                chunk = upstream.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(description="Kazumi 风格局域网弹幕播放器后端")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dandan-app-id", default=os.environ.get("DANDAN_APP_ID", ""))
    parser.add_argument("--dandan-app-secret", default=os.environ.get("DANDAN_APP_SECRET", ""))
    args = parser.parse_args()

    CONFIG["app_id"] = args.dandan_app_id.strip()
    CONFIG["app_secret"] = args.dandan_app_secret.strip()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    ip = lan_ip()
    sys.stderr.write("\n")
    sys.stderr.write("  局域网弹幕播放器已启动\n")
    sys.stderr.write("  本机访问    http://127.0.0.1:%d\n" % args.port)
    sys.stderr.write("  局域网访问  http://%s:%d\n" % (ip, args.port))
    sys.stderr.write("  弹幕源      %s\n\n" % ("已启用签名认证" if CONFIG["app_id"] else "匿名模式"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n已停止\n")


if __name__ == "__main__":
    main()