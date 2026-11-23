#!/usr/bin/env python3
"""开发预览：把 src/web/index.html 直接端到本地，并把 /api/* 反代到正在运行的 app。

用途：改了前端想立刻看到效果，但不重建 .app / .dmg。
用法：
    python3 scripts/preview_web.py            # 默认 http://127.0.0.1:8911/
    python3 scripts/preview_web.py --port 9000 --api http://127.0.0.1:8765

注意：这是只读预览，不写入任何文件；端口默认 8911，避开 app 自己的 8765 与 8899。
"""
import argparse
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "src", "web", "index.html")
API = "http://127.0.0.1:8765"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静一点
        sys.stderr.write("  %s\n" % (fmt % args))

    def _serve_index(self):
        try:
            body = open(INDEX, "rb").read()
        except OSError as e:
            self.send_error(500, "读不到 %s: %s" % (INDEX, e))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self):
        url = API + self.path
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=payload, method=self.command)
        for k in ("Content-Type", "Accept"):
            if self.headers.get(k):
                req.add_header(k, self.headers[k])
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                body = r.read()
                self.send_response(r.status)
                self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as e:
            body = e.read() or b"{}"
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            body = ('{"error":%r}' % str(e)).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._serve_index()
        if self.path.startswith("/api/") or self.path.startswith("/v1/"):
            return self._proxy()
        self.send_error(404)

    def do_POST(self):
        if self.path.startswith("/api/") or self.path.startswith("/v1/"):
            return self._proxy()
        self.send_error(404)


def main():
    global API
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8911)
    ap.add_argument("--api", default=API)
    a = ap.parse_args()
    API = a.api.rstrip("/")
    if not os.path.exists(INDEX):
        sys.exit("找不到 %s" % INDEX)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print("预览：http://127.0.0.1:%d/   （前端 %s · API 反代 → %s）" % (a.port, INDEX, API))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止")


if __name__ == "__main__":
    main()
