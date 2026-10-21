# -*- coding: utf-8 -*-
"""把 data/products.json（官网产品中心的真实假发数据）转成发型库条目。

关键点：产品名本身信息量很低（"女士直发01款"），真正决定出图效果的是
「长度 + 类型 + 发色 + 网底/佩戴方式」这三层语义，所以这里做一次映射：
  category_name  ->  长度语义（15cm=齐耳下颌 / 20cm=锁骨 / 30,35cm=及胸）
                  ->  类型语义（直发/卷发/纹理/锡纸烫/发片/全头套/混发）
  colors 字段     ->  该款可选色号（每款都是自然色·深棕·浅棕·酒红，属于变体不是款式）
再套模板生成 prompt_zh / prompt_en，写进 data/styles.json。

用法：
  python tools/products_to_styles.py            # 追加（按产品编码去重）
  python tools/products_to_styles.py --reset    # 先清掉旧的 p* 条目再导
  python tools/products_to_styles.py --color 深棕   # 统一出图色号（默认 深棕）
"""
import argparse
import json
import os
import re
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

DATA = os.path.join(ROOT, "data")
COVERS = os.path.join(DATA, "covers")
PRODUCTS = os.path.join(DATA, "products.json")
STYLES_JSON = os.path.join(DATA, "styles.json")

# ---- 长度语义：15cm 是发丝长度，落位大致是齐耳~下颌 ----
LEN_ZH = {
    "15": ("短发", "齐耳到下颌长度", "ear-to-jaw length"),
    "20": ("中长", "锁骨长度", "collarbone length"),
    "30": ("长发", "及胸长度", "chest length"),
}

# ---- 类型语义：(中文描述, 英文描述, 额外标签) ----
TYPE_ZH = {
    "直发": ("顺直无卷度，发尾自然垂顺，发丝有光泽", "straight with no curl, natural fall, glossy", ["直发"]),
    "卷发": ("松散卷度，卷型饱满不僵硬，发量蓬松", "loose full curls, natural bounce, voluminous", ["卷发"]),
    "纹理": ("表面有层次纹理，发丝分明不贴头皮", "layered texture on the surface, defined strands", ["纹理"]),
    "锡纸烫": ("细密锡纸烫小卷，发根蓬松立体", "fine tin-foil perm curls, lifted voluminous roots", ["锡纸烫"]),
    "发片": ("局部补发片，轻薄网底无痕贴合发缝", "hair topper patch, thin lace base, seamless on the part", ["发片"]),
    "全头套": ("整顶假发，全头包裹，发际线自然过渡", "full wig cap, whole-head coverage, natural hairline", ["全头套"]),
    "混发": ("真人发与纤维混纺，质感接近原生发", "human-synthetic blend, close to natural hair", ["混发"]),
}

EN_COLOR = {"自然色": "natural black", "自然黑": "natural black", "深棕": "dark brown",
            "浅棕": "light brown", "酒红": "burgundy"}

# ---- 荷风十二系列：官网 cp.php 拿不到款名，只在品牌动态文章里，单独存一文 ----
SERIES_ARTICLE = {
    "晴丝": "简约平直短发，线条干净规整，发丝疏密适中",
    "松絮": "蓬松层次碎短发，顶部纹理错开，发根不塌",
    "蝉尾": "个性狼尾短发，前短后长，额前短碎发",
    "芒刺": "立体免打理毛刺短发，发根挺立定型",
    "榴卷": "蓬松羊毛细卷短发，细密柔和小卷",
    "疏影": "错落层次纹理短发，长短错碎剪",
    "凝丝": "黑科技记忆丝齐耳短发，可揉可折",
    "圆云": "柔和圆弧免烫短发，头顶圆润蓬松",
    "敛云": "高颅收脸通勤短发，两侧贴腮骨",
    "平荷": "全真发齐刘海补发片，无痕遮发缝",
    "分菱": "偏分颅顶增发补发片，轻薄卡扣",
    "垂檐": "长款遮发际线刘海片，极薄轻盈",
}


def parse_cat(name):
    """'女士直发（15cm）' -> ('女', '直发', '15')"""
    gender = "男" if "男士" in name else "女"
    m = re.search(r"（([\d,\s]+)cm）", name)
    ln = (m.group(1).split(",")[0].strip() if m else "15")
    if ln not in LEN_ZH:
        ln = "15"
    typ = "直发"                      # 未标注类型的（如"女士长发"）按直发处理
    for k in ("锡纸烫", "发片", "全头套", "混发", "纹理", "卷发", "直发"):
        if k in name:
            typ = k
            break
    return gender, typ, ln


