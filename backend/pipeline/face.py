# -*- coding: utf-8 -*-
"""人脸检测：优先 YuNet（OpenCV 4.5.4+/5.x 通用，准且带关键点），退化到 Haar。

    cv2 5.0 已移除 CascadeClassifier，老代码会静默失效 —— 这里做了版本探测。
模型缺失时自动从 opencv_zoo 拉一份（232KB），只拉一次。
"""
import os

import numpy as np
from PIL import Image

MODEL = os.path.join(os.path.dirname(__file__), "..", "..", "models", "face_detection_yunet.onnx")
URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
       "face_detection_yunet/face_detection_yunet_2023mar.onnx")

try:
    import cv2
except Exception:
    cv2 = None

_det = None
_haar = None


def _ensure_model():
    if os.path.exists(MODEL) and os.path.getsize(MODEL) > 100_000:
        return MODEL
    os.makedirs(os.path.dirname(MODEL), exist_ok=True)
    try:
        import httpx
        r = httpx.get(URL, follow_redirects=True, timeout=60)
        if r.status_code == 200 and len(r.content) > 100_000:
            open(MODEL, "wb").write(r.content)
            return MODEL
    except Exception:
        pass
    return None


def _yunet():
    global _det
    if _det is not None:
        return _det
    if cv2 is None or not hasattr(cv2, "FaceDetectorYN_create"):
        return None
    p = _ensure_model()
    if not p:
        return None
    _det = cv2.FaceDetectorYN_create(p, "", (320, 320), 0.6, 0.3, 5000)
    return _det


def detect(im: Image.Image):
    """返回 [(x, y, w, h, score)]，按面积降序。"""
    arr = np.array(im.convert("RGB"))[:, :, ::-1].copy()   # RGB -> BGR
    d = _yunet()
    if d is not None:
        h, w = arr.shape[:2]
        d.setInputSize((w, h))
        _, faces = d.detect(arr)
        out = []
        if faces is not None:
            for f in faces:
                x, y, fw, fh = f[:4]
                out.append((int(x), int(y), int(fw), int(fh), float(f[-1])))
        return sorted(out, key=lambda t: -t[2] * t[3])

    global _haar
    if cv2 is not None and hasattr(cv2, "CascadeClassifier") and _haar is None:
        try:
            haarp = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
            _haar = cv2.CascadeClassifier(haarp)
        except Exception:
            _haar = False
    if _haar:
        g = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
        g = cv2.equalizeHist(g)
        faces = _haar.detectMultiScale(g, 1.08, 5, minSize=(60, 60))
        return [(int(x), int(y), int(w), int(h), 1.0) for (x, y, w, h) in faces]
    return []


def biggest(im: Image.Image):
    f = detect(im)
    return f[0][:4] if f else None


def detect_full(im: Image.Image):
    """带 5 关键点的检测（质检/姿态矫正用）。

    返回 (box, pts, score)：
        box = (x, y, w, h)
        pts = [(x,y) ×5] 顺序为「图像左侧眼、图像右侧眼、鼻尖、左嘴角、右嘴角」，
              拿不到关键点（Haar 兜底 / 无模型）时为 None
        score = YuNet 置信度
    """
    arr = np.array(im.convert("RGB"))[:, :, ::-1].copy()
    d = _yunet()
    if d is None:
        b = biggest(im)
        return (b, None, 0.0) if b else (None, None, 0.0)
    h, w = arr.shape[:2]
    d.setInputSize((w, h))
    _, faces = d.detect(arr)
    if faces is None or not len(faces):
        return None, None, 0.0
    f = max(faces, key=lambda r: float(r[2]) * float(r[3]))
    box = (int(f[0]), int(f[1]), int(f[2]), int(f[3]))
    pts = [(float(f[i]), float(f[i + 1])) for i in (4, 6, 8, 10, 12)]
    return box, pts, float(f[-1])
