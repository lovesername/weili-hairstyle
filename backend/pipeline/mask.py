# -*- coding: utf-8 -*-
"""头发 mask。

两条路：
  A. 有 models/face_parsing.onnx（BiSeNet 人脸解析，CelebAMask-HQ 19 类）时直接取 hair 通道，
     发丝级精度，官方权重可搜 face-parsing.PyTorch 的 79999_iter.pth 转 onnx。
  B. 没模型时用肤色排除 + 头区几何约束的启发式，够用且零依赖：
       头区 = 人脸框外扩；hair = 头区内且「不是皮肤」且「与皮肤亮度差异够大」的像素
       再做闭运算 + 保留顶部连通域 + 羽化。

返回 PIL 'L' 图，255=头发。
"""
import os

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

MODEL = os.path.join(os.path.dirname(__file__), "..", "..", "models", "face_parsing.onnx")
_model = None


def _load_onnx():
    global _model
    if _model is not None:
        return _model
    if not os.path.exists(MODEL):
        return None
    try:
        import onnxruntime as ort
        _model = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
        return _model
    except Exception:
        return None


def _hair_via_onnx(im: Image.Image):
    sess = _load_onnx()
    if sess is None:
        return None
    inp = im.resize((512, 512), Image.BILINEAR)
    arr = np.asarray(inp).astype(np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], np.float32)
    std = np.array([0.229, 0.224, 0.225], np.float32)
    arr = (arr - mean) / std
    arr = np.transpose(arr, (2, 0, 1))[None]
    name = sess.get_inputs()[0].name
    out = sess.run(None, {name: arr})[0]
    lbl = out[0].argmax(0)
    # CelebAMask-HQ: 17 = hair
    m = (lbl == 17).astype(np.uint8) * 255
    return Image.fromarray(m).resize(im.size, Image.BILINEAR)


def _skin_mask(im: Image.Image):
    """YCrCb 肤色范围，OpenCV 常用阈值。"""
    ycc = np.asarray(im.convert("YCbCr")).astype(np.int16)
    y, cb, cr = ycc[..., 0], ycc[..., 1], ycc[..., 2]
    skin = (y > 40) & (cb >= 77) & (cb <= 135) & (cr >= 133) & (cr <= 180)
    return skin


def _hair_heuristic(im: Image.Image, face_in_crop):
    W, H = im.size
    arr = np.asarray(im).astype(np.int16)
    skin = _skin_mask(im)
    if face_in_crop:
        fx, fy, fw, fh = face_in_crop
    else:
        fx, fy, fw, fh = W * 0.3, H * 0.22, W * 0.4, H * 0.38

    # 头区：人脸框外扩（上方多留，给发量和发顶）
    # 下沿必须留到胸口：原来卡在 fy+1.05*fh（下巴）会把长发直接截断，
    # 长发用户选款式只改到头皮那一圈 —— 表现就是"没变化"。
    x0 = max(0, int(fx - 0.70 * fw))
    x1 = min(W, int(fx + fw + 0.70 * fw))
    y0 = max(0, int(fy - 0.95 * fh))
    y1 = min(H, int(fy + fh * 2.10))
    head = np.zeros((H, W), bool)
    head[y0:y1, x0:x1] = True

    # 皮肤亮度参考
    if skin.any():
        skin_lum = float(arr[..., :3][skin][:, :3].mean()) if skin.sum() > 50 else 150.0
    else:
        skin_lum = 150.0

    lum = arr[..., 0] * 0.299 + arr[..., 1] * 0.587 + arr[..., 2] * 0.114
    sat = arr.max(2) - arr.min(2)

    # 只看"暗"或"高饱和"（真实染发如酒红/亚麻）；阈值过松会把偏红肤色误判成头发
    hair = head & (~skin) & ((lum < skin_lum * 0.92) | (sat > 90))
    # 去掉头发区域里零散的亮斑（高光）
    m = Image.fromarray((hair * 255).astype(np.uint8))
    m = m.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.MinFilter(9))   # 闭运算
    m = m.filter(ImageFilter.GaussianBlur(3))

    a = np.array(m, dtype=np.uint8, copy=True)
    # 只保留头区，砍掉衣领、肩膀、画面边缘的杂色
    keep = np.zeros_like(a)
    keep[y0:y1, x0:x1] = 255
    a = np.minimum(a, keep)
    a[int(H * 0.985):] = 0

    # 只留「跟头顶连着的」那块，砍掉衣服、背景里的同类暗色
    binm = (a > 96).astype(np.uint8)
    if binm.any():
        n, lab, st, _ = cv2.connectedComponentsWithStats(binm, 8)
        scalp = np.zeros_like(binm)
        sy1 = int(fy + fh * 0.55)
        scalp[max(0, int(fy - fh)):sy1, x0:x1] = 1
        out = np.zeros_like(binm)
        for i in range(1, n):
            comp = lab == i
            if (comp & scalp).any() and st[i, cv2.CC_STAT_AREA] > W * H * 0.002:
                out |= comp
        if out.any():
            a = (a * out).astype(np.uint8)
    return Image.fromarray(a)


