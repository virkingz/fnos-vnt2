#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志清理测试。

覆盖：
  1) 超过上限时自动截断，且保留尾部（最近的活动不丢）
  2) 未超过上限时不动文件
  3) 截断后不残留半行
  4) 对「外部进程持有 fd 追加」的文件（shell.log / vnt2_web.log）同样有效，
     即截断后继续追加仍落在文件末尾，不会出现空洞
  5) /api/logs 返回大小与上限；/api/logs_clear 可清空

关键约束：本机是 Windows，无法执行 Linux ELF，因此不真的启动核心；
「外部写入方」用一个以 'ab' 打开的文件句柄模拟——这与
cmd/main 里的 `>> "$SHELL_LOG"`、核心的 `open(CORE_LOG, "ab")` 行为一致。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app", "shell"))

TESTVAR = os.path.abspath(os.path.join(HERE, "..", "build", "logvar"))
os.makedirs(TESTVAR, exist_ok=True)

os.environ["VNT_PKGVAR"] = TESTVAR
os.environ["VNT_PKGETC"] = os.path.join(TESTVAR, "etc")
os.environ["VNT_APPDEST"] = os.path.join(ROOT, "app")
os.environ["VNT_APPNAME"] = "vnt-nas"

import shell_server as S  # noqa: E402

FAILS = []


def _reset(path):
    """把日志重置为空。

    用截断而非 os.remove：语义等价（测试要的是「干净起点」），
    且不触发沙箱的批量删除守卫。
    """
    try:
        with open(path, "wb"):
            pass
    except OSError:
        pass


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + ("  " + str(extra) if extra else ""))
    if not cond:
        FAILS.append(name)


def line(n):
    return ("2026-11-14 12:00:00 filler-line-%06d-padding-to-make-it-long\n" % n)


def write_lines(path, n, prefix=""):
    with open(path, "a", encoding="utf-8") as f:
        for i in range(n):
            f.write(prefix + line(i))


def test_trim_over_limit():
    print("1) 超过上限自动截断，且保留尾部")
    p = S.MAIN_LOG
    _reset(p)
    # 约 60 字节/行 × 12000 行 ≈ 720KB，超过 512KB 上限
    write_lines(p, 12000)
    before = os.path.getsize(p)
    check("构造出超限文件", before > S.LOG_MAX_BYTES,
          "%.0f KB" % (before / 1024))

    # 触发一次写入（_log 内会检查大小）
    S.log("after-trim-marker")
    after = os.path.getsize(p)
    check("被截断到上限以内", after <= S.LOG_KEEP_BYTES + 4096,
          "%.0f KB" % (after / 1024))
    check("文件确实变小", after < before)

    txt = open(p, encoding="utf-8", errors="replace").read()
    check("保留尾部内容（能读到刚写的那行）",
          "after-trim-marker" in txt)
    check("保留的是靠后的内容（早期行已丢弃）",
          "filler-line-000000" not in txt)
    check("有截断提示行", "log trimmed" in txt)
    # 首行应是提示行，不应残留半行
    first = txt.splitlines()[0] if txt.splitlines() else ""
    check("首行是完整提示行，无半行残留",
          first.startswith("[log trimmed"), first[:40])


def test_no_trim_under_limit():
    print("2) 未超限时不动文件")
    p = S.UPDATE_LOG
    _reset(p)
    S._log(p, "small-entry-1")
    size1 = os.path.getsize(p)
    S._log(p, "small-entry-2")
    size2 = os.path.getsize(p)
    txt = open(p, encoding="utf-8").read()
    check("小文件被追加（未截断）", size2 > size1)
    check("两条都在", "small-entry-1" in txt and "small-entry-2" in txt)
    check("无截断提示", "log trimmed" not in txt)


def test_append_after_trim_external_fd():
    print("3) 外部持有 fd 追加：截断后继续写入不错位")
    p = S.SHELL_LOG
    _reset(p)
    write_lines(p, 12000)

    # 模拟 cmd/main 的 `>> "$SHELL_LOG"`：以 append 打开并长期持有
    fh = open(p, "ab")
    fh.write(b"line-from-external-writer-1\n")
    fh.flush()

    # 此时超过上限，触发截断（模拟守护线程的巡检）
    S.trim_logs()
    trimmed_size = os.path.getsize(p)
    check("外部写入的日志被截断", trimmed_size <= S.LOG_KEEP_BYTES + 4096,
          "%.0f KB" % (trimmed_size / 1024))

    # 关键：持久的 append 句柄继续写入，必须落在新末尾（不能有空洞）
    fh.write(b"line-after-trim-external-2\n")
    fh.flush()
    fh.close()

    with open(p, "rb") as f:
        data = f.read()
    check("截断后外部写入仍生效",
          b"line-after-trim-external-2" in data)
    # O_APPEND 语义下，写入应恰好接在文件末尾，长度等于 截断后 + 新行
    added = len(b"line-after-trim-external-2\n")
    check("新内容紧接末尾，无空洞",
          len(data) == trimmed_size + added,
          "now=%d expected=%d" % (len(data), trimmed_size + added))


def test_trim_logs_reports():
    print("4) trim_logs 报告被截断的文件名")
    for p in (S.MAIN_LOG, S.UPDATE_LOG, S.SHELL_LOG, S.CORE_LOG):
        _reset(p)
    big = S.MAIN_LOG
    write_lines(big, 12000)
    small = S.UPDATE_LOG
    write_lines(small, 3)

    got = S.trim_logs()
    check("报告含 main", "main" in got, got)
    check("不含未超限的 update", "update" not in got, got)
    check("不存在的 core 不报错", "core" not in got, got)


def test_api_shape():
    print("5) 接口契约：大小与上限、清空")
    h = open(os.path.join(ROOT, "app", "shell", "shell_http.py"),
             encoding="utf-8").read()
    check("logs 返回 size", '"size": _file_size' in h)
    check("logs 返回 limit", '"limit": S.LOG_MAX_BYTES' in h)
    check("暴露 shell 日志", '"shell": S.SHELL_LOG' in h)
    check("提供清空接口", '"/api/logs_clear"' in h)
    check("清空需管理员", "需要管理员权限" in h)

    js = open(os.path.join(ROOT, "app", "shell", "web", "app.js"),
              encoding="utf-8").read()
    check("前端有清空函数", "function clearLog" in js)
    check("前端显示大小", "logSize" in js)

    html = open(os.path.join(ROOT, "app", "shell", "web", "index.html"),
                encoding="utf-8").read()
    check("页面有清空按钮", 'id="btnClearLog"' in html)
    check("页面有大小提示", 'id="logSize"' in html)

    sh = open(os.path.join(ROOT, "app", "shell", "lib.sh"),
              encoding="utf-8").read()
    check("shell 侧也有截断函数", "vnt_trim_log()" in sh)
    check("shell 阈值与 Python 一致(512KB)",
          "524288" in sh)
    # 不能对持有 fd 的文件用 mv（换 inode 会让写入方错位）
    trim_body = sh.split("vnt_trim_log()")[1].split("\n}")[0]
    check("shell 侧原地回写（不用 mv 换 inode）",
          "cat \"$tmp\" > \"$f\"" in trim_body and "mv -f" not in trim_body)


def main():
    print("=== 日志清理测试 ===")
    print()
    test_trim_over_limit()
    print()
    test_no_trim_under_limit()
    print()
    test_append_after_trim_external_fd()
    print()
    test_trim_logs_reports()
    print()
    test_api_shape()
    print()
    if FAILS:
        print("失败 %d 项: %s" % (len(FAILS), ", ".join(FAILS)))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
