# -*- coding: utf-8 -*-
"""用顾客自己的发丝纹理合成发髻：极坐标收拢 + 球面明暗 + 外圈碎发。
    python tools/bun2.py PLATE FB
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DBG = os.path.join(ROOT, "storage", "debug")


def best_tile(img, fb, size=96, hair=None):
    """在头两侧的发团里找一块最像头发的纹理（暗 + 有纹理）。"""
    g = np.asarray(img.convert("L")).astype(np.float32)
    fx, fy, fw, fh = fb
    best, box = 1e9, None
    for cy in np.arange(fy - fh * 0.10, fy + fh * 0.55, fh * 0.08):
        for cx in (fx + fw * 0.06, fx + fw * 0.94):
            x0 = int(cx - size / 2)
            y0 = int(cy - size / 2)
            if x0 < 0 or y0 < 0 or x0 + size > g.shape[1] or y0 + size > g.shape[0]:
                continue
            patch = g[y0:y0 + size, x0:x0 + size]
            score = patch.mean() - patch.std() * 0.4
            if score < best:
                best, box = score, (x0, y0)
    tile = img.crop((box[0], box[1], box[0] + size, box[1] + size)).resize((size, size), Image.LANCZOS)
    if hair is not None:
        # 只保留真发像素，亮的地方（背景/高光边）用周围发色补掉 —— 不然发髻会带一圈绒毛
        hm = np.asarray(hair.crop((box[0], box[1], box[0] + size, box[1] + size)).resize((size, size), Image.LANCZOS))
        keep = (hm > 110).astype(np.uint8)
        if keep.sum() > size * size * 0.3:
            arr = np.asarray(tile.convert("RGB"))
            filled = cv2.inpaint(cv2.cvtColor(arr, cv2.COLOR_RGB2BGR), 1 - keep, 6, cv2.INPAINT_TELEA)
            arr = np.where(keep[..., None] > 0, arr, cv2.cvtColor(filled, cv2.COLOR_BGR2RGB))
            # 全部压到「发色」范围：取 keep 区的中位色做上下限
            med = np.median(arr[keep > 0].reshape(-1, 3), axis=0)
            arr = np.clip(arr, med * 0.45, med * 1.9 + 18).astype(np.uint8)
            tile = Image.fromarray(arr)
    return tile


def swirl(tile, r, tight=1.15, turns=0.18):
    t = np.asarray(tile.convert("RGB"))
    h, w, _ = t.shape
    yy, xx = np.mgrid[0:2 * r, 0:2 * r].astype(np.float32)
    dx = (xx - r) / r
    dy = (yy - r) / r
    ang = np.arctan2(dy, dx)
    rad = np.sqrt(dx * dx + dy * dy)
    # 纹理沿半径方向拉伸，并在中心加一点旋转（发髻的收拢感）
    u = ((ang / (2 * np.pi) + turns * (1 - np.clip(rad, 0, 1))) % 1.0) * (w - 1)
    v = np.clip(rad ** (1.0 / tight), 0, 1) * (h - 1)
    out = cv2.remap(t, u.astype(np.float32), v.astype(np.float32), cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REFLECT)
    return Image.fromarray(out)


def sphere(bun, r, light=(-0.45, -0.5)):
    a = np.asarray(bun.convert("RGB")).astype(np.float32)
    yy, xx = np.mgrid[0:2 * r, 0:2 * r].astype(np.float32)
    dx = (xx - r) / r
    dy = (yy - r) / r
    d2 = np.clip(1 - (dx * dx + dy * dy), 0, 1)
    nz = d2 ** 0.6
    lam = np.clip(dx * light[0] + dy * light[1] + nz, 0, 1)
    a = a * (0.35 + 1.15 * lam)[..., None]
    a = a + 26 * ((1 - lam) * 0.15 + 0.02)[..., None]
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def bun_alpha(r, soft=0.16, wobble=0.03):
    yy, xx = np.mgrid[0:2 * r, 0:2 * r].astype(np.float32)
    ang = np.arctan2(yy - r, xx - r)
    d = np.sqrt((xx - r) ** 2 + (yy - r) ** 2) / r
    edge = 1.0 + wobble * np.sin(ang * 7) + 0.02 * np.sin(ang * 13)
    return np.clip((edge - d) / soft, 0, 1)


def paste(plate, bun, center, alpha):
    out = np.asarray(plate.convert("RGB")).astype(np.float32)
    b = np.asarray(bun).astype(np.float32)
    h, w = alpha.shape
    x0, y0 = int(center[0] - w / 2), int(center[1] - h / 2)
    xs0, ys0 = max(0, x0), max(0, y0)
    xs1, ys1 = min(out.shape[1], x0 + w), min(out.shape[0], y0 + h)
    ax0, ay0 = xs0 - x0, ys0 - y0
    ax1, ay1 = ax0 + xs1 - xs0, ay0 + ys1 - ys0
    a = alpha[ay0:ay1, ax0:ax1][..., None]
    out[ys0:ys1, xs0:xs1] = b[ay0:ay1, ax0:ax1] * a + out[ys0:ys1, xs0:xs1] * (1 - a)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


if __name__ == "__main__":
    plate = Image.open(sys.argv[1]).convert("RGB")
    fb = tuple(float(x) for x in sys.argv[2].split(","))
    from pipeline import mask as mm
    tile = best_tile(plate, fb, hair=mm.hair_mask(plate, fb))
    tile.save(os.path.join(DBG, "bun2_tile.jpg"), quality=95)
    fx, fy, fw, fh = fb
    tiles = [plate.resize((400, 400))]
    for i, (r, sc, cy_k, tight) in enumerate([(110, 1.0, -0.16, 1.15), (125, 1.1, -0.22, 0.95), (100, 0.9, -0.10, 1.35)]):
        bun = sphere(swirl(tile, r, tight), r)
        a = bun_alpha(r)
        o = paste(plate, bun, (fx + fw / 2, fy + fh * cy_k), a)
        o = o.filter(ImageFilter.GaussianBlur(0.5))
        o.save(os.path.join(DBG, f"bun2_{i}.jpg"), quality=93)
        tiles.append(o.resize((400, 400)))
    s = Image.new("RGB", (400 * len(tiles) + 6 * (len(tiles) - 1), 400), (255, 255, 255))
    x = 0
    for t in tiles:
        s.paste(t, (x, 0))
        x += 406
    s.save(os.path.join(DBG, "bun2_sheet.jpg"), quality=94)
    print("ok")
