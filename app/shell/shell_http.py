#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vnt-nas 管理页 HTTP 层：路由、静态资源、对 vnt2_web 的反向代理。

入口：python3 shell_http.py
"""

import json
import os
import posixpath
import re
import socket
import socketserver
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shell_server as S  # noqa: E402

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

PROXY_PREFIX = "/core"

# 已记录的请求路径（用于排查网关转发，去重避免刷屏）
_SEEN_PATHS = set()


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def panel_url_for(handler, with_token=True):
    """构造 vnt2_web 面板的真实地址。

    vnt2_web 独立占用 VNT_WEB_PORT，页面必须用它自己的端口访问
    （前端资源与 API 都是根路径，无法挂在子路径下）。

    它原生支持 /?token=xxx：前端会读取该参数、写入 localStorage
    并自动进入控制台，随后把地址栏里的 token 抹掉。
    因此这里直接带上令牌，用户无需再手工粘贴。
    """
    host = (handler.headers.get("Host") or "").split(":")[0].strip()
    if not host or host in ("localhost", "127.0.0.1", "::1"):
        ip = handler._lan_ip() if hasattr(handler, "_lan_ip") else ""
        host = ip or "127.0.0.1"
    url = "http://%s:%d/" % (host, S.PORTS["VNT_WEB_PORT"])
    if with_token:
        tok = S.get_token()
        if tok:
            from urllib.parse import quote
            url += "?token=" + quote(tok, safe="")
    return url


def panel_url_bare(handler):
    """不带令牌的面板地址（用于展示，避免把令牌显示在界面上）。"""
    return panel_url_for(handler, with_token=False)


def check_uploaded_file(path):
    """上传文件的轻量校验：是 zip（含 vnt2_web）或合法可执行文件。"""
    import zipfile
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, "无法读取上传文件：%s" % e
    if size < 100 * 1024:
        return False, ("文件仅 %d 字节，不像是核心程序。"
                       "请上传 vnt2-*-linux-musl 压缩包或 vnt2_web 二进制。"
                       % size)

    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as z:
                if any(os.path.basename(n) == "vnt2_web" for n in z.namelist()):
                    return True, "ok"
            return False, "压缩包内未找到 vnt2_web"
        except Exception as e:  # noqa: BLE001
            return False, "压缩包损坏：%s" % e

    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError as e:
        return False, "无法读取文件：%s" % e
    if head == b"\x7fELF":
        return True, "ok"
    if head[:2] == b"MZ":
        return False, "这是 Windows 程序，当前系统需要 Linux 版本"
    return False, ("不是有效的核心文件。请上传 "
                   "vnt2-x86_64-unknown-linux-musl 或对应架构的 zip。")


def parse_multipart(raw, content_type):
    """极简 multipart/form-data 解析（仅标准库）。

    返回 (fields, files)：
      fields = {字段名: 文本值}
      files  = {字段名: (文件名, 字节内容)}
    """
    fields, files = {}, {}
    m = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not m:
        return fields, files
    boundary = ("--" + m.group(1)).encode()

    for part in raw.split(boundary):
        if not part or part in (b"--", b"--\r\n", b"\r\n"):
            continue
        part = part.lstrip(b"\r\n")
        if part.startswith(b"--"):
            continue
        head_end = part.find(b"\r\n\r\n")
        if head_end < 0:
            continue
        head = part[:head_end].decode("utf-8", "replace")
        body = part[head_end + 4:]
        # 去掉结尾的 CRLF
        if body.endswith(b"\r\n"):
            body = body[:-2]

        name_m = re.search(r'name="([^"]*)"', head)
        if not name_m:
            continue
        name = name_m.group(1)
        fn_m = re.search(r'filename="([^"]*)"', head)
        if fn_m:
            files[name] = (fn_m.group(1), body)
        else:
            fields[name] = body.decode("utf-8", "replace")
    return fields, files


def restart_self():
    """重启管理服务进程。

    改端口后需要重新监听，这里直接 execv 替换当前进程：
    不依赖外部脚本，也不会留下孤儿进程。
    延迟几秒是为了让当前请求的响应先发出去。
    """
    import time
    time.sleep(2)
    try:
        S.log("restarting panel service on new port")
        os.execv(sys.executable, [sys.executable,
                                  os.path.join(S.APPDEST, "shell", "shell_main.py")])
    except OSError as e:
        S.log("restart failed: %s" % e)


_ABS_PATH_RE = re.compile(r'(["\'`])/(?![/*])')


def rewrite_paths(text, prefix=PROXY_PREFIX):
    """把 vnt2_web 响应里的绝对路径改写为带代理前缀的路径。

    处理形式：
      HTML:  href="/assets/x.css"   src='/assets/x.js'
      JS:    fetch("/api/...")   De(`/api/...`)   url: '/api/...'

    规则：匹配「引号(含反引号) + 单个斜杠」，补上 prefix（默认 /core）。
    经飞牛网关访问时 prefix 需带上挂载点，即 /app/<appname>/core。
    排除 // 协议相对地址与 /* 注释；已带前缀的不重复处理（幂等）。
    """
    if not text:
        return text
    pre = prefix or PROXY_PREFIX
    short = pre.lstrip("/")   # 如 "core" 或 "app/vnt-nas/core"

    def repl(m):
        after = m.string[m.end():]
        # 已经是 pre/ 或 pre 后面紧跟引号，跳过，保证幂等
        if any(after.startswith(short + c) for c in ("/", '"', "'", "`")):
            return m.group(0)
        return m.group(1) + pre + "/"

    return _ABS_PATH_RE.sub(repl, text)


def jdump(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "vnt-nas-shell/1.0"
    protocol_version = "HTTP/1.1"

    # --- 基础设施 -------------------------------------------------------
    def log_message(self, fmt, *args):
        pass  # 静默，避免污染日志

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = jdump(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, code, obj):
        self._send(code, obj, "application/json; charset=utf-8")

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return {}

    def _is_admin(self):
        # 走统一网关时飞牛会注入该 Header；直连时无此 Header，视为管理员
        val = self.headers.get("X-Trim-Isadmin")
        if val is None:
            return True
        return str(val).lower() == "true"

    def _lan_ip(self):
        """探测本机在局域网中的地址，用于拼出可访问的面板 URL。"""
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect(("223.5.5.5", 80))
            return s.getsockname()[0]
        except OSError:
            return ""
        finally:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass

    # --- 路由 -----------------------------------------------------------
    def do_GET(self):
        try:
            self._route("GET")
        except Exception:  # noqa: BLE001
            S.log("GET error: %s" % traceback.format_exc())
            self._json(500, {"code": 1, "msg": "内部错误"})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        try:
            self._route("POST")
        except Exception:  # noqa: BLE001
            S.log("POST error: %s" % traceback.format_exc())
            self._json(500, {"code": 1, "msg": "内部错误"})

    def do_DELETE(self):
        self.do_POST()

    def _route(self, method):
        u = urlparse(self.path)
        raw_path = unquote(u.path)          # 保留原始路径，用于推断资源基址
        path = S.remove_gateway_prefix(raw_path)
        q = parse_qs(u.query)

        # 记录请求路径，便于核查飞牛网关的转发行为（去重写日志，避免刷屏）
        key = "%s %s" % (method, raw_path)
        if key not in _SEEN_PATHS:
            _SEEN_PATHS.add(key)
            if len(_SEEN_PATHS) <= 60:
                S.log("path: %s -> %s (host=%s)"
                      % (raw_path, path, self.headers.get("Host", "")))

        if path == "/api/health":
            return self._json(200, {"code": 0, "msg": "success", "data": {"ok": True}})

        if path == "/api/state":
            return self._json(200, {"code": 0, "msg": "success", "data": self._state()})

        if path == "/api/update/progress":
            st = S.update_snapshot()
            return self._json(200, {"code": 0, "msg": "success",
                                    "data": {"update": st,
                                             "installed": S.installed_version()}})

        if path == "/api/update" and method == "POST":
            if not self._is_admin():
                return self._json(403, {"code": 1, "msg": "需要管理员权限"})
            body = self._body()
            ver = (body.get("version") or "").strip() or None
            ok, msg = S.update_in_background(ver)
            return self._json(200 if ok else 409,
                              {"code": 0 if ok else 1, "msg": msg})

        if path == "/api/upload_core" and method == "POST":
            if not self._is_admin():
                return self._json(403, {"code": 1, "msg": "需要管理员权限"})
            return self._handle_upload()

        if path == "/api/proxy":
            if method == "GET":
                return self._json(200, {"code": 0, "msg": "success",
                                        "data": {"proxy": S.read_proxy(),
                                                 "scheme": S.proxy_scheme(),
                                                 "is_socks": S.proxy_is_socks(),
                                                 "has_curl": bool(S.have_curl())}})
            if not self._is_admin():
                return self._json(403, {"code": 1, "msg": "需要管理员权限"})
            body = self._body()
            raw = str(body.get("proxy") or "").strip()
            if raw and not re.match(
                    r"^(socks5h?|socks4a?|https?)://[^\s/]+:\d+$", raw) \
                    and not re.match(r"^[^\s/]+:\d+$", raw):
                return self._json(400, {
                    "code": 1,
                    "msg": "代理格式不正确，应形如 socks5://127.0.0.1:1080 或 http://127.0.0.1:7890",
                })
            saved = S.save_proxy(raw)
            msg = "代理已保存" if saved else "代理已清除"
            if saved and S.proxy_is_socks() and not S.have_curl():
                msg += "；注意：SOCKS 代理需要系统安装 curl，当前未检测到"
            # 代理变更会影响已运行的下载，提示用户
            return self._json(200, {"code": 0, "msg": msg,
                                    "data": {"proxy": saved}})

        if path == "/api/proxy_test" and method == "POST":
            if not self._is_admin():
                return self._json(403, {"code": 1, "msg": "需要管理员权限"})
            return self._json(200, self._test_proxy())

        if path == "/api/check_update" and method in ("GET", "POST"):
            cur = S.installed_version()
            has_core = os.path.isfile(S.CORE_BIN)
            probe_ok, probe_info = (False, "")
            if has_core:
                probe_ok, probe_info = S.probe_core()
            try:
                latest = S.latest_version()
            except Exception as e:  # noqa: BLE001
                # 查询失败时也要如实告知本地状态，前端才能给出正确动作
                return self._json(200, {
                    "code": 1, "msg": str(e),
                    "data": {"installed": cur, "has_core": has_core,
                             "probe_ok": probe_ok, "probe_info": probe_info,
                             "need_download": (not has_core) or (not probe_ok)},
                })

            need_download = (not has_core) or (not probe_ok)
            has_update = (latest != cur)
            # 三种动作：首次下载 / 升级 / 已是最新
            if need_download:
                action = "install"
                if not has_core:
                    tip = "尚未安装核心，可以下载 v%s" % latest
                else:
                    tip = "当前核心无法运行，建议重新下载 v%s" % latest
            elif has_update:
                action = "update"
                tip = "发现新版本 v%s（当前 v%s）" % (latest, cur)
            else:
                action = "none"
                tip = "已是最新版本 v%s" % latest

            return self._json(200, {
                "code": 0, "msg": "success",
                "data": {
                    "latest": latest,
                    "installed": cur,
                    "has_core": has_core,
                    "probe_ok": probe_ok,
                    "probe_info": probe_info,
                    "has_update": has_update,
                    "need_download": need_download,
                    "action": action,
                    "tip": tip,
                    "asset": S.asset_stem() or "",
                    "arch": S._machine(),
                },
            })

        if path in ("/api/core/start", "/api/core/stop", "/api/core/restart") \
                and method == "POST":
            if not self._is_admin():
                return self._json(403, {"code": 1, "msg": "需要管理员权限"})
            return self._core_action(path.rsplit("/", 1)[-1])

        if path == "/api/token":
            if method == "GET":
                return self._json(200, {"code": 0, "msg": "success",
                                        "data": {"hint": S.token_hint(),
                                                 "set": bool(S.get_token())}})
            if not self._is_admin():
                return self._json(403, {"code": 1, "msg": "需要管理员权限"})
            body = self._body()
            raw = str(body.get("token") or "").strip()
            if raw and len(raw) < 16:
                return self._json(400, {"code": 1,
                                        "msg": "令牌至少需要 16 位字符"})
            new = S.set_token(raw)
            restarted = ""
            if body.get("restart", True) and S.core_running():
                S.stop_core()
                restarted = S.start_core()[1]
            return self._json(200, {"code": 0,
                                    "msg": ("令牌已更新" if raw else "已重新生成随机令牌")
                                           + (("；" + restarted) if restarted else ""),
                                    "data": {"hint": S.token_hint()}})

        if path == "/api/panel_port" and method == "POST":
            # 修改面板端口：写入配置并重启管理服务（由 cmd/main 重新拉起）。
            # 新端口在重启后生效，因此这里只落盘并提示。
            if not self._is_admin():
                return self._json(403, {"code": 1, "msg": "需要管理员权限"})
            body = self._body()
            raw = str(body.get("port") or "").strip()
            if not raw.isdigit() or not (1 <= int(raw) <= 65535):
                return self._json(400, {"code": 1,
                                        "msg": "端口必须是 1-65535 的数字"})
            port = int(raw)
            try:
                with open(S.PORT_FILE, "w", encoding="utf-8") as f:
                    f.write("PANEL_PORT=%d\nVNT_WEB_PORT=%d\n"
                            % (port, S.PORTS["VNT_WEB_PORT"]))
            except OSError as e:
                return self._json(500, {"code": 1, "msg": "写入端口失败: %s" % e})

            # 端口变化需要重启管理服务；用后台方式重启以免请求被中断
            threading.Thread(target=restart_self, daemon=True).start()
            return self._json(200, {
                "code": 0,
                "msg": "端口已改为 %d，服务正在重启，请稍后用新端口访问" % port,
                "data": {"port": port},
            })

        if path == "/api/instances":
            return self._json(200, {"code": 0, "msg": "success",
                                    "data": {"instances": S.list_instances()}})

        if path == "/api/instance":
            name = (q.get("file_name") or [""])[0]
            text = S.read_instance(name)
            if text is None:
                return self._json(404, {"code": 1, "msg": "未找到该配置文件"})
            return self._json(200, {"code": 0, "msg": "success",
                                    "data": {"file_name": name, "text": text}})

        if path == "/api/bind" and method == "POST":
            # 设置核心监听范围；vnt2_web 需重启才能在新地址上生效
            if not self._is_admin():
                return self._json(403, {"code": 1, "msg": "需要管理员权限"})
            body = self._body()
            host = S.save_bind(body.get("bind") or "127.0.0.1")
            restarted = ""
            if S.core_running():
                S.stop_core()
                restarted = S.start_core()[1]
            tip = ("核心已监听全部网卡" if host == "0.0.0.0"
                   else "核心仅监听本机回环")
            return self._json(200, {
                "code": 0,
                "msg": tip + (("；" + restarted) if restarted else ""),
                "data": {"bind": host,
                         "url": self._panel_direct_url(host)},
            })

        if path == "/api/diag":
            # 自检：内嵌为什么可能不显示。逐项给出结论，避免静默空白。
            return self._json(200, dict(self._diag(raw_path)))

        if path == "/api/panel":
            # 面板地址 = vnt2_web 自己的端口。
            # 它的前端资源编译在 ELF 内、使用根路径与拼接片段，
            # 无法经子路径反代（会连机器码一起改坏），因此独立端口访问。
            #
            # vnt2_web 原生支持 /?token=xxx：前端会读取该参数、写入
            # localStorage 并自动进入控制台。因此返回带令牌的地址，
            # 内嵌与新窗口打开都无需再手工粘贴令牌；
            # 另给出不含令牌的地址用于界面展示，避免令牌显示在页面上。
            return self._json(200, {"code": 0, "msg": "success",
                                    "data": {
                                        "port": S.PORTS["VNT_WEB_PORT"],
                                        "url": panel_url_for(self),
                                        "display_url": panel_url_bare(self),
                                        "panel_path": "/",
                                        "has_token": bool(S.get_token()),
                                        "core_running": S.core_running(),
                                        "bind": S.read_bind(),
                                    }})

        if path == "/api/mirror" and method == "POST":
            body = self._body()
            m = str(body.get("mirror") or "").strip().rstrip("/")
            try:
                with open(S.MIRROR_FILE, "w", encoding="utf-8") as f:
                    f.write(m)
                return self._json(200, {"code": 0, "msg": "镜像前缀已保存"})
            except OSError as e:
                return self._json(500, {"code": 1, "msg": str(e)})

        if path == "/api/logs":
            name = (q.get("name") or ["main"])[0]
            files = {"main": S.MAIN_LOG, "update": S.UPDATE_LOG,
                     "core": S.CORE_LOG}
            if name not in files:
                return self._json(400, {"code": 1, "msg": "未知日志"})
            return self._json(200, {"code": 0, "msg": "success",
                                    "data": {"name": name,
                                             "text": S.tail(files[name], 300)}})

        if path.startswith(PROXY_PREFIX + "/") or path == PROXY_PREFIX:
            # 已废弃的代理入口：vnt2_web 的前端资源编译在 ELF 内、
            # JS 用绝对路径与拼接片段，做内容改写会把程序改坏，
            # 因此这里直接 302 到它的真实地址，避免出现白屏。
            target = panel_url_for(self)
            return self._send(302, b"", "text/plain",
                              {"Location": target})

        return self._static(path, raw_path)

    # --- 代理连通性测试 --------------------------------------------------
    def _test_proxy(self):
        """测试当前代理能否拉到 GitHub 的版本信息。

        直接打真实接口，成功即证明「代理 + 网络 + 目标站点」整条链路可用。
        """
        proxy = S.read_proxy()
        uses = "代理 %s" % proxy if proxy else "直连（未配置代理）"
        if proxy and S.proxy_is_socks() and not S.have_curl():
            return {"code": 1,
                    "msg": "SOCKS 代理需要 curl 支持，但系统未检测到 curl"}
        try:
            body = self._body()
            # 允许先测一个尚未保存的地址
            if body.get("proxy"):
                S.save_proxy(str(body["proxy"]))
                uses = "代理 %s" % S.read_proxy()
            t0 = time.time()
            ver = S.latest_version()
            cost = int((time.time() - t0) * 1000)
            if not ver:
                return {"code": 1, "msg": "未取到版本号（%s）" % uses}
            return {"code": 0,
                    "msg": "连接正常（%s），最新版本 v%s，耗时 %d ms"
                           % (uses, ver, cost),
                    "data": {"version": ver, "cost_ms": cost, "via": uses}}
        except Exception as e:  # noqa: BLE001
            return {"code": 1, "msg": "连接失败（%s）：%s" % (uses, e)}

    def _abs_url(self, path):
        """按请求实际使用的协议与 Host 拼出绝对地址。

        关键：必须沿用浏览器请求时的 Host（可能带非默认端口，如网关的 5666），
        不能用配置端口自行替换，否则拼出的地址访问不到。
        """
        host = (self.headers.get("Host") or "").strip()
        if not host:
            host = "%s:%d" % (self._lan_ip() or "127.0.0.1",
                              S.PORTS["PANEL_PORT"])
        # 网关通常走 HTTPS；直连面板端口时是 HTTP
        fwd = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
        scheme = fwd or ("https" if host.endswith(":5666") else "http")
        return "%s://%s%s" % (scheme, host, path)

    def _panel_direct_url(self, bind=None):
        """vnt2_web 直连地址（仅当监听范围为全部网卡时可用）。"""
        bind = bind or S.read_bind()
        if bind != "0.0.0.0":
            return ""
        req_host = (self.headers.get("Host") or "").split(":")[0].strip()
        if not req_host or req_host in ("localhost", "127.0.0.1", "::1"):
            req_host = self._lan_ip() or "127.0.0.1"
        return "http://%s:%d/" % (req_host, S.PORTS["VNT_WEB_PORT"])

    # --- 自检 -----------------------------------------------------------
    def _diag(self, raw_path):
        """逐项检查内嵌面板的可用性，返回结论列表。"""
        items = []
        mount = self._mount(raw_path)
        core_up = S.core_running()
        has_token = bool(S.get_token())
        has_core = os.path.isfile(S.CORE_BIN)

        def add(name, ok, detail):
            items.append({"name": name, "ok": bool(ok), "detail": detail})

        add("核心已下载", has_core,
            "核心二进制存在" if has_core else "尚未下载核心，请到「核心更新」下载")
        add("访问令牌已设置", has_token,
            "令牌已就绪" if has_token else "尚未设置访问令牌")
        add("核心正在运行", core_up,
            "核心进程在运行" if core_up else "核心未运行，面板无法加载")

        # 核心端口是否真的能连上（回环探测，与监听范围无关）
        port_ok = S._port_listening(S.PORTS["VNT_WEB_PORT"])
        add("核心端口可连接", port_ok,
            "127.0.0.1:%d 可连接" % S.PORTS["VNT_WEB_PORT"] if port_ok
            else "127.0.0.1:%d 连不上，核心可能启动失败"
                 % S.PORTS["VNT_WEB_PORT"])

        # 反向代理能否真正取到面板首页
        proxy_ok, proxy_detail = False, ""
        if core_up and has_token:
            try:
                code, body = S.http_get(
                    "http://127.0.0.1:%d/" % S.PORTS["VNT_WEB_PORT"], timeout=8,
                    headers={"Authorization": "Bearer %s" % S.get_token()})
                proxy_ok = (code == 200 and body)
                proxy_detail = ("代理可访问核心首页（%d 字节）" % len(body)
                                if proxy_ok else "核心首页返回 HTTP %s" % code)
            except Exception as e:  # noqa: BLE001
                proxy_detail = "代理访问核心失败：%s" % e
        else:
            proxy_detail = "核心未运行或缺少令牌，跳过"
        add("管理服务可代理核心", proxy_ok, proxy_detail)

        # 网关挂载前缀是否正确识别
        add("挂载前缀识别", True,
            "当前前缀：%s" % (mount or "/（直连）"))

        # 面板地址（展示用，不含令牌）
        panel_url = panel_url_bare(self)
        add("面板地址", True, panel_url + "（内嵌/跳转时会自动带令牌）")

        # 若监听范围为回环，说明内嵌依赖代理；放开 0.0.0.0 可绕过 iframe 限制
        bind = S.read_bind()
        add("核心监听范围", True,
            "全部网卡（0.0.0.0），可直连 %s" % self._panel_direct_url()
            if bind == "0.0.0.0"
            else "仅本机（127.0.0.1），内嵌依赖反向代理")

        all_ok = all(i["ok"] for i in items
                     if i["name"] not in ("核心监听范围", "挂载前缀识别", "面板地址"))
        return {
            "code": 0 if all_ok else 1,
            "msg": "自检通过" if all_ok else "自检发现问题，详见各项",
            "data": {
                "items": items,
                "panel_url": panel_url,
                "raw_path": raw_path,
                "host": self.headers.get("Host", ""),
                "mount": mount,
                "bind": bind,
            },
        }

    # --- 手动上传核心 ---------------------------------------------------
    def _handle_upload(self):
        """接收 multipart/form-data 上传的核心文件（zip 或裸二进制）。"""
        ctype = self.headers.get("Content-Type", "")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return self._json(400, {"code": 1, "msg": "未收到文件内容"})
        # 上限 200MB：核心约 9MB，留足余量但避免被塞爆
        if n > 200 * 1024 * 1024:
            return self._json(413, {"code": 1, "msg": "文件过大（上限 200MB）"})

        if "multipart/form-data" not in ctype.lower():
            return self._json(400, {"code": 1,
                                    "msg": "请以表单方式上传文件"})

        raw = self.rfile.read(n)
        fields, files = parse_multipart(raw, ctype)
        fileitem = files.get("core")
        if not fileitem:
            return self._json(400, {"code": 1, "msg": "未找到上传的文件"})

        fname, fdata = fileitem
        if not fdata:
            return self._json(400, {"code": 1, "msg": "上传的文件为空"})

        version_hint = (fields.get("version") or "").strip()

        # 落盘到缓存目录，交给后台任务安装
        try:
            os.makedirs(S.CACHE_DIR, exist_ok=True)
            tmp = os.path.join(S.CACHE_DIR, "upload_core.tmp")
            with open(tmp, "wb") as f:
                f.write(fdata)
        except OSError as e:
            return self._json(500, {"code": 1, "msg": "保存上传文件失败: %s" % e})

        # 立即做一次基本校验，让用户马上拿到明确错误
        ok, res = check_uploaded_file(tmp)
        if not ok:
            _rm(tmp)
            return self._json(400, {"code": 1, "msg": res})

        ok, msg = S.install_upload_in_background(tmp, fname, version_hint)
        return self._json(200 if ok else 409,
                          {"code": 0 if ok else 1, "msg": msg,
                           "data": {"filename": fname, "size": len(fdata)}})

    def _core_action(self, action):
        if action == "start":
            ok, msg = S.start_core()
            try:
                open(os.path.join(S.PKGVAR, ".autostart"), "w").close()
            except OSError:
                pass
        elif action == "stop":
            ok, msg = S.stop_core()
            try:
                os.remove(os.path.join(S.PKGVAR, ".autostart"))
            except OSError:
                pass
        else:
            S.stop_core()
            ok, msg = S.start_core()
        return self._json(200, {"code": 0 if ok else 1, "msg": msg,
                                "data": self._state()})

    def _state(self):
        cur = S.installed_version()
        inst = S.list_instances()
        return {
            "installed": cur,
            "has_core": os.path.isfile(S.CORE_BIN),
            "core_running": S.core_running(),
            "core_pid": S.core_pid(),
            "arch": S._machine(),
            "asset": S.asset_stem() or "",
            "mirror": S.read_mirror(),
            "proxy": S.read_proxy(),
            "proxy_is_socks": S.proxy_is_socks(),
            "has_curl": bool(S.have_curl()),
            "bind": S.read_bind(),
            "web_port": S.PORTS["VNT_WEB_PORT"],
            "panel_port": S.PORTS["PANEL_PORT"],
            "gateway_prefix": S.GATEWAY_PREFIX,
            "token_hint": S.token_hint(),
            "instance_count": len(inst),
            "is_admin": self._is_admin(),
            "update": S.update_snapshot(),
        }

    # --- 静态资源 -------------------------------------------------------
    def _mount(self, raw_path):
        """判断本应用当前的挂载前缀。

        直连访问时挂在 /，经飞牛统一网关时挂在 /app/<appname>。
        静态资源与页面内的相对路径都必须基于该前缀，
        否则用 /app/vnt-nas（没有结尾斜杠）访问时，
        浏览器会把 app.css 解析成 /app/app.css 而 404
        —— 曾导致页面无样式、脚本不执行。
        """
        pre = S.GATEWAY_PREFIX
        if raw_path == pre or raw_path.startswith(pre + "/"):
            return pre
        return ""

    def _static(self, path, raw_path="/"):
        if path in ("/", ""):
            path = "/index.html"
        rel = posixpath.normpath(path).lstrip("/")
        if rel.startswith(".."):
            return self._send(403, {"code": 1, "msg": "forbidden"})
        full = os.path.join(S.WEB_DIR, rel.replace("/", os.sep))
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            # SPA 回退
            idx = os.path.join(S.WEB_DIR, "index.html")
            if os.path.isfile(idx):
                full = idx
            else:
                return self._send(404, {"code": 1, "msg": "not found"})
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError as e:
            return self._send(500, {"code": 1, "msg": str(e)})

        # 给 HTML 注入 <base>：无论从 / 还是 /app/vnt-nas（无尾斜杠）
        # 访问，页面内相对路径都能正确解析到本应用的资源目录。
        if ext in (".html", ".htm"):
            mount = self._mount(raw_path)
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("utf-8", "replace")
            base = "%s/" % (mount or "")
            if "<base " not in text:
                inject = '<base href="%s">' % base
                if "<head>" in text:
                    text = text.replace("<head>", "<head>\n" + inject, 1)
                else:
                    text = inject + text
            data = text.encode("utf-8")

        return self._send(200, data, ctype)
