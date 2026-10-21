#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地 ComfyUI 自检：连通 → 节点 → 模型 → 真出一张（走完整 pipeline，产物落 storage/debug）。

    python tools/check_comfy.py                 # 用 test_face.jpg
    python tools/check_comfy.py 你的照片.jpg 4   # 指定照片 + 出 4 张候选

只读判断，不改项目状态；返回码 0 = 全绿。
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "backend"))
os.environ.setdefault("COMFY_HOST", "http://127.0.0.1:8188")

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from pipeline import compose, mask as maskmod, preprocess  # noqa: E402
from pipeline import select as picker  # noqa: E402
from providers.comfy import CKPT, HOST, Comfy  # noqa: E402

SIZE = int(os.environ.get("RENDER_SIZE", "768"))
OK, BAD = "[ok]  ", "[!!]  "


def main():
    photo = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "test_face.jpg")
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    prompt = sys.argv[3] if len(sys.argv) > 3 else (
        "short textured crop with faded sides, natural black, "
        "photorealistic, natural hair strands")
    ref_path = sys.argv[4] if len(sys.argv) > 4 else None   # 目标发型参考图（发型库封面）
    c = Comfy()

    print("=" * 64)
    print("1) 服务", HOST)
    if not c.available():
        print(BAD, "/system_stats 不通 → 先跑 D:\\AI\\ComfyUI\\run_comfy.bat")
        return 1
    st = httpx.get(f"{HOST}/system_stats", timeout=5).json()
    for d in st.get("devices", []):
        print(OK, d.get("name"), f"vram {d.get('vram_total', 0)/2**30:.1f}G",
              "free", f"{d.get('vram_free', 0)/2**30:.1f}G")

    print("2) 节点 / 模型")
    info = httpx.get(f"{HOST}/object_info", timeout=60).json()
    need = ["CheckpointLoaderSimple", "LoadImageMask", "VAEEncodeForInpaint", "KSampler",
            "SaveImage", "RepeatLatentBatch"]
    miss = [x for x in need if x not in info]
    print((BAD if miss else OK), "核心节点", "缺 " + ",".join(miss) if miss else "齐")
    ipa = [x for x in ("IPAdapterUnifiedLoader", "IPAdapterAdvanced", "IPAdapter") if x in info]
    print((OK if ipa else "[--]  "), "IP-Adapter 节点", ipa or "没装（跑纯 prompt 重绘）")

    ckpts = (info.get("CheckpointLoaderSimple", {}).get("input", {})
             .get("required", {}).get("ckpt_name", [[""]])[0])
    print((OK if CKPT in ckpts else BAD), "checkpoint", CKPT,
          "→" + ("在" if CKPT in ckpts else f"不在，实际有 {ckpts}"))

    print("3) 真出一张", os.path.basename(photo))
    data = open(photo, "rb").read()
    p = preprocess.prepare(data, size=SIZE)
    fb = maskmod.face_box_in_crop(p["face"], p["rect"], SIZE)
    m = maskmod.hair_mask(p["img"], fb)                      # 紧贴发丝：回贴用
    m_g = maskmod.dilate(m, px=max(4, SIZE // 128))          # 略扩：给模型看
    import numpy as np
    cover = float((np.asarray(m) > 127).mean()) * 100
    print(OK, f"人脸框 {p['face']} 裁剪 {p['rect']} mask 覆盖 {cover:.1f}%")

    ref_b = open(ref_path, "rb").read() if ref_path else None
    ab = compose.color_from_prompt(prompt)
    tgt = (0.0, ab[0], ab[1]) if ab else compose.hair_lab(p["img"], m)

    t0 = time.time()
    gens = c.generate_n(preprocess.to_bytes(p["img"], "PNG"),
                        preprocess.to_bytes(m_g, "PNG"),
                        prompt, "blurry, plastic hair, cartoon", SIZE, n=n,
                        ref_bytes=ref_b)
    dt = time.time() - t0
    print(OK, f"出图 {len(gens)} 张，{dt:.1f}s（{dt/max(1,len(gens)):.1f}s/张）")

    outs = [compose.composite(p["orig"], g, p["rect"], m, target_lab=tgt) for g in gens]
    idx, scores = picker.best(p["img"], gens, m) if len(gens) > 1 else (0, [])
    dbg = os.path.join(HERE, "storage", "debug")
    os.makedirs(dbg, exist_ok=True)
    paths = []
    for k, o in enumerate(outs):
        fp = os.path.join(dbg, f"check_comfy_{k}.jpg")
        o.save(fp, quality=93)
        paths.append(fp)
        sc = scores[k] if k < len(scores) else {}
        print("     候选", k, "★" if k == idx else " ", json.dumps(sc, ensure_ascii=False))

    # 拼对比图：原图 | 各候选
    W = p["orig"].width
    sheet = Image.new("RGB", (W * (len(outs) + 1), p["orig"].height), (20, 20, 20))
    sheet.paste(p["orig"], (0, 0))
    for k, o in enumerate(outs):
        sheet.paste(o.resize(p["orig"].size, Image.LANCZOS), (W * (k + 1), 0))
    sht = os.path.join(HERE, "storage", "debug", "check_comfy_sheet.jpg")
    sheet.save(sht, quality=90)
    print("4) 产物")
    print("     选中", paths[idx])
    print("     对比", sht)
    print("=" * 64)
    print("全绿：后端把 provider 切成 comfy 就能用（n=1~4 走择优）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
