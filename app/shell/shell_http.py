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
from urllib.parse import urlparse, parse_qs, unquote, quote

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
    """构造组网面板地址（同源子路径，实现原生应用形态）。

    经本应用反代到 /core/，因此：
      - 与管理页同源，可安全 iframe 内嵌（不受混合内容限制）
      - 地址栏是飞牛的 :5666/app/vnt-nas，看起来就是原生应用的一部分
      - 令牌由代理注入 Authorization，页面内无需手工粘贴

    vnt2_web 原生的 /?token=xxx 仍作为兜底保留：若反代链路异常，
    可改用带令牌的直连地址。
    """
    mount = handler._mount(getattr(handler, "path", "") or "/")
    url = "%s%s/" % (mount, PROXY_PREFIX)
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


# 匹配「引号 + 单斜杠 + 字母开头的路径」。
# 必须限定斜杠后是字母，否则会把下面这些一起误改：
#   SVG 自闭合标签  stroke='round'/%3E   → 斜杠后是 '%' 或 '>'
#   协议相对地址    "//host/x"            → 斜杠后是 '/'
#   代码注释        "/* ... */"           → 斜杠后是 '*'
# 真实资源路径（/assets/、/api/ 等）斜杠后都是字母，因此能精确命中。
_ABS_PATH_RE = re.compile(r'(["\'`])/(?=[A-Za-z])')


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


def inject_token(html, token, mount=""):
    """向 vnt2_web 的 HTML 注入 <base>、访问令牌与 API 接管钩子。

    这三件事共同解决「子路径托管 SPA」的问题：

    1) **`<base>` —— 关键**
       Vue Router 的 createWebHistory() 未显式传 base 时会读 `<base href>`：
           function normalizeBase(base){
             if(!base){ const el=document.querySelector('base')
                        base=(el&&el.getAttribute('href'))||'/' }
             ...
           }
       不注入时 base 取 `/`，而实际路径是 `/app/vnt-nas/core/`，
       路由表里没有这一条 → 渲染空白；直连路径恰为 `/` 所以正常。
       注入后 base 正确，路由即可匹配。

    2) **令牌**
       vnt2_web 的登录态存在 localStorage，且按「源」隔离。
       直连时令牌存在那个源；经本应用内嵌是另一个源，读不到便显示登录页。
       ⚠ 绝不能设 globalThis.__VNT_DESKTOP__：该标志为真时前端会把令牌
       置空（`At ? "" : ...`），而我们没接管 API，会导致全部请求 401。

    3) **`__VNT_API_REQUEST__` —— 官方 API 接管口**
       前端所有请求都经 De() 发出，而 De() 开头即：
           if(globalThis.__VNT_API_REQUEST__){
             const l = await globalThis.__VNT_API_REQUEST__({method,path,body})
             if(l.code!==0) throw ...; return l.data }
       定义该钩子后，前端不再自己 fetch，全部交给宿主。
       于是我们可自行拼出正确的代理地址并附带令牌——
       **无需改写 JS**，从根本上避免改坏压缩代码。
    """
    import json as _json
    if "vnt-web-access-token" in html:
        return html

    base_href = (mount or "") + PROXY_PREFIX + "/"
    api_prefix = (mount or "") + PROXY_PREFIX

    js = (
        "try{"
        "localStorage.setItem('vnt-web-access-token'," + _json.dumps(token) + ");"
        "}catch(e){}"
        # 接管全部 API：拼上代理前缀并附带令牌
        "globalThis.__VNT_API_REQUEST__=function(o){"
        "o=o||{};"
        "var u=" + _json.dumps(api_prefix) + "+(o.path||'');"
        "var h={'Content-Type':'application/json',"
        "'Authorization':'Bearer '+" + _json.dumps(token) + "};"
        "var init={method:o.method||'GET',headers:h,cache:'no-store'};"
        "if(o.body!=null&&o.method&&o.method!=='GET')"
        "init.body=typeof o.body==='string'?o.body:JSON.stringify(o.body);"
        "return fetch(u,init).then(function(r){"
        "return r.text().then(function(t){"
        "try{return JSON.parse(t)}catch(e){"
        "return {code:-1,msg:'非 JSON 响应('+r.status+')',data:null}};});"
        "});};"
        # 修正 <img> 的绝对路径。
        # 前端把资源写成绝对路径（如 <img src="/assets/vnt-icon-xxx.png">），
        # 且该路径在 minify 后的 JS 里，无法靠改写 HTML 覆盖；
        # 不改写 JS（minify 代码做字符串替换风险高），改为运行时补前缀。
        #
        # 触发条件：只要面板是经本应用 /core/ 代理的，src 的绝对路径就会
        # 落到站点根而 404（base 标签对绝对路径无效），因此必须补前缀；
        # 已带前缀或本就正确的一律跳过，保证幂等。
        "(function(){"
        "var P=" + _json.dumps(api_prefix + "/") + ";"
        "function fix(el){"
        "if(!el||el.nodeName!=='IMG')return;"
        "var raw=el.getAttribute('src');"
        "if(!raw)return;"
        "var s=raw.trim();"
        "if(s.charAt(0)!=='/'||s.slice(0,2)==='//')return;"
        "if(s.indexOf(P)===0)return;"
        "el.setAttribute('src',P+s.slice(1));"
        "}"
        "function fixAll(root){"
        "if(!root||!root.querySelectorAll)return;"
        "var l=root.querySelectorAll('img');"
        "for(var i=0;i<l.length;i++)fix(l[i]);"
        "}"
        "function ready(){"
        "fixAll(document);"
        "try{new MutationObserver(function(ms){"
        "for(var i=0;i<ms.length;i++){"
        "var m=ms[i];"
        "if(m.type==='attributes'){fix(m.target);continue;}"
        "var ns=m.addedNodes;"
        "if(!ns)continue;"
        "for(var j=0;j<ns.length;j++){var n=ns[j];"
        "if(n.nodeType===1){fix(n);fixAll(n);}}}"
        "}).observe(document.documentElement,"
        "{childList:true,subtree:true,attributes:true,attributeFilter:['src']});"
        "}catch(e){}"
        "}"
        "if(document.readyState==='loading')"
        "document.addEventListener('DOMContentLoaded',ready);"
        "else ready();"
        "})();"
    )
    script = "<script>%s</script>" % js
    # base 必须最先出现，且要早于任何相对 URL
    base_tag = '<base href="%s">' % base_href

    # 插在 <head> 之后：base 在最前，其次是脚本，确保都早于应用脚本执行
    inject = base_tag + script
    lower = html.lower()
    idx = lower.find("<head")
    if idx != -1:
        end = html.find(">", idx)
        if end != -1:
            return html[:end + 1] + inject + html[end + 1:]
    idx = lower.find("<html")
    if idx != -1:
        end = html.find(">", idx)
        if end != -1:
            return html[:end + 1] + inject + html[end + 1:]
    return inject + html