def hair_mask(im: Image.Image, face_in_crop=None) -> Image.Image:
    m = _hair_via_onnx(im)
    if m is not None:
        m = m.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(2))
        return m
    return _hair_heuristic(im, face_in_crop)


def dilate(mask: Image.Image, px: int = 10) -> Image.Image:
    """向外扩，保证发际线边缘被覆盖（生成完要能长出新发根）。"""
    k = max(3, px // 2 * 2 + 1)
    return mask.filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.GaussianBlur(px / 2))


def union(a: Image.Image, b: Image.Image) -> Image.Image:
    return Image.fromarray(np.maximum(np.asarray(a), np.asarray(b)))


def face_box_in_crop(face, rect, size):
    if not face:
        return None
    x, y, w, h = face
    k = size / rect[2]
    return ((x - rect[0]) * k, (y - rect[1]) * k, w * k, h * k)

# ---------------- 目标发型区域（换发型能真的变长/变蓬的关键） ----------------
# 只在"现有头发像素"里重绘，模型永远只能改质感、改不了轮廓：
# 短发选长发，出来还是短发，用户看到的就是"没变化"。所以 mask 要覆盖
# 目标发型**可能占的整块区域**，模型才有空间长出新长度。
LEN_END = {10: 0.90, 12: 0.94, 15: 0.98, 20: 1.12, 30: 1.30}   # 发尾 = 人脸框顶 + 框高 * 这个倍数
# 15 齐下颌 / 20 过下巴到脖 / 30 到锁骨。写太大（比如 2.0）会把衣服也画成头发。

_LEN_HINT = (
    (12, ("pixie", "undercut", "crop", "精灵", "寸头", "超短")),
    (30, ("30cm", "collarbone", "shoulder", "锁骨", "长发", "long hair")),
    (20, ("20cm", "chest", "中长发", "chest-length")),
    (15, ("15cm", "ear-to-jaw", "jaw", "bob", "pixie", "短发", "undercut", "crop")),
)


def length_cm(*texts):
    """从款式文案里认长度（cm）。认不出按 20cm（中长）算。"""
    t = " ".join([x or "" for x in texts]).lower()
    for cm, keys in _LEN_HINT:
        if any(k in t for k in keys):
            return cm
    return 20


