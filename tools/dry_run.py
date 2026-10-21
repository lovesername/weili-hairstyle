# -*- coding: utf-8 -*-
"""干跑：把「这一次会发给模型的东西」原样打出来，**一次 API 都不调**。

为什么要有：出图是按张计费的，出了问题不能靠反复重跑试。这个脚本把
  · 图1/图2 到底是哪两张图（顺序错了整张就"不对版"）
  · 实际拼出来的 prompt 全文
  · 参考图分辨率、人物图分辨率
全部落盘 + 打印，肉眼就能判断对不对。

用法：
    python tools/dry_run.py <photo_id> <style_id>
    python tools/dry_run.py <photo_id> <style_id> --gen storage/debug/xxx_raw.jpg
        # 额外用这张已有的出图走一遍回贴，检查几何/接缝（不联网）
    python tools/dry_run.py <photo_id> <style_id> --color 酒红
        # 检查换发色那一段有没有真的拼进 prompt（同样不联网）
"""
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from PIL import Image  # noqa: E402

import app  # noqa: E402
import db  # noqa: E402
import prompts  # noqa: E402
import styles as stylelib  # noqa: E402
from pipeline import preprocess  # noqa: E402

OUT = os.path.join(ROOT, "storage", "debug", "dryrun")


def md5(b):
    return hashlib.md5(b).hexdigest()[:10]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    gen_path = ""
    color = ""
    if "--color" in sys.argv:
        color = sys.argv[sys.argv.index("--color") + 1]
        args = [a for a in args if a != color]
    if "--gen" in sys.argv:
        gen_path = sys.argv[sys.argv.index("--gen") + 1]
        args = [a for a in args if a != gen_path]
    if len(args) < 2:
        print("用法: python tools/dry_run.py <photo_id> <style_id> [--gen 出图.jpg]")
        return 1

    photo_id, style_id = args[0], args[1]
    photo = db.get_photo(photo_id)
    style = stylelib.BY_ID[style_id]
    os.makedirs(OUT, exist_ok=True)

    # ---- 人物图（和 _run_job 完全同一条路）----
    p = preprocess.prepare(open(photo["path"], "rb").read(), size=app.SIZE)
    person_b = preprocess.to_bytes(p["pad_img"], "PNG")
    open(os.path.join(OUT, "2_person.png"), "wb").write(person_b)

    # ---- 参考图 ----
    ref_b, ref_path = app._edit_ref_bytes(style)
    if ref_b:
        open(os.path.join(OUT, "1_ref.jpg"), "wb").write(ref_b)

    prompt = prompts.build_edit(style, "zh", has_ref=bool(ref_b), color=color)

    print("=" * 70)
    print("款式      :", style.get("name"), "|", style.get("id"))
    print("换发色    :", color or "跟随款式原色（%s）" % (style.get("color") or "-"))
    print("provider  :", app.default_provider(), "->", app.PROVIDERS[app.default_provider()].model
          if hasattr(app.PROVIDERS[app.default_provider()], "model") else app.default_provider())
    print("图1 参考  :", ref_path or "(无)", md5(ref_b) if ref_b else "")
    if ref_b:
        print("            ", Image.open(os.path.join(OUT, "1_ref.jpg")).size)
    print("图2 人物  :", os.path.basename(photo["path"]), md5(person_b), p["pad_img"].size,
          " pad=", p["pad"])
    print("-" * 70)
    print("PROMPT:")
    print(prompt)
    print("-" * 70)

    # ---- 送图顺序自检：解码 provider 实际塞进去的两个 data URL ----
    prov = app.PROVIDERS[app.default_provider()]
    if hasattr(prov, "_payload"):
        body = prov._payload(person_b, prompt, ref_b, 1)
        urls = body.get("image_urls") or []
        import base64
        got = []
        for u in urls:
            raw = base64.b64decode(u.split(",", 1)[-1])
            got.append(md5(raw))
        ok = (len(got) == 2 and got[0] == md5(ref_b) and got[1] == md5(person_b))
        print("送图顺序  : image_urls[0]=%s image_urls[1]=%s -> %s"
              % (got[0] if got else "-", got[1] if len(got) > 1 else "-",
                 "OK（图1=参考，图2=本人）" if ok else "错！参考图不在第 1 位"))
        print("payload   : model=%s size=%s resolution=%s n=%s 图数=%d"
              % (body.get("model"), body.get("size"), body.get("resolution"),
                 body.get("n"), len(urls)))
    print("=" * 70)
    print("产物目录  :", OUT)

    # ---- 可选：用已有出图走一遍回贴（不联网）----
    if gen_path and os.path.exists(gen_path):
        from pipeline import compose
        gen = Image.open(gen_path).convert("RGB")
        W, H = p["orig"].size
        out = compose.composite_block(p["orig"], gen, (0, 0, W, H),
                                      pad=p["pad"], side=p["side"])
        dst = os.path.join(OUT, "3_composite.jpg")
        out.save(dst, quality=94)
        print("回贴预览  :", dst, out.size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
