#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版本解析与手动上传核心的测试。

版本解析是本次修复的重点：镜像会把重定向地址整段 URL 编码，
按 / 切分取末段会得到 https%3A%2F%2F... 这类垃圾值，
导致拼出错误下载地址而 404。
"""
import io
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "shell"))

TESTVAR = os.path.abspath(os.path.join(HERE, "..", "build", "vertest"))
os.makedirs(TESTVAR, exist_ok=True)
os.environ["VNT_PKGVAR"] = TESTVAR
os.environ["VNT_APPDEST"] = os.path.join(ROOT, "app")
os.environ["VNT_APPNAME"] = "vnt-nas"

import shell_server as S    # noqa: E402
import shell_http as H      # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + ("  " + str(extra) if extra else ""))
    if not cond:
        FAILS.append(name)


def test_normalize():
    print("1) 版本号解析")
    cases = [
        # (输入, 期望)
        ("2.0.11", "2.0.11"),
        ("v2.0.11", "2.0.11"),
        ("https://github.com/vnt-dev/vnt/releases/tag/v2.0.11", "2.0.11"),
        (" https://github.com/x/y/releases/tag/v2.0.11/ ", "2.0.11"),
        # 关键回归：镜像返回整段 URL 编码
        ("https%3A%2F%2Fgithub.com%2Fvnt-dev%2Fvnt%2Freleases%2Ftag%2Fv2.0.11",
         "2.0.11"),
        ("%2Fv2.0.11", "2.0.11"),
        ("v2.0.11?x=1", "2.0.11"),
        ("2.0.11-rc1", "2.0.11-rc1"),
        ("latest", None),
        ("", None),
        (None, None),
        ("vnt2-x86_64-unknown-linux-musl-v2.0.11.zip", "2.0.11"),
    ]
    for raw, want in cases:
        got = S._normalize_version(raw)
        check("%-52s -> %r" % (repr(raw)[:50], want), got == want, repr(got))


def test_zip_extract():
    print("2) 从压缩包提取核心")
    work = os.path.join(TESTVAR, "w1")
    import shutil
    shutil.rmtree(work, ignore_errors=True)

    # 正常包
    p = os.path.join(TESTVAR, "ok.zip")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("vnt2_web", b"#" + b"A" * 200)
        z.writestr("vnt2_cli", b"x")
    ok, res = S.extract_core_from_archive(p, work)
    check("正常 zip 可提取", ok, res)
    check("提取出的是 vnt2_web", ok and res.endswith("vnt2_web"), res)

    # 缺 vnt2_web
    p2 = os.path.join(TESTVAR, "bad.zip")
    with zipfile.ZipFile(p2, "w") as z:
        z.writestr("readme.txt", b"nothing")
    ok2, res2 = S.extract_core_from_archive(p2, work)
    check("缺核心的包被拒绝", not ok2, res2)

    # 裸 ELF 二进制
    p3 = os.path.join(TESTVAR, "vnt2_web")
    with open(p3, "wb") as f:
        f.write(b"\x7fELF" + b"\x00" * 200000)
    ok3, res3 = S.extract_core_from_archive(p3, work)
    check("裸 ELF 可接受", ok3, res3)

    # 过小文件
    p4 = os.path.join(TESTVAR, "tiny.bin")
    with open(p4, "wb") as f:
        f.write(b"\x7fELF" + b"\x00" * 10)
    ok4, res4 = S.extract_core_from_archive(p4, work)
    check("过小文件被拒绝", not ok4, res4)

    # 非可执行的文本
    p5 = os.path.join(TESTVAR, "text.txt")
    with open(p5, "wb") as f:
        f.write(b"hello world " * 20000)
    ok5, res5 = S.extract_core_from_archive(p5, work)
    check("普通文本被拒绝", not ok5, res5)

    # Windows 程序应给出明确提示
    p6 = os.path.join(TESTVAR, "win.exe")
    with open(p6, "wb") as f:
        f.write(b"MZ" + b"\x00" * 200000)
    ok6, res6 = H.check_uploaded_file(p6)
    check("Windows 程序被拒绝且有提示",
          (not ok6) and ("Windows" in res6), res6)


def test_multipart():
    print("3) multipart 解析")
    boundary = "----testboundary123"
    body = b""
    body += ("--%s\r\n" % boundary).encode()
    body += b'Content-Disposition: form-data; name="version"\r\n\r\n'
    body += b"2.0.11\r\n"
    body += ("--%s\r\n" % boundary).encode()
    body += (b'Content-Disposition: form-data; name="core"; '
             b'filename="vnt2-x86_64-unknown-linux-musl-v2.0.11.zip"\r\n')
    body += b"Content-Type: application/zip\r\n\r\n"
    body += b"PK\x03\x04FAKEDATA\r\n"
    body += ("--%s--\r\n" % boundary).encode()

    fields, files = H.parse_multipart(
        body, 'multipart/form-data; boundary=%s' % boundary)
    check("解析出文本字段 version", fields.get("version") == "2.0.11",
          repr(fields.get("version")))
    check("解析出文件字段 core", "core" in files, list(files))
    if "core" in files:
        fn, data = files["core"]
        check("文件名正确", fn.endswith(".zip"), fn)
        check("文件内容正确", data == b"PK\x03\x04FAKEDATA", repr(data))


def test_upload_flow():
    print("4) 上传安装流程（替换与回滚）")
    import shutil
    for d in (S.BIN_DIR, S.CACHE_DIR):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)

    # 先在位一个旧版本（用 ELF 魔数，模拟真实二进制）
    def fake_elf(tag):
        return b"\x7fELF" + tag + b"\x00" * 200000

    with open(S.CORE_BIN, "wb") as f:
        f.write(fake_elf(b"OLD-1.0.0"))
    os.chmod(S.CORE_BIN, 0o755)
    with open(S.CORE_VER_FILE, "w") as f:
        f.write("1.0.0")

    # 上传一个"新"二进制
    new = os.path.join(TESTVAR, "up_new.bin")
    with open(new, "wb") as f:
        f.write(fake_elf(b"NEW-2.0.11"))

    S.start_core = lambda: (True, "核心已启动")
    S.stop_core = lambda: (True, "已停止")

    ok, msg = S.install_from_upload(
        new, "vnt2-x86_64-unknown-linux-musl-v2.0.11.zip", "")
    check("上传安装成功", ok, msg)
    check("版本号识别为 2.0.11", S.installed_version() == "2.0.11",
          S.installed_version())
    check("来源记录标注 upload",
          "upload" in open(S.CORE_SRC_FILE, encoding="utf-8").read())
    with open(S.CORE_BIN, "rb") as f:
        check("二进制已替换", b"NEW-2.0.11" in f.read(64))
    check("旧版本已备份",
          os.path.isfile(os.path.join(S.CACHE_DIR, "vnt2_web.bak")))

    # 启动失败应回滚
    new2 = os.path.join(TESTVAR, "up_bad.bin")
    with open(new2, "wb") as f:
        f.write(fake_elf(b"BROKEN"))
    S.start_core = lambda: (False, "无法执行")
    ok2, msg2 = S.install_from_upload(
        new2, "vnt2-x86_64-unknown-linux-musl-v9.9.9.zip", "")
    check("启动失败被判定为失败", not ok2, msg2)
    check("触发回滚", "回滚" in msg2, msg2)
    check("版本回到 2.0.11", S.installed_version() == "2.0.11",
          S.installed_version())
    with open(S.CORE_BIN, "rb") as f:
        check("二进制已回滚", b"NEW-2.0.11" in f.read(64))

    # 缺 vnt2_web 的 zip 应被拒绝
    badzip = os.path.join(TESTVAR, "nozip.zip")
    with zipfile.ZipFile(badzip, "w") as z:
        z.writestr("readme.txt", b"nothing" * 5000)
    ok3, msg3 = S.install_from_upload(badzip, "core.zip", "")
    check("缺核心的压缩包被拒绝", not ok3, msg3)


def main():
    test_normalize()
    test_zip_extract()
    test_multipart()
    test_upload_flow()
    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), "; ".join(FAILS)))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