def _face_hole(im: Image.Image, face_in_crop, W, H, pad_ratio=0.44, top=0.14, box=None):
    """护脸区：用**肤色**判断，不用几何椭圆。

    用椭圆护脸会把「贴在脸两侧的长发」一起护住 —— 剪短时那两撮就留在原地，
    出来是寸头 + 两条长鬓角（实测踩过）。肤色 mask 只护真正的皮肤，
    头发（不是肤色）该抹就抹，刘海也能自由盖到额头上。

    box: (x0, x1, y0, y1) 绝对像素，改保护区范围。默认只到下巴；
         抹两侧披发时要放大到脖子以下，否则下巴底下的皮肤会被误抹出一条灰带。
    """
    fx, fy, fw, fh = face_in_crop
    sk = _skin_mask(im)
    if box:
        x0, x1, y0, y1 = box
    else:
        x0 = int(fx - 0.08 * fw)
        x1 = int(fx + 1.08 * fw)
        y0 = int(fy + top * fh)
        y1 = int(fy + 1.05 * fh)
    x0, x1 = max(0, int(x0)), min(W, int(x1))
    y0, y1 = max(0, int(y0)), min(H, int(y1))
    # box 必须做成**椭圆**，不能是矩形：矩形裁出来的护脸区是直角，
    # mask 的边界就变成画面里一条竖硬边（实测：脸上像被切了两刀，贴回去
    # 就是两块糊斑 + 直边）。椭圆外切 box，边缘天然收敛。
    boxm = Image.new("L", (W, H), 0)
    ImageDraw.Draw(boxm).ellipse([x0, y0, x1, y1], fill=255)
    bm = np.asarray(boxm.filter(ImageFilter.GaussianBlur(max(3, (x1 - x0) // 24)))) > 60
    sk = sk & bm

    fallback = Image.new("L", (W, H), 0)
    ImageDraw.Draw(fallback).ellipse(
        [fx + fw / 2 - fw * pad_ratio, fy + fh * top,
         fx + fw / 2 + fw * pad_ratio, fy + fh * 1.02], fill=255)

    if sk.sum() < W * H * 0.008:      # 暗光/浓妆测不到肤色 → 退回椭圆
        return fallback

    m = Image.fromarray((sk * 255).astype(np.uint8))
    # 闭运算把眼睛、眉毛、嘴唇这些洞填掉（不然护脸区千疮百孔，模型会去重画五官）
    m = m.filter(ImageFilter.MaxFilter(21)).filter(ImageFilter.MinFilter(21))
    a = np.asarray(m)
    binm = (a > 96).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(binm, 8)
    if n > 1:
        big = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
        a = np.where(lab == big, 255, 0).astype(np.uint8)
    # 往外放几像素，护住脸的边缘（下巴/颧骨线不能被重画，否则人脸会变）
    out = Image.fromarray(a).filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(2))
    return out


# ---------------- 扎发 / 盘发 ----------------
# 扎发不是"长度"问题，是"头发往哪儿走"的问题。只改 prompt 的话，SD 会保留原来的
# 长发轮廓，只在头顶抹两把 —— 用户看到的就是"点完没反应"（截图里那张就是）。
# 做法和剪短同源：先用几何把扎点以下的头发清掉（长度/披散锁死），
# 再把「头顶 + 扎点以上」整块交给模型去画紧贴的头发和发髻/马尾。
UPDO_LINE = {"high": 0.58, "low": 1.02}   # 扎点 y = 人脸框顶 + 框高 * 这个倍数

_UPDO_HIGH = ("high ponytail", "top bun", "高马尾", "丸子头", "丸子", "高扎")
_UPDO_LOW = ("low ponytail", "french low updo", "low updo", "chignon", "updo",
             "低马尾", "低盘", "盘发", "扎发", "束发", "马尾", "ponytail", "bun")
# 半扎 / 编发 不动几何：下半还是长发，硬剪会把款式剪没
_UPDO_SKIP = ("half-up", "half up", "半丸子", "半扎", "braid", "辫")


def updo_kind(*texts):
    """认扎发款式，返回 'high' / 'low' / None。"""
    t = " ".join([x or "" for x in texts]).lower()
    if not t or any(k in t for k in _UPDO_SKIP):
        return None
    if any(k in t for k in _UPDO_HIGH):
        return "high"
    if any(k in t for k in _UPDO_LOW):
        return "low"
    return None


def bun_mask(im: Image.Image, face_in_crop, up=0.62, half=0.46, down=0.12, hole_top=0.10):
    """头顶发髻位：只给一小块椭圆（压住发际线上沿），其余一律不动。

    区域给大了模型会把整头重画；给成一小块、贴着头顶，它只会往那儿加一个髻。
    """
    W, H = im.size
    fx, fy, fw, fh = face_in_crop
    cx = fx + fw / 2
    mk = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mk).ellipse([cx - fw * half, fy - fh * up, cx + fw * half, fy + fw * down],
                               fill=255)
    hole = _face_hole(im, face_in_crop, W, H, pad_ratio=0.46, top=hole_top)
    a = np.minimum(np.asarray(mk), 255 - np.asarray(hole))
    out = Image.fromarray(a.astype(np.uint8))
    return out.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(4))


def updo_band(im: Image.Image, hair: Image.Image, face_in_crop, y_line: int, grow=31, blur=8):
    """扎发第一步要清理的带：扎点以下的旧长发（含脖子/衣领那一段）。

    必须挖掉脸：带子横穿整幅画面，不挖脸就会把脸颊也一起拍平，
    下巴上留一条硬边（试过，很难看）。
    """
    W, H = hair.size
    a = np.asarray(hair) > 100
    a[:max(0, y_line - 10), :] = False
    # 下沿别压到衣领：压到就会把领子也一起重画成灰白（实测）
    a[int(min(H, y_line + face_in_crop[3] * 0.16)):, :] = False
    m = Image.fromarray((a * 255).astype(np.uint8))
    m = m.filter(ImageFilter.MaxFilter(grow)).filter(ImageFilter.GaussianBlur(blur))
    hole = _face_hole(im, face_in_crop, W, H, pad_ratio=0.46, top=0.16)
    out = np.minimum(np.asarray(m), 255 - np.asarray(hole))
    return Image.fromarray(out.astype(np.uint8))


def updo_cap(im: Image.Image, face_in_crop, kind="high", hole=0.46, hole_top=0.16):
    """贴着头骨的「帽形」+ 头顶发髻位。

    这是扎发能不能成形的关键：mask 轮廓决定模型画出来的轮廓。
    用宽椭圆（bob 那种宽度）→ 出来永远是 bob；用贴头骨的窄帽形 → 头发才会全部收上去。
    """
    W, H = im.size
    fx, fy, fw, fh = face_in_crop
    cx = fx + fw / 2
    cap = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(cap)
    d.ellipse([cx - fw * 0.56, fy - fh * 0.26, cx + fw * 0.56, fy + fh * 0.58], fill=255)
    if kind == "high":
        # 头顶发髻：给足位置，模型才画得出丸子
        d.ellipse([cx - fw * 0.44, fy - fh * 0.74, cx + fw * 0.44, fy - fh * 0.02], fill=255)
    hole_im = _face_hole(im, face_in_crop, W, H, pad_ratio=hole, top=hole_top)
    a = np.minimum(np.asarray(cap), 255 - np.asarray(hole_im))
    out = Image.fromarray(a.astype(np.uint8))
    return out.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(3))


