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


def test_embed_restored():
    print("4) 恢复内嵌：面板经 /core/ 同源代理")
    js = open(os.path.join(ROOT, "app", "shell", "web", "app.js"),
              encoding="utf-8").read()
    html = open(os.path.join(ROOT, "app", "shell", "web", "index.html"),
                encoding="utf-8").read()
    src = open(os.path.join(ROOT, "app", "shell", "shell_http.py"),
               encoding="utf-8").read()

    check("前端有 iframe 引用", "coreFrame" in js)
    check("页面有 iframe 标签", "<iframe" in html)
    check("内嵌用后端返回的 url", "f.src = d.url" in js)
    check("空内容有提示（不静默白屏）", "watchFrame" in js and "frameTip" in html)
    check("跳转仍可用（同源新窗口）", "window.open(d.url" in js)
    check("界面展示用 display_url", "d.display_url || d.url" in js)

    check("后端实现反向代理", "def _proxy_panel" in src)
    check("代理使用 http_raw", "S.http_raw" in src)
    check("代理失败给可读错误页", "def _error_page" in src)
    check("沿用 rewrite_paths 改写", "rewrite_paths(" in src)
    check("代理注入令牌",
          'Bearer %s" % S.get_token()' in src)

    # 改写正则必须限制斜杠后为字母，否则会误改 SVG 自闭合标签
    check("改写正则限定字母开头",
          "(?=[A-Za-z])" in src, "需防止 SVG 的 /> 被误改")


def test_desktop_entries():
    print("5) 桌面只有一个图标（管理页）")
    import json
    cfg = json.load(open(os.path.join(ROOT, "app", "ui", "config"),
                         encoding="utf-8"))
    urls = cfg[".url"]
    check("只有一个入口", len(urls) == 1, list(urls))

    main = urls.get("vnt-nas.main", {})
    check("入口为 iframe", main.get("type") == "iframe", main.get("type"))
    check("走统一网关", main.get("gatewayPrefix") == "/app/vnt-nas",
          main.get("gatewayPrefix"))
    check("指向管理页", main.get("url") == "/app/vnt-nas", main.get("url"))
    check("已移除面板图标", "vnt-nas.panel" not in urls)

    # 组网面板现在经 /core/ 内嵌，不再需要独立的直连图标

    # 向导仍需提供两个端口字段（管理页端口 + 面板端口，后者避免冲突）
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

    # service_port 必须与实际默认端口一致。
    # 曾出现 manifest 仍写旧值 19988、而应用默认已改为 19099 的不一致：
    # 飞牛会按 manifest 声明的端口探测应用状态，端口错会让应用在
    # 应用中心显示异常（如「已安装」页状态/图标渲染不出来）。
    m = re.search(r"service_port\s*=\s*(\d+)", mf)
    check("manifest 声明了 service_port", bool(m))
    if m:
        check("service_port 与默认管理页端口一致（%d）"
              % S.PANEL_PORT_DEFAULT,
              int(m.group(1)) == S.PANEL_PORT_DEFAULT,
              "manifest=%s 默认=%d" % (m.group(1), S.PANEL_PORT_DEFAULT))

    # 图标文件必须齐备且尺寸正确。
    # ui/config 的 icon 用 "images/icon_{0}.png"，{0} 会被飞牛替换为
    # 64 与 256，因此这两个文件缺一不可；尺寸也须与文件名对应
    # （曾见「已安装」页图标空白的反馈，故纳入回归）。
    def png_size(path):
        try:
            d = open(path, "rb").read()
        except OSError:
            return None
        if d[:8] != b"\x89PNG\r\n\x1a\n":
            return "not-png"
        import struct
        return struct.unpack(">II", d[16:24])

    icon_spec = {
        "images/icon_64.png": (64, 64),
        "images/icon_256.png": (256, 256),
    }
    check("图标路径使用 {0} 占位符",
          main.get("icon") == "images/icon_{0}.png", main.get("icon"))
    for rel, want in icon_spec.items():
        p = os.path.join(ROOT, "app", "ui", rel)
        got = png_size(p)
        check("app/ui/%s 存在且为 PNG" % rel, got not in (None, "not-png"),
              got)
        check("app/ui/%s 尺寸为 %dx%d" % (rel, want[0], want[1]),
              got == want, got)

    # 只声明了 {0} 两个尺寸，目录里不应有多余尺寸（避免误以为需 128）
    img_dir = os.path.join(ROOT, "app", "ui", "images")
    pngs = sorted(n for n in os.listdir(img_dir) if n.endswith(".png"))
    check("images 目录只含 64/256 两个图标",
          pngs == ["icon_256.png", "icon_64.png"], pngs)

    # 根目录图标（应用中心列表用）也必须齐备且尺寸正确
    for fn, want in (("ICON.PNG", (64, 64)), ("ICON_256.PNG", (256, 256))):
        got = png_size(os.path.join(ROOT, fn))
        check("根目录 %s 尺寸为 %dx%d" % (fn, want[0], want[1]),
              got == want, got)


