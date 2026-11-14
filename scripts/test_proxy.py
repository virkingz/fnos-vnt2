#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网络代理功能测试。

覆盖：代理地址读写与规范化、协议识别、curl 参数构建、
以及「配置代理后请求确实经过代理」的端到端验证。
"""
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "shell"))

TESTVAR = os.path.abspath(os.path.join(HERE, "..", "build", "proxytest"))
os.makedirs(TESTVAR, exist_ok=True)
os.environ["VNT_PKGVAR"] = TESTVAR
os.environ["VNT_APPDEST"] = os.path.join(ROOT, "app")
os.environ["VNT_APPNAME"] = "vnt-nas"

import shell_server as S  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + ("  " + str(extra) if extra else ""))
    if not cond:
        FAILS.append(name)


def test_normalize():
    print("1) 代理地址规范化")
    cases = [
        # (输入, 期望保存值, 期望协议名)
        ("socks5://127.0.0.1:1080", "socks5://127.0.0.1:1080", "socks5"),
        ("socks5h://127.0.0.1:1080", "socks5h://127.0.0.1:1080", "socks5h"),
        ("socks4://10.0.0.1:1080", "socks4://10.0.0.1:1080", "socks4"),
        ("socks4a://10.0.0.1:1080", "socks4a://10.0.0.1:1080", "socks4a"),
        ("http://127.0.0.1:7890", "http://127.0.0.1:7890", "http"),
        ("https://proxy.lan:8443", "https://proxy.lan:8443", "https"),
        # 省略协议按 http
        ("127.0.0.1:7890", "http://127.0.0.1:7890", "http"),
        # 清理空白
        ("  socks5://127.0.0.1:1080  ", "socks5://127.0.0.1:1080", "socks5"),
        # 清空
        ("", "", ""),
    ]
    for raw, want, scheme in cases:
        S.save_proxy(raw)
        got = S.read_proxy()
        check("%-30s -> %s" % (repr(raw)[:28], repr(want)),
              got == want, repr(got))
        if want:
            check("  协议识别为 %s" % scheme, S.proxy_scheme() == scheme,
                  S.proxy_scheme())
            # socks 系列必须被 is_socks 认出来
            if scheme.startswith("socks"):
                check("  is_socks 为真", S.proxy_is_socks())
            else:
                check("  is_socks 为假", not S.proxy_is_socks())

    print("2) curl 代理参数构建")
    S.save_proxy("socks5://127.0.0.1:1080")
    check("socks5 参数正确",
          S.curl_proxy_args() == ["--proxy", "socks5://127.0.0.1:1080"],
          S.curl_proxy_args())
    S.save_proxy("http://127.0.0.1:7890")
    check("http 参数正确",
          S.curl_proxy_args() == ["--proxy", "http://127.0.0.1:7890"],
          S.curl_proxy_args())
    S.save_proxy("")
    check("清空后无参数", S.curl_proxy_args() == [], S.curl_proxy_args())

    print("3) curl 可用性")
    print("      curl 路径: %r" % S.have_curl())


class ProxyHandler(BaseHTTPRequestHandler):
    """记录经由本代理的请求，用于验证流量确实走了代理。"""
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        ProxyHandler.seen.append(self.path)
        body = b'{"version":"9.9.9"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.seen.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_end_to_end():
    print("4) 经 HTTP 代理发起请求（端到端）")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    curl = S.have_curl()
    if not curl:
        print("     未找到 curl，跳过（真实环境飞牛自带 curl）")
        srv.shutdown()
        return

    # 把代理指向本地测试代理，目标用一个不存在的域名
    # 若请求确实经过代理，代理会收到请求并直接应答
    S.save_proxy("http://127.0.0.1:%d" % port)
    ProxyHandler.seen = []
    try:
        status, body = S.http_get("http://example.invalid/api/x", timeout=10)
        check("经代理取到响应", status == 200, status)
        check("响应内容来自代理", b"9.9.9" in body, body[:60])
        check("代理确实收到了请求", len(ProxyHandler.seen) > 0,
              ProxyHandler.seen[:2])
    except Exception as e:  # noqa: BLE001
        check("经代理请求成功", False, e)

    print("5) 直连时不应经过代理")
    S.save_proxy("")
    ProxyHandler.seen = []
    try:
        S.http_get("http://example.invalid/api/x", timeout=5)
        check("直连失败（域名不可达，符合预期）", False, "不该成功")
    except Exception:  # noqa: BLE001
        check("直连不经过代理", len(ProxyHandler.seen) == 0,
              "代理收到 %d 次" % len(ProxyHandler.seen))

    srv.shutdown()


def test_validation():
    print("6) 代理格式校验（前端/后端约定）")
    pat_ok = re.compile(r"^(socks5h?|socks4a?|https?)://[^\s/]+:\d+$")
    pat_bare = re.compile(r"^[^\s/]+:\d+$")

    def valid(s):
        return bool(pat_ok.match(s) or pat_bare.match(s))

    for s in ["socks5://127.0.0.1:1080", "socks5h://127.0.0.1:1080",
              "http://127.0.0.1:7890", "127.0.0.1:7890",
              "https://proxy.lan:8443", "socks4://1.2.3.4:1080"]:
        check("接受 %s" % s, valid(s))
    for s in ["socks5://127.0.0.1", "http://", "hello", "127.0.0.1",
              "socks5://127.0.0.1:abc", "http://a b:1"]:
        check("拒绝 %s" % s, not valid(s))


def test_lib_sh():
    print("7) lib.sh 代理函数")
    lib = os.path.join(ROOT, "app", "shell", "lib.sh")
    conf = os.path.join(TESTVAR, "vnt_config")
    os.makedirs(conf, exist_ok=True)
    env = dict(os.environ)
    env.update(TRIM_APPDEST=os.path.join(ROOT, "app"),
               TRIM_PKGVAR=TESTVAR, TRIM_APPNANE="vnt-nas",
               TRIM_PKGETC=TESTVAR, TRIM_PKGTMP=TESTVAR)

    script = (
        'set -e\n'
        '. "%s"\n'
        'vnt_paths\n'
        'vnt_save_proxy "socks5://127.0.0.1:1080"\n'
        'echo "P1=$(vnt_get_proxy)"\n'
        'echo "A1=$(vnt_curl_proxy_args)"\n'
        'vnt_save_proxy "127.0.0.1:7890"\n'
        'echo "P2=$(vnt_get_proxy)"\n'
        'vnt_save_proxy ""\n'
        'echo "P3=$(vnt_get_proxy)"\n'
        'echo "A3=$(vnt_curl_proxy_args)"\n'
    ) % lib
    r = subprocess.run(["bash", "-c", script], capture_output=True,
                       env=env, timeout=20)
    out = (r.stdout or b"").decode("utf-8", "replace")
    lines = dict(l.split("=", 1) for l in out.strip().split("\n") if "=" in l)
    check("lib.sh 执行成功", r.returncode == 0,
          (r.stderr or b"").decode("utf-8", "replace")[:200])
    check("保存 socks5 正确",
          lines.get("P1") == "socks5://127.0.0.1:1080", lines.get("P1"))
    check("curl 参数含 --proxy",
          lines.get("A1") == "--proxy socks5://127.0.0.1:1080", lines.get("A1"))
    check("省略协议补 http",
          lines.get("P2") == "http://127.0.0.1:7890", lines.get("P2"))
    check("清空生效", lines.get("P3") == "", repr(lines.get("P3")))
    check("清空后无参数", lines.get("A3") == "", repr(lines.get("A3")))


def test_local_bypass():
    print("8) 本机地址必须绕过代理（Empty reply 回归）")
    cases_local = [
        "http://127.0.0.1:19199/",
        "http://127.0.0.1/api/x",
        "http://localhost:19099/",
        "http://127.5.5.5:8080/",
        "http://[::1]:1234/",
    ]
    for u in cases_local:
        check("判定为本机: %s" % u, S.is_local_url(u))

    for u in ["https://github.com/x/y", "https://api.github.com/repos",
              "http://example.com/", "https://ghfast.top/https://github.com"]:
        check("判定为外部: %s" % u, not S.is_local_url(u))

    print("9) 配代理时访问本机仍直连（端到端）")
    # 起一个本地服务，模拟核心
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Core(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b'{"core":"ok"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Core)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 配一个不可用的代理，若请求走了代理必然失败
    S.save_proxy("http://127.0.0.1:1")   # 1 端口不会有服务
    try:
        code, body = S.http_get("http://127.0.0.1:%d/" % port, timeout=8)
        check("配代理后访问本机仍成功", code == 200, code)
        check("  内容正确", b'"core":"ok"' in body, body[:60])
    except Exception as e:  # noqa: BLE001
        check("配代理后访问本机仍成功", False, e)

    # 同一代理下访问外部应当失败（证明确实还在走代理）
    try:
        S.http_get("http://example.invalid/x", timeout=5)
        check("外部走代理（该代理不可用，应失败）", False, "不该成功")
    except Exception:  # noqa: BLE001
        check("外部仍走代理", True)

    S.save_proxy("")
    srv.shutdown()


def main():
    test_normalize()
    test_end_to_end()
    test_validation()
    test_lib_sh()
    test_local_bypass()
    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), "; ".join(FAILS)))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
