# -*- coding: utf-8 -*-
"""本地直接跑一单（不经 HTTP），用来看真实效果。

    python tools/try_style.py PHOTO STYLE_ID [STYLE_ID ...]
输出 storage/debug/try_<style>_<k>.jpg 和一行改动率。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend", "pipeline"))

import numpy as np
from PIL import Image

import prompts
from pipeline import compose, mask as maskmod, preprocess
from pipeline import select as picker
from providers.base import registry as build_registry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DBG = os.path.join(ROOT, "storage", "debug")
COVERS = os.path.join(ROOT, "data", "covers")
os.makedirs(DBG, exist_ok=True)


def load_env():
    p = os.path.join(ROOT, ".env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def styles():
    import styles as stylelib
    stylelib.reload()
    return stylelib.ALL


def changed_pct(a: Image.Image, b: Image.Image):
    A = np.asarray(a.convert("RGB"), np.int16)
    B = np.asarray(b.convert("RGB"), np.int16)
    d = np.abs(A - B).max(2)
    return float((d > 8).mean() * 100)


def _bun_score(im, fb):
    import numpy as _np
    g = _np.asarray(im.convert("L")).astype(_np.float32)
    H, W = g.shape
    fx, fy, fw, fh = fb
    y0, y1 = int(max(0, fy - fh * 0.70)), int(min(H, fy + fh * 0.05))
    x0, x1 = int(max(0, fx + fw * 0.05)), int(min(W, fx + fw * 0.95))
    if y1 <= y0 or x1 <= x0:
        return 0.0
    return float((g[y0:y1, x0:x1] < 95).mean())


def run(photo, sid, size=768, n=3):
    load_env()
    prov_name = os.environ.get("PROVIDER") or ""
    reg = build_registry()
    if not prov_name:
        for k in ("comfy", "gemini", "openai", "mock"):
            if k in reg:
                prov_name = k
                break
    prov = reg[prov_name]
    st = {s["id"]: s for s in styles()}[sid]

    data = open(photo, "rb").read()
    p = preprocess.prepare(data, size=size)
    fb = maskmod.face_box_in_crop(p["face"], p["rect"], size)

    L = maskmod.length_cm(st.get("name_en"), st.get("prompt_en"),
                          st.get("prompt_zh"), " ".join(st.get("tags") or []))
    updo = maskmod.updo_kind(st.get("name"), st.get("name_en"), st.get("prompt_en"),
                             st.get("prompt_zh"), " ".join(st.get("tags") or []))
    if updo:
        L = 15 if updo == "high" else 20
    m_hair = maskmod.hair_mask(p["img"], fb)
    m = maskmod.style_mask(p["img"], fb, L)
    m_g = maskmod.dilate(m, px=max(6, size // 96))

    short_case = False
    end_y = None
    if fb:
        end_y = fb[1] + fb[3] * maskmod.LEN_END.get(int(L), 1.12)
        short_case = end_y < maskmod.hair_bottom(m_hair) - 0.02 * size
    if updo:
        short_case = False

    prompt = prompts.build(st, "en")
    neg = prompts.NEGATIVE_EN
    if updo:
        prompt = prompts.build_short(st, "en")
        neg = prompts.build_short_neg(prompts.NEGATIVE_EN)
    elif short_case:
        prompt = prompts.build_short(st, "en")
        neg = prompts.build_short_neg(prompts.NEGATIVE_EN)
    crop_b, mask_b = preprocess.to_bytes(p["img"], "PNG"), preprocess.to_bytes(m_g, "PNG")

    ref_b = None
    cu = st.get("cover_url") or ""
    if cu.startswith("/covers/"):
        rp = os.path.join(COVERS, os.path.basename(cu))
        if os.path.exists(rp):
            ref_b = open(rp, "rb").read()

    ab = compose.color_from_prompt((st.get("prompt_en") or "") + " " + (st.get("prompt_zh") or ""))
    target_lab = (0.0, ab[0], ab[1]) if ab else None

    # 扎发：短底 + 头顶发髻（与 app._do_updo 同一套）
    if updo and prov_name == "comfy":
        y = int(fb[1] + fb[3] * maskmod.LEN_END.get(15, 0.98))
        cut, _ = compose.cut_hair(p["img"], m_hair, y)
        cut.save(os.path.join(DBG, f"try_{sid}_updo_cut.jpg"), quality=91)
        band = maskmod.updo_band(p["img"], m_hair, fb, y, grow=25, blur=6)
        band.save(os.path.join(DBG, f"try_{sid}_updo_band.png"))
        cleans = prov.generate_n(preprocess.to_bytes(cut, "PNG"), preprocess.to_bytes(band, "PNG"),
                                 prompts.UPDO_CLEAN_POS, prompts.UPDO_CLEAN_NEG, size, n=3,
                                 denoise=1.0)
        clean = min(cleans, key=lambda g: compose.hair_ratio_below(g, y))
        clean.save(os.path.join(DBG, f"try_{sid}_updo_clean.jpg"), quality=92)
        plate = Image.composite(clean, cut, band)
        patch = band
        if updo == "high":
            bm = maskmod.bun_mask(plate, fb, up=0.74, half=0.46, down=0.10)
            bm.save(os.path.join(DBG, f"try_{sid}_bun.png"))
            cands = prov.generate_n(preprocess.to_bytes(plate, "PNG"), preprocess.to_bytes(bm, "PNG"),
                                    prompts.UPDO_HIGH_POS + "natural black hair, photorealistic",
                                    prompts.UPDO_NEG, size, n=3, denoise=0.92, cfg=8.0)
            best = max(cands, key=lambda g: _bun_score(g, fb))
            plate = compose.composite_patch(plate, best, (0, 0, size, size), plate, bm)
            patch = Image.fromarray(np.maximum(np.asarray(band), np.asarray(bm)))
        outs = [compose.composite_patch(p["orig"], plate, p["rect"], plate, patch,
                                        target_lab=target_lab)]
        idx, scores, gens, m = 0, [], [plate], maskmod.style_mask(p["img"], fb, 15)
    else:
        # 剪短：几何剪到位（长度锁死）→ 模型只修发尾那一小条
        gen_src_b, gen_src_img = crop_b, p["img"]
        base_img, patch = p["img"], m_g
        v2 = False
        if short_case and prov_name == "comfy":
            y_line = int(end_y) if end_y else int(size * 0.62)
            cut, patch = compose.cut_hair(p["img"], m_hair, y_line)
            cut.save(os.path.join(DBG, f"try_{sid}_cut.jpg"), quality=91)
            base_img = gen_src_img = cut
            gen_src_b = preprocess.to_bytes(cut, "PNG")
            patch = compose.tail_band(m_hair, y_line, fb[3] if fb else size * 0.3)
            patch.save(os.path.join(DBG, f"try_{sid}_band.png"))
            mask_b = preprocess.to_bytes(patch, "PNG")
            v2 = True

        gen_kw = {"ref_bytes": ref_b}
        if v2:
            gen_kw["denoise"] = 0.7
        if n > 1 and getattr(prov, "supports_n", False):
            gens = prov.generate_n(gen_src_b, mask_b, prompt, neg, size, n=n, **gen_kw)
        else:
            gens = [prov.generate(gen_src_b, mask_b, prompt, neg, size, **gen_kw)]

        if v2:
            outs = [compose.composite_patch(p["orig"], g, p["rect"], base_img, patch,
                                            target_lab=target_lab) for g in gens]
        elif short_case:
            outs = [compose.composite_short(p["orig"], g, p["rect"], base_img, m_g,
                                            target_lab=target_lab) for g in gens]
        else:
            outs = [compose.composite(p["orig"], g, p["rect"], m, target_lab=target_lab,
                                      stats_mask=m_hair, full=False) for g in gens]
        idx, scores = picker.best(gen_src_img, gens, patch) if len(gens) > 1 else (0, [])

    print(f"[{sid}] {st['name']} L={L} short={short_case} n={len(gens)} pick={idx} "
          f"changed={changed_pct(p['orig'], outs[idx]):.1f}%")
    m.save(os.path.join(DBG, f"try_{sid}_mask.png"))
    for k, o in enumerate(outs):
        path = os.path.join(DBG, f"try_{sid}_{k}.jpg")
        o.save(path, quality=93)
    for k, g in enumerate(gens):
        g.save(os.path.join(DBG, f"try_{sid}_gen{k}.jpg"), quality=92)
    return outs[idx], p["orig"], idx


if __name__ == "__main__":
    photo = sys.argv[1]
    ids = sys.argv[2:] or ["ps0101"]
    res = []
    for s in ids:
        try:
            o, orig, k = run(photo, s)
            res.append((s, o))
        except Exception as e:
            import traceback
            traceback.print_exc()
            print("FAIL", s, e)
    # 拼一张对比
    if res:
        orig = Image.open(photo).convert("RGB")
        h = 640
        tiles = [orig.resize((int(orig.width * h / orig.height), h))]
        for s, o in res:
            tiles.append(o.resize((int(o.width * h / o.height), h)))
        wsum = sum(t.width for t in tiles) + 8 * (len(tiles) - 1)
        sheet = Image.new("RGB", (wsum, h), (255, 255, 255))
        x = 0
        for t in tiles:
            sheet.paste(t, (x, 0))
            x += t.width + 8
        out = os.path.join(DBG, "try_sheet.jpg")
        sheet.save(out, quality=94)
        print("sheet:", out)
