# -*- coding: utf-8 -*-
"""扎发最终方案验证：cut → 模型清带（脖子/衣领，顺带把头发收成短底）→ 头顶加髻。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend", "pipeline"))

import numpy as np
from PIL import Image

import prompts
from pipeline import compose, mask as maskmod, preprocess
from providers.comfy import Comfy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DBG = os.path.join(ROOT, "storage", "debug")


def run(photo, sid, size=768):
    import styles as stylelib
    stylelib.reload()
    st = {s["id"]: s for s in stylelib.ALL}[sid]
    p = preprocess.prepare(open(photo, "rb").read(), size=size)
    fb = maskmod.face_box_in_crop(p["face"], p["rect"], size)
    m_hair = maskmod.hair_mask(p["img"], fb)
    y = int(fb[1] + fb[3] * maskmod.LEN_END.get(15, 0.98))
    prov = Comfy()

    cut, _ = compose.cut_hair(p["img"], m_hair, y)
    band = maskmod.updo_band(p["img"], m_hair, fb, y, grow=25, blur=6)
    clean = prov.generate(preprocess.to_bytes(cut, "PNG"), preprocess.to_bytes(band, "PNG"),
                          prompts.UPDO_CLEAN_POS, prompts.UPDO_CLEAN_NEG, size, denoise=1.0)
    clean.save(os.path.join(DBG, f"f1_{sid}_clean.jpg"), quality=92)
    plate = compose.composite_patch(cut, clean, (0, 0, size, size), cut, band)
    plate.save(os.path.join(DBG, f"f1_{sid}_plate.jpg"), quality=92)

    bm = maskmod.bun_mask(plate, fb, up=0.74, half=0.46, down=0.10)
    bg = prov.generate(preprocess.to_bytes(plate, "PNG"), preprocess.to_bytes(bm, "PNG"),
                       prompts.UPDO_HIGH_POS + "natural black hair, photorealistic",
                       prompts.UPDO_NEG, size, denoise=0.92, cfg=8.0)
    patch = Image.fromarray(np.maximum(np.asarray(band), np.asarray(bm)))
    out = compose.composite_patch(p["orig"], bg, p["rect"], plate, patch)
    out.save(os.path.join(DBG, f"f1_{sid}.jpg"), quality=94)

    tiles = [p["img"].resize((380, 380)), cut.resize((380, 380)), plate.resize((380, 380)),
             bg.resize((380, 380)), out.resize((380, 380))]
    sheet = Image.new("RGB", (380 * len(tiles) + 6 * (len(tiles) - 1), 380), (255, 255, 255))
    x = 0
    for t in tiles:
        sheet.paste(t, (x, 0))
        x += 386
    sheet.save(os.path.join(DBG, f"f1_{sid}_sheet.jpg"), quality=93)
    print(sid, "ok")


if __name__ == "__main__":
    for sid in sys.argv[2:]:
        run(sys.argv[1], sid)
