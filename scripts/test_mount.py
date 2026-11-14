#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""挂载点与端口模型测试。

覆盖两件事：
  1. 用户只配置一个面板端口；vnt2_web 使用内部端口
  2. 页面在「直连 /」与「网关 /app/<appname>（无尾斜杠）」下都能正确
     加载资源 —— 后者曾因缺少 <base> 导致页面无样式、脚本不执行
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "shell"))
os.environ.setdefault("VNT_PKGVAR", os.path.join(ROOT, "build", "mounttest"))

import shell_http as H  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + ("  " + str(extra) if extra else ""))
    if not cond:
        FAILS.append(name)


def test_mount_detect():
    print("1) 挂载点识别")
    pre = H.S.GATEWAY_PREFIX
    cases = [
        ("/", ""),
        ("/index.html", ""),
        ("/app.js", ""),
        (pre, pre),
        (pre + "/", pre),
        (pre + "/app.css", pre),
        ("/app/other", ""),          # 其他应用不归本应用
    ]
    for raw, want in cases:
        got = H.Handler._mount(None, raw)
        check("%-28s -> %r" % (raw, want), got == want, repr(got))


def test_rewrite_with_mount():
    print("2) 代理改写带上挂载点")
    for mount, expect in [("", "/core/assets/x.css"),
                          (H.S.GATEWAY_PREFIX,
                           H.S.GATEWAY_PREFIX + "/core/assets/x.css")]:
        pre = mount + "/core"
        got = H.rewrite_paths('href="/assets/x.css"', pre)
        check("mount=%-14r -> %s" % (mount, expect), got == 'href="%s"' % expect, got)

    # 网关前缀下的 API 也要带挂载点
    pre = H.S.GATEWAY_PREFIX + "/core"
    got = H.rewrite_paths('fetch("/api/instances")', pre)
    check("网关下 API 前缀正确",
          got == 'fetch("%s/api/instances")' % pre, got)

    # 幂等
    once = H.rewrite_paths('fetch("/api/x")', pre)
    check("幂等", H.rewrite_paths(once, pre) == once)


def test_port_model():
    print("3) 端口模型：只暴露面板端口")
    ports = H.S.read_ports()
    check("含 PANEL_PORT", "PANEL_PORT" in ports, list(ports))
    check("含内部 VNT_WEB_PORT", "VNT_WEB_PORT" in ports, list(ports))
    check("内部端口与面板端口不同",
          ports["VNT_WEB_PORT"] != ports["PANEL_PORT"],
          "%s vs %s" % (ports["VNT_WEB_PORT"], ports["PANEL_PORT"]))


def test_base_injection_local():
    print("4) index.html 具备 base 注入前提")
    p = os.path.join(ROOT, "app", "shell", "web", "index.html")
    src = open(p, encoding="utf-8").read()
    check("含 <head> 标签", "<head>" in src)
    check("未自带 <base>（由服务端注入）", "<base " not in src)
    check("使用相对路径引用样式", 'href="app.css"' in src)
    check("使用相对路径引用脚本", 'src="app.js"' in src)


def main():
    test_mount_detect()
    test_rewrite_with_mount()
    test_port_model()
    test_base_injection_local()
    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), "; ".join(FAILS)))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
