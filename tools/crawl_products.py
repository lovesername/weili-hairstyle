# -*- coding: utf-8 -*-
"""抓发知发官网「产品中心」的真实假发数据。

站点结构（2026-10 实测）：
  cp.php                                  -> 内联 const categories = {...}（11 个分类，带 count）
  api/products/get_products.php?category_id=N -> {"success":true,"data":[{...}]}
  api/products/fallback_products.php?category_id=N  -> 主接口失败时的兜底
  product_detail.php?id=N                 -> 详情页（多图）

用法：
  python tools/crawl_products.py                # 全量（分类 -> 产品 -> 缩略图）
  python tools/crawl_products.py --no-images    # 只要 JSON
  python tools/crawl_products.py --detail 20    # 对 image_count>1 的产品再抓详情图
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

BASE = "https://www.bbkjfwg.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
IMGDIR = os.path.join(DATA, "products")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def get(url, referer=None, timeout=25, binary=False):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Referer": referer or BASE + "/cp.php",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return raw if binary else raw.decode("utf-8", "replace")


def parse_categories(html):
    """从 cp.php 内联脚本里取出分类表。"""
    m = re.search(r"const\s+categories\s*=\s*(\{.*?\});\s*\n", html, re.S)
    if not m:
        m = re.search(r"const\s+categories\s*=\s*(\{.*?\});", html, re.S)
    if not m:
        raise SystemExit("没找到 categories，页面结构可能变了")
    return json.loads(m.group(1))


def fetch_products(cid):
    """先打主接口，失败走 fallback。返回 (list, 用的接口)。"""
    for path in ("api/products/get_products.php",
                 "api/products/fallback_products.php"):
        url = f"{BASE}/{path}?category_id={cid}"
        try:
            d = json.loads(get(url))
        except Exception as e:
            print(f"    ! {path} 异常 {type(e).__name__}")
            continue
        if d.get("success") and isinstance(d.get("data"), list):
            return d["data"], path
        print(f"    ! {path} -> {d.get('message')}")
    return [], None


def download(rel_url, out_dir, referer):
    """rel_url 形如 uploads/products/images/S01/S0101.jpg。"""
    if not rel_url:
        return None
    rel_url = rel_url.lstrip("/")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", rel_url.replace("/", "__"))
    dst = os.path.join(out_dir, name)
    if os.path.exists(dst) and os.path.getsize(dst) > 1024:
        return name
    url = rel_url if rel_url.startswith("http") else f"{BASE}/{rel_url}"
    try:
        blob = get(url, referer=referer, binary=True)
    except Exception as e:
        print(f"    ! 图片失败 {rel_url} {type(e).__name__}")
        return None
    if len(blob) < 512:
        return None
    os.makedirs(out_dir, exist_ok=True)
    with open(dst, "wb") as f:
        f.write(blob)
    return name


def detail_images(pid):
    """详情页里所有 uploads 图。"""
    try:
        html = get(f"{BASE}/product_detail.php?id={pid}")
    except Exception:
        return []
    imgs = re.findall(r'(?:src|data-src|data-original)="([^"]*uploads/products[^"]*)"', html)
    out, seen = [], set()
    for i in imgs:
        i = i.strip()
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--detail", type=int, default=0,
                    help="image_count>=N 的产品额外抓详情页图片，0 表示不抓")
    ap.add_argument("--sleep", type=float, default=0.35)
    a = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)
    html = get(BASE + "/cp.php")
    cats = parse_categories(html)
    flat = [(g, c) for g, items in cats.items() for c in items]
    expect = sum(int(c.get("count") or 0) for _, c in flat)
    print(f"[+] 分类 {len(flat)} 个（{'+'.join(cats.keys())}），页面声明产品数 {expect}")

    all_items, report = [], {"categories": [], "api": {}, "missing_images": [], "detail": 0}
    for gender, c in flat:
        cid = int(c["id"])
        print(f"[>] {gender} / {c['name']} (id={cid}, 声明 {c.get('count')})")
        items, used = fetch_products(cid)
        report["api"][str(cid)] = used
        print(f"    拿到 {len(items)}")
        for it in items:
            it["gender"] = gender
            it["category_code"] = c.get("code")
            it["series_code"] = c.get("series_code")
            if not a.no_images:
                it["cover_file"] = download(it.get("thumbnail_image"), IMGDIR,
                                            f"{BASE}/cp.php")
                if not it["cover_file"]:
                    report["missing_images"].append(it.get("product_code") or it.get("id"))
            all_items.append(it)
            time.sleep(a.sleep)
        report["categories"].append({"id": cid, "gender": gender, "name": c["name"],
                                     "code": c.get("code"), "declared": c.get("count"),
                                     "got": len(items)})
        time.sleep(a.sleep)

    if a.detail:
        todo = [it for it in all_items if int(it.get("image_count") or 0) >= a.detail]
        print(f"[>] 详情页补图 {len(todo)} 个")
        for it in todo:
            imgs = detail_images(it["id"])
            it["detail_images"] = imgs
            if not a.no_images:
                it["detail_files"] = [f for f in
                                      (download(u, IMGDIR, f"{BASE}/product_detail.php?id={it['id']}")
                                       for u in imgs) if f]
            report["detail"] += 1
            time.sleep(a.sleep)

    json.dump(cats, open(os.path.join(DATA, "categories.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(all_items, open(os.path.join(DATA, "products.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    report["total"] = len(all_items)
    report["with_cover"] = sum(1 for x in all_items if x.get("cover_file"))
    json.dump(report, open(os.path.join(DATA, "products_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    print(f"\n[=] 产品 {report['total']} 条（声明 {expect}），有封面 {report['with_cover']}")
    from collections import Counter
    for k, v in Counter(x["category_name"] for x in all_items).most_common():
        print(f"    {v:>3}  {k}")
    print("[=] 写入 data/products.json / categories.json / products_report.json")


if __name__ == "__main__":
    main()
