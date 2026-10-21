# -*- coding: utf-8 -*-
"""把每款发型的**实拍图**（正面 / 侧面 / 背面）从来源站扒下来。

来源站的产品详情页里，一款产品的相册是这样的：

    uploads/products/images/S01/S0101.jpg        主图
    uploads/products/images/S01/S0101_front.jpg  正面
    uploads/products/images/S01/S0101_side.jpg   侧面
    uploads/products/images/S01/S0101_back.jpg   背面

以前只下了封面（S0101.jpg），所以库里每款只有一张。这里按产品编号把整组都下来，
存到 data/gallery/<款式id>/（main.jpg / front.jpg / side.jpg / back.jpg），
并把清单写回 data/styles.json 的 images 字段，首页点「看图」就能逐张预览。

    python tools/crawl_gallery.py            # 全量（已下过的跳过）
    python tools/crawl_gallery.py --limit 5  # 先试 5 款
    python tools/crawl_gallery.py --force    # 连页面缓存一起重来
"""
import io
import json
import os
import re
import sys
import time

import httpx
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
GAL = os.path.join(DATA, "gallery")
HTML_CACHE = os.path.join(DATA, "gallery_html")
STYLES_JSON = os.path.join(DATA, "styles.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
IMG_RE = re.compile(r"uploads/products/images/[^\"'\s>]+?\.(?:jpg|jpeg|png|webp)", re.I)
LABELS = {"front": "正面", "side": "侧面", "back": "背面", "": "主图"}


def client():
    # 这台机器 HTTP(S)_PROXY 指向内部代理会 502，直连才通
    return httpx.Client(timeout=30, follow_redirects=True, trust_env=False,
                        headers={"User-Agent": UA, "Referer": "https://www.bbkjfwg.com/"})


def fetch_html(c, sid, url, force=False):
    cache = os.path.join(HTML_CACHE, sid + ".html")
    if os.path.exists(cache) and not force:
        return open(cache, encoding="utf-8", errors="ignore").read()
    r = c.get(url)
    r.raise_for_status()
    html = r.text
    os.makedirs(HTML_CACHE, exist_ok=True)
    open(cache, "w", encoding="utf-8").write(html)
    return html


def pick_images(html, code):
    """只留属于这一款产品的图（别把同页的"相关产品"缩略图也下下来）。"""
    code = code.upper()
    pat = re.compile(r"^%s(?:[_-]([a-z0-9]+))?\.(jpg|jpeg|png|webp)$" % re.escape(code), re.I)
    out, seen = [], set()
    for path in IMG_RE.findall(html):
        base = path.rsplit("/", 1)[-1]
        m = pat.match(base)
        if not m:
            continue
        if base.lower() in seen:
            continue
        seen.add(base.lower())
        out.append((path, (m.group(1) or "").lower()))
    # 顺序：主图 → 正面 → 侧面 → 背面 → 其它
    order = {"": 0, "front": 1, "side": 2, "back": 3}
    out.sort(key=lambda x: (order.get(x[1], 9), x[1]))
    return out


def save_img(raw, dst, max_side=1200):
    im = Image.open(io.BytesIO(raw))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    if max(im.size) > max_side:
        k = max_side / float(max(im.size))
        im = im.resize((int(im.width * k), int(im.height * k)), Image.LANCZOS)
    im.save(dst, "JPEG", quality=88, optimize=True)
    return im.size


def main():
    limit = 0
    force = "--force" in sys.argv
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    styles = json.load(open(STYLES_JSON, encoding="utf-8"))
    targets = [s for s in styles if s.get("source") and s.get("product_code")]
    if limit:
        targets = targets[:limit]
    print("要处理 %d 款" % len(targets))

    os.makedirs(GAL, exist_ok=True)
    ok = skip = fail = 0
    total_imgs = 0
    with client() as c:
        for i, s in enumerate(targets, 1):
            sid, code = s["id"], s["product_code"]
            folder = os.path.join(GAL, sid)
            try:
                html = fetch_html(c, sid, s["source"], force=force)
                pairs = pick_images(html, code)
                if not pairs:
                    print("[%d/%d] %s %s 详情页没有图" % (i, len(targets), sid, code))
                    fail += 1
                    continue
                os.makedirs(folder, exist_ok=True)
                images, used = [], set()
                for path, suffix in pairs:
                    name = (suffix or "main") + ".jpg"
                    if name in used:                      # 同后缀只留第一张
                        continue
                    used.add(name)
                    dst = os.path.join(folder, name)
                    if not (os.path.exists(dst) and not force):
                        r = c.get("https://www.bbkjfwg.com/" + path)
                        r.raise_for_status()
                        save_img(r.content, dst)
                        time.sleep(0.12)
                    images.append({"label": LABELS.get(suffix, suffix or "其它"),
                                   "file": name, "src": path})
                s["images"] = images
                total_imgs += len(images)
                ok += 1
                print("[%d/%d] %s %s -> %d 张 %s" % (
                    i, len(targets), sid, code, len(images),
                    " ".join(x["label"] for x in images)))
            except Exception as e:
                print("[%d/%d] %s 失败: %s" % (i, len(targets), sid, str(e)[:120]))
                fail += 1
            time.sleep(0.25)

    json.dump(styles, open(STYLES_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    rep = {"ok": ok, "fail": fail, "images": total_imgs,
           "avg": round(total_imgs / float(ok or 1), 2), "at": time.strftime("%F %T")}
    json.dump(rep, open(os.path.join(DATA, "gallery_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("完成:", rep)


if __name__ == "__main__":
    main()