# ---- 逐款量出来的指标（tools/analyze_products.py 产出），有则用，没则退品类 ----
METRICS = {}
_mpath = os.path.join(DATA, "products_metrics.json")
if os.path.exists(_mpath):
    for m in json.load(open(_mpath, encoding="utf-8")):
        METRICS[m["product"]] = m

CURL_DESC = {   # 卷度词 -> prompt 片段
    "直发": "顺直无卷度，发尾自然垂顺，发丝有光泽",
    "微卷": "发尾带自然微卷弧度，不刻意不僵硬",
    "卷发": "松散卷度，卷型饱满不僵硬，发量蓬松",
    "小卷": "细密小卷，卷度紧实有弹性",
}
EN_CURL = {"直发": "straight, glossy natural fall", "微卷": "soft natural waves",
           "卷发": "loose full curls", "小卷": "tight small curls"}
TYPE_EXTRA = {
    "直发": "", "卷发": "",
    "纹理": "表面有层次纹理，发丝分明不贴头皮",
    "锡纸烫": "发根蓬松立体",
    "发片": "轻薄网底无痕贴合发缝",
    "全头套": "全头包裹，发际线自然过渡",
    "混发": "真人发与纤维混纺，质感接近原生发",
}
EN_TYPE = {"纹理": "layered texture", "锡纸烫": "lifted roots", "发片": "thin lace topper base",
           "全头套": "full cap coverage, natural hairline", "混发": "human hair blend",
           "直发": "", "卷发": ""}
VOL_DESC = {"贴头": "服帖不蓬炸", "适中": None, "蓬松": "整体蓬松有空气感", "极蓬松": "发量极多，蓬松炸开"}
EN_VOL = {"蓬松": "voluminous and airy", "贴头": "sleek close to head"}

# 发尾实测落点（hair_bottom_ratio：头发最低行相对脸框底端的偏移/脸高）
DROP_ZH = [(-0.15, "耳上短款"), (0.15, "发尾及颌"), (0.60, "发尾及肩"), (9, "发尾过肩落胸")]
EN_DROP = ["short above-ear length", "jaw-grazing length", "shoulder-grazing length", "past-shoulder length"]


def drop_word(r):
    for i, (lim, w) in enumerate(DROP_ZH):
        if r < lim:
            return w, EN_DROP[i]
    return DROP_ZH[-1][1], EN_DROP[-1]


def build(gender, typ, ln, color, metric=None):
    """metric 有值时用图里量到的卷度/发量/发色，没有就用品类语义。
    品类是权威：直发类最多容许"微卷"，卷发类最多少许"微卷"，不许互相矛盾。"""
    lt, lzh, len_ = LEN_ZH[ln]
    if metric:
        curl = metric.get("curl_word") or ("直发" if typ == "直发" else "卷发")
        allow = {"直发": ("直发", "微卷"), "卷发": ("卷发", "小卷", "微卷")}.get(typ)
        if allow and curl not in allow:
            curl = "微卷"                      # 夹回品类容许范围
        cdesc = CURL_DESC.get(curl, CURL_DESC["直发"])
        vol = metric.get("vol_word")
        vdesc = VOL_DESC.get(vol, "")
        vdesc = f"，{vdesc}" if vdesc else ""
        color = metric.get("hair_color", color)
    else:
        curl = "卷发" if typ in ("卷发", "锡纸烫") else ("微卷" if typ == "纹理" else "直发")
        cdesc = CURL_DESC[curl]
        vdesc = ""
    # 名词槽：品类是"形态"词（发片/全头套/混发/锡纸烫/纹理）就保留，是直发/卷发就用卷度词
    noun = typ if typ in ("发片", "全头套", "混发", "锡纸烫", "纹理") else curl
    tex = TYPE_EXTRA.get(typ, "")
    extra = f"，{tex}" if tex else ""
    # 长度措辞：有实测落点且品类是整顶款时用实测，否则用品类标注
    len_en = len_
    if metric and metric.get("hair_bottom_ratio") is not None and typ not in ("发片", "全头套"):
        lw, len_en = drop_word(metric["hair_bottom_ratio"])
        lzh = lw
    tags = [lt, curl] + ([typ] if typ not in (curl, "直发") else []) + [gender]
    seen, uniq = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    zh = (f"{lzh}的{color}{noun}，{cdesc}{vdesc}{extra}，"
          f"发丝质感真实，发根自然，发际线衔接原生发")
    en = (f"{len_en} {EN_COLOR.get(color, 'natural black')} wig, "
          f"{EN_CURL.get(curl, EN_CURL['直发'])}"
          f"{', ' + EN_TYPE[typ] if EN_TYPE.get(typ) else ''}, realistic strand texture")
    return zh, en, uniq, color


