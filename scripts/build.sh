#!/usr/bin/env bash
# 用官方 fnpack 打包（备选方案）。
#
# 推荐改用 scripts/pack.sh：它自行组装 fpk，会把 cmd/* 权限设为 755；
# 而 fnpack 在 Windows 上会把脚本权限打成 666，飞牛可能因此执行失败。
#
# 用法：bash scripts/build.sh [版本号]
set -euo pipefail

PROJ="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJ"

VERSION="${1:-}"

FNPACK="${FNPACK:-}"
if [ -z "$FNPACK" ]; then
    for c in fnpack fnpack.exe; do
        if command -v "$c" >/dev/null 2>&1; then FNPACK="$c"; break; fi
    done
fi
if [ -z "$FNPACK" ]; then
    echo "未找到 fnpack。请先下载并放入 PATH：" >&2
    echo "  Windows: https://static2.fnnas.com/fnpack/fnpack-1.2.3-windows-amd64" >&2
    echo "  Linux  : https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-amd64" >&2
    echo "" >&2
    echo "或者直接使用无需额外依赖的：bash scripts/pack.sh" >&2
    exit 1
fi

echo "==> 生成图标"
python scripts/gen_icons.py 2>/dev/null || python3 scripts/gen_icons.py

echo "==> 同步前端图标"
cp -f app/ui/images/icon_256.png app/shell/web/icon.png

echo "==> 语法自检"
python -m py_compile app/shell/shell_server.py app/shell/shell_http.py \
    app/shell/shell_main.py 2>/dev/null || python3 -m py_compile \
    app/shell/shell_server.py app/shell/shell_http.py app/shell/shell_main.py
for f in cmd/*; do bash -n "$f"; done
bash -n app/shell/lib.sh
echo "    语法自检通过"

if [ -n "$VERSION" ]; then
    echo "==> 设置版本为 $VERSION"
    sed -i.bak "s/^version[[:space:]]*=.*/version = ${VERSION}/" manifest && rm -f manifest.bak
fi

echo "==> 清理缓存文件"
rm -rf app/shell/__pycache__ 2>/dev/null || true
find . -name '.DS_Store' -delete 2>/dev/null || true

echo "==> 调用 fnpack 打包"
"$FNPACK" build

echo
echo "==> 产物"
ls -lh *.fpk 2>/dev/null || echo "（未找到 fpk，请检查 fnpack 输出）"
