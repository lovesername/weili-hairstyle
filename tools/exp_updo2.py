# -*- coding: utf-8 -*-
"""两段式扎发：先做「干净底片」（把披发区填成脖子/衣领/背景），再画扎发。
    python tools/exp_updo2.py PHOTO STYLE_ID [high|low]
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend", "pipeline"))

import numpy as np
from PIL import Image, ImageDraw

import prompts
from pipeline import compose, mask as maskmod, preprocess
from providers.comfy import Comfy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DBG = os.path.join(ROOT, "storage", "debug")

CLEAN_POS = ("plain shirt collar, bare neck and nape skin, clean studio background, "
             "no hair, smooth fabric, photorealistic")
CLEAN_NEG = ("hair, long hair, hair strands, hair on clothes, fur, wig, texture noise, halo, blur")


def lower_mask(hair, fb, y_line):
    W, H = hair.size
    a = np.asarray(hair) > 100
    a[:max(0, y_line - 10), :] = False
    m = Image.fromarray((a * 255).astype(np.uint8))
    from PIL import ImageFilter
    m = m.filter(ImageFilter.MaxFilter(17)).filter(ImageFilter.GaussianBlur(5))
    return m


def up_region(im, fb, kind, y_line):
    m = maskmod.hair_mask(im, fb)
    W, H = im.size
    fx, fy, fw, fh = fb
    cx = fx + fw / 2
    up = fh * (0.58 if kind == "high" else 0.34)
    half = fw * (0.94 if kind == "high" else 0.88)
    reg = Image.new("L", (W, H), 0)
    ImageDraw.Draw(reg).ellipse([cx - half, max(0, fy - up), cx + half, fy + fh * 0.85], fill=255)
    a = np.maximum(np.asarray(m), np.asarray(reg))
    a[max(0, int(y_line + fh * 0.10)):, :] = 0
    hole = maskmod._face_hole(im, fb, W, H, pad_ratio=0.46, top=0.16)
    a = np.minimum(a, 255 - np.asarray(hole))
    from PIL import ImageFilter
    return Image.fromarray(a.astype(np.uint8)).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(2))


def run(photo, sid, kind="high", size=768):
    import styles as stylelib
    stylelib.reload()
    st = {s["id"]: s for s in stylelib.ALL}[sid]
    p = preprocess.prepare(open(photo, "rb").read(), size=size)
    fb = maskmod.face_box_in_crop(p["face"], p["rect"], size)
    m_hair = maskmod.hair_mask(p["img"], fb)
    y_line = int(fb[1] + fb[3] * maskmod.UPDO_LINE[kind])
    lm = lower_mask(m_hair, fb, y_line)
    lm.save(os.path.join(DBG, f"u2_{sid}_lower.png"))
    prov = Comfy()

    # 段 1：干净底片
    clean_gens = prov.generate_n(preprocess.to_bytes(p["img"], "PNG"),
                                 preprocess.to_bytes(lm, "PNG"),
                                 CLEAN_POS, CLEAN_NEG, size, n=3, denoise=1.0)
    for i, g in enumerate(clean_gens):
        g.save(os.path.join(DBG, f"u2_{sid}_clean{i}.jpg"), quality=91)

    # 段 2：在底片上画扎发（只允许扎点以上）
    pr = prompts.build_updo(st, kind, "en")
    neg = prompts.build_updo_neg(prompts.NEGATIVE_EN)
    tiles = [p["img"].resize((400, 400))]
    for i, clean in enumerate(clean_gens):
        rg = up_region(clean, fb, kind, y_line)
        gens = prov.generate_n(preprocess.to_bytes(clean, "PNG"), preprocess.to_bytes(rg, "PNG"),
                               pr, neg, size, n=2, denoise=0.95)
        for k, g in enumerate(gens):
            g.save(os.path.join(DBG, f"u2_{sid}_c{i}_{k}_gen.jpg"), quality=91)
            out = compose.composite_patch(p["orig"], g, p["rect"], clean, rg)
            out.save(os.path.join(DBG, f"u2_{sid}_c{i}_{k}.jpg"), quality=92)
            print(f"clean{i} gen{k} 扎点下头发 {compose.hair_ratio_below(g, y_line):.3f}")
            tiles.append(out.resize((400, 400)))
    sheet = Image.new("RGB", (400 * len(tiles) + 6 * (len(tiles) - 1), 400), (255, 255, 255))
    x = 0
    for t in tiles:
        sheet.paste(t, (x, 0))
        x += 406
    sheet.save(os.path.join(DBG, f"u2_{sid}_sheet.jpg"), quality=93)
    print("y_line", y_line, "sheet", f"u2_{sid}_sheet.jpg")


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "high")
