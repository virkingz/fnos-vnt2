#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内嵌面板与核心监听范围的测试。

架构说明（重要）：
  vnt2_web 把前端资源编译进 ELF 二进制（RustEmbed），JS 里的 /api/...
  是拼接用的常量片段。因此**不能**用子路径反向代理 + 内容改写的方式
  把它挂到 /core/ 下 —— 实测改写会命中 232 处 ELF 机器码，
  把程序本身改坏，导致页面白屏。

  正解：让 vnt2_web 以自己的端口独立提供服务，管理页通过 iframe
  指向它的真实地址。

本测试同时守住一个真实回归：JS 里曾出现赋值了未声明的 PAGE 变量，
导致脚本抛错中断、iframe 的 src 根本没被设置。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "shell"))

TESTVAR = os.path.abspath(os.path.join(HERE, "..", "build", "embedtest"))
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


def strip_literals(src):
    """去掉字符串字面量与注释，避免把 HTML 片段里的 class="x" 当成赋值。"""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "\"'`":
            q = c
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == q:
                    i += 1
                    break
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def test_no_undeclared():
    print("1) 前端不得引用未声明变量（PAGE 回归）")
    js = open(os.path.join(ROOT, "app", "shell", "web", "app.js"),
              encoding="utf-8").read()
    check("不再出现 PAGE", "PAGE" not in js)

    code = strip_literals(js)
    declared = set(re.findall(r"\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)", code))
    declared |= set(re.findall(r"function\s+([A-Za-z_$][\w$]*)", code))
    params = set()
    for p in re.findall(r"function\s*[A-Za-z_$\w]*\s*\(([^)]*)\)", code):
        for name in p.split(","):
            name = name.strip()
            if name:
                params.add(name)
    multi = set(re.findall(r",\s*([A-Za-z_$][\w$]*)\s*=(?!=)", code))
    builtins = {
        "window", "document", "location", "console", "Math", "JSON",
        "XMLHttpRequest", "FormData", "setTimeout", "setInterval",
        "clearInterval", "clearTimeout", "fetch", "Uint8Array", "confirm",
        "alert", "navigator", "URLSearchParams", "Promise", "Array",
        "Object", "String", "Number", "Boolean", "Error", "encodeURIComponent",
    }
    assigns = set(re.findall(
        r"(?<![.\w$!<>=])([A-Za-z_$][\w$]*)\s*=(?!=)", code))
    suspicious = {a for a in assigns
                  if a not in declared and a not in builtins
                  and a not in params and a not in multi}
    check("没有可疑的裸赋值变量", not suspicious, sorted(suspicious))


def test_bind():
    print("2) 核心监听范围")
    for host in ("0.0.0.0", "127.0.0.1"):
        got = S.save_bind(host)
        check("保存 %s 生效" % host, got == host, got)
        check("  读回一致", S.read_bind() == host, S.read_bind())
    got = S.save_bind("1.2.3.4")
    check("非法值回落到 127.0.0.1", got == "127.0.0.1", got)
    # 默认值必须是 0.0.0.0：面板改用独立端口对外提供服务
    try:
        os.remove(S.BIND_FILE)
    except OSError:
        pass
    check("默认监听 0.0.0.0（面板需独立端口访问）",
          S.read_bind() == "0.0.0.0", S.read_bind())


def test_ui_ids():
    print("3) 前端元素与后端字段对齐")
    html = open(os.path.join(ROOT, "app", "shell", "web", "index.html"),
                encoding="utf-8").read()
    js = open(os.path.join(ROOT, "app", "shell", "web", "app.js"),
              encoding="utf-8").read()

    used = set(re.findall(r"\$\('([\w]+)'\)", js))
    defined = set(re.findall(r'id="([\w]+)"', html))
    missing = sorted(used - defined)
    check("前端引用的 id 都已定义", not missing, missing)

    for eid in ("coreFrame", "btnReloadFrame", "panelUrl", "panelReady",
                "f_bind", "btnSaveBind", "directUrl", "btnDiag", "diagText"):
        check("存在 #%s" % eid, eid in defined)


def test_iframe_targets_real_port():
    print("4) 内嵌指向核心自己的端口（不再走子路径代理）")
    js = open(os.path.join(ROOT, "app", "shell", "web", "app.js"),
              encoding="utf-8").read()
    check("iframe 使用后端返回的 url", "f.src = d.url" in js
          or "f.src = r.data.url" in js)
    check("不再拼 /core/ 作为 iframe 地址",
          "f.src = API + '/core/'" not in js)

    import shell_http as H
    src = open(os.path.join(ROOT, "app", "shell", "shell_http.py"),
               encoding="utf-8").read()
    check("面板接口指向 VNT_WEB_PORT",
          'S.PORTS["VNT_WEB_PORT"]' in src)
    check("已移除内容改写式代理",
          "def _proxy_panel" not in src)


def test_panel_url():
    print("5) 面板地址推导与令牌自动注入")
    import shell_http as H

    class Fake(H.Handler):
        def __init__(self, host):
            self._h = host

        def _lan_ip(self):
            return "10.0.0.5"

        @property
        def headers(self):
            return {"Host": self._h}

    S.PORTS["VNT_WEB_PORT"] = 19199
    S.set_token("MyTestToken1234567890")

    # 带令牌版本：vnt2_web 会自动登录
    got = H.panel_url_for(Fake("192.168.3.251"))
    check("带令牌地址", got ==
          "http://192.168.3.251:19199/?token=MyTestToken1234567890", got)

    # 展示版本：不含令牌，避免界面泄露
    got = H.panel_url_bare(Fake("192.168.3.251"))
    check("展示地址不含令牌",
          got == "http://192.168.3.251:19199/", got)
    check("展示地址无 token 字样", "token" not in got, got)

    check("回环时回落局域网地址",
          H.panel_url_bare(Fake("127.0.0.1")) == "http://10.0.0.5:19199/")

    # 令牌含特殊字符时应正确编码
    S.set_token("aBc!@#$%^&*()_+=-{}[]")
    got = H.panel_url_for(Fake("nas.lan"))
    check("特殊字符令牌被 URL 编码",
          "token=" in got and " " not in got and "#" not in got.split("token=")[1],
          got)


def test_token_url_support():
    print("7) vnt2_web 的 ?token= 自动登录机制（前端契约）")
    js = open(os.path.join(ROOT, "app", "shell", "web", "app.js"),
              encoding="utf-8").read()
    check("内嵌用带令牌的 url", "f.src = d.url" in js)
    check("跳转用带令牌的 url", "window.open(d.url" in js)
    check("界面展示用 display_url",
          "d.display_url || d.url" in js)


def test_diag_shape():
    print("6) 自检项完整性")
    import inspect
    import shell_http as H
    src = inspect.getsource(H.Handler._diag)
    for name in ("核心已下载", "访问令牌已设置", "核心正在运行",
                 "核心端口可连接", "管理服务可代理核心", "面板地址"):
        check("自检包含「%s」" % name, name in src)


def main():
    test_no_undeclared()
    test_bind()
    test_ui_ids()
    test_iframe_targets_real_port()
    test_panel_url()
    test_token_url_support()
    test_diag_shape()
    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), "; ".join(FAILS)))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
