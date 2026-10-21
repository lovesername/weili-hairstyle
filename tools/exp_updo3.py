# -*- coding: utf-8 -*-
"""扎发（高）三段式验证：
   1) 几何把耳线以下的旧长发换成背景/衣领（确定性）
   2) 模型清理这一带（脖子/衣领/背景，不出头发）
   3) 贴头骨的帽形 + 发髻位 mask，画「全部收上去的扎发」
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend", "pipeline"))

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

import prompts
from pipeline import compose, mask as maskmod, preprocess
from providers.comfy import Comfy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DBG = os.path.join(ROOT, "storage", "debug")

CLEAN_POS = ("bare neck and collarbone skin, plain shirt collar, clean studio background, "
             "natural skin tone, no hair, no strands, photorealistic portrait")
CLEAN_NEG = ("hair, long hair, strands, dark patch, blur, halo, distorted neck, seam, fur collar")


def band_mask(hair, y_line, grow=31, blur=8):
    a = np.asarray(hair) > 100
    a[:max(0, y_line - 10), :] = False
    m = Image.fromarray((a * 255).astype(np.uint8))
    return m.filter(ImageFilter.MaxFilter(grow)).filter(ImageFilter.GaussianBlur(blur))


def cap_mask(im, fb, kind="high"):
    W, H = im.size
    fx, fy, fw, fh = fb
    cx = fx + fw / 2
    cap = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(cap)
    d.ellipse([cx - fw * 0.58, fy - fh * 0.24, cx + fw * 0.58, fy + fh * 0.60], fill=255)
    d.ellipse([cx - fw * 0.36, fy - fh * 0.50, cx + fw * 0.36, fy - fh * 0.02], fill=255)
    hole = maskmod._face_hole(im, fb, W, H, pad_ratio=0.46, top=0.16)
    a = np.minimum(np.asarray(cap), 255 - np.asarray(hole))
    return Image.fromarray(a).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(3))


def run(photo, sid, kind="high", size=768, n=3):
    import styles as stylelib
    stylelib.reload()
    st = {s["id"]: s for s in stylelib.ALL}[sid]
    p = preprocess.prepare(open(photo, "rb").read(), size=size)
    fb = maskmod.face_box_in_crop(p["face"], p["rect"], size)
    m_hair = maskmod.hair_mask(p["img"], fb)
    y = int(fb[1] + fb[3] * maskmod.UPDO_LINE[kind])
    prov = Comfy()

    cut, _ = compose.cut_hair(p["img"], m_hair, y)
    cut.save(os.path.join(DBG, f"u8_{sid}_cut.jpg"), quality=91)
    band = band_mask(m_hair, y)
    clean = prov.generate(preprocess.to_bytes(cut, "PNG"), preprocess.to_bytes(band, "PNG"),
                          CLEAN_POS, CLEAN_NEG, size, denoise=1.0)
    clean.save(os.path.join(DBG, f"u8_{sid}_clean.jpg"), quality=92)

    cap = cap_mask(clean, fb, kind)
    pr = prompts.build_updo(st, kind, "en")
    neg = prompts.build_updo_neg(prompts.NEGATIVE_EN)
    gens = prov.generate_n(preprocess.to_bytes(clean, "PNG"), preprocess.to_bytes(cap, "PNG"),
                           pr, neg, size, n=n, denoise=0.95)
    patch = Image.fromarray(np.maximum(np.asarray(band), np.asarray(cap)))
    tiles = [p["img"].resize((400, 400)), cut.resize((400, 400)), clean.resize((400, 400))]
    for i, g in enumerate(gens):
        g.save(os.path.join(DBG, f"u8_{sid}_gen{i}.jpg"), quality=92)
        out = compose.composite_patch(p["orig"], g, p["rect"], clean, patch)
        out.save(os.path.join(DBG, f"u8_{sid}_{i}.jpg"), quality=93)
        tiles.append(out.resize((400, 400)))
        print(i, "扎点下头发", round(compose.hair_ratio_below(g, y), 3))
    sheet = Image.new("RGB", (400 * len(tiles) + 6 * (len(tiles) - 1), 400), (255, 255, 255))
    x = 0
    for t in tiles:
        sheet.paste(t, (x, 0))
        x += 406
    sheet.save(os.path.join(DBG, f"u8_{sid}_sheet.jpg"), quality=93)
    print("y", y, "sheet", f"u8_{sid}_sheet.jpg")


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "high")
