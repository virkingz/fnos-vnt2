#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vnt-nas 管理页后端。

职责：
  1. 托管管理页 UI（配置生成、核心版本与更新、日志）。
  2. 托管 vnt2_web 核心进程：启动、停止、健康检查、崩溃拉起。
  3. 反向代理 vnt2_web 的 Web UI 与 API，并注入 Bearer token。
  4. 从 GitHub Releases 检查并下载/替换核心二进制。

仅使用 Python 标准库，兼容 Python 3.8+。
监听：Unix socket（飞牛统一网关） + 0.0.0.0:<PANEL_PORT>（用户配置的对外端口）。
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
APPDEST = os.environ.get("VNT_APPDEST", "/var/apps/vnt-nas/target")
PKGVAR = os.environ.get("VNT_PKGVAR", "/var/apps/vnt-nas/var")
PKGETC = os.environ.get("VNT_PKGETC", "/var/apps/vnt-nas/etc")
APPNAME = os.environ.get("VNT_APPNAME", "vnt-nas")

BIN_DIR = os.path.join(PKGVAR, "bin")
CONF_DIR = os.path.join(PKGVAR, "vnt_config")
LOG_DIR = os.path.join(PKGVAR, "log")
CACHE_DIR = os.path.join(PKGVAR, "cache")
PORT_FILE = os.path.join(PKGVAR, "ports.env")
MIRROR_FILE = os.path.join(CONF_DIR, "mirror")
PROXY_FILE = os.path.join(CONF_DIR, "proxy")
BIND_FILE = os.path.join(PKGVAR, "bind")
TOKEN_FILE = os.path.join(BIN_DIR, "webtoken")
CORE_PID_FILE = os.path.join(PKGVAR, "core.pid")
CORE_VER_FILE = os.path.join(BIN_DIR, ".vnt_version")
CORE_SRC_FILE = os.path.join(BIN_DIR, ".vnt_source")
CORE_BIN = os.path.join(BIN_DIR, "vnt2_web")
CORE_LOG = os.path.join(LOG_DIR, "vnt2_web.log")
UPDATE_LOG = os.path.join(LOG_DIR, "update.log")
MAIN_LOG = os.path.join(LOG_DIR, "main.log")
SHELL_LOG = os.path.join(LOG_DIR, "shell.log")

# 日志上限：单个文件超过 LOG_MAX_BYTES 即原地截断，只保留最近 LOG_KEEP_BYTES。
# 为什么用「原地截断」而不是「改名轮转」：
#   shell.log 由后端进程以 >> 追加、vnt2_web.log 由核心进程持有 fd 追加，
#   改名会让写入方继续写旧 inode，轮转等于失效。
#   而两者都是 O_APPEND 打开，原地截断后下一次写入自然落到新末尾，安全有效。
LOG_MAX_BYTES = 512 * 1024
LOG_KEEP_BYTES = 256 * 1024

# HTTPS 面板代理所需的证书（自签，自动生成）
TLS_DIR = os.path.join(PKGVAR, "tls")
TLS_CERT = os.path.join(TLS_DIR, "panel.crt")
TLS_KEY = os.path.join(TLS_DIR, "panel.key")

WEB_DIR = os.path.join(APPDEST, "shell", "web")
SOCK_PATH = os.path.join(APPDEST, "app.sock")
GATEWAY_PREFIX = "/app/%s" % APPNAME

REPO = "vnt-dev/vnt"
RELEASES_LATEST = "https://github.com/%s/releases/latest" % REPO
API_LATEST = "https://api.github.com/repos/%s/releases/latest" % REPO
DL_BASE = "https://github.com/%s/releases/download" % REPO
# vnt 每个 release 都会附带 latest.json，比解析重定向更可靠
LATEST_JSON = "https://github.com/%s/releases/latest/download/latest.json" % REPO

# 端口默认值：PANEL_PORT 是用户唯一需要配置的端口
PANEL_PORT_DEFAULT = 19099
# vnt2_web 端口：组网面板直接用它对外提供服务（桌面第二个图标即指向此端口）
VNT_WEB_PORT_DEFAULT = 19199

UA = "vnt-nas-fpk/1.0"

