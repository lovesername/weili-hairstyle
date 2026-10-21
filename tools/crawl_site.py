# -*- coding: utf-8 -*-
"""站点素材爬取器：sitemap → 栏目页 → 图片 + 文本关键词，产出可入库的 catalog。

    python tools/crawl_site.py --host https://www.bbkjfwg.com --max-pages 60

产物：
    data/catalog.json      [{id,title,page,image,local,sha1,size,tags,text}]
    data/covers/*.jpg      去重后的图片（按 sha1 命名）
    data/vocab.json       从正文抽出的发型/发色/长度词频（用于给新图自动打标）

礼貌爬：0.4s 间隔、单线程、跳过 robots 里 Disallow 的 /api/ /admin/ /core/。
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.parse as up

import httpx
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
COVERS = os.path.join(DATA, "covers")
SKIP = ("/api/", "/admin/", "/core/", "/logs/", "/uploads/temp/")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

# 发型/发色/长度词表：命中即打标
VOCAB = {
    "短发": ["短发", "波波", "bob", "齐耳", "精灵", "碎发", "寸头"],
    "中长": ["锁骨", "齐肩", "中长", "lob"],
    "长发": ["长发", "及腰", "长直", "长卷"],
    "卷发": ["卷发", "波浪", "卷度", "烫", "羊毛卷", "蛋卷", "螺旋"],
    "扎发": ["马尾", "丸子头", "盘发", "编发", "麻花"],
    "发色": ["发色", "染", "棕色", "亚麻", "奶茶", "酒红", "灰棕", "黑茶", "挑染", "渐层"],
    "男士": ["男士", "男款", "商务男"],
    "假发": ["假发", "发片", "发套", "补发", "头顶", "全头"],
    "护理": ["护理", "洗护", "打理", "保养", "头皮"],
}

COLOR_WORDS = ["自然黑", "深棕", "栗棕", "浅棕", "亚麻", "红棕", "奶茶", "灰棕", "酒红", "黑茶", "挑染"]


def clean(url: str, base: str) -> str:
    return up.urljoin(base, url)


def fetch(c, url, timeout=20):
    for _ in range(2):
        try:
            r = c.get(url, timeout=timeout, follow_redirects=True)
            if r.status_code == 200:
                return r
        except Exception:
            time.sleep(0.8)
    return None


def text_of(html: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"&nbsp;|&amp;|&quot;", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_of(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def tags_of(text: str):
    hit = [k for k, words in VOCAB.items() if any(w.lower() in text.lower() for w in words)]
    colors = [c for c in COLOR_WORDS if c in text]
    return hit, colors


def images_of(html: str, base: str):
    urls = re.findall(r'(?:src|data-src|data-original)="([^"]+\.(?:jpg|jpeg|png|webp))"', html, re.I)
    out = []
    for u in urls:
        u = clean(u, base)
        if "logo" in u or "icon" in u or "favicon" in u or "avatar" in u:
            continue
        out.append(u)
    return list(dict.fromkeys(out))


def save_image(c, url, title, tags):
    r = fetch(c, url, 30)
    if not r:
        return None
    raw = r.content
    if len(raw) < 12 * 1024:          # 太小的图直接丢（多为装饰）
        return None
    sha = hashlib.sha1(raw).hexdigest()
    dst = os.path.join(COVERS, sha[:16] + ".jpg")
    if not os.path.exists(dst):
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception:
            return None
        w, h = im.size
        if min(w, h) < 380:            # 分辨率不够当封面
            return None
        # 统一成 512 正方形居中裁剪，正好能当款式封面
        side = min(w, h)
        im = im.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        im = im.resize((512, 512), Image.LANCZOS)
        im.save(dst, "JPEG", quality=88)
    return {"sha1": sha, "file": os.path.basename(dst), "size": [Image.open(dst).width, 512]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="https://www.bbkjfwg.com")
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--sleep", type=float, default=0.4)
    ap.add_argument("--out", default=DATA)
    a = ap.parse_args()
    os.makedirs(COVERS, exist_ok=True)

    catalog, seen_img, vocab = [], set(), {}
    with httpx.Client(headers={"User-Agent": UA}, timeout=20) as c:
        sm = fetch(c, a.host.rstrip("/") + "/sitemap.xml")
        pages = []
        if sm:
            pages = re.findall(r"<loc>([^<]+)</loc>", sm.text)
            print(f"[+] sitemap {len(pages)} 个 URL")
        if not pages:
            pages = [a.host + "/"]

        pages = [p for p in pages if not any(s in p for s in SKIP)][: a.max_pages]
        for i, page in enumerate(pages, 1):
            r = fetch(c, page)
            if not r:
                print(f"  !! {page} 取不到")
                continue
            text = text_of(r.text)
            title = title_of(r.text) or page
            tags, colors = tags_of(text)
            for k, words in VOCAB.items():
                for w in words:
                    if w.lower() in text.lower():
                        vocab[w] = vocab.get(w, 0) + 1

            for img in images_of(r.text, page)[:12]:
                if img in seen_img:
                    continue
                seen_img.add(img)
                meta = save_image(c, img, title, tags)
                if not meta:
                    continue
                catalog.append({
                    "id": meta["sha1"][:12],
                    "title": title[:80],
                    "page": page,
                    "image": img,
                    "file": meta["file"],
                    "tags": tags,
                    "colors": colors,
                    "text": text[:600],
                })
                print(f"  [{i}/{len(pages)}] + {meta['file']}  {title[:40]}")
            time.sleep(a.sleep)

    os.makedirs(a.out, exist_ok=True)
    json.dump(catalog, open(os.path.join(a.out, "catalog.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(dict(sorted(vocab.items(), key=lambda x: -x[1])),
              open(os.path.join(a.out, "vocab.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    print(f"\n[=] 页面 {len(pages)} 张，图片落盘 {len(catalog)} 张，去重后唯一 URL {len(seen_img)}")
    print(f"[=] catalog -> {os.path.join(a.out,'catalog.json')}")
    print(f"[=] 词频 top10 -> {list(sorted(vocab.items(), key=lambda x: -x[1])[:10])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
