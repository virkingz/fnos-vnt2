#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vnt-nas 管理页服务入口。

同时提供两个监听：
  1. Unix socket  $TRIM_APPDEST/app.sock  —— 供飞牛统一网关 /app/vnt-nas 转发
  2. TCP 0.0.0.0:<PANEL_PORT>             —— 管理页（用户配置的端口）

组网面板由 vnt2_web 以自己的端口（VNT_WEB_PORT）独立对外提供，
桌面第二个图标直接指向它，管理页内只提供跳转入口，不做内嵌。

启动后自动拉起 vnt2_web 核心（若二进制与令牌就绪）。
"""

import os
import socket
import socketserver
import sys
import threading
import time
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import shell_server as S          # noqa: E402
import shell_http as H            # noqa: E402


HAVE_AF_UNIX = hasattr(socket, "AF_UNIX")


if HAVE_AF_UNIX:
    class UnixHTTPServer(ThreadingHTTPServer):
        address_family = socket.AF_UNIX
        allow_reuse_address = True

        def server_bind(self):
            try:
                if os.path.exists(self.server_address):
                    os.unlink(self.server_address)
            except OSError:
                pass
            socketserver.TCPServer.server_bind(self)
            try:
                os.chmod(self.server_address, 0o660)
            except OSError:
                pass

        def get_request(self):
            req, _ = self.socket.accept()
            return req, ("unix", 0)


def serve_unix():
    if not HAVE_AF_UNIX:
        S.log("unix socket 不受当前平台支持（非 fnOS 环境），跳过")
        return
    try:
        srv = UnixHTTPServer(S.SOCK_PATH, H.Handler)
    except Exception as e:  # noqa: BLE001
        S.log("unix socket listen failed: %s" % e)
        return
    S.log("unix socket listening: %s" % S.SOCK_PATH)
    try:
        srv.serve_forever()
    except Exception as e:  # noqa: BLE001
        S.log("unix server stopped: %s" % e)


def panel_bind_host():
    """管理页 TCP 的绑定地址。

    默认 127.0.0.1（仅本机）——飞牛桌面入口走 Unix socket，不依赖该端口对外，
    因此默认不对外暴露，减少攻击面。

    Docker 部署需要从容器外访问管理页，此时由 entrypoint 置
    PANEL_BIND=0.0.0.0（容器内的绑定范围与端口映射是两回事）。
    """
    v = (os.environ.get("PANEL_BIND") or "").strip()
    if v in ("0.0.0.0", "127.0.0.1", "::", "::1"):
        return v
    return "127.0.0.1"


def serve_tcp():
    """在配置的面板端口上提供服务。

    管理页与组网面板都经由本服务转发（面板经 /core/ 反代），
    因此用户只需关心这一个端口。vnt2_web 只在内部回环端口运行。
    """
    port = S.PORTS["PANEL_PORT"]
    host = panel_bind_host()
    try:
        srv = ThreadingHTTPServer((host, port), H.Handler)
    except OSError as e:
        S.log("panel port %d unavailable on %s: %s" % (port, host, e))
        # 端口被占：尝试就近另选一个，避免应用完全起不来
        for alt in range(port + 1, port + 11):
            try:
                srv = ThreadingHTTPServer((host, alt), H.Handler)
                S.PORTS["PANEL_PORT"] = alt
                S.log("panel port fallback to %d" % alt)
                break
            except OSError:
                continue
        else:
            S.log("no usable panel port, service not started")
            return

    S.log("panel listening: %s:%d" % (host, S.PORTS["PANEL_PORT"]))
    if host in ("0.0.0.0", "::"):
        S.log("提示: 管理页对局域网开放；如需仅本机访问，设 PANEL_BIND=127.0.0.1")
    try:
        with open(S.PORT_FILE, "w", encoding="utf-8") as f:
            f.write("PANEL_PORT=%d\nVNT_WEB_PORT=%d\n"
                    % (S.PORTS["PANEL_PORT"], S.PORTS["VNT_WEB_PORT"]))
    except OSError:
        pass
    srv.serve_forever()


def bootstrap_core():
    """启动时按需拉起核心；无配置时仅记录，不阻断管理页。"""
    # 确保访问令牌存在（面板与接口都要用），避免状态显示不一致
    S.get_token()
    S.log("bootstrap: token %s" % S.token_hint())

    if not os.path.isfile(S.CORE_BIN):
        S.log("bootstrap: 核心二进制不存在，请在管理页下载核心")
        return

    ok, info = S.probe_core()
    if not ok:
        S.log("bootstrap: 核心无法运行（%s），请在管理页重新下载核心" % info)
        return

    ok, msg = S.start_core()
    S.log("bootstrap: %s" % msg)
    try:
        open(os.path.join(S.PKGVAR, ".autostart"), "w").close()
    except OSError:
        pass


def main():
    S.log("=== vnt-nas shell backend starting ===")
    # 启动即巡检一次：把上次运行遗留的过大日志压回安全范围
    S.trim_logs()
    threading.Thread(target=S._watcher, daemon=True).start()
    threading.Thread(target=serve_unix, daemon=True).start()
    threading.Thread(target=lambda: (time.sleep(2), bootstrap_core()),
                     daemon=True).start()
    serve_tcp()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        S.log("interrupted, shutting down")
        S.stop_core()
