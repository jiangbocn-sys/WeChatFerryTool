"""生成应用图标 assets/app.ico（多尺寸）。

为什么要多尺寸
--------------
通知区只显示 16×16。如果只放一张 256 的图让系统缩放，细笔画会发虚；
.ico 里塞 16/32/48/64/128/256 各一档，系统会挑最合适的那张，边缘清晰得多。

图形与托盘一致：微信绿圆 + 白色加号。

用法
----
    .venv\\Scripts\\python.exe tools\\make_icons.py
产物
----
    assets/app.ico        （给 exe 用：build_exe.ps1 的 --icon）
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT_DIR = Path(__file__).resolve().parent.parent
SIZES = (16, 32, 48, 64, 128, 256)
GREEN = (7, 193, 96, 255)      # 微信品牌绿 #07C160


def draw(size: int, color: tuple = GREEN, plus: tuple = (255, 255, 255, 255)) -> Image.Image:
    """按比例画：边距 4/64、笔画厚 6/64、加号半长 12/64。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    margin = max(1, round(size * 4 / 64))
    d.ellipse((margin, margin, size - margin - 1, size - margin - 1), fill=color)

    thick = max(1, round(size * 6 / 64))
    half = size * 12 / 64
    c = size / 2
    d.rectangle((c - half, c - thick / 2, c + half, c + thick / 2), fill=plus)
    d.rectangle((c - thick / 2, c - half, c + thick / 2, c + half), fill=plus)
    return img


def main() -> int:
    out_dir = PROJECT_DIR / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    ico = out_dir / "app.ico"

    base = draw(256)
    base.save(ico, format="ICO", sizes=[(s, s) for s in SIZES])

    # 顺手导出一张 png，便于预览/文档引用
    base.save(out_dir / "app.png", format="PNG")

    got = Image.open(ico)
    print(f"已生成 {ico}")
    print(f"  ico 内含尺寸: {sorted(got.info.get('sizes', []))}")
    print(f"  预览图: {out_dir / 'app.png'}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    raise SystemExit(main())
