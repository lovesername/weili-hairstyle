# -*- coding: utf-8 -*-
"""原型：用顾客自己的发丝纹理合成一个发髻（bun），贴到头顶/脑后。

思路：把头顶一块头发纹理做极坐标重映射（发丝向圆心收拢），
再用椭圆软边 + 径向明暗贴上去。这样发色/质感与原发 100% 一致。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DBG = os.path.join(ROOT, "storage", "debug")


def hair_tile(img, fb, size=160):
    """从头顶取一块纯发丝纹理（自动挑最暗的那块 = 头发，不会取到背景）。"""
    fx, fy, fw, fh = fb
    cx = int(fx + fw / 2)
    g = np.asarray(img.convert("L")).astype(np.float32)
    best, best_y = None, None
    for yy in range(int(fy - fh * 0.45), int(fy + fh * 0.45), 8):
        y0 = max(0, min(g.shape[0] - size, yy))
        x0 = max(0, min(g.shape[1] - size, cx - size // 2))
        lum = float(g[y0:y0 + size, x0:x0 + size].mean())
        if best is None or lum < best:
            best, best_y = lum, y0
    x0 = max(0, min(g.shape[1] - size, cx - size // 2))
    return img.crop((x0, best_y, x0 + size, best_y + size)).resize((size, size), Image.LANCZOS)


def polar_bun(tile, r=120, rot=0.0, tight=1.0):
    """极坐标重映射：发丝从中心放射，像收拢的发髻。"""
    t = np.asarray(tile.convert("RGB"))
    h, w, _ = t.shape
    # 目标极坐标网格
    yy, xx = np.mgrid[0:2 * r, 0:2 * r].astype(np.float32)
    dx = (xx - r) / r
    dy = (yy - r) / r
    ang = np.arctan2(dy, dx) + rot
    rad = np.sqrt(dx * dx + dy * dy)
    # 半径映射：中心收缩（tight 越大，中心纹理越密）
    rr = np.clip(rad ** (1.0 / tight), 0, 1)
    u = ((ang / (2 * np.pi)) % 1.0) * (w - 1)
    v = rr * (h - 1)
    out = cv2.remap(t, u.astype(np.float32), v.astype(np.float32), cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REFLECT)
    return Image.fromarray(out)


def bun_alpha(r, soft=0.22):
    a = np.zeros((2 * r, 2 * r), np.float32)
    yy, xx = np.mgrid[0:2 * r, 0:2 * r].astype(np.float32)
    d = np.sqrt((xx - r) ** 2 + (yy - r) ** 2) / r
    a[:] = np.clip((1.0 - d) / soft, 0, 1)
    return a


def shade(bun, r, light=(-0.35, -0.4), strength=1.0):
    """径向明暗：左上亮、右下暗，圆球感。"""
    arr = np.asarray(bun.convert("RGB")).astype(np.float32)
    yy, xx = np.mgrid[0:2 * r, 0:2 * r].astype(np.float32)
    dx = (xx - r) / r
    dy = (yy - r) / r
    nz = np.clip(1.0 - (dx * dx + dy * dy), 0, 1) ** 0.5
    lam = np.clip(dx * light[0] + dy * light[1] + nz * 0.9, 0, 1)
    k = (0.55 + 0.9 * lam)[..., None] * strength
    arr = arr * k + 12 * (1 - nz)[..., None]
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def place(plate, bun, center, alpha, scale=1.0):
    r = bun.width // 2
    if scale != 1.0:
        s = int(bun.width * scale)
        bun = bun.resize((s, s), Image.LANCZOS)
        alpha = np.asarray(Image.fromarray((alpha * 255).astype(np.uint8)).resize((s, s))) / 255.0
        r = s // 2
    cx, cy = center
    out = np.asarray(plate.convert("RGB")).astype(np.float32)
    b = np.asarray(bun).astype(np.float32)
    h, w = alpha.shape
    x0, y0 = int(cx - w / 2), int(cy - h / 2)
    xs0, ys0 = max(0, x0), max(0, y0)
    xs1, ys1 = min(out.shape[1], x0 + w), min(out.shape[0], y0 + h)
    ax0, ay0 = xs0 - x0, ys0 - y0
    ax1, ay1 = ax0 + (xs1 - xs0), ay0 + (ys1 - ys0)
    a = alpha[ay0:ay1, ax0:ax1][..., None]
    out[ys0:ys1, xs0:xs1] = b[ay0:ay1, ax0:ax1] * a + out[ys0:ys1, xs0:xs1] * (1 - a)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


if __name__ == "__main__":
    plate = Image.open(sys.argv[1])
    fb = tuple(float(x) for x in sys.argv[2].split(","))
    tile = hair_tile(plate, fb)
    tile.save(os.path.join(DBG, "bun_tile.jpg"), quality=95)
    r = 110
    outs = []
    for i, (rot, tight, sc) in enumerate([(0.0, 1.0, 1.0), (0.7, 1.3, 0.95), (1.4, 0.8, 1.1)]):
        bun = shade(polar_bun(tile, r, rot, tight), r)
        a = bun_alpha(r)
        cx = fb[0] + fb[2] / 2
        cy = fb[1] - fb[3] * 0.12
        o = place(plate, bun, (cx, cy), a, sc).filter(ImageFilter.GaussianBlur(0.6))
        o.save(os.path.join(DBG, f"bun_{i}.jpg"), quality=93)
        outs.append(o)
    h = 420
    tiles = [plate.resize((h, h))] + [o.resize((h, h)) for o in outs]
    s = Image.new("RGB", (h * len(tiles) + 6 * (len(tiles) - 1), h), (255, 255, 255))
    x = 0
    for t in tiles:
        s.paste(t, (x, 0))
        x += h + 6
    s.save(os.path.join(DBG, "bun_sheet.jpg"), quality=94)
    print("ok")
