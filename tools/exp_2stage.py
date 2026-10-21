# -*- coding: utf-8 -*-
"""两阶段换发实验：先把长发抹成寸头/背景，再长目标发型。

    python tools/exp_2stage.py PHOTO STYLE_ID
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import numpy as np
from PIL import Image

import prompts
import styles as stylelib
from pipeline import compose, mask as mm, preprocess
from providers.comfy import Comfy

DBG = os.path.join(ROOT, "storage", "debug")
os.makedirs(DBG, exist_ok=True)

BUZZ = ("very short buzz cut, shaved sides, cropped hair above ears, ears and neck fully visible, "
        "clean scalp, black stubble")
BUZZ_NEG = ("long hair, shoulder length hair, hair strands, hair falling on shoulders, "
            "hair covering neck, hair covering ears, wig")


def main(photo, sid):
    stylelib.reload()
    st = stylelib.BY_ID[sid]
    p = preprocess.prepare(open(photo, "rb").read(), size=768)
    fb = mm.face_box_in_crop(p["face"], p["rect"], 768)
    L = mm.length_cm(st.get("name_en"), st.get("prompt_en"), st.get("prompt_zh"), " ".join(st.get("tags") or []))
    m_hair = mm.hair_mask(p["img"], fb)
    m = mm.style_mask(p["img"], fb, L)
    mg = mm.dilate(m, px=8)
    cb, mb = preprocess.to_bytes(p["img"], "PNG"), preprocess.to_bytes(mg, "PNG")

    prov = Comfy()
    # ---- 阶段 A：抹掉长发 ----
    a = prov.generate(cb, mb, BUZZ, BUZZ_NEG, 768, denoise=1.0)
    a.save(os.path.join(DBG, f"2s_{sid}_A.jpg"), quality=92)
    da = np.abs(np.asarray(p["img"], np.int16) - np.asarray(a, np.int16)).max(2)
    print("A 抹除: mask内改动 %.1f%%" % ((da[np.asarray(mg) > 127] > 8).mean() * 100))

    # ---- 阶段 B：在 A 上长目标发型 ----
    ab_a = preprocess.to_bytes(a, "PNG")
    m2 = mm.style_mask(a, fb, L)          # A 的头型重新算 mask
    mg2 = mm.dilate(m2, px=8)
    mb2 = preprocess.to_bytes(mg2, "PNG")
    prompt = prompts.build(st, "en")
    ref_b = None
    cu = st.get("cover_url") or ""
    if cu.startswith("/covers/"):
        rp = os.path.join(ROOT, "data", "covers", os.path.basename(cu))
        if os.path.exists(rp):
            ref_b = open(rp, "rb").read()
    b = prov.generate(ab_a, mb2, prompt, prompts.NEGATIVE_EN, 768, denoise=0.95, ref_bytes=ref_b)
    b.save(os.path.join(DBG, f"2s_{sid}_B.jpg"), quality=92)

    # ---- 回贴到原图 ----
    lab = compose.color_from_prompt((st.get("prompt_en") or "") + " " + (st.get("prompt_zh") or ""))
    tl = (0.0, lab[0], lab[1]) if lab else None
    out = compose.composite(p["orig"], b, p["rect"], m, target_lab=tl, stats_mask=m_hair, full=True)
    out.save(os.path.join(DBG, f"2s_{sid}_out.jpg"), quality=93)
    d = np.abs(np.asarray(p["orig"], np.int16) - np.asarray(out, np.int16)).max(2)
    print("最终改动 %.1f%%" % ((d > 8).mean() * 100))

    h = 700
    tiles = [p["orig"], a, b, out]
    tiles = [t.resize((int(t.width * h / t.height), h)) for t in tiles]
    sheet = Image.new("RGB", (sum(t.width for t in tiles) + 24, h), (255, 255, 255))
    x = 0
    for t in tiles:
        sheet.paste(t, (x, 0))
        x += t.width + 8
    sheet.save(os.path.join(DBG, f"2s_{sid}_sheet.jpg"), quality=94)
    print("sheet", os.path.join(DBG, f"2s_{sid}_sheet.jpg"))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