def updo_region(im: Image.Image, face_in_crop=None, kind="low", cut_y=None,
                hole=0.46, hole_top=0.16):
    """扎发要重画的区域：扎点以上的现有头发 + 头顶留出的发髻/马尾位置，挖掉脸。"""
    W, H = im.size
    base = hair_mask(im, face_in_crop)
    if not face_in_crop:
        face_in_crop = (W * 0.30, H * 0.22, W * 0.40, H * 0.38)
    fx, fy, fw, fh = face_in_crop
    cx = fx + fw / 2

    a = np.asarray(base).astype(np.uint8).copy()
    if cut_y:
        a[max(0, int(cut_y)):, :] = 0

    up = fh * (0.58 if kind == "high" else 0.34)
    half = fw * (0.94 if kind == "high" else 0.88)
    reg = Image.new("L", (W, H), 0)
    ImageDraw.Draw(reg).ellipse([cx - half, max(0.0, fy - up), cx + half, fy + fh * 0.80],
                                fill=255)
    a = np.maximum(a, np.asarray(reg))

    hole_im = _face_hole(im, face_in_crop, W, H, pad_ratio=hole, top=hole_top)
    a = np.minimum(a, 255 - np.asarray(hole_im))
    out = Image.fromarray(a.astype(np.uint8))
    return out.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(2))