def norm_cover(src, dst, size=512):
    im = Image.open(src).convert("RGB")
    w, h = im.size
    side = min(w, h)
    # 产品图是竖构图人像，裁上半身：从顶部 6% 开始取方图，保证头发在画面里
    top = int(h * 0.06)
    side = min(w, h - top)
    box = ((w - side) // 2, top, (w - side) // 2 + side, top + side)
    im = im.crop(box).resize((size, size), Image.LANCZOS)
    im.save(dst, "JPEG", quality=88)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true",
                    help="先删掉所有 p* 开头的条目（产品导入的），保留手工新增的")
    ap.add_argument("--color", default="深棕", choices=list(EN_COLOR))
    a = ap.parse_args()

    if not os.path.exists(PRODUCTS):
        print("先跑 python tools/crawl_products.py")
        return 1
    prods = json.load(open(PRODUCTS, encoding="utf-8"))
    os.makedirs(COVERS, exist_ok=True)
    items = json.load(open(STYLES_JSON, encoding="utf-8")) if os.path.exists(STYLES_JSON) else []

    if a.reset:
        n0 = len(items)
        items = [x for x in items if not x.get("imported_from") == "products"]
        print(f"[reset] 清掉 {n0 - len(items)} 条产品导入的记录")

    have = {x.get("product_code") for x in items if x.get("product_code")}
    added, skip, noimg = [], 0, []

    for i, p in enumerate(prods, 1):
        code = p.get("product_code") or str(p.get("id"))
        if code in have:
            skip += 1
            continue
        src = os.path.join(DATA, "products", p.get("cover_file") or "")
        if not p.get("cover_file") or not os.path.exists(src):
            noimg.append(code)
            continue
        gender, typ, ln = parse_cat(p.get("category_name", ""))
        metric = dict(METRICS.get(code) or {})
        if metric:
            from analyze_products import curl_word, vol_word
            metric["curl_word"] = curl_word(metric["curl"])
            metric["vol_word"] = vol_word(metric["volume"])
            if metric["vol_word"] == "适中":
                metric["vol_word"] = None
        zh, en, tags, color = build(gender, typ, ln, a.color, metric)
        sid = "p" + re.sub(r"\W", "", code.lower())[:12]
        dst = os.path.join(COVERS, sid + ".jpg")
        if not os.path.exists(dst):
            norm_cover(src, dst)
        name = (p.get("name") or code).replace("（", "·").replace("）", "")
        items.append({
            "id": sid,
            "name": name[:20],
            "name_en": f"{gender} {typ} {ln}cm",
            "tags": tags,
            "color": color,
            "colors": [c for c in (p.get("colors") or "").split("、") if c],
            "curl": metric.get("curl_word"),
            "volume": metric.get("vol_word"),
            "curl_coh": metric.get("curl"),
            "prompt_zh": zh,
            "prompt_en": en,
            "cover": sid + ".jpg",
            "product_code": code,
            "style_code": p.get("style_code"),
            "category": p.get("category_name"),
            "source": f"https://www.bbkjfwg.com/product_detail.php?id={p.get('id')}",
            "imported_from": "products",
        })
        added.append({"id": sid, "name": name, "tags": tags})

    json.dump(items, open(STYLES_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump({"series_from_article": SERIES_ARTICLE},
              open(os.path.join(DATA, "series.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    from collections import Counter
    print(f"\n[=] 产品 {len(prods)} 条 → 新增 {len(added)}，已存在跳过 {skip}，缺图 {len(noimg)}")
    for k, v in Counter(tuple(x["tags"]) for x in added).most_common(12):
        print(f"    {v:>3}  {'/'.join(k)}")
    print(f"[=] 发型库现在 {len(items)} 条（其中产品 {sum(1 for x in items if x.get('imported_from')=='products')}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