for d in (BIN_DIR, CONF_DIR, LOG_DIR, CACHE_DIR):
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def _trim_log(path, max_bytes=None, keep_bytes=None):
    """日志超过上限时原地截断，只保留尾部。

    用「保留尾部」而非整体清空：出问题的现场往往在最后几屏，
    截断后仍能看到最近的活动，同时把文件压回安全大小。

    原地截断对 O_APPEND 写入方（后端进程、核心进程）是安全的——
    它们持有的是同一个 inode，下次写入直接落到新末尾。
    """
    limit = LOG_MAX_BYTES if max_bytes is None else max_bytes
    keep = LOG_KEEP_BYTES if keep_bytes is None else keep_bytes
    try:
        if os.path.getsize(path) <= limit:
            return False
    except OSError:
        return False
    try:
        with open(path, "rb") as f:
            f.seek(-keep, os.SEEK_END)
            tail = f.read()
        # 从换行处切开，避免留下半行
        nl = tail.find(b"\n")
        if 0 <= nl < len(tail) - 1:
            tail = tail[nl + 1:]
        with open(path, "wb") as f:
            f.write(("[log trimmed: 仅保留最近 %d KB]\n"
                     % (keep // 1024)).encode("utf-8"))
            f.write(tail)
        return True
    except OSError:
        return False


def trim_logs():
    """对全部日志做一次大小检查；返回被截断的文件名列表。"""
    trimmed = []
    for name, path in (("main", MAIN_LOG), ("update", UPDATE_LOG),
                       ("shell", SHELL_LOG), ("core", CORE_LOG)):
        if _trim_log(path):
            trimmed.append(name)
    return trimmed


def _log(path, msg):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass
    # 追加后检查大小（低频，开销可忽略）：只在超限时才真正读文件
    try:
        if os.path.getsize(path) > LOG_MAX_BYTES:
            _trim_log(path)
    except OSError:
        pass


def log(msg):
    _log(MAIN_LOG, msg)


# ---------------------------------------------------------------------------
# 端口与镜像
# ---------------------------------------------------------------------------
def read_ports():
    """读取端口配置。

    PANEL_PORT   : 用户唯一配置的端口，管理页与组网面板对外监听
    VNT_WEB_PORT : vnt2_web 内部端口，仅回环，由壳反代
    兼容早期版本仅含 SHELL_PORT 的配置。
    """
    ports = {"PANEL_PORT": PANEL_PORT_DEFAULT,
             "VNT_WEB_PORT": VNT_WEB_PORT_DEFAULT}
    raw = {}
    try:
        with open(PORT_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    raw[k.strip()] = v.strip()
    except OSError:
        pass

    if raw.get("PANEL_PORT", "").isdigit():
        ports["PANEL_PORT"] = int(raw["PANEL_PORT"])
    elif raw.get("SHELL_PORT", "").isdigit():
        ports["PANEL_PORT"] = int(raw["SHELL_PORT"])   # 旧配置迁移
    if raw.get("VNT_WEB_PORT", "").isdigit():
        ports["VNT_WEB_PORT"] = int(raw["VNT_WEB_PORT"])
    return ports


PORTS = read_ports()


def read_mirror():
    try:
        with open(MIRROR_FILE, "r", encoding="utf-8") as f:
            return f.read().strip().rstrip("/")
    except OSError:
        return ""


def url_with_mirror(url):
    m = read_mirror()
    return "%s/%s" % (m, url) if m else url


# ---------------------------------------------------------------------------
# 网络代理：与「镜像前缀」是两种不同机制，可同时使用
#   - 镜像前缀：把请求 URL 改写成镜像地址（URL 改写）
#   - 代理    ：让请求经由 SOCKS5 / HTTP 代理隧道出去（网络层）
# 支持形式：
#   socks5://127.0.0.1:1080     本地解析 DNS
#   socks5h://127.0.0.1:1080    由代理解析 DNS（穿透性更好）
#   socks4:// / socks4a://
#   http://127.0.0.1:7890       HTTP/HTTPS 代理
#   127.0.0.1:1080              省略协议时按 http 处理
# ---------------------------------------------------------------------------
def read_proxy():
    try:
        with open(PROXY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def save_proxy(raw):
    p = (raw or "").strip()
    if p and "://" not in p:
        p = "http://" + p          # 省略协议时按 HTTP 代理处理
    try:
        with open(PROXY_FILE, "w", encoding="utf-8") as f:
            f.write(p)
        os.chmod(PROXY_FILE, 0o600)
    except OSError as e:
        log("save proxy failed: %s" % e)
    return p


def proxy_scheme(p=None):
    p = read_proxy() if p is None else p
    if not p:
        return ""
    return p.split("://", 1)[0].lower() if "://" in p else "http"


def proxy_is_socks(p=None):
    return proxy_scheme(p).startswith("socks")


def have_curl():
    for c in ("/usr/bin/curl", "/usr/local/bin/curl", "curl"):
        try:
            r = subprocess.run([c, "--version"], capture_output=True, timeout=5)
            if r.returncode == 0:
                return c
        except Exception:  # noqa: BLE001
            continue
    return ""


def curl_proxy_args():
    """返回 curl 的代理参数列表；未配置代理时返回空列表。"""
    p = read_proxy()
    if not p:
        return []
    return ["--proxy", p]


# ---------------------------------------------------------------------------
# vnt2_web 的监听范围。
#
# 组网面板直接由 vnt2_web 以自己的端口对外提供（桌面第二个图标即指向它），
# 因此它需要对外监听：
#   0.0.0.0   —— 局域网内均可打开组网面板（默认）
#   127.0.0.1 —— 只在本机，需经管理页的反向代理访问（更安全）
# ---------------------------------------------------------------------------
def read_bind():
    """vnt2_web 的监听地址。默认 0.0.0.0（局域网可直接打开面板）。"""
    try:
        with open(BIND_FILE, "r", encoding="utf-8") as f:
            v = f.read().strip()
        if v in ("127.0.0.1", "0.0.0.0"):
            return v
    except OSError:
        pass
    return "0.0.0.0"


def save_bind(host):
    """保存监听范围，返回最终生效值。"""
    host = "0.0.0.0" if str(host).strip() == "0.0.0.0" else "127.0.0.1"
    try:
        with open(BIND_FILE, "w", encoding="utf-8") as f:
            f.write(host)
    except OSError as e:
        log("save bind failed: %s" % e)
    return host


# ---------------------------------------------------------------------------
# token
# ---------------------------------------------------------------------------
def get_token():
    """读取访问令牌；不存在则生成并落盘。vnt2_web 要求至少 16 位。"""
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            t = f.read().strip()
            if len(t) >= 16:
                return t
    except OSError:
        pass
    return set_token("")


def set_token(raw):
    """规范化并保存令牌。长度不足则生成随机值补齐。"""
    import secrets
    t = (raw or "").strip()
    if len(t) < 16:
        t = secrets.token_urlsafe(24)
    t = t[:64]
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(t)
        os.chmod(TOKEN_FILE, 0o600)
    except OSError as e:
        log("save token failed: %s" % e)
    return t


def token_hint():
    t = ""
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            t = f.read().strip()
    except OSError:
        return "未设置"
    if not t:
        return "未设置"
    return "%s****%s（%d 位）" % (t[:3], t[-3:], len(t)) if len(t) > 8 else "已设置"


# ---------------------------------------------------------------------------
# 架构映射
# ---------------------------------------------------------------------------
def asset_stem():
    m = _machine()
    table = {
        "x86_64": "x86_64-unknown-linux-musl",
        "amd64": "x86_64-unknown-linux-musl",
        "aarch64": "aarch64-unknown-linux-musl",
        "arm64": "aarch64-unknown-linux-musl",
        "armv7l": "armv7-unknown-linux-musleabihf",
        "armv7": "armv7-unknown-linux-musleabihf",
        "armv6l": "arm-unknown-linux-musleabihf",
    }
    triple = table.get(m)
    return "vnt2-%s" % triple if triple else None


def _machine():
    try:
        return os.uname().machine
    except AttributeError:
        return os.environ.get("TRIM_SYS_ARCH", "x86_64")


# ---------------------------------------------------------------------------
# HTTP 小工具
# ---------------------------------------------------------------------------
def is_local_url(url):
    """判断是否本机回环地址。

    访问本机服务绝不该走代理：代理通常无法回连宿主回环，
    会导致「Empty reply from server」这类失败
    （曾出现：访问核心 127.0.0.1:19199 被塞进代理隧道而失败）。
    """
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    return host.startswith("127.")


def resolve_effective_url(url, timeout=25):
    """解析重定向后的最终地址。

    有代理且目标非本机时用 curl（Python 的 urllib 无法经 SOCKS 代理取有效地址）；
    否则用 urllib。返回最终 URL 字符串。
    """
    use_proxy = bool(read_proxy()) and not is_local_url(url)

    if use_proxy:
        curl = have_curl()
        if curl:
            args = [curl, "-sSLI", "-o", os.devnull,
                    "--max-time", str(timeout),
                    "-w", "%{url_effective}", "-A", UA]
            args += curl_proxy_args()
            args.append(url)
            r = subprocess.run(args, capture_output=True, timeout=timeout + 15)
            if r.returncode != 0:
                raise RuntimeError(
                    (r.stderr or b"").decode("utf-8", "replace").strip()
                    or "curl 退出码 %d" % r.returncode)
            return r.stdout.decode("utf-8", "replace").strip()
        if proxy_is_socks():
            raise RuntimeError("SOCKS 代理需要 curl 支持，但系统未找到 curl")

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if use_proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": read_proxy(),
                                         "https": read_proxy()}))
        with opener.open(req, timeout=timeout) as resp:
            return resp.geturl()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.geturl()

def http_get(url, timeout=20, headers=None):
    """发起 GET 请求，返回 (status, body)。

    配置了代理时改用 curl：Python 标准库的 urllib 不支持 SOCKS5，
    而 curl 原生支持 socks5/socks5h/http/https。
    未配置代理、或目标是本机地址时沿用 urllib。
    """
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)

    use_proxy = bool(read_proxy()) and not is_local_url(url)

    if use_proxy:
        curl = have_curl()
        if curl:
            args = [curl, "-sSL", "--max-time", str(timeout),
                    "-w", "\n%{http_code}", "-A", UA]
            for k, v in hdrs.items():
                args += ["-H", "%s: %s" % (k, v)]
            args += curl_proxy_args()
            args.append(url)
            try:
                r = subprocess.run(args, capture_output=True, timeout=timeout + 15)
                if r.returncode != 0:
                    raise RuntimeError(
                        (r.stderr or b"").decode("utf-8", "replace").strip()
                        or "curl 退出码 %d" % r.returncode)
                out = r.stdout
                idx = out.rfind(b"\n")
                code = int(out[idx + 1:] or b"0")
                return code, out[:idx]
            except Exception as e:  # noqa: BLE001
                raise RuntimeError("经代理请求失败：%s" % e)
        # 没找到 curl：HTTP 代理仍可交给 urllib
        if not proxy_is_socks():
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": read_proxy(),
                                             "https": read_proxy()}))
            req = urllib.request.Request(url, headers=hdrs)
            with opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        raise RuntimeError("SOCKS 代理需要 curl 支持，但系统未找到 curl")

    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def http_raw(url, method="GET", headers=None, body=None, timeout=30):
    """向本机上游发起请求，返回 (status, body, headers)。

    专门用于「面板反向代理」：目标是 127.0.0.1 上的 vnt2_web，
    因此**不走代理**（本机地址始终直连，见 is_local_url），
    也不需要 curl，用标准库即可。支持任意方法与请求体。

    返回 (status_code, bytes, {header: value})。
    """
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)

    # 重定向不自动跟随：前端资源由代理逐一手动处理，跟随会打乱路径
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs2, newurl):
            return None

    # 必须显式禁用代理：目标是回环地址，而 urllib 默认会读取 http_proxy
    # 等环境变量，把本机请求也塞进代理隧道（曾导致连 127.0.0.1 失败）。
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect)
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        # 4xx/5xx 是上游的有效响应，原样透传给浏览器
        return e.code, e.read(), dict(e.headers.items())
    except urllib.error.URLError as e:
        # 连不上上游：向上抛，由调用方转成可读错误页
        raise RuntimeError(str(getattr(e, "reason", e)))
    with resp:
        return resp.status, resp.read(), dict(resp.headers.items())