def style_mask(im: Image.Image, face_in_crop=None, length=20, hole=0.46, hole_top=0.16):
    """现有头发 ∪ 目标发型区域，挖掉脸。

    length: 15 齐耳 / 20 过下巴 / 30 到锁骨。
    """
    base = hair_mask(im, face_in_crop)
    W, H = im.size
    if not face_in_crop:
        face_in_crop = (W * 0.30, H * 0.22, W * 0.40, H * 0.38)
    fx, fy, fw, fh = face_in_crop
    cx = fx + fw / 2

    end_y = min(H * 0.99, fy + fh * LEN_END.get(int(length), 1.42))
    half_w = fw * (0.84 if length >= 30 else 0.80)
    top = max(0.0, fy - fh * 0.80)

    reg = Image.new("L", (W, H), 0)
    ImageDraw.Draw(reg).ellipse([cx - half_w, top, cx + half_w, end_y], fill=255)

    # 整张脸都要护住（用肤色，不用椭圆：椭圆会把脸两侧的长发一起护住 → 剪短失败）
    hole_im = _face_hole(im, face_in_crop, W, H, pad_ratio=hole, top=hole_top)

    a = np.maximum(np.asarray(base), np.asarray(reg))
    a = np.minimum(a, 255 - np.asarray(hole_im))
    a[int(H * 0.99):] = 0
    out = Image.fromarray(a.astype(np.uint8))
    return out.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(2))


def cut_mask(im: Image.Image, face_in_crop=None, length=15, feather=6):
    """「剪短」专用：只抹掉**目标轮廓之外**的现有头发，保住发际线与头顶。

    为什么不能直接用 style_mask：那块 mask 连头顶一起交给模型重画，发际线会漂，
    重画完额头上一圈灰雾、脸被拉长（实测）。剪短真正要做的是"把长发剪到目标长度"，
    也就是把目标椭圆外的旧长发换成背景/脖子/衣领，椭圆内的原样留着。

    返回 255 = 需要抹掉的旧头发。
    """
    base = hair_mask(im, face_in_crop)
    W, H = im.size
    if not face_in_crop:
        face_in_crop = (W * 0.30, H * 0.22, W * 0.40, H * 0.38)
    fx, fy, fw, fh = face_in_crop
    cx = fx + fw / 2
    end_y = min(H * 0.99, fy + fh * LEN_END.get(int(length), 1.12))
    half_w = fw * 1.02            # 略宽，保住耳朵两侧的目标发量
    top = max(0.0, fy - fh * 1.05)

    keep = Image.new("L", (W, H), 0)
    ImageDraw.Draw(keep).ellipse([cx - half_w, top, cx + half_w, end_y], fill=255)

    a = np.minimum(np.asarray(base), 255 - np.asarray(keep))
    # 脸永远不抹
    hole = Image.new("L", (W, H), 0)
    ImageDraw.Draw(hole).ellipse(
        [cx - fw * 0.46, fy + fh * 0.16, cx + fw * 0.46, fy + fh * 1.02], fill=255)
    a = np.minimum(a, 255 - np.asarray(hole))
    out = Image.fromarray(a.astype(np.uint8))
    return out.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(feather / 2))


def hair_bottom(mask: Image.Image) -> int:
    """头发最低点（按行像素数 >2% 宽度算），比 bbox 稳。

    用 getbbox 会被几粒零散暗像素带偏，导致明明要加长却被判成"剪短"。
    """
    a = np.asarray(mask) > 127
    if not a.any():
        return 0
    rows = a.sum(1)
    idx = np.where(rows > a.shape[1] * 0.02)[0]
    return int(idx[-1]) if len(idx) else 0


# ---------------- 扎发 / 盘发：剪影法 ----------------
# 之前那条路是「几何剪短 + 头顶加一个髻」，出来的效果是 bob 上顶个丸子 ——
# 用户一眼就看穿"这不是换发型，是加了个东西"（2026-10-21 截图投诉）。
# 真正要发生的事是：**原来那个发型的体积整块消失**，换成"贴头皮收拢 + 扎点起髻"。
# 所以这里直接给一个几何剪影（头骨帽 + 髻），剪影外的头发全部抹掉，
# 模型只在剪影里重排发丝走向。轮廓 100% 由几何决定，不听模型的。
UPDO_SIL_TOP = -0.40     # 剪影顶 = fy + fh * 这个值（负值 = 额头上方，贴头骨的颅顶）
UPDO_SIL_BOT = 0.82      # 剪影底 = fy + fh * 这个值（耳下 / 后颈）
UPDO_SIL_HW = 0.60       # 最大半宽 = fw * 这个值（贴头骨，宽了就是 bob）

