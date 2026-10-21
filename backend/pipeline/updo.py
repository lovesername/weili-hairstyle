# -*- coding: utf-8 -*-
"""扎发 / 盘发：剪影法。

之前的做法是「几何剪短 + 只在头顶加一小块髻」，出来是 bob 顶上顶个丸子，
用户一眼看穿"这不是换发型，是加了个东西"。真正要发生的是：
**现有发型的体积整块消失**，换成贴头皮收拢 + 扎点起髻。

所以这一步的轮廓 100% 由几何决定（mask.updo_target）：剪影外的头发全部抹掉，
模型只在剪影里重排发丝走向、补出髻的质感。模型没有机会把 bob 留在原地。

构图问题：手机自拍的裁剪框常常紧贴头顶，头顶上方一个像素都没有。
高髻要 0.66×脸高 的空间，没空间就只能"降级成后颈低盘"——同样是头发全部收起来，
但不需要额外构图。硬凑高髻的结果是髻被画面裁掉一半（试过，很难看）。
"""
import numpy as np
from PIL import Image, ImageFilter, ImageDraw

from . import mask as mk

# 髻顶 = fy - BUN_TOP*fh：头顶上方至少要有这么多空间，才画得下高髻
BUN_TOP = 0.66


def hair_top(hair: Image.Image) -> int:
    a = np.asarray(hair) > 100
    if not a.any():
        return 0
    rows = np.where(a.sum(1) > a.shape[1] * 0.02)[0]
    return int(rows[0]) if len(rows) else 0


def headroom(fb, kind="high") -> float:
    """头顶上方还剩多少余量（像素）。负数 = 髻会被裁。"""
    if not fb:
        return 0.0
    return float(fb[1] - BUN_TOP * fb[3])


def pick_kind(fb, kind="high", H=768, margin=0.02) -> str:
    """空间不够就别画高髻，降成后颈低盘。"""
    if kind != "high":
        return kind
    return "high" if headroom(fb, "high") >= margin * H else "low"


def prepare(im: Image.Image, fb, hair: Image.Image, kind="high"):
    """返回 dict：canvas / fb / hair / tgt / kill / kind。

    目前不做补边（补边会改构图：人变小或画面变高，用户一眼就看出来）。
    """
    kind = pick_kind(fb, kind, im.width)
    tgt, kill = mk.updo_silhouette(im, fb, kind, hair=hair)
    return {"canvas": im, "fb": fb, "hair": hair, "tgt": tgt, "kill": kill,
            "kind": kind, "pad": 0, "k": 1.0}


# ---------------- 以下是"画布补边"的实验路径 ----------------
# 保留但不用：补边能让高髻完整，代价是最终图里的人缩小 8~15%，
# 或者画面被动变高。业务上不如直接降成低盘自然。

def pad_needed(fb, kind="high", H=768, margin=0.03) -> int:
    if kind != "high" or not fb:
        return 0
    fx, fy, fw, fh = fb
    d = fy - BUN_TOP * fh
    m = margin * H
    if d >= m:
        return 0
    pad = H * (m - d) / float(H - d)
    return max(0, min(int(H * 0.45), int(round(pad)) + 1))


def _scaled(im: Image.Image, k: float, pad: int, H: int) -> Image.Image:
    """等比缩小到 (H-pad) 见方，贴在底部；顶部补背景。

    顶部**不能**用镜像/复制：镜像会把脸倒过来，模型会照着那个半张脸再画一张。
    只取最上面几行的中位色铺平。
    """
    s = H - pad
    small = im.resize((s, s), Image.LANCZOS)
    out = Image.new(im.mode, (H, H), (255, 255, 255) if im.mode == "RGB" else 0)
    if pad > 0 and im.mode == "RGB":
        arr = np.asarray(small.crop((0, 0, s, max(2, min(8, s // 20))))).astype(np.float32)
        col = np.median(arr.reshape(-1, 3), axis=0)
        ramp = np.linspace(1.05, 1.0, H).astype(np.float32)[:, None, None]
        band = np.clip(col[None, None, :] * ramp, 0, 255).astype(np.uint8)
        out.paste(Image.fromarray(np.repeat(band, H, axis=1)), (0, 0))
        out = out.filter(ImageFilter.GaussianBlur(3))
    out.paste(small, (pad // 2, pad))
    return out


def unpad(im: Image.Image, pad: int) -> Image.Image:
    """裁掉补出来的区域，回到原始构图（内容框是 (pad//2, pad, +s, H)）。"""
    if pad <= 0:
        return im
    H = im.width
    s = H - pad
    x0 = pad // 2
    return im.crop((x0, pad, x0 + s, H)).resize((H, H), Image.LANCZOS)