def _urlopen(url, timeout=60, headers=None):
    """打开 URL 用于流式下载。

    返回一个带 .status / .headers / .read() 的对象，屏蔽 urllib 与 curl 的差异。
    配置代理且目标非本机时走 curl（支持 socks5），否则走 urllib。
    """
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)

    if read_proxy() and not is_local_url(url):
        curl = have_curl()
        if not curl:
            if proxy_is_socks():
                raise RuntimeError("SOCKS 代理需要 curl 支持，但系统未找到 curl")
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": read_proxy(),
                                             "https": read_proxy()}))
            return opener.open(urllib.request.Request(url, headers=hdrs),
                               timeout=timeout)
        return _CurlStream(curl, url, hdrs, timeout)

    return urllib.request.urlopen(
        urllib.request.Request(url, headers=hdrs), timeout=timeout)


class _CurlStream:
    """把 curl 的输出包装成类似 urllib 响应的对象。

    头部信息通过 -D 写到临时文件，主体从 stdout 流式读取，
    这样大文件下载不必先落到内存。
    """

    def __init__(self, curl, url, headers, timeout):
        import tempfile
        self._hdr = tempfile.NamedTemporaryFile(delete=False, suffix=".hdr")
        self._hdr.close()
        args = [curl, "-sSL", "--max-time", str(timeout),
                "-A", headers.get("User-Agent", UA),
                "-D", self._hdr.name, "-o", "-"]
        for k, v in headers.items():
            if k.lower() == "user-agent":
                continue
            args += ["-H", "%s: %s" % (k, v)]
        args += curl_proxy_args()
        args.append(url)

        self._proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._status = 0
        self._length = 0
        self._wait_headers()

    def _wait_headers(self):
        """等待 curl 写出响应头，解析状态码与内容长度。"""
        for _ in range(100):
            try:
                with open(self._hdr.name, "r", encoding="utf-8",
                          errors="replace") as f:
                    txt = f.read()
            except OSError:
                txt = ""
            if "\r\n\r\n" in txt or "\n\n" in txt:
                for line in txt.replace("\r\n", "\n").split("\n"):
                    low = line.lower()
                    if line.startswith("HTTP/"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            self._status = int(parts[1])
                    elif low.startswith("content-length:"):
                        v = line.split(":", 1)[1].strip()
                        if v.isdigit():
                            self._length = int(v)
                return
            time.sleep(0.1)

    @property
    def status(self):
        return self._status

    @property
    def headers(self):
        # 仅暴露下载进度需要的内容长度；用轻量字典模拟
        outer = self

        class _H:
            def get(self, key, default=None):
                if key.lower() == "content-length":
                    return str(outer._length) if outer._length else None
                return default
        return _H()

    def read(self, size=-1):
        if size is None or size < 0:
            return self._proc.stdout.read()
        return self._proc.stdout.read(size)

    def close(self):
        try:
            if self._proc.stdout:
                self._proc.stdout.close()
            self._proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass
        _try_rm(self._hdr.name)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# 版本号形态：仅允许 数字.数字[.数字] 可选预发布后缀
# 严格校验很关键：镜像会把重定向地址整段 URL 编码，
# 按 / 切分取末段会得到 https%3A%2F%2F... 这类垃圾值，
# 进而拼出错误下载地址导致 404。
_VER_RE = re.compile(r"^v?(\d+(?:\.\d+){1,3}(?:[-+.][0-9A-Za-z.\-]+)?)$")


def _normalize_version(raw):
    """从可能含有 URL 编码/路径的文本中提取干净版本号，失败返回 None。"""
    if not raw:
        return None
    s = str(raw).strip().strip('"').strip("'")

    # 先尝试 URL 解码：镜像/重定向可能给出编码后的整段地址
    if "%" in s:
        try:
            from urllib.parse import unquote
            s = unquote(s)
        except Exception:  # noqa: BLE001
            pass

    # 去掉常见的 URL 前缀、查询串与结尾斜杠
    s = s.split("?")[0].split("#")[0].rstrip("/")
    # 去掉压缩包后缀，避免把 "2.0.11.zip" 当成版本号
    for ext in (".zip", ".tar.gz", ".tgz", ".gz"):
        if s.lower().endswith(ext):
            s = s[: -len(ext)]

    # 取最后一段路径（正常形如 .../releases/tag/v2.0.11）
    cand = s.rsplit("/", 1)[-1] if "/" in s else s

    m = _VER_RE.match(cand.strip())
    if m:
        return m.group(1)

    # 兜底：从整串里扫描第一个版本号片段。
    # 允许前面是 v/V（如 asset 名里的 "-v2.0.11"），
    # 但要求版本号前不是其他字母或点，避免匹配到 x86_64 之类的片段。
    for m2 in re.finditer(
            r"(?:^|[^0-9A-Za-z.]|\bv)v?(\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.\-]+)?)",
            s):
        return m2.group(1)
    return None


def latest_version():
    """返回最新版本号（不含 v 前缀），失败抛异常。

    按可靠性依次尝试：
      1. latest.json —— vnt 官方随每个 release 发布的版本清单
      2. releases/latest 的 302 重定向（可被 ghproxy 类镜像代理）
      3. GitHub API
    每次都对结果做严格校验，避免把 URL 当版本号。
    """
    # 1) latest.json（走镜像前缀，最稳）
    for ver_src in (url_with_mirror(LATEST_JSON),
                    "%s/latest.json" % url_with_mirror(
                        "%s/latest" % DL_BASE.replace("/download", ""))):
        try:
            status, body = http_get(ver_src, timeout=20)
            if status == 200 and body:
                data = json.loads(body.decode("utf-8", "replace"))
                for key in ("version", "tag_name", "tag", "name"):
                    v = _normalize_version(data.get(key))
                    if v:
                        return v
        except Exception as e:  # noqa: BLE001
            log("latest.json failed (%s): %s" % (ver_src, e))

    # 2) 解析重定向
    try:
        eff = resolve_effective_url(url_with_mirror(RELEASES_LATEST), 25)
        v = _normalize_version(eff)
        if v:
            return v
        log("redirect gave unparsable value: %r" % str(eff)[:120])
    except Exception as e:  # noqa: BLE001
        log("redirect latest failed: %s" % e)

    # 3) GitHub API
    try:
        status, body = http_get(API_LATEST,
                                headers={"User-Agent": UA,
                                         "Accept": "application/vnd.github+json"})
        if status == 200:
            data = json.loads(body.decode("utf-8", "replace"))
            v = _normalize_version(data.get("tag_name"))
            if v:
                return v
    except Exception as e:  # noqa: BLE001
        log("api latest failed: %s" % e)

    raise RuntimeError("无法获取 vnt 最新版本，请检查网络、配置镜像，或改用「手动上传核心」")


def installed_version():
    try:
        with open(CORE_VER_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 更新状态（供前端轮询）
# ---------------------------------------------------------------------------
UPDATE_STATE = {
    "running": False,
    "stage": "idle",       # idle|checking|downloading|verifying|installing|restarting|done|error
    "progress": 0,
    "message": "",
    "target": "",
    "error": "",
    "finished_at": 0,
}
_update_lock = threading.Lock()


def update_set(**kw):
    with _update_lock:
        UPDATE_STATE.update(kw)


def update_snapshot():
    with _update_lock:
        return dict(UPDATE_STATE)


# ---------------------------------------------------------------------------
# 核心进程管理
# ---------------------------------------------------------------------------
_core_proc = None
_core_lock = threading.Lock()


def core_pid():
    try:
        with open(CORE_PID_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def core_running():
    p = core_pid()
    if p and pid_alive(p):
        return True
    # 端口兜底探测（vnt2_web 始终在回环监听）
    return _port_listening(PORTS["VNT_WEB_PORT"])


_probe_cache = {"key": None, "ok": False, "info": ""}


def probe_core(use_cache=True):
    """探测核心二进制能否运行，返回 (ok, 版本或错误)。

    结果按文件大小+mtime 缓存，避免每次「检查更新」都去执行一遍二进制。
    """
    if not os.path.isfile(CORE_BIN):
        return False, "核心二进制不存在"

    try:
        st = os.stat(CORE_BIN)
        key = (st.st_size, int(st.st_mtime))
    except OSError:
        key = None

    if use_cache and key is not None and _probe_cache["key"] == key:
        return _probe_cache["ok"], _probe_cache["info"]

    try:
        os.chmod(CORE_BIN, 0o755)
    except OSError:
        pass
    ok, info = False, ""
    try:
        r = subprocess.run([CORE_BIN, "--version"], capture_output=True,
                           timeout=10)
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        if r.returncode == 0:
            ok, info = True, (out or "ok")
        else:
            ok, info = False, (err or ("退出码 %d" % r.returncode))
    except subprocess.TimeoutExpired:
        ok, info = False, "执行超时（可能缺少运行库或架构不匹配）"
    except OSError as e:
        ok, info = False, "无法执行：%s" % e

    _probe_cache.update(key=key, ok=ok, info=info)
    return ok, info


def start_core():
    """启动 vnt2_web。返回 (ok, message)。"""
    global _core_proc
    with _core_lock:
        if core_running():
            return True, "核心已在运行"
        if not os.path.isfile(CORE_BIN):
            return False, "核心二进制不存在，请先下载核心"

        ok, info = probe_core()
        if not ok:
            return False, "核心无法运行：%s" % info

        token = get_token()
        # vnt2_web 的监听地址可配置（默认全部网卡）：
        # 这样 19199 端口本身仍可直连访问，作为反代失效时的兜底通道。
        # 反代走 /core/ 时用的是回环地址，与监听范围无关。
        host = read_bind()
        env = dict(os.environ)
        env["VNT_WEB_TOKEN"] = token
        args = [
            CORE_BIN,
            "--addr", "%s:%d" % (host, PORTS["VNT_WEB_PORT"]),
            "--token", token,
        ]
        try:
            lf = open(CORE_LOG, "ab")
        except OSError:
            lf = subprocess.DEVNULL
        try:
            # cwd 设为 PKGVAR：vnt2_web 会在 ./vnt_config/ 管理组网实例，
            # 与壳的 CONF_DIR 指向同一处，便于管理页展示配置清单。
            _core_proc = subprocess.Popen(
                args, cwd=PKGVAR, env=env,
                stdout=lf, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as e:  # noqa: BLE001
            return False, "启动核心失败: %s" % e

        try:
            with open(CORE_PID_FILE, "w", encoding="utf-8") as f:
                f.write(str(_core_proc.pid))
        except OSError:
            pass

        log("core started pid=%d bind=%s:%d"
            % (_core_proc.pid, host, PORTS["VNT_WEB_PORT"]))

        # 等待端口就绪
        probe_host = "127.0.0.1"
        for _ in range(25):
            if not pid_alive(_core_proc.pid):
                return False, "核心进程立即退出，请查看 vnt2_web.log"
            try:
                s = socket.socket()
                s.settimeout(1)
                s.connect((probe_host, PORTS["VNT_WEB_PORT"]))
                s.close()
                return True, "核心已启动"
            except OSError:
                time.sleep(0.4)
        return True, "核心已启动（端口尚未就绪）"


def _port_listening(port, host="127.0.0.1"):
    try:
        s = socket.socket()
        s.settimeout(1)
        s.connect((host, port))
        s.close()
        return True
    except OSError:
        return False


def stop_core():
    global _core_proc
    with _core_lock:
        p = core_pid()
        if _core_proc and _core_proc.poll() is None:
            p = _core_proc.pid
        if not p or not pid_alive(p):
            _try_rm(CORE_PID_FILE)
            _core_proc = None
            return True, "核心未在运行"
        log("stopping core pid=%d" % p)
        try:
            os.kill(p, 15)
        except OSError:
            pass
        for _ in range(20):
            if not pid_alive(p):
                break
            time.sleep(0.5)
        if pid_alive(p):
            try:
                os.kill(p, 9)
            except OSError:
                pass
            time.sleep(0.5)
        _try_rm(CORE_PID_FILE)
        _core_proc = None

        # 校验端口确实释放，避免「报告已停止但端口仍被占用」，
        # 否则随即重启会因端口冲突而失败
        for _ in range(10):
            if not _port_listening(PORTS["VNT_WEB_PORT"]):
                break
            time.sleep(0.3)
        if _port_listening(PORTS["VNT_WEB_PORT"]):
            log("WARN: port %d still listening after stop"
                % PORTS["VNT_WEB_PORT"])
            return True, "核心已发送停止信号，但端口仍被占用"
        return True, "核心已停止"


def _try_rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


# 核心自动拉起守护：核心异常退出时自动重启
_watch_fail = 0


def _watcher():
    global _watch_fail
    while True:
        time.sleep(10)
        try:
            # 日志体积巡检：shell.log / vnt2_web.log 由外部进程持有 fd 追加，
            # 不经过 _log()，因此这里统一兜底（仅是 stat，开销可忽略）。
            trim_logs()

            marker = os.path.join(PKGVAR, ".autostart")
            if not os.path.isfile(marker):
                continue
            if core_running():
                _watch_fail = 0
                continue
            if not os.path.isfile(CORE_BIN):
                continue
            if _watch_fail >= 3:
                # 连续失败后不再自动重试，避免刷日志；由用户手动启动
                continue
            log("watcher: core down, restarting (try %d)" % (_watch_fail + 1))
            ok, msg = start_core()
            if not ok:
                _watch_fail += 1
                log("watcher: restart failed: %s" % msg)
            else:
                _watch_fail = 0
        except Exception as e:  # noqa: BLE001
            log("watcher error: %s" % e)


# ---------------------------------------------------------------------------
# 核心更新
# ---------------------------------------------------------------------------
def rollback_core():
    """用备份恢复核心二进制与版本记录。返回 (ok, msg)。"""
    bak = os.path.join(CACHE_DIR, "vnt2_web.bak")
    if not os.path.isfile(bak):
        return False, "没有可用的备份"
    try:
        import shutil
        stop_core()
        shutil.copy2(bak, CORE_BIN)
        os.chmod(CORE_BIN, 0o755)
        # 恢复版本号：备份时一并存了旧版本，避免版本显示与实际二进制不符
        ver_bak = os.path.join(CACHE_DIR, "vnt2_web.bak.version")
        if os.path.isfile(ver_bak):
            with open(ver_bak, "r", encoding="utf-8") as f:
                old = f.read().strip()
            with open(CORE_VER_FILE, "w", encoding="utf-8") as f:
                f.write(old)
        else:
            _try_rm(CORE_VER_FILE)
        log("rolled back core from backup")
        return True, "已回滚到上一个版本"
    except OSError as e:
        return False, "回滚失败: %s" % e


def extract_core_from_archive(path, work):
    """从 zip（或裸二进制）中取出 vnt2_web，返回 (ok, 路径或错误信息)。"""
    import zipfile

    # 先认定为 zip
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            member = None
            for n in z.namelist():
                if os.path.basename(n) == "vnt2_web":
                    member = n
                    break
            if not member:
                # 退一步：找唯一像可执行文件的条目
                cands = [n for n in z.namelist()
                         if not n.endswith("/") and n.startswith("vnt2_")]
                if len(cands) == 1:
                    member = cands[0]
                else:
                    return False, "压缩包内未找到 vnt2_web"
            os.makedirs(work, exist_ok=True)
            z.extract(member, work)
            out = os.path.join(work, member)
            if not os.path.getsize(out):
                return False, "解压出的文件为空"
            return True, out

    # 不是 zip：按裸二进制处理（用户可能直接上传 vnt2_web）
    try:
        with open(path, "rb") as f:
            head = f.read(4)
        size = os.path.getsize(path)
    except OSError as e:
        return False, "无法读取文件：%s" % e

    if size < 1024 * 100:
        return False, "文件过小（%d 字节），不像是核心程序" % size

    # ELF / Mach-O / PE 魔数校验，避免上传错文件
    magic_ok = (head == b"\x7fELF" or head[:2] in (b"MZ",)
                or head[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"))
    if not magic_ok:
        return False, ("文件既不是 zip，也不是可执行程序"
                       "（期望 zip 压缩包或 vnt2_web 二进制）")
    return True, path


def _install_core_file(new_bin, ver_label, source_desc, step):
    """把已就绪的二进制安装到位：停核心 → 备份 → 原子替换 → 重启 → 失败回滚。

    下载与手动上传共用此流程，保证行为一致。
    """
    import shutil

    step(78, "停止当前核心", "installing")
    stop_core()

    step(84, "备份旧核心", "installing")
    bak = os.path.join(CACHE_DIR, "vnt2_web.bak")
    if os.path.isfile(CORE_BIN):
        shutil.copy2(CORE_BIN, bak)
        # 一并备份版本号，供回滚时恢复
        try:
            with open(os.path.join(CACHE_DIR, "vnt2_web.bak.version"),
                      "w", encoding="utf-8") as f:
                f.write(installed_version())
        except OSError:
            pass

    step(90, "替换核心二进制", "installing")
    staged = CORE_BIN + ".new"
    shutil.copy2(new_bin, staged)
    os.chmod(staged, 0o755)
    os.replace(staged, CORE_BIN)
    _probe_cache["key"] = None   # 二进制已替换，作废探测缓存

    if ver_label:
        with open(CORE_VER_FILE, "w", encoding="utf-8") as f:
            f.write(ver_label)
    with open(CORE_SRC_FILE, "w", encoding="utf-8") as f:
        f.write("version=%s\nsource=%s\ninstalled_at=%s\n"
                % (ver_label or "unknown", source_desc,
                   time.strftime("%Y-%m-%d %H:%M:%S")))

    step(95, "启动核心", "restarting")
    ok, msg = start_core()
    if not ok:
        _roll_ok, roll_msg = rollback_core()
        if _roll_ok:
            start_core()
            step(100, "启动失败，已回滚到旧版本", "error")
            return False, "新核心启动失败，已回滚（%s）。原因：%s" % (roll_msg, msg)
        step(100, "启动失败且无法回滚", "error")
        return False, "新核心启动失败且回滚失败：%s" % msg

    step(100, "核心已就绪", "done")
    update_set(finished_at=time.time(), error="")
    return True, "核心已安装"


def install_from_upload(tmp_file, filename="", version_hint=""):
    """安装用户手动上传的核心文件。返回 (ok, message)。"""
    work = os.path.join(CACHE_DIR, "upload_unpack")

    def step(p, msg, stage="installing"):
        update_set(progress=p, message=msg, stage=stage)
        _log(UPDATE_LOG, "[%d%%] %s" % (p, msg))

    try:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)

        step(20, "校验上传文件", "verifying")
        ok, res = extract_core_from_archive(tmp_file, work)
        if not ok:
            update_set(stage="error", error=res)
            return False, res
        new_bin = res

        # 版本号：优先用户填写，其次从文件名里猜，最后从二进制里探测
        ver_label = _normalize_version(version_hint) or ""
        if not ver_label and filename:
            ver_label = _normalize_version(filename) or ""
        if not ver_label:
            ver_label = _probe_version_of(new_bin) or ""

        step(60, "准备安装", "installing")
        ok, msg = _install_core_file(
            new_bin, ver_label or "",
            "upload:%s" % (filename or "core"), step)
        if ok and ver_label:
            msg = "核心已安装（版本 %s）" % ver_label
        elif ok:
            msg = "核心已安装（未提供版本号，将显示为手动安装）"
        _try_rm(tmp_file)
        shutil.rmtree(work, ignore_errors=True)
        return ok, msg

    except Exception as e:  # noqa: BLE001
        m = "安装上传文件失败: %s" % e
        update_set(stage="error", error=m)
        return False, m
    finally:
        _try_rm(tmp_file)


def _probe_version_of(binary):
    """尝试执行二进制取版本号（失败返回空）。"""
    try:
        os.chmod(binary, 0o755)
        r = subprocess.run([binary, "--version"], capture_output=True, timeout=10)
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        return _normalize_version(out.strip()) or _normalize_version(err.strip()) or ""
    except Exception:  # noqa: BLE001
        return ""


def install_upload_in_background(tmp_file, filename="", version_hint=""):
    """在后台安装上传的核心文件。"""
    def run():
        update_set(running=True, stage="installing", progress=0,
                   message="正在安装上传的核心", error="", finished_at=0)
        try:
            ok, msg = install_from_upload(tmp_file, filename, version_hint)
            if not ok:
                update_set(stage="error", error=msg, message=msg, progress=100)
            else:
                update_set(message=msg)
        except Exception as e:  # noqa: BLE001
            update_set(stage="error", error=str(e), message=str(e), progress=100)
        finally:
            update_set(running=False)

    if update_snapshot()["running"]:
        _try_rm(tmp_file)
        return False, "已有更新任务在进行中"
    threading.Thread(target=run, daemon=True).start()
    return True, "已开始安装上传的核心"


def do_update(version=None, progress_cb=None):
    """下载并安装指定版本（默认最新）。返回 (ok, message)。"""
    stem = asset_stem()
    if not stem:
        return False, "当前架构 %s 无官方预编译包" % _machine()

    try:
        ver = version or latest_version()
    except Exception as e:  # noqa: BLE001
        return False, str(e)

    asset = "%s-v%s.zip" % (stem, ver)
    url = url_with_mirror("%s/v%s/%s" % (DL_BASE, ver, asset))
    tmp_zip = os.path.join(CACHE_DIR, asset + ".download")
    work = os.path.join(CACHE_DIR, "unpack")

    def step(p, msg, stage="downloading"):
        update_set(progress=p, message=msg, stage=stage)
        _log(UPDATE_LOG, "[%d%%] %s" % (p, msg))
        if progress_cb:
            progress_cb(p, msg)

    try:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)

        step(5, "准备下载 %s" % asset, "downloading")
        # 经 _urlopen：配置代理时自动走 curl（支持 socks5）
        with _urlopen(url, timeout=600) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            with open(tmp_zip, "wb") as out:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    got += len(chunk)
                    if total > 0:
                        step(5 + int(got * 55 / total),
                             "下载中 %d%%" % int(got * 100 / total))
                    else:
                        step(45, "下载中 %.1f MB" % (got / 1048576.0))
        step(62, "下载完成，校验压缩包", "verifying")

        import zipfile
        if not zipfile.is_zipfile(tmp_zip):
            return False, "下载文件不是有效 zip，可能被镜像劫持或网络中断"
        step(70, "解压核心", "verifying")
        ex_ok, res = extract_core_from_archive(tmp_zip, work)
        if not ex_ok:
            return False, res
        new_bin = res

        # 复用统一的安装流程（停核心 → 备份 → 原子替换 → 重启 → 回滚）
        ok, msg = _install_core_file(new_bin, ver, url, step)
        _try_rm(tmp_zip)
        import shutil as _sh
        _sh.rmtree(work, ignore_errors=True)
        if not ok:
            return False, msg
        return True, "核心已更新到 %s" % ver

    except urllib.error.HTTPError as e:
        msg = "下载失败 HTTP %s（镜像可能未同步该版本）" % e.code
        update_set(stage="error", error=msg)
        return False, msg
    except Exception as e:  # noqa: BLE001
        msg = "更新失败: %s" % e
        update_set(stage="error", error=msg)
        return False, msg


def update_in_background(version=None):
    def run():
        update_set(running=True, stage="checking", progress=0,
                   message="检查版本", error="", finished_at=0)
        try:
            ver = version
            if not ver:
                ver = latest_version()
            update_set(target=ver, stage="downloading", progress=2,
                       message="目标版本 v%s" % ver)
            ok, msg = do_update(ver)
            if not ok:
                update_set(stage="error", error=msg, message=msg, progress=100)
        except Exception as e:  # noqa: BLE001
            update_set(stage="error", error=str(e), message=str(e), progress=100)
        finally:
            update_set(running=False)

    if update_snapshot()["running"]:
        return False, "已有更新任务在进行中"
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return True, "更新任务已启动"


# ---------------------------------------------------------------------------
# 组网配置：由 vnt2_web 自行在 vnt_config/ 下管理，壳只做只读展示
# ---------------------------------------------------------------------------
def config_dir():
    """vnt2_web 的实例配置目录（cwd=PKGVAR 时即 vnt_config/）。"""
    return os.path.join(PKGVAR, "vnt_config")


def list_instances():
    """列出 vnt2_web 管理的配置文件，供管理页展示。"""
    out = []
    d = config_dir()
    try:
        for name in sorted(os.listdir(d)):
            if not name.endswith(".toml"):
                continue
            path = os.path.join(d, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            out.append({
                "file_name": name,
                "size": st.st_size,
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(st.st_mtime)),
            })
    except OSError:
        pass
    return out


def read_instance(name):
    """读取某个实例配置原文（做路径校验，防目录穿越）。"""
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    if not name.endswith(".toml"):
        name += ".toml"
    path = os.path.join(config_dir(), name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def tail(path, n=200):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


def remove_gateway_prefix(path):
    if path == GATEWAY_PREFIX:
        return "/"
    if path.startswith(GATEWAY_PREFIX + "/"):
        return path[len(GATEWAY_PREFIX):]
    return path
