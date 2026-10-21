# -*- coding: utf-8 -*-
"""扎发四段式验证：cut → 拍平 → 模型清带 → 帽形画扎发。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend", "pipeline"))

import cv2
import numpy as np
from PIL import Image, ImageFilter

import prompts
from pipeline import compose, mask as maskmod, preprocess
from providers.comfy import Comfy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DBG = os.path.join(ROOT, "storage", "debug")


def flatten(im, band):
    """把带内像素拍成「上方/下方邻居的纯色」，抹掉一切像头发的纹理。"""
    a = np.asarray(im.convert("RGB")).astype(np.float32)
    m = np.asarray(band) > 60
    out = a.copy()
    H, W, _ = a.shape
    # 从下往上抬：带外第一行（衣服/背景）的颜色往上铺
    carry = None
    for y in range(H - 1, -1, -1):
        row = m[y]
        good = ~row
        carry = out[y].copy() if carry is None else np.where(good[:, None], out[y], carry)
        out[y, row] = carry[row]
    for k in (61, 121):
        b = cv2.blur(np.clip(out, 0, 255).astype(np.uint8), (k, 1)).astype(np.float32)
        out[m] = b[m]
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(2))


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
    band = maskmod.updo_band(p["img"], m_hair, fb, y)
    flat = flatten(cut, band)
    flat.save(os.path.join(DBG, f"u11_{sid}_flat.jpg"), quality=92)

    clean = prov.generate(preprocess.to_bytes(flat, "PNG"), preprocess.to_bytes(band, "PNG"),
                          prompts.UPDO_CLEAN_POS, prompts.UPDO_CLEAN_NEG, size, denoise=1.0)
    clean.save(os.path.join(DBG, f"u11_{sid}_clean.jpg"), quality=92)

    cap = maskmod.updo_cap(clean, fb, kind)
    pr = prompts.build_updo(st, kind, "en")
    neg = prompts.build_updo_neg(prompts.NEGATIVE_EN)
    gens = prov.generate_n(preprocess.to_bytes(clean, "PNG"), preprocess.to_bytes(cap, "PNG"),
                           pr, neg, size, n=n, denoise=0.95, cfg=8.0)
    patch = Image.fromarray(np.maximum(np.asarray(band), np.asarray(cap)))
    tiles = [p["img"].resize((380, 380)), flat.resize((380, 380)), clean.resize((380, 380))]
    for i, g in enumerate(gens):
        out = compose.composite_patch(p["orig"], g, p["rect"], clean, patch)
        out.save(os.path.join(DBG, f"u11_{sid}_{i}.jpg"), quality=93)
        tiles.append(out.resize((380, 380)))
    sheet = Image.new("RGB", (380 * len(tiles) + 6 * (len(tiles) - 1), 380), (255, 255, 255))
    x = 0
    for t in tiles:
        sheet.paste(t, (x, 0))
        x += 386
    sheet.save(os.path.join(DBG, f"u11_{sid}_sheet.jpg"), quality=93)
    print("y", y, "sheet", f"u11_{sid}_sheet.jpg")


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "high")
