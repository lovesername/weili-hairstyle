# -*- coding: utf-8 -*-
"""扎发三种喂 mask 方式的对照实验。用法：
    python tools/exp_updo.py PHOTO STYLE_ID
输出 storage/debug/up_<tag>.jpg 与每张的"扎点以下头发占比"。
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


def hair_mask_full(img, fb, size, kind):
    """整个头发轮廓 + 头顶留位，不切下面。"""
    m = maskmod.hair_mask(img, fb)
    W, H = img.size
    fx, fy, fw, fh = fb
    cx = fx + fw / 2
    up = fh * (0.58 if kind == "high" else 0.34)
    half = fw * (0.94 if kind == "high" else 0.88)
    reg = Image.new("L", (W, H), 0)
    ImageDraw.Draw(reg).ellipse([cx - half, max(0, fy - up), cx + half, fy + fh * 1.05], fill=255)
    a = np.maximum(np.asarray(m), np.asarray(reg))
    hole = maskmod._face_hole(img, fb, W, H, pad_ratio=0.46, top=0.16)
    a = np.minimum(a, 255 - np.asarray(hole))
    return Image.fromarray(a.astype(np.uint8)).filter(ImageFilterMax(5))


def ImageFilterMax(px):
    from PIL import ImageFilter
    return ImageFilter.MaxFilter(px)


def run(photo, sid, kind="high", size=768):
    import styles as stylelib
    stylelib.reload()
    st = {s["id"]: s for s in stylelib.ALL}[sid]
    p = preprocess.prepare(open(photo, "rb").read(), size=size)
    fb = maskmod.face_box_in_crop(p["face"], p["rect"], size)
    m_hair = maskmod.hair_mask(p["img"], fb)
    y_line = int(fb[1] + fb[3] * maskmod.UPDO_LINE[kind])
    cut, _ = compose.cut_hair(p["img"], m_hair, y_line)
    cut.save(os.path.join(DBG, f"up_{sid}_cut.jpg"), quality=91)

    region = maskmod.updo_region(cut, fb, kind, y_line)
    full = hair_mask_full(p["img"], fb, size, kind)

    pr = prompts.build_updo(st, kind, "en")
    neg = prompts.build_updo_neg(prompts.NEGATIVE_EN)
    prov = Comfy()

    variants = [
        ("A_region_cut", cut, region, 0.92),
        ("B_full_orig", p["img"], full, 0.95),
        ("C_full_cut", cut, full, 0.95),
    ]
    tiles = [p["img"].resize((420, 420))]
    labels = ["orig"]
    for tag, base, mk, dn in variants:
        gens = prov.generate_n(preprocess.to_bytes(base, "PNG"), preprocess.to_bytes(mk, "PNG"),
                               pr, neg, size, n=3, denoise=dn)
        for i, g in enumerate(gens):
            g.save(os.path.join(DBG, f"up_{sid}_{tag}_{i}.jpg"), quality=91)
            out = compose.composite_patch(p["orig"], g, p["rect"], base, mk)
            out.save(os.path.join(DBG, f"up_{sid}_{tag}_{i}_out.jpg"), quality=92)
            r = compose.hair_ratio_below(g, y_line)
            print(f"{tag} #{i} 扎点下头发 {r:.3f}")
            tiles.append(out.resize((420, 420)))
            labels.append(f"{tag}{i}")
    sheet = Image.new("RGB", (420 * len(tiles) + 6 * (len(tiles) - 1), 420), (255, 255, 255))
    x = 0
    for t in tiles:
        sheet.paste(t, (x, 0))
        x += 426
    sheet.save(os.path.join(DBG, f"up_{sid}_sheet.jpg"), quality=93)
    print("y_line", y_line, "sheet", f"up_{sid}_sheet.jpg")


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "high")
