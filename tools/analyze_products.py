# -*- coding: utf-8 -*-
"""逐张分析产品图，量出每款的"卷度 + 发色 + 发量"，给 prompt 提供区分度。

分工（重要，别把权威标注扔给 CV）：
  长度   直接用品类名里的 15/20/30-35cm —— 这是厂家的权威标注，不用猜
  卷度   头发区域的结构张量方向一致性 coh（0~1）：直发发丝朝向一致 coh 高、
         卷发杂乱 coh 低（实测校准见下）
  发色   头发像素中位色 -> 最近调色板色名
  发量   头发面积/人脸面积，只取极端值（很蓬松/很贴头）才写进 prompt

校准结果（--calibrate，用品类真值对照）：
  直发 coh 0.36 [0.28,0.43] > 纹理 0.21 > 卷发 0.13 [0.10,0.19] > 锡纸烫 0.07
  -> 阈值 0.26/0.19/0.10：直发与卷发 Q3/Q1 不重叠，可干净分开

用法：
  python tools/analyze_products.py --calibrate
  python tools/analyze_products.py              # 写 data/products_metrics.json
"""
import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict

import cv2
import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
DATA = os.path.join(ROOT, "data")

from pipeline import face as facedet  # noqa: E402

PALETTE = [
    ("自然黑", (22, 128, 128)),
    ("深棕",   (36, 138, 140)),
    ("栗棕",   (48, 143, 148)),
    ("浅棕",   (58, 145, 155)),
    ("亚麻",   (68, 138, 160)),
    ("酒红",   (38, 152, 138)),
]

THR = {"curl_s": 0.26, "curl_l": 0.19, "curl_xs": 0.10,
       "vol_xhi": 1.5, "vol_hi": 0.75, "vol_lo": 0.35}


def lab_of(rgb):
    r, g, b = [c / 255.0 for c in rgb]
    f = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = f(r), f(g), f(b)
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047
    y = (r * 0.2126 + g * 0.7152 + b * 0.0722)
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883
    fx = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx_, fy_, fz_ = fx(x), fx(y), fx(z)
    return (116 * fy_ - 16, 500 * (fx_ - fy_), 200 * (fy_ - fz_))


def nearest_color(lab):
    d = lambda r: (lab[0] - r[0]) ** 2 + (lab[1] - r[1]) ** 2 + (lab[2] - r[2]) ** 2
    return min(PALETTE, key=lambda p: d(p[1]))[0]


def parse_cat(name):
    """'女士直发（15cm）' -> ('女','直发',15)。长度是品类权威标注。"""
    gender = "男" if "男士" in name else "女"
    import re
    m = re.search(r"（([\d,\s]+)cm）", name)
    ln = int((m.group(1).split(",")[0].strip() if m else 15))
    typ = "直发"
    for k in ("锡纸烫", "发片", "全头套", "混发", "纹理", "卷发", "直发"):
        if k in name:
            typ = k
            break
    return gender, typ, ln