# 逐行宽度轮廓：上半段用四分之一椭圆（圆颅顶，别做成"人形图标"那种尖头+宽肩），
# 下半段从耳上最宽处收到后颈。
def _updo_hw(t):
    t = np.clip(np.asarray(t, dtype=np.float32), 0.0, 1.0)
    uu = np.clip((0.34 - t) / 0.34, 0.0, 1.0)
    upper = np.sqrt(np.maximum(0.0, 1.0 - uu * uu))
    u = np.clip((t - 0.34) / 0.66, 0.0, 1.0)
    lower = 1.0 - 0.68 * (u ** 1.15)   # 往下收得更狠：耳下要贴头皮，留宽就是 bob
    return np.where(t < 0.34, upper, lower)


def _sil_rows(fb, W, H, kind):
    """剪影逐行半宽（像素）。返回 (ys, half_widths, top, bot)。"""
    fx, fy, fw, fh = fb
    cx = fx + fw / 2
    top = max(0, int(fy + fh * UPDO_SIL_TOP))
    bot = min(H - 1, int(fy + fh * UPDO_SIL_BOT))
    ys = np.arange(top, bot + 1)
    if len(ys) < 2:
        return ys, np.zeros(0), top, bot, cx
    t = (ys - top) / float(bot - top)
    hw = _updo_hw(t) * UPDO_SIL_HW * fw
    return ys, hw, top, bot, cx


def updo_target(im: Image.Image, face_in_crop, kind="high") -> Image.Image:
    """扎发的目标剪影：头骨帽（+ 发髻）。模型只在这块里画头发。"""
    W, H = im.size
    fx, fy, fw, fh = face_in_crop
    ys, hw, top, bot, cx = _sil_rows(face_in_crop, W, H, kind)
    a = np.zeros((H, W), np.uint8)
    for y, w_ in zip(ys, hw):
        a[int(y), max(0, int(cx - w_)):min(W, int(cx + w_))] = 255

    over = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(over)
    if kind == "high":
        # 高扎：颅顶一个圆髻，压在剪影顶上（-0.26 比 -0.40 低，天然连成一片）。
        # 位置别太高：髻一浮空，模型就画成"头上顶个球"，正是用户截图里那种效果。
        d.ellipse([cx - fw * 0.27, fy - fh * 0.66, cx + fw * 0.27, fy - fh * 0.26], fill=255)
    else:
        # 低扎：后颈一团，别给宽（给宽 = bob 回来了）
        d.ellipse([cx - fw * 0.30, fy + fh * 0.69, cx + fw * 0.30, fy + fh * 1.01], fill=255)
    m = Image.fromarray(np.maximum(a, np.asarray(over)))
    # 挖掉皮肤（脸/额头/脖子）：让模型根本没有机会重画五官。
    # 挖出来的边界正好是原图的发际线 —— 头发还是沿着原本的发际线长出来的。
    hole = _face_hole(im, face_in_crop, W, H, pad_ratio=0.44, top=0.00)
    m = Image.fromarray(np.minimum(np.asarray(m), 255 - np.asarray(hole)).astype(np.uint8))
    return m.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(3))


