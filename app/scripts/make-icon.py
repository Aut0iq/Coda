"""
Иконка Coda в стиле vici: тёмный фон с виньеткой, золотая антиква с металлическим градиентом и тенью,
за буквой светящаяся дуга. Из одного рисунка получаются:
  icon.png          — обычная иконка 1024x1024 (с фоном)
  adaptive-icon.png — слой для адаптивной иконки Android (буква и дуга на прозрачном фоне, внутри безопасной зоны)
  monochrome-icon.png — силуэт буквы для тематических иконок Android 13+
Шрифт: Bodoni MT Bold (BOD_B.TTF, есть в Windows с Office). Зависимости: Pillow, numpy.
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "assets"
FONT = r"C:\Windows\Fonts\BOD_B.TTF"
LETTER = "C"
S = 2                       # суперсэмплинг: рисуем в 2048, отдаём 1024
N = 1024 * S


def hexrgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


def lerp_stops(t, stops):
    """t: массив 0..1 → цвета по списку (позиция, #rrggbb)."""
    pos = [p for p, _ in stops]
    cols = np.stack([hexrgb(c) for _, c in stops])
    out = np.zeros(t.shape + (3,), dtype=np.float32)
    for ch in range(3):
        out[..., ch] = np.interp(t, pos, cols[:, ch])
    return out


def background():
    y, x = np.mgrid[0:N, 0:N].astype(np.float32)
    d = np.hypot(x - N * 0.5, y - N * 0.50) / (N * 0.72)
    t = np.clip(d, 0, 1) ** 1.35
    c0, c1 = hexrgb("#1a1016"), hexrgb("#08060a")
    img = c0 * (1 - t[..., None]) + c1 * t[..., None]
    return Image.fromarray(img.astype("uint8"), "RGB").convert("RGBA")


def glyph_mask(scale):
    """Маска буквы (L, N×N): высота ≈ 520 px (в масштабе 1024) × scale, центр — в середине рисунка."""
    target_h = 520 * S * scale
    size = 400
    font = ImageFont.truetype(FONT, size)
    l, t, r, b = font.getbbox(LETTER)
    size = int(size * target_h / (b - t))
    font = ImageFont.truetype(FONT, size)
    l, t, r, b = font.getbbox(LETTER)
    m = Image.new("L", (N, N), 0)
    ImageDraw.Draw(m).text((N / 2 - (l + r) / 2, N * 0.525 - (t + b) / 2), LETTER, font=font, fill=255)
    return m


def arc_layer(scale, fade_ends):
    """Светящаяся красная дуга. Центр окружности ниже иконки, видна верхняя часть."""
    y, x = np.mgrid[0:N, 0:N].astype(np.float32)
    cx, cy, r = N * 0.5, (1102 - 512) * S * scale + N * 0.5, 717 * S * scale
    d = np.hypot(x - cx, y - cy)
    w = 7.5 * S * scale                                       # половина толщины линии
    ring = np.clip(1.0 - (np.abs(d - r) - w) / (1.6 * S), 0, 1)
    edge = np.abs(x - N * 0.5) / (N * 0.5)
    fall = 1.0 - 0.58 * edge ** 1.6                           # к краям тускнеет
    if fade_ends:
        fall *= np.clip(1.0 - (edge / 0.80) ** 3, 0, 1) ** 0.8  # на прозрачном слое концы растворяются
    ring *= fall
    core = Image.fromarray((ring * 255).astype("uint8"), "L")
    glow1 = core.filter(ImageFilter.GaussianBlur(11 * S * scale))
    glow2 = core.filter(ImageFilter.GaussianBlur(38 * S * scale))
    # лёгкая красная дымка внутри дуги
    inside = np.clip((r - d) / (95 * S * scale), 0, 1)
    haze = (np.exp(-(((r - d) / (120 * S * scale)) ** 2)) * (d < r) * fall * 0.20 * 255).astype("uint8")
    del inside
    red = np.array([225, 50, 43], dtype=np.float32)
    rgba = np.zeros((N, N, 4), dtype=np.float32)
    rgba[..., :3] = red
    a = (np.asarray(core, dtype=np.float32) * 1.0
         + np.asarray(glow1, dtype=np.float32) * 1.05
         + np.asarray(glow2, dtype=np.float32) * 0.80
         + haze.astype(np.float32))
    rgba[..., 3] = np.clip(a, 0, 255)
    # ядро линии чуть светлее, как у vici (#e1322b в центре)
    return Image.fromarray(rgba.astype("uint8"), "RGBA")


def letter_layer(mask, scale):
    """Золотая буква с диагональным металлическим градиентом + тень."""
    bbox = mask.getbbox()
    x0, y0, x1, y1 = bbox
    y, x = np.mgrid[0:N, 0:N].astype(np.float32)
    t = ((x - x0) + (y - y0)) / float((x1 - x0) + (y1 - y0))
    t = np.clip(t, 0, 1)
    col = lerp_stops(t, [(0.00, "#fff0bd"), (0.18, "#f1cf72"), (0.40, "#d9ab47"), (0.58, "#b9882d"),
                         (0.76, "#c99b3c"), (1.00, "#e6bc58")])
    gold = Image.fromarray(col.astype("uint8"), "RGB").convert("RGBA")
    gold.putalpha(mask)
    sh = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    shadow_mask = Image.new("L", (N, N), 0)
    shadow_mask.paste(mask, (int(15 * S * scale), int(17 * S * scale)))
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(9 * S * scale))
    sh.putalpha(shadow_mask.point(lambda v: int(v * 0.82)))
    return sh, gold


def compose(scale, with_bg, fade_ends):
    img = background() if with_bg else Image.new("RGBA", (N, N), (0, 0, 0, 0))
    img = Image.alpha_composite(img, arc_layer(scale, fade_ends))
    sh, gold = letter_layer(glyph_mask(scale), scale)
    img = Image.alpha_composite(img, sh)
    img = Image.alpha_composite(img, gold)
    return img


def down(im, mode=None):
    out = im.resize((1024, 1024), Image.LANCZOS)
    return out.convert(mode) if mode else out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    down(compose(1.0, True, False), "RGB").save(OUT / "icon.png", optimize=True)
    # слой адаптивной иконки: всё внутри безопасной зоны (~61%), поэтому рисунок уменьшен
    down(compose(0.80, False, True)).save(OUT / "adaptive-icon.png", optimize=True)
    # силуэт для тематических иконок: белая буква, альфа — форма
    m = glyph_mask(0.80).resize((1024, 1024), Image.LANCZOS)
    mono = Image.new("RGBA", (1024, 1024), (255, 255, 255, 0))
    mono.putalpha(m)
    mono.save(OUT / "monochrome-icon.png", optimize=True)
    print("saved to", OUT.resolve())


if __name__ == "__main__":
    main()
