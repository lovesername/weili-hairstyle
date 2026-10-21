# -*- coding: utf-8 -*-
"""扎发/盘发本地直跑（不经 HTTP）：一次一款，出对比图。

    python tools/try_updo2.py storage/uploads/xxx.jpg high
    python tools/try_updo2.py storage/uploads/xxx.jpg low

走的就是 backend/app.py::_do_updo，和线上同一条代码路径。
输出 storage/debug/updo2_<kind>.jpg（左原图 / 右结果）
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "backend"))

import app  # noqa: E402
from pipeline import mask as mk  # noqa: E402
from pipeline import preprocess as pp  # noqa: E402

DBG = os.path.join(ROOT, "storage", "debug")


def main(path, kind="high"):
    prov = app.PROVIDERS["comfy"]
    if not prov.available():
        print("ComfyUI 没起来（127.0.0.1:8188）")
        return 2
    p = pp.prepare(open(path, "rb").read(), size=app.SIZE)
    fb = mk.face_box_in_crop(p["face"], p["rect"], app.SIZE)
    m_hair = mk.hair_mask(p["img"], fb)
    outs, gen = app._do_updo(prov, p, fb, m_hair, kind, f"try2_{kind}")
    out = outs[0]
    out.save(os.path.join(DBG, f"updo2_{kind}.jpg"), quality=94)
    side = app.compose.side_by_side(p["orig"], out)
    side.save(os.path.join(DBG, f"updo2_{kind}_cmp.jpg"), quality=93)
    print("done:", os.path.join(DBG, f"updo2_{kind}_cmp.jpg"),
          "changed%%=%.1f" % _chg(p["orig"], out))
    return 0


def _chg(a, b):
    import numpy as np
    from PIL import Image
    x = np.asarray(a.convert("RGB").resize((512, 512))).astype(int)
    y = np.asarray(b.convert("RGB").resize((512, 512))).astype(int)
    return float((np.abs(x - y).max(2) > 8).mean() * 100)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "high"))