def test_panel_url():
    print("6) 面板地址推导与令牌自动注入")
    import shell_http as H

    class Fake(H.Handler):
        def __init__(self, host, path="/api/panel"):
            self._h = host
            self.path = path

        def _lan_ip(self):
            return "10.0.0.5"

        def _mount(self, raw_path):
            pre = S.GATEWAY_PREFIX
            if raw_path == pre or raw_path.startswith(pre + "/"):
                return pre
            return ""

        @property
        def headers(self):
            return {"Host": self._h}

    S.PORTS["VNT_WEB_PORT"] = 19199
    S.set_token("MyTestToken1234567890")

    # 直连时：面板挂在 /core/
    got = H.panel_url_for(Fake("192.168.3.251", "/api/panel"))
    check("直连时给出 /core/", got == "/core/", got)

    # 经网关时：带上挂载点，保证同源且路径正确
    got = H.panel_url_for(Fake("nas.lan:5666", "/app/vnt-nas/api/panel"))
    check("网关时给出 /app/vnt-nas/core/",
          got == "/app/vnt-nas/core/", got)

    # 展示地址与访问地址一致（令牌由代理注入，不再拼进 URL）
    bare = H.panel_url_bare(Fake("192.168.3.251", "/api/panel"))
    check("展示地址同为 /core/", bare == "/core/", bare)
    check("地址里不含令牌（代理注入）", "token" not in bare, bare)


def test_architecture():
    """架构约束：核心监听可配置（兜底直连），面板另经 /core/ 反代。"""
    print("7) 架构约束（反代 + 直连兜底）")
    srv = open(os.path.join(ROOT, "app", "shell", "shell_server.py"),
               encoding="utf-8").read()
    # 核心监听地址保持可配置：反代失效时 19199 仍可直连作为兜底
    check("核心监听地址可配置（bind）",
          '"--addr", "%s:%d" % (host' in srv)
    check("bind 默认全部网卡",
          'return "0.0.0.0"' in srv)
    check("存在 http_raw（代理用）", "def http_raw" in srv)
    check("http_raw 显式禁用代理（本机直连）",
          "ProxyHandler({})" in srv)

    h = open(os.path.join(ROOT, "app", "shell", "shell_http.py"),
             encoding="utf-8").read()
    check("PROXY_PREFIX 指向 /core", 'PROXY_PREFIX = "/core"' in h)
    check("实现 _proxy_panel", "def _proxy_panel" in h)
    check("提供代理对照诊断", "/api/probe_proxy" in h)

    main = open(os.path.join(ROOT, "app", "shell", "shell_main.py"),
                encoding="utf-8").read()
    check("端口文件记录两个端口",
          "PANEL_PORT=%d\\nVNT_WEB_PORT=%d" in main)


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
    test_embed_restored()
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