def jdump(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


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
            # 设置 vnt2_web 的监听范围（仅本机 / 全部网卡）。
            # 组网面板由它自己的端口对外提供，因此改完需重启核心生效。
            if not self._is_admin():
                return self._json(403, {"code": 1, "msg": "需要管理员权限"})
            body = self._body()
            host = S.save_bind(body.get("bind") or "0.0.0.0")
            restarted = ""
            if S.core_running():
                S.stop_core()
                restarted = S.start_core()[1]
            tip = ("核心已监听全部网卡（局域网可直接打开组网面板）"
                   if host == "0.0.0.0" else "核心仅监听本机回环")
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
            # 面板地址 = 本应用的同源子路径 /core/（由 _proxy_panel 反代）。
            # 同源带来两个好处：
            #   1. 可直接 iframe 内嵌，不受浏览器混合内容策略限制
            #   2. 地址栏保持飞牛的 :5666/app/vnt-nas，形如原生应用
            # 令牌由反代注入 Authorization，页面内无需手工粘贴。
            return self._json(200, {"code": 0, "msg": "success",
                                    "data": {
                                        "port": S.PORTS["VNT_WEB_PORT"],
                                        "url": panel_url_for(self),
                                        "display_url": panel_url_bare(self),
                                        "panel_path": PROXY_PREFIX + "/",
                                        "has_token": bool(S.get_token()),
                                        "core_running": S.core_running(),
                                        "bind": S.read_bind(),
                                    }})

        if path == "/api/probe_proxy":
            # 对照诊断：把「直连核心」与「经本应用 /core/ 代理」的响应
            # 放在一起比对，找出差异。这是定位白屏的最直接手段。
            return self._json(200, {"code": 0, "msg": "success",
                                    "data": self._probe_proxy(raw_path)})

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
                     "shell": S.SHELL_LOG, "core": S.CORE_LOG}
            if name not in files:
                return self._json(400, {"code": 1, "msg": "未知日志"})
            return self._json(200, {"code": 0, "msg": "success",
                                    "data": {"name": name,
                                             "size": _file_size(files[name]),
                                             "limit": S.LOG_MAX_BYTES,
                                             "text": S.tail(files[name], 300)}})

        if path == "/api/logs_clear" and method == "POST":
            # 手动清空全部日志（截断为 0，不做删除，避免持有 fd 的写入方错位）
            if not self._is_admin():
                return self._json(403, {"code": 1, "msg": "需要管理员权限"})
            body = self._body()
            which = (body.get("name") or "all").strip()
            files = {"main": S.MAIN_LOG, "update": S.UPDATE_LOG,
                     "shell": S.SHELL_LOG, "core": S.CORE_LOG}
            if which != "all" and which not in files:
                return self._json(400, {"code": 1, "msg": "未知日志"})
            targets = (files.values() if which == "all"
                       else [files[which]])
            cleared = []
            for p in targets:
                try:
                    with open(p, "wb"):
                        pass
                    cleared.append(os.path.basename(p))
                except OSError:
                    pass
            S.log("logs cleared: %s" % (", ".join(cleared) or "无"))
            return self._json(200, {"code": 0, "msg": "已清空",
                                    "data": {"cleared": cleared}})

        if path.startswith(PROXY_PREFIX + "/") or path == PROXY_PREFIX:
            # 把 vnt2_web 的原生页面代理到本应用路径下，实现「原生应用形态」：
            # 经飞牛统一网关访问时是同源的 HTTPS，可直接 iframe 内嵌，
            # 不存在混合内容拦截问题，地址栏也是飞牛的 :5666。
            return self._proxy_panel(raw_path, path)

        return self._static(path, raw_path)

    # --- vnt2_web 反向代理 -------------------------------------------------
    def _proxy_panel(self, raw_path, path):
        """把 /core/** 的请求转发给 vnt2_web，并改写响应中的绝对路径。

        与「改磁盘上的二进制」不同：这里拿到的是 HTTP 响应里的纯文本
        （HTML / JS / CSS），不存在 ELF 机器码问题，可安全改写。

        三条改写规则（见 rewrite_paths）：
          HTML: href="/assets/x.css"  src='/assets/x.js'
          JS:   "/api/version"  fetch("/api/instances")
        改写为带本应用挂载点的前缀，浏览器才会从本应用取资源。

        另注入 Authorization，使内嵌后无需手工粘贴令牌。
        """
        mount = self._mount(raw_path)      # 直连为 ""，网关为 /app/vnt-nas
        prefix = mount + PROXY_PREFIX      # 如 /app/vnt-nas/core

        # 去掉挂载点与 /core，还原成 vnt2_web 认识的路径
        rest = path[len(PROXY_PREFIX):] or "/"
        if not rest.startswith("/"):
            rest = "/" + rest
        qs = ""
        if "?" in self.path:
            qs = "?" + self.path.split("?", 1)[1]
        target_path = rest + qs

        upstream = "http://127.0.0.1:%d%s" % (S.PORTS["VNT_WEB_PORT"],
                                              target_path)
        headers = {"Authorization": "Bearer %s" % S.get_token()}
        # 透传少数对渲染有影响的头
        for k in ("Accept", "Accept-Language", "Range", "User-Agent"):
            v = self.headers.get(k)
            if v:
                headers[k] = v

        body = None
        clen = self.headers.get("Content-Length")
        if clen and clen.isdigit() and int(clen) > 0:
            body = self.rfile.read(int(clen))
            headers["Content-Type"] = (self.headers.get("Content-Type")
                                       or "application/octet-stream")

        try:
            code, data, hdrs = S.http_raw(upstream, method=self.command,
                                          headers=headers, body=body,
                                          timeout=30)
        except Exception as e:  # noqa: BLE001
            return self._error_page(
                502, "无法连接组网面板核心",
                "原因：%s<br>请确认核心已下载并已启动。" % e)

        ctype = ""
        for k, v in (hdrs or {}).items():
            if k.lower() == "content-type":
                ctype = v
                break

        # 只改写 HTML：其中的 /assets/... 是绝对路径，浏览器会从站点根取。
        # **不改写 JS**：API 已由注入的 __VNT_API_REQUEST__ 钩子接管，
        # 且 minify 后的 JS 做字符串替换风险高（曾疑似改坏导致白屏）。
        # 图片等二进制一律原样透传。
        if data and "text/html" in ctype:
            try:
                text = rewrite_paths(data.decode("utf-8"), prefix)
                text = inject_token(text, S.get_token(), mount)
                data = text.encode("utf-8")
            except UnicodeDecodeError:
                pass

        # 转发上游的响应头，但要避开「本地会重新生成/已发送」的头，
        # 否则会出现重复（实测曾出现两个 Server、两个 Date、两个
        # Cache-Control），部分网关/浏览器会因此行为异常。
        skip = {
            "content-type", "content-length",      # 由 _send 按改写后的长度重发
            "connection", "transfer-encoding", "keep-alive",
            "server", "date",                      # send_response 会自动补
            "cache-control",                       # _send 统一设为 no-store
        }
        out = {}
        for k, v in (hdrs or {}).items():
            if k.lower() in skip:
                continue
            out[k] = v
        return self._send(code, data, ctype or "application/octet-stream", out)

    def _error_page(self, code, title, detail):
        """代理失败时的可读错误页，避免浏览器只显示白屏。"""
        import html as _h
        body = (
            "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>%s</title></head><body style=\"font-family:system-ui,"
            "-apple-system,'Segoe UI',sans-serif;max-width:600px;margin:14vh auto;"
            "padding:0 20px;color:#2c2c2a\">"
            "<h1 style=\"font-size:18px;font-weight:500;margin:0 0 12px\">%s</h1>"
            "<p style=\"font-size:14px;line-height:1.7;color:#5f5e5a\">%s</p>"
            "</body></html>" % (_h.escape(title), _h.escape(title), detail))
        return self._send(code, body, "text/html; charset=utf-8",
                          {"Cache-Control": "no-store"})

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
        """组网面板的直连地址（监听范围为全部网卡时局域网可用）。"""
        bind = bind or S.read_bind()
        if bind != "0.0.0.0":
            return ""
        req_host = (self.headers.get("Host") or "").split(":")[0].strip()
        if not req_host or req_host in ("localhost", "127.0.0.1", "::1"):
            req_host = self._lan_ip() or "127.0.0.1"
        return "http://%s:%d/" % (req_host, S.PORTS["VNT_WEB_PORT"])

    def _probe_proxy(self, raw_path=""):
        """比对「直连核心」与「经本应用代理」的响应差异。

        白屏排查时，光看「能不能取到」不够，还要看两者内容是否一致。
        这里把两边的状态码、长度、指纹、以及页面引用的资源列表并列，
        差异一眼可见。
        """
        cop = "http://127.0.0.1:%d/" % S.PORTS["VNT_WEB_PORT"]
        tok = S.get_token()
        auth = {"Authorization": "Bearer %s" % tok} if tok else {}
        out = {"direct": {}, "proxy": {}, "diff": []}

        # ① 直连核心
        try:
            code, body = S.http_get(cop, timeout=8, headers=auth)
            out["direct"] = {"status": code, "len": len(body),
                             "head": body[:160].decode("utf-8", "replace")}
        except Exception as e:  # noqa: BLE001
            out["direct"] = {"error": str(e)}

        # ② 经本应用 /core/
        mount = self._mount(raw_path)
        purl = "http://127.0.0.1:%d%s%s/" % (S.PORTS["PANEL_PORT"],
                                            mount or "", PROXY_PREFIX)
        try:
            code, body = S.http_get(purl, timeout=8)
            txt = body.decode("utf-8", "replace")
            refs = re.findall(r'(?:src|href)="([^"]+)"', txt)
            out["proxy"] = {
                "url": purl, "status": code, "len": len(body),
                "head": txt[:160],
                "has_token_script": "vnt-web-access-token" in txt,
                "refs": refs[:8],
                "refs_count": len(refs),
            }
        except Exception as e:  # noqa: BLE001
            out["proxy"] = {"url": purl, "error": str(e)}

        # ③ 差异
        d = out.get("direct", {})
        p = out.get("proxy", {})
        if d.get("error"):
            out["diff"].append("直连核心失败：%s" % d["error"])
        if p.get("error"):
            out["diff"].append("经代理失败：%s" % p["error"])
        if not out["diff"]:
            if abs(d.get("len", 0) - p.get("len", 0)) > 400:
                out["diff"].append(
                    "两边长度差异较大（直连 %s / 代理 %s），"
                    "代理版多了注入脚本属正常，但需确认注入内容正确"
                    % (d.get("len"), p.get("len")))
            if not p.get("has_token_script"):
                out["diff"].append("代理版**没有**注入令牌脚本 → 会停在登录页")
            for r in p.get("refs", []):
                if r.startswith("/assets/"):
                    out["diff"].append(
                        "资源引用**未改写**：%s → 浏览器会 404，必然白屏" % r)
            if not out["diff"]:
                out["diff"].append("两边均正常，未见明显差异")
        return out

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

        # 面板端口能否取到首页（组网面板由 vnt2_web 自己的端口提供）
        panel_ok, panel_detail = False, ""
        if core_up and has_token:
            try:
                code, body = S.http_get(
                    "http://127.0.0.1:%d/" % S.PORTS["VNT_WEB_PORT"], timeout=8,
                    headers={"Authorization": "Bearer %s" % S.get_token()})
                panel_ok = (code == 200 and body)
                panel_detail = ("面板首页可访问（%d 字节）" % len(body)
                                if panel_ok else "面板首页返回 HTTP %s" % code)
            except Exception as e:  # noqa: BLE001
                panel_detail = "访问面板失败：%s" % e
        else:
            panel_detail = "核心未运行或缺少令牌，跳过"
        add("组网面板可访问", panel_ok, panel_detail)

        # 关键：内嵌用的是 /core/ 代理路径，必须单独验证这条链路。
        # 上面那项测的是「直连核心」，与内嵌无关——曾因此误判「全绿却空白」。
        mount = self._mount(raw_path)
        proxy_url = "http://127.0.0.1:%d%s%s/" % (
            S.PORTS["PANEL_PORT"], mount or "", PROXY_PREFIX)
        proxy_ok, proxy_detail = False, ""
        try:
            code, body = S.http_get(proxy_url, timeout=8)
            txt = body.decode("utf-8", "replace")
            if code != 200:
                proxy_detail = "代理返回 HTTP %s（内嵌会失败）" % code
            elif "<div" not in txt and "<html" not in txt.lower():
                proxy_detail = ("代理返回的不是页面（%d 字节）：%s"
                                % (len(body), txt[:120]))
            else:
                want = (mount or "") + PROXY_PREFIX + "/assets/"
                proxy_ok = True
                proxy_detail = ("代理页面可访问（%d 字节）" % len(body)
                                + ("，资源路径已改写"
                                   if want in txt
                                   else "，但未发现已改写的资源路径"))
            add("内嵌代理链路（/core/）", proxy_ok, proxy_detail)
        except Exception as e:  # noqa: BLE001
            add("内嵌代理链路（/core/）", False,
                "代理请求失败：%s —— 这就是内嵌空白的原因" % e)

        # 网关挂载前缀是否正确识别
        add("挂载前缀识别", True,
            "当前前缀：%s" % (mount or "/（直连）"))

        # 面板地址（展示用，不含令牌）
        panel_url = panel_url_bare(self)
        add("面板地址", True, panel_url + "（跳转打开时会自动带令牌）")

        # vnt2_web 的监听范围
        bind = S.read_bind()
        add("核心监听范围", True,
            "全部网卡（0.0.0.0），局域网可直接打开 %s"
            % self._panel_direct_url()
            if bind == "0.0.0.0"
            else "仅本机（127.0.0.1），仅这台机器可访问")

        skip = ("挂载前缀识别", "面板地址", "核心监听范围")
        all_ok = all(i["ok"] for i in items if i["name"] not in skip)
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