def analyze(path):
    im = Image.open(path).convert("RGB")
    W, H = im.size
    px = np.asarray(im, dtype=np.int16)
    faces = facedet.detect(im)
    if not faces:
        return None
    fx, fy, fw, fh, score = faces[0]

    # 背景分割（纯色影棚底）
    border = np.concatenate([px[:4].reshape(-1, 3), px[-4:].reshape(-1, 3),
                             px[:, :4].reshape(-1, 3), px[:, -4:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    person = np.sqrt(((px - bg) ** 2).sum(axis=2)) > 42

    # 深色低饱和 -> 头发候选（放宽到 150 让高光下的发丝也进来）
    mx = px.max(axis=2)
    mn = px.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) * 255 // np.maximum(mx, 1), 0)
    cand = (person & (mx < 150) & (sat < 95)).astype(np.uint8)
    # 闭运算把高光断开的发丝接回去，再取最大连通域
    cand = cv2.morphologyEx(cand, cv2.MORPH_CLOSE, np.ones((13, 13), np.uint8))
    # 头发最多到脸底下 2.2 倍脸高，掐掉衣服阴影
    ylimit = min(H, fy + fh + int(fh * 2.2))
    cand[ylimit:] = 0
    n, lab, stats, _ = cv2.connectedComponentsWithStats(cand, 8)
    if n <= 1:
        return None
    best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if stats[best, cv2.CC_STAT_AREA] < 800:
        return None
    comp = lab == best

    ys, xs = np.where(comp)
    # 脸框内部那块（眉眼）不该算头发
    inside = (ys >= fy) & (ys < fy + fh) & (xs >= fx) & (xs < fx + fw)
    keep_px = int(comp.sum() - inside.sum())
    if keep_px < 800:
        return None

    # 卷曲度：结构张量方向一致性。直发发丝朝向一致 coh 高，卷发杂乱 coh 低。
    # 校准（品类真值）：直发 0.36 [0.28,0.43] > 纹理 0.21 > 卷发 0.13 [0.10,0.19] > 锡纸烫 0.07
    g = np.asarray(im.convert("L"), dtype=np.float32)
    hm = comp.copy()
    hm[fy:fy + fh, fx:fx + fw] = False
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)[hm]
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)[hm]
    jxx, jyy, jxy = float((gx * gx).sum()), float((gy * gy).sum()), float((gx * gy).sum())
    tr = jxx + jyy
    if tr < 1e-6:
        return None
    coh = float(np.sqrt((jxx - jyy) ** 2 + 4 * jxy * jxy) / tr)

    # 发量
    volume = float(keep_px / (fw * fh))

    # 发色
    rgb = px[ys, xs][~inside].astype(np.float32)
    med = tuple(int(np.median(rgb[:, i])) for i in range(3))
    color = nearest_color(lab_of(med))

    return {"face_score": round(score, 3), "hair_color": color, "hair_rgb": med,
            "curl": round(coh, 3), "volume": round(volume, 3),
            "hair_bottom_ratio": round((ys.max() - (fy + fh)) / fh, 3)}


def curl_word(cu):
    """cu = 方向一致性，高=直、低=卷。"""
    if cu > THR["curl_s"]:
        return "直发"
    if cu > THR["curl_l"]:
        return "微卷"
    if cu > THR["curl_xs"]:
        return "卷发"
    return "小卷"


def vol_word(vo):
    if vo > THR["vol_xhi"]:
        return "极蓬松"
    if vo > THR["vol_hi"]:
        return "蓬松"
    if vo < THR["vol_lo"]:
        return "贴头"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calibrate", action="store_true")
    a = ap.parse_args()

    prods = json.load(open(os.path.join(DATA, "products.json"), encoding="utf-8"))
    out, fail = [], []
    for p in prods:
        src = os.path.join(DATA, "products", p.get("cover_file") or "")
        if not os.path.exists(src):
            fail.append((p.get("product_code"), "no file"))
            continue
        try:
            m = analyze(src)
        except Exception as e:
            fail.append((p.get("product_code"), f"{type(e).__name__}:{e}"))
            continue
        if not m:
            fail.append((p.get("product_code"), "no face/hair"))
            continue
        m["product"] = p.get("product_code")
        m["category"] = p.get("category_name")
        out.append(m)

    print(f"[=] 分析 {len(out)} 张，失败 {len(fail)} {fail[:4]}")

    if a.calibrate:
        groups = defaultdict(list)
        for m in out:
            _, typ, _ = parse_cat(m["category"] or "")
            groups[typ].append(m)
        print("\n品类(真值) -> curl 中位[Q1,Q3] | vol 中位 | 预测卷度分布")
        for k, v in sorted(groups.items(), key=lambda x: -len(x[1])):
            c = sorted(x["curl"] for x in v)
            q1, q3 = c[len(c) // 4], c[3 * len(c) // 4]
            pred = Counter(curl_word(x["curl"]) for x in v).most_common()
            print(f"  {k:<5} n={len(v):<3} curl {statistics.median(c):5.2f} [{q1:5.2f},{q3:5.2f}]"
                  f"  vol {statistics.median(x['volume'] for x in v):4.2f}  预测 {pred}")
        print("\n发色分布:", Counter(m["hair_color"] for m in out).most_common())
        acc = None
        tt = [m for m in out if parse_cat(m["category"])[1] in ("直发", "卷发")]
        if tt:
            hit = sum((curl_word(m["curl"]) == "直发") == (parse_cat(m["category"])[1] == "直发")
                      for m in tt)
            acc = hit / len(tt)
            print(f"\n直发vs卷发 二分类准确率: {acc:.0%} (n={len(tt)})")
        return 0

    json.dump(out, open(os.path.join(DATA, "products_metrics.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("[=] 写入 data/products_metrics.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
