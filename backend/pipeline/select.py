# -*- coding: utf-8 -*-
"""多候选择优：生成 N 张，自动挑"最像本人、最不歪"的那张。

这是"看起来很精准"的关键工程手段，不是模型本身的功劳：
一次性生成 4 张，用三个客观指标打分，只把分数最高的给用户看。

三个指标：
  keep   脸部像素保护度 —— 除头发 mask 外，人脸框内的像素改动量。
         换发只该动头发，脸区任何变化都是崩（这条最狠，也最能拦住"换头感"）
  sim    SFace 余弦相似度 —— 生成图人脸 vs 原图人脸（身份有没有漂到别人）
  hair   发际线连续性 —— mask 上边界逐列二阶差分，锯齿大说明发际线糊了

total = 0.44*keep + 0.40*sim + 0.16*hair
keep 或 sim 低于阈值直接判废，保证不会把崩脸的那张递出去。

用法：
  from pipeline import select
  idx, scores = select.best(ref_img, [c0, c1, c2, c3], mask)
"""
import os

import numpy as np
from PIL import Image

from . import face as facedet

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SFACE_PATH = os.path.join(_ROOT, "models", "face_recognition_sface_2021dec.onnx")

_rec = None
_rec_failed = False

FACE_MIN = 0.34          # 相似度低于此值视为崩脸
KEEP_MIN = 0.55          # 脸区保护度低于此值视为把人脸改了
W = {"keep": 0.44, "sim": 0.40, "hair": 0.16}


def _recognizer():
    global _rec, _rec_failed
    if _rec is not None or _rec_failed:
        return _rec
    try:
        import cv2
        if not os.path.exists(_SFACE_PATH):
            raise FileNotFoundError(_SFACE_PATH)
        _rec = cv2.FaceRecognizerSF.create(_SFACE_PATH, "")
    except Exception as e:
        print("SFace 不可用，退化为仅几何打分:", e)
        _rec_failed = True
        _rec = None
    return _rec


def _yunet():
    import cv2
    p = os.path.join(_ROOT, "models", "face_detection_yunet.onnx")
    return cv2.FaceDetectorYN_create(p, "", (320, 320), 0.7, 0.3, 5000)


_yn = None


def _det(im: Image.Image):
    """返回 (face_row_ndarray(15), box) 或 (None, None)。"""
    global _yn
    import cv2
    if _yn is None:
        _yn = _yunet()
    arr = np.array(im.convert("RGB"))[:, :, ::-1].copy()
    h, w = arr.shape[:2]
    _yn.setInputSize((w, h))
    _, faces = _yn.detect(arr)
    if faces is None or not len(faces):
        return None, None
    best = max(faces, key=lambda f: f[2] * f[3])
    return best, (int(best[0]), int(best[1]), int(best[2]), int(best[3]))


def _aligned(im, row):
    import cv2
    rec = _recognizer()
    if rec is None:
        return None
    arr = np.array(im.convert("RGB"))[:, :, ::-1].copy()
    return rec.alignCrop(arr, row)


def _embed(im, row):
    rec = _recognizer()
    cov = _aligned(im, row)
    if rec is None or cov is None:
        return None
    return rec.feature(cov).flatten()


def _sim(a, b):
    if a is None or b is None:
        return 0.5                      # 模型不可用时给中性分，不误杀
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _five(row):
    """YuNet 15 列：0-3 box, 4-13 五点(x,y)*5, 14 score。返回以眼距归一化的五点。"""
    pts = np.array(row[4:14], dtype=np.float32).reshape(5, 2)
    d = np.linalg.norm(pts[0] - pts[1]) + 1e-6        # 右眼-左眼
    return (pts - pts[2]) / d                          # 以鼻尖为原点、眼距为尺度


def _geom(ref_pts, cand_pts):
    if ref_pts is None or cand_pts is None:
        return 0.5
    d = np.linalg.norm(ref_pts - cand_pts, axis=1).mean()
    return float(max(0.0, 1.0 - d / 0.55))             # 偏移半个眼距 -> 0 分


def _keep(ref_img: Image.Image, cand_img: Image.Image, box, mask: Image.Image = None):
    """脸框内、头发 mask 外的平均像素改动。换发不该动这里。"""
    if box is None:
        return 0.0
    a = np.asarray(ref_img.convert("RGB"), dtype=np.float32)
    b = np.asarray(cand_img.convert("RGB"), dtype=np.float32)
    if a.shape != b.shape:
        b = np.asarray(cand_img.convert("RGB").resize(ref_img.size), dtype=np.float32)
    x, y, w, h = box
    H, W_ = a.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W_, x + w), min(H, y + h)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return 0.0
    prot = np.ones((H, W_), dtype=bool)
    if mask is not None:
        m = np.asarray(mask.convert("L").resize((W_, H))) > 96
        prot &= ~m
    sel = np.zeros((H, W_), dtype=bool)
    sel[y0:y1, x0:x1] = True
    sel &= prot
    if sel.sum() < 200:
        return 0.5
    diff = np.abs(a - b)[sel].mean()
    return float(max(0.0, 1.0 - diff / 24.0))          # 平均差 24 灰阶以上判 0


def _hair_edge(im, mask: Image.Image):
    """发际线连续性：mask 上边界每列第一个前景像素的纵坐标，二阶差分越小越顺。"""
    a = np.asarray(mask.convert("L")) > 128
    if not a.any():
        return 0.5
    cols = np.where(a.any(axis=0))[0]
    top = np.array([np.argmax(a[:, c]) for c in cols], dtype=np.float32)
    if len(top) < 10:
        return 0.5
    d2 = np.abs(np.diff(top, 2)).mean()
    return float(max(0.0, 1.0 - d2 / 6.0))


def score(ref_img: Image.Image, cand_img: Image.Image, mask: Image.Image = None):
    ref_row, ref_box = _det(ref_img)
    cand_row, _ = _det(cand_img)
    keep = _keep(ref_img, cand_img, ref_box, mask)
    if ref_row is None or cand_row is None:
        return {"keep": round(keep, 4), "sim": 0.0, "hair": 0.5, "total": round(0.44 * keep, 4),
                "ok": False, "why": "没检出人脸"}
    e_ref = _embed(ref_img, ref_row)
    e_cand = _embed(cand_img, cand_row)
    sim = _sim(e_ref, e_cand)
    hair = _hair_edge(cand_img, mask) if mask is not None else 0.5
    total = W["keep"] * keep + W["sim"] * sim + W["hair"] * hair
    ok = (sim >= FACE_MIN) and (keep >= KEEP_MIN)
    why = ""
    if sim < FACE_MIN:
        why = f"身份漂移(sim={sim:.2f})"
    elif keep < KEEP_MIN:
        why = f"脸部被改动(keep={keep:.2f})"
    return {"keep": round(keep, 4), "sim": round(sim, 4), "hair": round(hair, 4),
            "total": round(total, 4), "ok": ok, "why": why}


def best(ref_img: Image.Image, cands, mask: Image.Image = None):
    """返回 (idx, [score...])。全部不达标时取 total 最高的那张，仍返回，但 ok=False。"""
    scores = [score(ref_img, c, mask) for c in cands]
    ok_idx = [i for i, s in enumerate(scores) if s["ok"]]
    if ok_idx:
        idx = max(ok_idx, key=lambda i: scores[i]["total"])
    else:
        idx = max(range(len(cands)), key=lambda i: scores[i]["total"]) if cands else -1
    return idx, scores
