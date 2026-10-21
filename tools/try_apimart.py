# -*- coding: utf-8 -*-
"""apimart 单款直跑（不经 HTTP），用于验证 key 和效果。

用法：
    python tools/try_apimart.py storage/uploads/xxx.jpg ps0101 [张数]

出图：storage/debug/api_<style>_<k>.jpg  + 对比图 storage/debug/api_<style>_cmp.jpg
成本：脚本会打印每张的 cost（apimart 返回值）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import app  # noqa: E402
import prompts  # noqa: E402
import styles  # noqa: E402
from pipeline import compose as cp  # noqa: E402
from pipeline import mask as mk  # noqa: E402
from pipeline import preprocess as pp  # noqa: E402
from PIL import Image  # noqa: E402

if len(sys.argv) < 3:
    print(__doc__)
    raise SystemExit(2)
path, sid = sys.argv[1], sys.argv[2]
n = int(sys.argv[3]) if len(sys.argv) > 3 else 2

prov = app.PROVIDERS["apimart"]
if not prov.available():
    print("缺 key：", app.REASON["apimart"])
    raise SystemExit(3)

style = styles.BY_ID[sid]
p = pp.prepare(open(path, "rb").read(), size=app.SIZE)
fb = mk.face_box_in_crop(p["face"], p["rect"], app.SIZE)
m_hair = mk.hair_mask(p["img"], fb)
m = mk.style_mask(p["img"], fb, 20)

ref_b = None
cu = style.get("cover_url") or ""
if cu.startswith("/covers/"):
    rp = os.path.join(app.COVERS, os.path.basename(cu))
    if os.path.exists(rp):
        ref_b = open(rp, "rb").read()
print("款式:", style.get("id"), style.get("name"), "| 参考图:", bool(ref_b),
      "| 模型:", prov.model)

pos = prompts.build_edit(style, "zh", has_ref=bool(ref_b))
gens = prov.generate_n(pp.to_bytes(p["img"], "PNG"), pp.to_bytes(m, "PNG"), pos, "",
                       app.SIZE, n=n, ref_bytes=ref_b)

outs = []
for k, g in enumerate(gens):
    g.save(os.path.join(app.DBG, f"api_{sid}_{k}.jpg"), quality=93)
    o = cp.composite_block(p["orig"], g, p["rect"])
    o.save(os.path.join(app.DBG, f"api_{sid}_{k}_full.jpg"), quality=94)
    outs.append(o)
print("出图:", len(outs), "张 ->", app.DBG)

# 对比图：原图 | 结果
W = p["orig"].width
crop_h = int(W * 0.9)
cmp_im = Image.new("RGB", (W * (len(outs) + 1), crop_h), "white")
src = p["orig"].crop((0, 0, W, min(crop_h, p["orig"].height))).resize((W, crop_h))
cmp_im.paste(src, (0, 0))
for i, o in enumerate(outs):
    cmp_im.paste(o.resize((W, crop_h), Image.LANCZOS), (W * (i + 1), 0))
out_cmp = os.path.join(app.DBG, f"api_{sid}_cmp.jpg")
cmp_im.save(out_cmp, quality=92)
print("对比:", out_cmp)
