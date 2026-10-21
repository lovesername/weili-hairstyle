# -*- coding: utf-8 -*-
"""上传图预处理：EXIF 纠正 → 人脸检测 → 头肩区方形裁剪 → 缩放。

裁剪框决定成败：框太小（只框住脸）生成时会缺发量；框太大（整个人）头发像素占比太低，
模型会把头发糊成一团。实践值：以人脸框为基准，左右各扩 1.15×w，上方扩 1.05×h，
下方扩 1.35×h，再取正方形。
"""
import io

from PIL import Image, ImageFilter, ImageOps

from . import face as facedet

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None


def load_image(data: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    return im.convert("RGB")


def detect_face(im: Image.Image):
    """返回 (x, y, w, h) 或 None。YuNet 优先，Haar 兜底（cv2 5.x 已无 Haar）。"""
    return facedet.biggest(im)


def _crown_top(im, x_center, half_band, dark=92):
    """头部竖带里最上面那撮深色（头发）像素的行号；找不到返回 None。

    为什么需要：人脸框推出的方框常常把「头顶那撮头发」切在框外（框顶落在发旋下面）。
    云端模型在第 0 行就画头发，回贴时羽化边正好压在原来残留的头发上 ——
    出来就是用户说的「头顶浮着一团鬼影」。
    """
    if np is None:
        return None
    g = np.asarray(im.convert("L"), dtype=np.uint8)
    H, W = g.shape
    x0 = max(0, int(x_center - half_band))
    x1 = min(W, int(x_center + half_band))
    if x1 - x0 < 8:
        return None
    band = g[:, x0:x1]
    need = max(3, int((x1 - x0) * 0.06))
    rows = (band < dark).sum(axis=1)
    idx = np.where(rows >= need)[0]
    return int(idx[0]) if len(idx) else None


def head_crop_rect(im: Image.Image, face=None):
    """算出头肩方形裁剪框（含越界收缩）。"""
    W, H = im.size
    if face:
        x, y, w, h = face
        left = x - 1.15 * w
        right = x + w + 1.15 * w
        top = y - 1.05 * h
        bottom = y + h + 1.35 * h
    else:
        # 无检测：假设人像居中，头在画面上部 55%
        cw = W * 0.72
        left, right = (W - cw) / 2, (W + cw) / 2
        top, bottom = H * 0.02, H * 0.02 + cw

    cx, cy = (left + right) / 2, (top + bottom) / 2
    side = max(right - left, bottom - top)
    # 保证方形不越界：先夹住中心，再夹边长
    side = min(side, min(W, H) * 1.0)
    x0 = min(max(0, cx - side / 2), W - side)
    y0 = min(max(0, cy - side / 2), H - side)

    # 头顶必须整颗进框：框顶切进头发 → 模型从第 0 行画头发 → 回贴羽化边出鬼影
    if face:
        crown = _crown_top(im, x + w / 2.0, side * 0.32)
        margin = int(side * 0.05)
        if crown is not None and y0 > crown - margin:
            y0 = max(0, min(y0, crown - margin))
            y0 = min(y0, max(0, H - side))
    return int(x0), int(y0), int(side), int(side)


def _pad_square(im: Image.Image):
    """整图 + 补边成正方形，补边用「边缘延展 + 重度模糊」，看不出接缝。

    为什么不再用方框切：切出来是正方形，但切掉的那部分回贴时没人管 ——
    头顶被切在框外，羽化边上就浮出一团原来的头发；衣领/水印被切，底下就留一条旧的。
    补边成正方形，模型看到的是整张图，回贴时能盖满整幅。
    """
    W, H = im.size
    side = max(W, H)
    pl, pt = (side - W) // 2, (side - H) // 2
    pr, pb = side - W - pl, side - H - pt
    if pl == pr == pt == pb == 0:
        return im.copy(), (0, 0, 0, 0)
    if np is None:
        canvas = Image.new("RGB", (side, side), (240, 240, 240))
        canvas.paste(im, (pl, pt))
        return canvas, (pl, pt, pr, pb)
    a = np.pad(np.asarray(im), ((pt, pb), (pl, pr), (0, 0)), mode="edge")
    padded = Image.fromarray(a)
    if pl or pr or pt or pb:
        blurred = padded.filter(ImageFilter.GaussianBlur(max(6, side // 40)))
        mask = Image.new("L", (side, side), 255)
        mask.paste(0, (pl, pt, pl + W, pt + H))
        padded = Image.composite(blurred, padded, mask)
    return padded, (pl, pt, pr, pb)


def prepare(data: bytes, size: int = 768, pad_color=(255, 255, 255)):
    """返回 dict:
    img      头肩方框裁剪版（本地 comfy / 局部管线用）
    rect     该方框在原图中的位置
    face     原图人脸框
    orig     原图
    pad_img  整图补边成正方形（云端整头重画用），pad 为四边补了多少像素
    """
    orig = load_image(data)
    face = detect_face(orig)
    rect = head_crop_rect(orig, face)
    crop = orig.crop((rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]))
    crop = crop.resize((size, size), Image.LANCZOS)
    sq, pad = _pad_square(orig)
    return {"img": crop, "rect": rect, "face": face, "orig": orig,
            "pad_img": sq.resize((size, size), Image.LANCZOS),
            "pad": pad, "side": max(orig.size)}

def to_bytes(im: Image.Image, fmt="JPEG", q=95) -> bytes:
    b = io.BytesIO()
    im.save(b, fmt, quality=q)
    return b.getvalue()
