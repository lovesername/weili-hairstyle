# -*- coding: utf-8 -*-
"""扎发剪影校准：把剪影/抹除区画到真实裁剪图上，肉眼确认覆盖范围对不对。

    python tools/calib_sil.py storage/uploads/xxx_crop.jpg
出 storage/debug/calib_*.jpg
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import numpy as np
from PIL import Image, ImageDraw

from pipeline import compose as cp
from pipeline import mask as mk
from pipeline import preprocess as pp
from pipeline import updo as up

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DBG = os.path.join(ROOT, "storage", "debug")


def main(path):
    data = open(path, "rb").read()
    p = pp.prepare(data, size=768)
    im = p["img"]
    fb = mk.face_box_in_crop(p["face"], p["rect"], 768)
    print("face_box_in_crop:", [round(x, 1) for x in fb] if fb else None)
    hair = mk.hair_mask(im, fb)
    print("hair_top:", up.hair_top(hair), " pad:", up.pad_needed(fb, "high", 768))

    for kind in ("high", "low"):
        r = up.prepare(im, fb, hair, kind)
        canvas = r["canvas"]
        fb2 = r["fb"]
        vis = canvas.copy()
        d = ImageDraw.Draw(vis)
        d.rectangle([fb2[0], fb2[1], fb2[0] + fb2[2], fb2[1] + fb2[3]], outline=(0, 255, 0), width=2)
        vis.paste(Image.new("RGB", vis.size, (255, 40, 0)), (0, 0),
                  Image.fromarray(((np.asarray(r["tgt"]) > 128) * 95).astype(np.uint8)))
        vis.save(os.path.join(DBG, f"calib_{kind}_tgt.jpg"), quality=92)
        vis2 = canvas.copy()
        vis2.paste(Image.new("RGB", vis2.size, (0, 80, 255)), (0, 0),
                   Image.fromarray(((np.asarray(r["kill"]) > 128) * 95).astype(np.uint8)))
        vis2.save(os.path.join(DBG, f"calib_{kind}_kill.jpg"), quality=92)
        cp.erase_region(canvas, r["kill"]).save(os.path.join(DBG, f"calib_{kind}_erased.jpg"),
                                               quality=92)
    print("saved:", DBG)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "test_face.jpg"))
