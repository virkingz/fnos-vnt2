#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心更新机制测试。

用本地 HTTP 服务模拟 GitHub Releases，验证下载/解压/校验/替换/回滚。

注意：本机是 Windows，无法执行 Linux ELF 二进制，因此测试会把
「启动核心」这一步替换为可控的桩，从而单独验证更新机制本身。
真实设备上的可执行性由 probe_core 负责，另有单独用例覆盖。
"""
import io
import os
import shutil
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "shell"))

TESTVAR = os.path.abspath(os.path.join(HERE, "..", "build", "testvar"))
os.makedirs(TESTVAR, exist_ok=True)

os.environ["VNT_PKGVAR"] = TESTVAR
os.environ["VNT_PKGETC"] = os.path.join(TESTVAR, "etc")
os.environ["VNT_APPDEST"] = os.path.join(ROOT, "app")
os.environ["VNT_APPNAME"] = "vnt-nas"

import shell_server as S  # noqa: E402

FAKE_VERSION = "9.9.9"
OLD_VERSION = "9.9.8"
ZIP_NAME = "vnt2-x86_64-unknown-linux-musl-v%s.zip" % FAKE_VERSION
FAKE_BIN = b"#!/bin/sh\necho fake %s\n" % FAKE_VERSION.encode()
OLD_BIN = b"#!/bin/sh\necho fake %s\n" % OLD_VERSION.encode()

FAILS = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + ("  " + str(extra) if extra else ""))
    if not cond:
        FAILS.append(name)


def clean_state():
    """每次用例从干净状态开始，避免互相污染。"""
    for d in (S.BIN_DIR, S.CONF_DIR, S.LOG_DIR, S.CACHE_DIR):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)


def build_zip(with_bin=True, valid=True):
    if not valid:
        return b"not a zip at all"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if with_bin:
            z.writestr("vnt2_web", FAKE_BIN)
        z.writestr("vnt2_cli", b"fake")
        z.writestr("vnt2_ctrl", b"fake")
    return buf.getvalue()


ZIP_BYTES = build_zip()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/latest.json":
            body = ('{"version":"%s","notes":"","pub_date":"2026-01-01"}'
                    % FAKE_VERSION).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/redirect/latest"):
            self.send_response(302)
            self.send_header("Location", "/tag/v%s" % FAKE_VERSION)
            self.end_headers()
            return
        if self.path == "/api/latest":
            body = ('{"tag_name":"v%s"}' % FAKE_VERSION).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.endswith(".zip"):
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(ZIP_BYTES)))
            self.end_headers()
            self.wfile.write(ZIP_BYTES)
            return
        self.send_response(404)
        self.end_headers()


# ---- 桩：让「启动核心」可控，隔离环境差异 ----
START_BEHAVIOR = {"ok": True, "msg": "核心已启动"}
START_CALLS = {"n": 0}


def stub_start_core():
    START_CALLS["n"] += 1
    return START_BEHAVIOR["ok"], START_BEHAVIOR["msg"]


def stub_stop_core():
    return True, "已停止"


S.start_core = stub_start_core
S.stop_core = stub_stop_core


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]

    S.RELEASES_LATEST = base + "/redirect/latest"
    S.API_LATEST = base + "/api/latest"
    S.LATEST_JSON = base + "/latest.json"
    S.DL_BASE = base + "/dl"

    print("1) 版本查询：优先 latest.json（与真实环境一致）")
    check("得到 %s" % FAKE_VERSION, S.latest_version() == FAKE_VERSION,
          S.latest_version())

    print("1b) latest.json 不可用时回退到重定向")
    saved = S.LATEST_JSON
    S.LATEST_JSON = base + "/no-such-latest.json"
    check("回退后仍能取到版本", S.latest_version() == FAKE_VERSION,
          S.latest_version())
    S.LATEST_JSON = saved

    print("1c) 版本号含 URL 编码时也能正确解析（回归）")
    for raw in ["https%3A%2F%2Fgithub.com%2Fvnt-dev%2Fvnt%2Freleases"
                "%2Ftag%2Fv2.0.11",
                "https://github.com/vnt-dev/vnt/releases/tag/v2.0.11",
                "vnt2-x86_64-unknown-linux-musl-v2.0.11.zip"]:
        got = S._normalize_version(raw)
        check("解析 %s..." % raw[:34], got == "2.0.11", got)

    print("2) 首次安装：全新环境，无旧版本")
    clean_state()
    global ZIP_BYTES
    ZIP_BYTES = build_zip()
    START_BEHAVIOR.update(ok=True, msg="核心已启动")
    ok, msg = S.do_update(FAKE_VERSION)
    check("更新成功", ok, msg)
    check("二进制内容正确", open(S.CORE_BIN, "rb").read() == FAKE_BIN)
    check("版本号已记录", S.installed_version() == FAKE_VERSION,
          S.installed_version())
    check("来源记录已写入", os.path.isfile(S.CORE_SRC_FILE))
    check("下载缓存已清理",
          not os.path.isfile(os.path.join(S.CACHE_DIR, ZIP_NAME + ".download")))
    check("首次无旧版本，不产生备份（符合预期）",
          not os.path.isfile(os.path.join(S.CACHE_DIR, "vnt2_web.bak")))

    print("3) 升级：从旧版本升到新版本")
    clean_state()
    with open(S.CORE_BIN, "wb") as f:
        f.write(OLD_BIN)
    with open(S.CORE_VER_FILE, "w") as f:
        f.write(OLD_VERSION)
    ok, msg = S.do_update(FAKE_VERSION)
    check("升级成功", ok, msg)
    check("二进制已替换为新版", open(S.CORE_BIN, "rb").read() == FAKE_BIN)
    check("版本号已更新", S.installed_version() == FAKE_VERSION)
    check("旧版已备份", os.path.isfile(os.path.join(S.CACHE_DIR, "vnt2_web.bak")))
    check("备份内容为旧版", open(os.path.join(S.CACHE_DIR, "vnt2_web.bak"), "rb").read() == OLD_BIN)

    print("4) 新版本启动失败时必须自动回滚")
    clean_state()
    with open(S.CORE_BIN, "wb") as f:
        f.write(OLD_BIN)
    with open(S.CORE_VER_FILE, "w") as f:
        f.write(OLD_VERSION)
    # 第一次 start_core 返回失败（模拟新版跑不起来）
    START_BEHAVIOR.update(ok=False, msg="二进制无法执行")
    ok, msg = S.do_update(FAKE_VERSION)
    check("更新被判定为失败", not ok, msg)
    check("提示中包含回滚", "回滚" in msg, msg)
    check("二进制已回到旧版", open(S.CORE_BIN, "rb").read() == OLD_BIN)
    check("版本号已回到旧版（不会显示成新版）",
          S.installed_version() == OLD_VERSION, S.installed_version())
    START_BEHAVIOR.update(ok=True, msg="核心已启动")

    print("5) 压缩包缺少 vnt2_web 时应拒绝")
    clean_state()
    with open(S.CORE_BIN, "wb") as f:
        f.write(OLD_BIN)
    with open(S.CORE_VER_FILE, "w") as f:
        f.write(OLD_VERSION)
    ZIP_BYTES = build_zip(with_bin=False)
    ok, msg = S.do_update(FAKE_VERSION)
    check("拒绝安装", not ok, msg)
    check("旧二进制未被破坏", open(S.CORE_BIN, "rb").read() == OLD_BIN)
    check("版本号未被改写", S.installed_version() == OLD_VERSION)

    print("6) 下载内容不是 zip 时应拒绝")
    clean_state()
    with open(S.CORE_BIN, "wb") as f:
        f.write(OLD_BIN)
    ZIP_BYTES = build_zip(valid=False)
    ok, msg = S.do_update(FAKE_VERSION)
    check("拒绝安装", not ok, msg)
    check("旧二进制未被破坏", open(S.CORE_BIN, "rb").read() == OLD_BIN)

    print("7) 手动回滚")
    clean_state()
    with open(S.CORE_BIN, "wb") as f:
        f.write(b"broken-binary")
    with open(S.CORE_VER_FILE, "w") as f:
        f.write("0.0.1")
    bak = os.path.join(S.CACHE_DIR, "vnt2_web.bak")
    with open(bak, "wb") as f:
        f.write(OLD_BIN)
    with open(bak + ".version", "w") as f:
        f.write(OLD_VERSION)
    ok, msg = S.rollback_core()
    check("回滚成功", ok, msg)
    check("二进制已恢复", open(S.CORE_BIN, "rb").read() == OLD_BIN)
    check("版本号已恢复", S.installed_version() == OLD_VERSION)

    print("8) 无备份时回滚应安全失败")
    clean_state()
    ok, msg = S.rollback_core()
    check("明确报告无备份", (not ok) and ("备份" in msg), msg)

    print("9) 架构映射")
    import re
    for arch, expect in [("x86_64", "vnt2-x86_64-unknown-linux-musl"),
                         ("aarch64", "vnt2-aarch64-unknown-linux-musl"),
                         ("armv7l", "vnt2-armv7-unknown-linux-musleabihf"),
                         ("armv6l", "vnt2-arm-unknown-linux-musleabihf")]:
        S._machine = lambda a=arch: a
        got = S.asset_stem()
        check("%s -> %s" % (arch, expect), got == expect, got)
    S._machine = lambda: "riscv64"
    check("不支持的架构返回 None", S.asset_stem() is None)

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
