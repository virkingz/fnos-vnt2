#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板反代端到端验证：/core/ 能否把 vnt2_web 正常代理到本应用路径下。

用模拟上游返回与真实 vnt2_web 同特征的响应（绝对路径资源 + /api 调用），
再起真实的管理服务，请求 /core/**，断言：

  1. 首页可取，且 /assets/ 被改写为带挂载点的路径（否则浏览器 404 白屏）
  2. JS 里的 "/api/xxx" 字面量被同样改写
  3. 静态资源与 API 都能经代理取到（路径已去掉 /core 前缀）
  4. 令牌由代理注入，上游能收到 Authorization
  5. SVG/data URI 不被误改（真实 bug 回归）
  6. 上游不可用时返回可读错误页，而不是白屏
  7. 网关挂载点下（/app/vnt-nas/core/）也能工作
"""

import http.client
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "shell"))

FAILS = []


def check(name, ok, extra=""):
    print("  %s %s%s" % ("[PASS]" if ok else "[FAIL]", name,
                         "" if ok else "  -> %r" % (extra,)))
    if not ok:
        FAILS.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# 与真实 vnt2_web 同特征的 HTML：含绝对路径资源 + data URI（内含 SVG）
INDEX_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<link rel="icon" href="data:image/svg+xml,%3Csvg viewBox='0 0 64 64'%3E%3Crect fill='%23fff' stroke-width='2'/%3E%3C/svg%3E" type="image/svg+xml">
<script type="module" crossorigin src="/assets/index-abc.js"></script>
<link rel="stylesheet" crossorigin href="/assets/index-abc.css">
</head><body><div id="app">VNT PANEL</div></body></html>"""

INDEX_JS = ('fetch("/api/instances");const t="/api/version";'
            'const p="/api/start/status";')


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen_auth = []

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        Upstream.seen_auth.append(self.headers.get("Authorization", ""))
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self._send(200, INDEX_HTML.encode())
        if p == "/assets/index-abc.js":
            return self._send(200, INDEX_JS.encode(),
                              "application/javascript")
        if p == "/assets/index-abc.css":
            return self._send(200, b"#app{color:red}", "text/css")
        if p == "/api/instances":
            return self._send(200, b'{"code":0,"data":[]}',
                              "application/json")
        if p == "/api/version":
            return self._send(200, b'{"code":0,"data":{"version":"2.0.11"}}',
                              "application/json")
        return self._send(404, b"nope")


def main():
    tmp = os.path.join(ROOT, "build", "paneltest")
    os.makedirs(os.path.join(tmp, "var"), exist_ok=True)
    os.environ["VNT_PKGVAR"] = os.path.join(tmp, "var")
    os.environ["VNT_APPDEST"] = os.path.join(tmp, "target")
    os.environ["VNT_PKGETC"] = os.path.join(tmp, "etc")
    os.environ["VNT_APPNAME"] = "vnt-nas"

    import shell_server as S
    import shell_http as H
    from http.server import ThreadingHTTPServer

    S.log = lambda m: None
    up_port = free_port()
    panel_port = free_port()
    S.PORTS["VNT_WEB_PORT"] = up_port
    S.set_token("TestToken123456789")
    S.core_running = lambda: True

    up = ThreadingHTTPServer(("127.0.0.1", up_port), Upstream)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", panel_port), H.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.4)

    def get(path, host="127.0.0.1:%d" % panel_port):
        c = http.client.HTTPConnection("127.0.0.1", panel_port, timeout=10)
        c.request("GET", path, headers={"Host": host})
        r = c.getresponse()
        b = r.read()
        h = dict(r.getheaders())
        c.close()
        return r.status, b, h

    # 1. 直连（无挂载点）下的首页与改写
    print("1) 直连 /core/ 首页与路径改写")
    code, body, hdrs = get("/core/")
    txt = body.decode("utf-8", "replace")
    check("首页 200", code == 200, code)
    check("含面板内容", "VNT PANEL" in txt, txt[:80])
    check("已注入桌面宿主标志", "__VNT_DESKTOP__" in txt, txt[:150])
    check("标志早于应用脚本",
          txt.find("__VNT_DESKTOP__") < txt.find("assets/index-abc.js"))
    check("已注入令牌兜底", "vnt-web-access-token" in txt, txt[:200])
    check("资源已改写为 /core/assets/",
          'src="/core/assets/index-abc.js"' in txt, txt[:400])
    check("CSS 已改写",
          'href="/core/assets/index-abc.css"' in txt, txt[:400])
    check("SVG 的 / 未被误改（回归）",
          "stroke-width='2'/%3E" in txt and "/core/%3E" not in txt,
          txt[:300])
    check("data URI 保持完整", "data:image/svg+xml" in txt)

    # 2. JS 里的 API 路径改写
    print("2) JS 内的 API 字面量改写")
    code, body, hdrs = get("/core/assets/index-abc.js")
    js = body.decode("utf-8", "replace")
    check("JS 200", code == 200, code)
    check("fetch 路径已改写", 'fetch("/core/api/instances")' in js, js[:200])
    check("常量路径已改写", '"/core/api/version"' in js, js[:200])
    check("嵌套路径已改写", '"/core/api/start/status"' in js, js[:200])
    check("无残留裸 /api/", '"/api/' not in js, js[:200])

    # 3. 代理转发时已剥掉 /core 前缀
    print("3) 代理转发路径正确性")
    code, body, _ = get("/core/api/instances")
    check("API 经代理可取", code == 200 and b'"code":0' in body, body[:60])
    code, body, _ = get("/core/assets/index-abc.css")
    check("CSS 经代理可取", code == 200 and b"#app" in body, body[:40])

    # 4. 令牌注入
    print("4) 代理注入访问令牌")
    check("上游收到 Bearer 令牌",
          any(h.startswith("Bearer TestToken") for h in Upstream.seen_auth),
          Upstream.seen_auth[:3])

    # 5. 网关挂载点下同样工作
    print("5) 经统一网关挂载点（/app/vnt-nas/core/）")
    code, body, _ = get("/app/vnt-nas/core/", host="nas.lan:5666")
    txt = body.decode("utf-8", "replace")
    check("网关路径首页 200", code == 200, code)
    check("改写带上挂载点",
          'src="/app/vnt-nas/core/assets/index-abc.js"' in txt, txt[:400])

    # 6. 上游不可用 → 可读错误页
    print("6) 上游不可用时给出可读错误页")
    S.PORTS["VNT_WEB_PORT"] = free_port()   # 指向无人监听的端口
    code, body, hdrs = get("/core/")
    check("返回 502", code == 502, code)
    check("是 HTML", "text/html" in hdrs.get("Content-Type", ""),
          hdrs.get("Content-Type"))
    txt = body.decode("utf-8", "replace")
    check("含可读原因", "无法连接组网面板核心" in txt, txt[:120])

    # 7. panel_url_for 给出同源路径
    print("7) 面板地址为同源子路径")
    S.PORTS["VNT_WEB_PORT"] = up_port

    class Fake(H.Handler):
        def __init__(self, path, host):
            self.path = path
            self._h = host

        @property
        def headers(self):
            return {"Host": self._h}

    check("直连时给出 /core/",
          H.panel_url_for(Fake("/api/panel", "127.0.0.1")) == "/core/",
          H.panel_url_for(Fake("/api/panel", "127.0.0.1")))
    got = H.panel_url_for(Fake("/app/vnt-nas/api/panel", "nas.lan:5666"))
    check("网关时给出 /app/vnt-nas/core/",
          got == "/app/vnt-nas/core/", got)

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), "; ".join(FAILS)))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
