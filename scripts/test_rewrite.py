#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路径改写函数的行为测试。

注意：该函数已不再用于面板代理（vnt2_web 前端资源编译在 ELF 内、
使用根路径与拼接片段，改写会破坏机器码）。这里保留函数级测试，
仅用于验证工具函数本身的正确性，避免误删后无人守护。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "shell"))
os.environ.setdefault("VNT_PKGVAR", os.path.join(ROOT, "build", "rwtest"))

import shell_http as H  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + ("  " + str(extra) if extra else ""))
    if not cond:
        FAILS.append(name)


def main():
    print("1) 应当改写的绝对路径")
    should = [
        ('<link rel="stylesheet" crossorigin href="/assets/index-Cgs9Rwnv.css">',
         '/core/assets/index-Cgs9Rwnv.css'),
        ('<script type="module" crossorigin src="/assets/index-kNrnhhWo.js"></script>',
         '/core/assets/index-kNrnhhWo.js'),
        ('fetch("/api/instances")', '/core/api/instances'),
        ("url: '/api/info?file_name=x'", '/core/api/info?file_name=x'),
        ('src="/favicon.ico"', '/core/favicon.ico'),
        ('De(`/api/info?file_name=${encodeURIComponent(e)}`)',
         '/core/api/info?file_name='),
        ('const p = "/api/start/status?file_name=a.toml"', '/core/api/start/status'),
    ]
    for src, want in should:
        got = H.rewrite_paths(src)
        check("改写 " + src[:46], want in got, "" if want in got else got[:96])

    print("2) 不应误伤的内容")
    keep = [
        "//cdn.example.com/a.js",
        "date_is_2024/01/01",
        "https://github.com/vnt-dev/vnt",
        "http://127.0.0.1:19099/",
        "/core/assets/already-rewritten.js",
        "ratio 16/9 and 1/2",
        "a / b",
        "上/下",
    ]
    for src in keep:
        got = H.rewrite_paths(src)
        check("保持原样 " + src[:40], got == src,
              "" if got == src else "-> " + got[:80])

    print("3) 幂等性：重复改写不应重复加前缀")
    once = H.rewrite_paths('fetch("/api/instances")')
    twice = H.rewrite_paths(once)
    check("二次改写结果不变", once == twice, "%r vs %r" % (once, twice))

    print("4) 真实 vnt2_web 前端片段（若存在）")
    # 从解压目录里找前端文件做真实校验
    cand = []
    for base in ("/tmp/vntx", os.path.join(ROOT, "build", "vntx")):
        if os.path.isdir(base):
            cand.append(base)
    found = False
    for base in cand:
        for name in os.listdir(base):
            p = os.path.join(base, name)
            if not os.path.isfile(p):
                continue
            try:
                with open(p, "rb") as f:
                    head = f.read(4)
                if head != b"\x7fELF":
                    continue
                with open(p, "rb") as f:
                    raw = f.read()
                text = raw.decode("utf-8", "ignore")
                if 'href="/assets/' not in text:
                    continue
                found = True
                out = H.rewrite_paths(text)
                check("真实二进制中 /assets/ 已改写",
                      'href="/core/assets/' in out)
                check("未把 /assets/ 改坏",
                      out.count('href="/core/assets/') ==
                      text.count('href="/assets/'))
            except Exception as e:  # noqa: BLE001
                print("     跳过 %s: %s" % (name, e))
    if not found:
        print("     （未找到 vnt2_web 二进制，跳过）")

    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), "; ".join(FAILS)))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
