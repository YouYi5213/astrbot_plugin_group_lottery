"""生成插件市场展示用的 logo.png（256x256）。

绘制内容：紫色渐变圆角方块 + 礼盒 + 丝带 + 一枚小钥匙（呼应「私聊发密钥」）。
运行： python tests/make_logo.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
SS = 4  # 超采样倍率，先大后缩，得到平滑边缘
W = SIZE * SS
OUT = Path(__file__).resolve().parent.parent / "logo.png"

BG_TOP = (139, 118, 255)
BG_BOTTOM = (92, 63, 224)
BOX = (255, 255, 255)
BOX_SHADE = (232, 228, 255)
RIBBON = (255, 199, 92)
RIBBON_DARK = (240, 168, 48)
KEY = (255, 236, 179)


def rounded_gradient(size: int, radius: int) -> Image.Image:
    """竖直渐变 + 圆角遮罩。"""
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        grad.putpixel(
            (0, y),
            tuple(round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)),
        )
    grad = grad.resize((size, size))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [(0, 0), (size - 1, size - 1)], radius=radius, fill=255
    )

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def draw_gift(draw: ImageDraw.ImageDraw, s: int) -> None:
    """画礼盒（盒身 + 盒盖 + 丝带 + 蝴蝶结）。"""
    u = s / 256  # 单位缩放

    def px(*values: float) -> list[float]:
        return [v * u for v in values]

    # 盒身
    draw.rounded_rectangle(px(58, 116, 198, 208), radius=10 * u, fill=BOX)
    # 盒盖
    draw.rounded_rectangle(px(46, 88, 210, 132), radius=10 * u, fill=BOX)
    # 盒盖底部阴影
    draw.rectangle(px(46, 124, 210, 132), fill=BOX_SHADE)

    # 竖向丝带
    draw.rectangle(px(116, 88, 140, 208), fill=RIBBON)
    # 横向丝带（盒盖上）
    draw.rectangle(px(46, 104, 210, 120), fill=RIBBON)

    # 蝴蝶结：两个圆环
    draw.ellipse(px(72, 44, 130, 96), outline=RIBBON_DARK, width=int(12 * u))
    draw.ellipse(px(126, 44, 184, 96), outline=RIBBON_DARK, width=int(12 * u))
    draw.ellipse(px(114, 76, 142, 104), fill=RIBBON_DARK)


def draw_key(draw: ImageDraw.ImageDraw, s: int) -> None:
    """在右下角画一枚小钥匙（先描一圈深色轮廓，避免压在白色盒身上看不清）。"""
    u = s / 256

    def px(*values: float) -> list[float]:
        return [v * u for v in values]

    cx, cy, r = 198 * u, 194 * u, 27 * u
    angle = math.radians(45)
    dx, dy = math.cos(angle), math.sin(angle)
    x0, y0 = cx + dx * r * 0.75, cy + dy * r * 0.75
    x1, y1 = x0 + dx * 36 * u, y0 + dy * 36 * u

    def stamp(color: tuple[int, int, int], grow: float) -> None:
        rr = r + grow
        draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=color)
        draw.line([x0, y0, x1, y1], fill=color, width=int((13 + grow * 2) * u))
        for offset in (0.55, 0.82):
            bx = x0 + dx * 36 * u * offset
            by = y0 + dy * 36 * u * offset
            draw.line(
                [bx, by, bx - dy * 14 * u, by + dx * 14 * u],
                fill=color,
                width=int((10 + grow * 2) * u),
            )

    stamp((70, 46, 190), 3.0 * u)  # 轮廓
    stamp(KEY, 0.0)

    # 钥匙孔
    hole = r * 0.42
    draw.ellipse([cx - hole, cy - hole, cx + hole, cy + hole], fill=(124, 96, 240))


def main() -> None:
    canvas = rounded_gradient(W, radius=int(56 * SS / 1))
    draw = ImageDraw.Draw(canvas)
    draw_gift(draw, W)
    draw_key(draw, W)

    canvas = canvas.resize((SIZE, SIZE), Image.LANCZOS)
    canvas.save(OUT, format="PNG", optimize=True)
    print(f"已生成 {OUT}（{SIZE}x{SIZE}）")


if __name__ == "__main__":
    main()