def updo_kill(im: Image.Image, face_in_crop, target: Image.Image, hair=None,
              grow=13) -> Image.Image:
    """剪影**之外**的头发：全部抹掉。这是"换发型"和"加东西"的分界线。

    只抹真头发像素（hair_mask），不碰背景/衣服；护住脸（肤色），
    否则下巴和脸颊会被一起抹平。
    """
    W, H = im.size
    if hair is None:
        hair = hair_mask(im, face_in_crop)
    keep = target.filter(ImageFilter.MaxFilter(max(3, int(grow) | 1)))

    arr = np.asarray(im.convert("RGB")).astype(np.float32)
    lum = arr[..., 0] * 0.299 + arr[..., 1] * 0.587 + arr[..., 2] * 0.114
    fx, fy, fw, fh = face_in_crop
    sk = _skin_mask(im)
    fb_box = np.zeros((H, W), bool)
    fb_box[int(fy):int(fy + fh), int(fx):int(fx + fw)] = True
    face_sk = sk & fb_box
    skin_lum = float(np.median(lum[face_sk])) if face_sk.sum() > 200 else 170.0

    # 只抹"真头发"：比肤色暗一大截，或者本来就在头顶那一圈。
    # 不设这个门槛，下巴/脖子上的阴影（肤色检测不到、又够暗）会被当成头发抹掉，
    # 填成背景色后在脖子上留一条灰带（实测）。
    dark_enough = lum < max(55.0, skin_lum * 0.60)
    upper = np.zeros((H, W), bool)
    upper[:int(fy + fh * 0.30), :] = True
    a = (np.asarray(hair) > 100) & (np.asarray(keep) <= 100) & (dark_enough | upper)
    # 保护范围分两段：脸+额头整幅宽；脖子只护中间那一窄条。
    # 整幅宽护到锁骨会把粉色衣服也算成"皮肤"，肩膀上的发尾就抹不掉了（实测）。
    h1 = _face_hole(im, face_in_crop, W, H, pad_ratio=0.44, top=0.02,
                    box=(fx - 0.10 * fw, fx + 1.10 * fw, fy + 0.02 * fh, fy + 1.02 * fh))
    h2 = _face_hole(im, face_in_crop, W, H, pad_ratio=0.44, top=0.02,
                    box=(fx + 0.14 * fw, fx + 0.86 * fw, fy + 1.02 * fh, fy + 1.24 * fh))
    hole = np.maximum(np.asarray(h1), np.asarray(h2))
    a = a & (hole <= 100)
    out = Image.fromarray((a * 255).astype(np.uint8))
    # 往外放一圈：卷发的碎发、发丝的羽化边都在 hair_mask 之外（阈值以下的灰边），
    # 不放就会留下一圈毛毛躁躁的碎发（实测，长卷发那组满屏都是）。
    out = out.filter(ImageFilter.MaxFilter(max(3, int(grow) * 2 + 1)))
    # 再把"贴着头发、又明显比肤色暗"的像素补进来：垂在衣服上的卷发尾
    # 经常是一撮一撮断开的，hair_mask 连不上，但颜色骗不了人。
    near = np.asarray(Image.fromarray(((np.asarray(hair) > 100) * 255).astype(np.uint8))
                      .filter(ImageFilter.MaxFilter(41))) > 100
    very_dark = lum < max(45.0, skin_lum * 0.52)
    extra = near & very_dark & (hole <= 60) & (np.asarray(keep) <= 100)
    a2 = ((np.asarray(out) > 100) | extra) & (hole <= 60)
    out = Image.fromarray((a2 * 255).astype(np.uint8))
    return out.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(2))


def head_rect_mask(im: Image.Image, face_in_crop, grow=0.06) -> Image.Image:
    """整个头部的外接矩形（比人脸框大一圈）。用来当"不能取色"的区域：
    抹掉两侧披发时，行插值只能从背景取色，不能跨过脸去取嘴唇的红
    （不设这个限制，脖子口那一行会插出一条粉色横杠，实测）。"""
    W, H = im.size
    fx, fy, fw, fh = face_in_crop
    x0 = max(0, int(fx - fw * (0.12 + grow)))
    x1 = min(W, int(fx + fw * (1.12 + grow)))
    y0 = max(0, int(fy - fh * 0.55))
    # 下沿只到下巴下面一点：再往下是衣服，衣服必须能当取色源，
    # 否则衣领会被填成背景灰（领口一片灰斑，实测）。
    y1 = min(H, int(fy + fh * 0.98))
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).rectangle([x0, y0, x1, y1], fill=255)
    return m.filter(ImageFilter.GaussianBlur(3))


def updo_silhouette(im: Image.Image, face_in_crop, kind="high", hair=None):
    """返回 (target, kill)：目标剪影 / 要抹掉的旧头发。"""
    tgt = updo_target(im, face_in_crop, kind)
    return tgt, updo_kill(im, face_in_crop, tgt, hair=hair)
