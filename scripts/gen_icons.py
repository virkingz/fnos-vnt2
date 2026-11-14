#!/usr/bin/env python3
"""生成飞牛 fpk 所需图标：根目录 ICON.PNG/ICON_256.PNG 与 app/ui/images 下的入口图标。

图案：深蓝圆角底 + 青绿色网状节点，表示「组网」。
使用 4 倍超采样后 LANCZOS 缩放，保证小尺寸边缘干净。

依赖 Pillow。仓库中已提交生成好的图标，因此缺少 Pillow 时
本脚本会直接跳过（保留现有图标），不会让打包失败。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 图标为已提交的产物，无 Pillow 时保留现状即可
try:
    from PIL import Image, ImageDraw
except ImportError:
    print("未安装 Pillow，跳过图标生成（沿用仓库中已有的图标）")
    sys.exit(0)

BG = (12, 68, 124, 255)      # #0C447C
BG2 = (24, 95, 165, 255)     # #185FA5
NODE = (93, 202, 165, 255)   # #5DCAA5
NODE_HI = (193, 246, 226, 255)
LINE = (93, 202, 165, 200)

S = 1024  # 超采样画布


def render(size):
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角背景
    pad = int(S * 0.055)
    radius = int(S * 0.22)
    d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=radius, fill=BG)
    # 顶部一层浅色提升层次（内缩圆角）
    inset = int(S * 0.13)
    d.rounded_rectangle(
        [inset, inset, S - inset, S - inset],
        radius=int(radius * 0.7),
        fill=BG2,
    )

    # 三个节点构成三角网状
    cx, cy = S / 2, S / 2
    r = S * 0.235
    pts = [
        (cx, cy - r),
        (cx - r * 0.9, cy + r * 0.62),
        (cx + r * 0.9, cy + r * 0.62),
    ]
    # 连线
    lw = int(S * 0.038)
    for i in range(3):
        d.line([pts[i], pts[(i + 1) % 3]], fill=LINE, width=lw)

    # 节点
    nr = S * 0.088
    for i, (px, py) in enumerate(pts):
        d.ellipse([px - nr, py - nr, px + nr, py + nr], fill=NODE_HI)
        ir = nr * 0.55
        d.ellipse([px - ir, py - ir, px + ir, py + ir], fill=NODE)

    return img.resize((size, size), Image.LANCZOS)


def main():
    targets = [
        (os.path.join(ROOT, "ICON.PNG"), 64),
        (os.path.join(ROOT, "ICON_256.PNG"), 256),
        (os.path.join(ROOT, "app", "ui", "images", "icon_64.png"), 64),
        (os.path.join(ROOT, "app", "ui", "images", "icon_256.png"), 256),
    ]
    for path, size in targets:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        render(size).save(path, "PNG", optimize=True)
        print(f"wrote {path} ({size}x{size})")


if __name__ == "__main__":
    main()
