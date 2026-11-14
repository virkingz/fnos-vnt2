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
    print("2) 面板代理监听范围")
    for host in ("0.0.0.0", "127.0.0.1"):
        got = S.save_bind(host)
        check("保存 %s 生效" % host, got == host, got)
        check("  读回一致", S.read_bind() == host, S.read_bind())
    got = S.save_bind("1.2.3.4")
    check("非法值回落到 127.0.0.1", got == "127.0.0.1", got)
    # 默认值必须是 0.0.0.0：面板代理要对局域网可用
    try:
        os.remove(S.BIND_FILE)
    except OSError:
        pass
    check("默认监听 0.0.0.0（面板代理需对局域网可用）",
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

    for eid in ("panelUrl", "panelReady", "f_bind", "btnSaveBind",
                "directUrl", "btnDiag", "diagText", "btnOpenPanel",
                "btnCopyPanel", "btnStartCore2"):
        check("存在 #%s" % eid, eid in defined)


def test_no_iframe():
    print("4) 不再内嵌：管理页只提供跳转入口")
    js = open(os.path.join(ROOT, "app", "shell", "web", "app.js"),
              encoding="utf-8").read()
    html = open(os.path.join(ROOT, "app", "shell", "web", "index.html"),
                encoding="utf-8").read()

    check("前端无 iframe 引用", "coreFrame" not in js)
    check("页面无 iframe 标签", "<iframe" not in html)
    check("跳转用带令牌的 url", "window.open(d.url" in js)
    check("界面展示用 display_url", "d.display_url || d.url" in js)
    check("已移除混合内容检测（不再内嵌，无需处理）",
          "checkMixedContent" not in js)

    src = open(os.path.join(ROOT, "app", "shell", "shell_http.py"),
               encoding="utf-8").read()
    check("面板接口使用 vnt2_web 自己的端口",
          'S.PORTS["VNT_WEB_PORT"]' in src)
    check("已移除内容改写式代理",
          "def _proxy_panel" not in src)


def test_desktop_entries():
    print("5) 桌面两个图标：管理页 iframe + 面板直连 url")
    import json
    cfg = json.load(open(os.path.join(ROOT, "app", "ui", "config"),
                         encoding="utf-8"))
    urls = cfg[".url"]
    check("存在两个入口", len(urls) == 2, list(urls))

    main = urls.get("vnt-nas.main", {})
    panel = urls.get("vnt-nas.panel", {})

    check("管理页入口为 iframe", main.get("type") == "iframe",
          main.get("type"))
    check("管理页走统一网关", main.get("gatewayPrefix") == "/app/vnt-nas",
          main.get("gatewayPrefix"))

    # 面板图标必须直连 vnt2_web 端口，不能用 iframe（会被混合内容拦）
    check("面板入口为 url（外部打开）", panel.get("type") == "url",
          panel.get("type"))
    # 端口用向导占位符，这样用户改端口后图标依然有效
    # （真实仓库佐证：fn-rtl8125 / FN_WOL / Fndesk 等均用 ${字段名} 形式）
    check("面板入口端口引用向导字段",
          panel.get("port") == "${wizard_panel_port}", panel.get("port"))
    check("面板入口不带网关前缀", "gatewayPrefix" not in panel)
    check("两入口标题不同", main.get("title") != panel.get("title"))

    # 向导必须提供该字段，否则占位符解析不出来
    for wf in ("install", "config"):
        w = open(os.path.join(ROOT, "wizard", wf), encoding="utf-8").read()
        check("wizard/%s 含 wizard_panel_port 字段" % wf,
              '"field": "wizard_panel_port"' in w)
        check("wizard/%s 含 wizard_web_port 字段" % wf,
              '"field": "wizard_web_port"' in w)

    # manifest 的应用卡片入口应是管理页
    mf = open(os.path.join(ROOT, "manifest"), encoding="utf-8").read()
    check("卡片入口为管理页",
          "desktop_applaunchname = vnt-nas.main" in mf)


def test_panel_url():
    print("6) 面板地址推导与令牌自动注入")
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


def test_architecture():
    """架构约束：vnt2_web 以自己端口对外提供面板，不再有代理层。"""
    print("7) 架构约束（无代理层，面板由核心自己提供）")
    srv = open(os.path.join(ROOT, "app", "shell", "shell_server.py"),
               encoding="utf-8").read()
    check("核心监听地址可配置（bind）",
          '"--addr", "%s:%d" % (host' in srv)

    main = open(os.path.join(ROOT, "app", "shell", "shell_main.py"),
                encoding="utf-8").read()
    check("不再启动面板代理", "serve_proxy" not in main)
    check("不再写入 PROXY_PORT", "PROXY_PORT" not in main)
    check("端口文件仍记录两个端口",
          "PANEL_PORT=%d\\nVNT_WEB_PORT=%d" in main)

    # 代理模块应已彻底移除
    check("代理模块已删除",
          not os.path.isfile(os.path.join(ROOT, "app", "shell", "shell_tls.py")))
    h = open(os.path.join(ROOT, "app", "shell", "shell_http.py"),
             encoding="utf-8").read()
    check("shell_http 无 PROXY_PORT 引用", "PROXY_PORT" not in h)
    check("shell_http 无 panel_scheme", "panel_scheme" not in h)

    ls = open(os.path.join(ROOT, "app", "shell", "lib.sh"),
              encoding="utf-8").read()
    check("lib.sh 无 PROXY_PORT", "PROXY_PORT" not in ls)


def test_token_url_support():
    print("8) vnt2_web 的 ?token= 自动登录机制（前端契约）")
    js = open(os.path.join(ROOT, "app", "shell", "web", "app.js"),
              encoding="utf-8").read()
    check("跳转用带令牌的 url", "window.open(d.url" in js)
    check("界面展示用 display_url",
          "d.display_url || d.url" in js)
    check("打开前校验核心与令牌",
          "核心未运行，请先启动核心" in js and "尚未设置访问令牌" in js)


def test_ports():
    print("10) 两个端口的读写与冲突处理")
    # 默认值
    check("管理页端口默认 19099", S.PANEL_PORT_DEFAULT == 19099,
          S.PANEL_PORT_DEFAULT)
    check("面板端口默认 19199", S.VNT_WEB_PORT_DEFAULT == 19199,
          S.VNT_WEB_PORT_DEFAULT)

    # 读写往返
    S.PORTS["PANEL_PORT"] = 20111
    S.PORTS["VNT_WEB_PORT"] = 20222
    with open(S.PORT_FILE, "w", encoding="utf-8") as f:
        f.write("PANEL_PORT=20111\nVNT_WEB_PORT=20222\n")
    got = S.read_ports()
    check("读回管理页端口", got["PANEL_PORT"] == 20111, got["PANEL_PORT"])
    check("读回面板端口", got["VNT_WEB_PORT"] == 20222, got["VNT_WEB_PORT"])

    # 旧配置迁移：只有 SHELL_PORT 时映射到 PANEL_PORT
    with open(S.PORT_FILE, "w", encoding="utf-8") as f:
        f.write("SHELL_PORT=33333\n")
    got = S.read_ports()
    check("旧 SHELL_PORT 迁移为 PANEL_PORT",
          got["PANEL_PORT"] == 33333, got["PANEL_PORT"])

    # 脚本层面：两端口相同应回退
    sh = open(os.path.join(ROOT, "cmd", "install_callback"),
              encoding="utf-8").read()
    check("安装脚本检测端口冲突", "两个端口相同" in sh)
    check("安装脚本读取 wizard_panel_port", "wizard_panel_port" in sh)
    cf = open(os.path.join(ROOT, "cmd", "config_callback"),
              encoding="utf-8").read()
    check("配置脚本支持面板端口", "wizard_panel_port" in cf)
    check("配置脚本检测端口冲突", "两端口冲突" in cf)


def test_diag_shape():
    print("11) 自检项完整性")
    import inspect
    import shell_http as H
    src = inspect.getsource(H.Handler._diag)
    for name in ("核心已下载", "访问令牌已设置", "核心正在运行",
                 "核心端口可连接", "组网面板可访问", "面板地址",
                 "核心监听范围"):
        check("自检包含「%s」" % name, name in src)
    check("自检不再提「面板代理」", "面板代理" not in src)


def main():
    test_no_undeclared()
    test_bind()
    test_ui_ids()
    test_no_iframe()
    test_desktop_entries()
    test_panel_url()
    test_architecture()
    test_token_url_support()
    test_ports()
    test_diag_shape()
    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), "; ".join(FAILS)))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
