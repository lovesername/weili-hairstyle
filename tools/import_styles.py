# -*- coding: utf-8 -*-
"""把抓到的素材 / 本地款式图批量灌进发型库（data/styles.json）。

三种来源：
  1. 抓取产物：python tools/import_styles.py --catalog
     只收「单人 + 人脸够大 + 不是文字图」的图，自动打标 + 生成 prompt
  2. 本地文件夹：python tools/import_styles.py --dir D:/款式图 --tags 短发,卷发
     一个文件 = 一款，文件名即款名
  3. 清单：python tools/import_styles.py --csv list.csv    列：name,tags,cover,color

产物：
  data/styles.json   追加去重（按封面 sha1）
  data/covers/<id>.jpg  统一 512×512
  data/import_report.json  收了多少、拒了多少、为什么拒
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline import face as facedet  # noqa: E402

DATA = os.path.join(ROOT, "data")
COVERS = os.path.join(DATA, "covers")
STYLES_JSON = os.path.join(DATA, "styles.json")

COLORS = ["自然黑", "深棕", "栗棕", "浅棕", "亚麻", "红棕", "奶茶", "灰棕", "酒红", "黑茶", "挑染"]

TPL = {
    "短发": ("干净利落的{color}短发，层次自然，发尾略带弧度，发丝有光泽",
             "clean {en} short haircut, natural layers, slight curve at ends, glossy strands"),
    "中长": ("锁骨长度的{color}中长发，发尾自然内扣，发丝顺滑",
             "collarbone-length {en} hair, inward ends, sleek strands"),
    "长发": ("及胸的{color}长直发，中分，发丝顺滑有光泽",
             "chest-length straight {en} hair, center part, sleek and glossy"),
    "卷发": ("蓬松的{color}卷发，卷度自然不僵硬，发量饱满",
             "voluminous {en} curls, natural non-stiff waves, full density"),
    "扎发": ("利落的{color}扎发，头顶蓬起，脸颊两侧留少量碎发",
             "neat {en} updo, lifted crown, a few loose strands framing the face"),
    "男士": ("男士{color}短发，两侧修剪干净，顶部有纹理",
             "men {en} short hair, clean sides, textured top"),
}
EN = {"自然黑": "natural black", "深棕": "dark brown", "栗棕": "chestnut brown",
      "浅棕": "light brown", "亚麻": "flaxen", "红棕": "auburn", "奶茶": "milk-tea brown",
      "灰棕": "ash brown", "酒红": "burgundy", "黑茶": "black-tea brown", "挑染": "balayage"}


def norm_cover(src_path, dst_path, size=512):
    im = Image.open(src_path).convert("RGB")
    w, h = im.size
    side = min(w, h)
    im = im.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
    im = im.resize((size, size), Image.LANCZOS)
    im.save(dst_path, "JPEG", quality=88)
    return dst_path


def quality_gate(path):
    """返回 (ok, reason, info)。款式封面要求：单人、脸够大、图不是文字稿。"""
    try:
        im = Image.open(path).convert("RGB")
    except Exception as e:
        return False, f"打不开:{e}", {}
    g = im.convert("L")
    px = list(g.resize((64, 64)).getdata())
    mean = sum(px) / len(px)
    var = sum((p - mean) ** 2 for p in px) / len(px)
    if var < 260:                       # 大片纯色/白底文字图
        return False, "对比度太低(像文字稿/白底图)", {}
    faces = facedet.detect(im)
    if not faces:
        return False, "没检出人脸", {}
    if len(faces) > 1:
        return False, f"多人合影({len(faces)}人)", {}
    x, y, w, h, score = faces[0]
    ratio = w / im.width
    if ratio < 0.12:
        return False, f"人脸太小({ratio:.0%})", {}
    if score < 0.6:
        return False, f"人脸置信度低({score:.2f})", {}
    return True, "", {"face_ratio": round(ratio, 3), "score": round(score, 3),
                      "size": im.size, "contrast": round(var)}


def guess_color(text):
    for c in COLORS:
        if c in text:
            return c
    return "自然黑"


def build_prompt(tags, color):
    tag = next((t for t in ("短发", "中长", "长发", "卷发", "扎发", "男士") if t in tags), "")
    zh, en = TPL.get(tag, ("{color}发型，发丝质感真实，自然光泽",
                           "{en} hairstyle, realistic strand texture, natural shine"))
    return zh.format(color=color), en.format(en=EN.get(color, "natural black"))


def name_from_title(title, tags, color, n):
    title = re.sub(r"[｜|].*$", "", title).strip()
    m = re.search(r"([\u4e00-\u9fa5]{2,8}(?:系列|款|版|发型))", title)
    if m:
        return m.group(1)[:14]
    base = next((t for t in ("短发", "中长", "长发", "卷发", "扎发", "男士") if t in tags), "发型")
    return f"{base}·{color}#{n:02d}"


def load_existing():
    if os.path.exists(STYLES_JSON):
        return json.load(open(STYLES_JSON, encoding="utf-8"))
    return []


def add(items, entry, report, seen_sha):
    sha = entry.pop("_sha")
    if sha in seen_sha:
        report["skipped"].append({"src": entry.get("source"), "reason": "重复图"})
        return
    seen_sha.add(sha)
    items.append(entry)
    report["added"].append({"id": entry["id"], "name": entry["name"], "tags": entry["tags"]})


def refine_tags(title, body_tags):
    """标题里出现什么就算什么；标题没有才退回正文标签，且最多留 2 个主类目。
    原因：新闻正文常同时提到短发/长发/护理，直接全收会让每款标签都一样、筛选失效。
    """
    from crawl_site import VOCAB as V   # 复用词表
    title = title or ""
    hit = [k for k, words in V.items() if any(w in title for w in words)]
    if not hit:
        hit = [t for t in body_tags if t in ("短发", "中长", "长发", "卷发", "扎发", "男士")]
    primary = [t for t in hit if t != "发色"][:2]
    return primary or ["假发"]


def from_catalog(items, report, seen_sha, keep_junk=False):
    cat_path = os.path.join(DATA, "catalog.json")
    if not os.path.exists(cat_path):
        print("先跑 tools/crawl_site.py")
        return
    cat = json.load(open(cat_path, encoding="utf-8"))
    n = len(items)
    for it in cat:
        p = os.path.join(COVERS, it["file"])
        ok, why, info = quality_gate(p)
        if not ok:
            report["rejected"].append({"src": p, "reason": why})
            continue
        text = it.get("text", "") + it.get("title", "")
        color = (it.get("colors") or [guess_color(text)])[0]
        tags = refine_tags(it.get("title", ""), it.get("tags") or [])
        sha = hashlib.sha1(open(p, "rb").read()).hexdigest()
        sid = "c" + sha[:10]
        dst = os.path.join(COVERS, sid + ".jpg")
        if not os.path.exists(dst):
            norm_cover(p, dst)
        zh, en = build_prompt(tags, color)
        add(items, {
            "id": sid, "name": name_from_title(it.get("title", ""), tags, color, len(items) + 1),
            "name_en": f"{color} {' '.join(tags)} hair", "tags": tags,
            "prompt_zh": zh, "prompt_en": en, "cover": sid + ".jpg",
            "source": it.get("page", ""), "_sha": sha,
        }, report, seen_sha)
    print(f"[catalog] {len(cat)} 张 → 通过 {len(items)-n} 张")


def from_dir(items, report, seen_sha, folder, tags, color=None):
    n = len(items)
    for fn in sorted(os.listdir(folder)):
        if not fn.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        p = os.path.join(folder, fn)
        ok, why, info = quality_gate(p)
        if not ok and not tags:
            report["rejected"].append({"src": p, "reason": why})
            continue
        c = color or guess_color(fn)
        sha = hashlib.sha1(open(p, "rb").read()).hexdigest()
        sid = "d" + sha[:10]
        dst = os.path.join(COVERS, sid + ".jpg")
        if not os.path.exists(dst):
            norm_cover(p, dst)
        zh, en = build_prompt(tags, c)
        add(items, {
            "id": sid, "name": os.path.splitext(fn)[0][:20], "name_en": " ".join(tags) + " hair",
            "tags": tags, "prompt_zh": zh, "prompt_en": en, "cover": sid + ".jpg",
            "source": p, "_sha": sha,
        }, report, seen_sha)
    print(f"[dir] {folder} → 通过 {len(items)-n} 张")


def from_csv(items, report, seen_sha, path):
    n = len(items)
    for row in csv.DictReader(open(path, encoding="utf-8-sig")):
        p = row.get("cover", "")
        if not os.path.exists(p):
            report["rejected"].append({"src": p, "reason": "文件不存在"})
            continue
        tags = [t for t in re.split(r"[,，/]", row.get("tags", "")) if t]
        c = row.get("color") or guess_color(row.get("name", ""))
        sha = hashlib.sha1(open(p, "rb").read()).hexdigest()
        sid = "s_" + sha[:8]
        dst = os.path.join(COVERS, sid + ".jpg")
        if not os.path.exists(dst):
            norm_cover(p, dst)
        zh, en = build_prompt(tags, c)
        zh = row.get("prompt_zh") or zh
        en = row.get("prompt_en") or en
        add(items, {
            "id": sid, "name": row.get("name") or sid, "name_en": " ".join(tags) + " hair",
            "tags": tags, "prompt_zh": zh, "prompt_en": en, "cover": sid + ".jpg",
            "source": p, "_sha": sha,
        }, report, seen_sha)
    print(f"[csv] {path} → 通过 {len(items)-n} 条")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", action="store_true")
    ap.add_argument("--dir")
    ap.add_argument("--csv")
    ap.add_argument("--tags", default="", help="逗号分隔，例如 短发,卷发")
    ap.add_argument("--color")
    a = ap.parse_args()

    os.makedirs(COVERS, exist_ok=True)
    items = load_existing()
    seen_sha = {hashlib.sha1(open(os.path.join(COVERS, x["cover"]), "rb").read()).hexdigest()
                for x in items if x.get("cover") and os.path.exists(os.path.join(COVERS, x["cover"]))}
    report = {"added": [], "skipped": [], "rejected": []}

    if a.catalog:
        from_catalog(items, report, seen_sha)
    if a.dir:
        tags = [t.strip() for t in re.split(r"[,，]", a.tags) if t.strip()]
        from_dir(items, report, seen_sha, a.dir, tags, a.color)
    if a.csv:
        from_csv(items, report, seen_sha, a.csv)

    json.dump(items, open(STYLES_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(report, open(os.path.join(DATA, "import_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n[=] 发型库现在 {len(items)} 条（新增 {len(report['added'])}）")
    print(f"[=] 拒绝 {len(report['rejected'])} 张，原因分布：")
    from collections import Counter
    for k, v in Counter(r["reason"].split("(")[0] for r in report["rejected"]).most_common():
        print(f"      {k}: {v}")
    print(f"[=] {STYLES_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
