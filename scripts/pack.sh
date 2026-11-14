#!/usr/bin/env bash
# 纯 shell 打包器：不依赖 fnpack，自行组装 fpk（tar.gz）。
# 优势：在 Windows / Linux 上都能正确设置 cmd/* 的可执行权限（755）。
# 用法：bash scripts/pack.sh [版本号]
set -euo pipefail

PROJ="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJ"

NAME="vnt-nas"
STAGE="build/.stage"
OUT="$PROJ/${NAME}.fpk"

if [ -n "${1:-}" ]; then
    sed -i.bak "s/^version[[:space:]]*=.*/version = ${1}/" manifest && rm -f manifest.bak
fi
VERSION="$(grep -E '^version' manifest | head -1 | sed 's/.*=[[:space:]]*//')"
echo "==> 打包 $NAME v$VERSION"

python scripts/gen_icons.py >/dev/null 2>&1 || python3 scripts/gen_icons.py >/dev/null
cp -f app/ui/images/icon_256.png app/shell/web/icon.png
rm -rf app/shell/__pycache__ 2>/dev/null || true

echo "==> 组装暂存目录"
rm -rf "$STAGE" "$OUT"
mkdir -p "$STAGE/cmd" "$STAGE/config" "$STAGE/wizard"

# 1) app.tgz —— 应用本体，解压到 target/（与 fnpack 一致：附带 config 副本）
rm -rf build/.appstage
mkdir -p build/.appstage
cp -rf app/shell app/ui build/.appstage/
cp -rf config build/.appstage/config
find build/.appstage -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# 统一换行为 LF。Windows 上编辑会把 .py/.sh 写成 CRLF，
# 打进包后在 Linux 上虽多数能跑，但不规范且可能触发解释器报错。
python - <<'PYEOF'
import os
for root, _dirs, files in os.walk('build/.appstage'):
    for name in files:
        p = os.path.join(root, name)
        # 只处理文本类文件，跳过图片等二进制
        if os.path.splitext(name)[1].lower() in ('.png', '.jpg', '.jpeg', '.gif', '.ico'):
            continue
        try:
            raw = open(p, 'rb').read()
        except OSError:
            continue
        if b'\r\n' in raw:
            open(p, 'wb').write(raw.replace(b'\r\n', b'\n'))
PYEOF

chmod 755 build/.appstage/shell/lib.sh
tar -czf "$STAGE/app.tgz" -C build/.appstage shell ui config

# 2) 生命周期脚本：统一 755，并去除 CRLF（避免 Linux 下解释器报错）
for f in cmd/*; do
    [ -f "$f" ] || continue
    base="$(basename "$f")"
    sed 's/\r$//' "$f" > "$STAGE/cmd/$base"
    chmod 755 "$STAGE/cmd/$base"
done

# 3) 配置与向导
cp -f config/privilege config/resource "$STAGE/config/"
chmod 644 "$STAGE/config/"*
for f in wizard/*; do
    [ -f "$f" ] || continue
    sed 's/\r$//' "$f" > "$STAGE/wizard/$(basename "$f")"
    chmod 644 "$STAGE/wizard/$(basename "$f")"
done

# 4) 图标（大写命名，符合 fnpack 约定）
cp -f ICON.PNG ICON_256.PNG "$STAGE/"

# 5) manifest：补 checksum（app.tgz 的 MD5）
if command -v md5sum >/dev/null 2>&1; then
    SUM="$(md5sum "$STAGE/app.tgz" | awk '{print $1}')"
elif command -v md5 >/dev/null 2>&1; then
    SUM="$(md5 -q "$STAGE/app.tgz")"
else
    SUM="$(python -c "import hashlib,sys;print(hashlib.md5(open(sys.argv[1],'rb').read()).hexdigest())" "$STAGE/app.tgz")"
fi
sed 's/\r$//' manifest | grep -v '^[[:space:]]*checksum' > "$STAGE/manifest"
printf 'checksum              = %s\n' "$SUM" >> "$STAGE/manifest"

echo "==> 生成 fpk"
# 注意：条目名不能带 ./ 前缀，与 fnpack 产物保持一致
( cd "$STAGE" && tar -czf "$OUT" $(ls -A) )
rm -rf "$STAGE" build/.appstage

echo
echo "==> 产物: $OUT"
ls -lh "$OUT"
