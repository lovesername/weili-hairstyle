# -*- coding: utf-8 -*-
"""回贴与调色 —— 这一步决定"看不出是AI"还是"一眼假"。

流程：
  1. 生成图从裁剪尺寸缩回原图裁剪框尺寸
  2. 只取头部区域做 LAB 亮度匹配（a/b 只拉 35%，保留你新染的发色）
  3. 用羽化 mask 贴回原图（边缘 2~6px 渐变，消除接缝）
  4. 轻锐化发丝
"""
import os

import cv2
import numpy as np
from PIL import Image, ImageFilter


def _lab(im: Image.Image):
    return np.asarray(im.convert("LAB")).astype(np.float32)


def _from_lab(a: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "LAB").convert("RGB")


# 发色词 → (a,b) 目标值。注意 PIL 的 "LAB" 里 a/b 的中性点是 **0**（不是 128），
# 这里写错会把整头压成血红（16:30 踩过，别再改成 128）。
COLOR_AB = (
    (("自然黑", "黑色", "黑发", "jet black", "natural black", "black hair"), (0.0, 0.0)),
    (("深棕", "dark brown"), (5.0, 9.0)),
    (("栗棕", "chestnut"), (8.0, 14.0)),
    (("浅棕", "light brown", "棕黄"), (10.0, 22.0)),
    (("酒红", "樱桃红", "burgundy", "cherry red"), (20.0, 2.0)),
    (("红棕", "auburn", "red hair"), (16.0, 12.0)),
    (("亚麻", "flaxen"), (3.0, 22.0)),
    (("金", "blonde", "golden"), (5.0, 32.0)),
    (("灰", "silver", "grey", "gray"), (0.0, 1.0)),
    (("棕", "brown"), (7.0, 16.0)),
)


def color_from_prompt(text: str):
    """从文案里认发色，返回 LAB (a,b)；认不出返回 None（那就用原图发色）。"""
    t = (text or "").lower()
    for keys, ab in COLOR_AB:
        if any(k.lower() in t for k in keys):
            return ab
    return None


def hair_lab(im: Image.Image, mask: Image.Image):
    """取一张图里头发区域的 LAB 均值（L,a,b）。用来把「发型库封面的发色」当目标发色。"""
    a = _lab(im)
    m = np.asarray(mask) > 128
    if m.sum() < 300:
        return None
    return tuple(float(a[..., c][m].mean()) for c in range(3))


def match_color(gen: Image.Image, src: Image.Image, region: Image.Image, ab_weight=0.55,
                target_lab=None):
    """把 gen 的头发颜色向目标对齐。

    L   ：永远跟 src 走（原图的光照/曝光，不然一眼假）
    a/b ：跟 target 走（给了 target_lab 用发型库封面的发色，没给就用 src 自己的）
    """
    g = _lab(gen)
    s = _lab(src)
    m = np.asarray(region) > 96
    if m.sum() < 500:
        return gen
    if target_lab is not None:
        ab_weight = max(ab_weight, 0.85)   # 有封面参照就以封面发色为准，压住模型乱加暖色
    out = g.copy()
    for c in range(3):
        gv = g[..., c][m]
        sv = s[..., c][m]
        if gv.std() < 1e-3 or sv.std() < 1e-3:
            continue
        gm, gs = gv.mean(), gv.std()
        sm, ss = sv.mean(), sv.std()
        if c == 0:
            # L：整段统计迁移（跟着原图光照走，明暗关系才对）
            out[..., c] = (g[..., c] - gm) * (ss / gs) + sm
        else:
            # a/b：只平移色相，不做方差缩放！
            # 缩放会把发丝高光的色差放大成绿/红彩虹边（这个坑踩过）
            sm = sv.mean() if target_lab is None else target_lab[c]
            out[..., c] = g[..., c] + (sm - gm) * ab_weight
    return _from_lab(out)


def hair_only_alpha(gen: Image.Image, region: Image.Image, hair_l: float, soft=28.0) -> np.ndarray:
    """只在"确实是头发"的像素上给 alpha，把混在 mask 里的背景/衣服剔掉。

    目标区域比原来的头发大一圈时，模型会把背景也重画一遍 —— 直接按 mask 贴，
    头上就会顶一个亮圈的"光晕"。这里用亮度判定：比头发亮一大截的算背景，不贴。
    """
    L = _lab(gen)[..., 0]
    a = np.clip((hair_l + 55.0 - L) / soft, 0.0, 1.0)
    return a * (np.asarray(region) / 255.0)


def _match_bg(gen: Image.Image, src: Image.Image, mask: Image.Image,
              stats_mask: Image.Image) -> Image.Image:
    """把生成图里"背景那部分"的亮度对齐到原图同位置，消掉整块贴换来的灰圈。"""
    m = np.asarray(mask) > 110
    st = np.asarray(stats_mask) > 128
    bg = m & ~st
    if bg.sum() < 2000:
        return gen
    g, s = _lab(gen), _lab(src)
    out = g.copy()
    for c in range(3):
        d = float(s[..., c][bg].mean() - g[..., c][bg].mean())
        out[..., c] = g[..., c] + d
    return _from_lab(out)


def _drop_islands(alpha: Image.Image, stats_mask: Image.Image, w: int, h: int) -> Image.Image:
    """砍掉孤立的小黑块。

    模型经常顺手在耳朵旁边画出耳环/发卷之类的东西，alpha 判定"够黑"就留下了。
    规则：只有**跟原头发连着的**、或者**面积够大**的连通域才算头发。
    """
    a = np.asarray(alpha)
    binm = (a > 120).astype(np.uint8)
    if binm.sum() == 0:
        return alpha
    n, lab, st, _ = cv2.connectedComponentsWithStats(binm, 8)
    ref = np.asarray(stats_mask.resize((w, h), Image.LANCZOS)) > 128
    keep = np.zeros_like(binm)
    for i in range(1, n):
        area = st[i, cv2.CC_STAT_AREA]
        comp = lab == i
        if area > w * h * 0.02 or (comp & ref).sum() > area * 0.12:
            keep[comp] = 1
    if keep.sum() == 0:
        keep = binm
    keep = cv2.dilate(keep, np.ones((5, 5), np.uint8), iterations=1)
    return Image.fromarray((a * keep).astype(np.uint8))


def linear_fill(im: Image.Image, mask: Image.Image, skin=None, skip_dark=False) -> Image.Image:
    """按行做左右线性插值，把 mask 区填成"周围像素的延伸"。

    只用在**衣领那一段**（y >= 下巴）：那里左右两侧都是衣服，行插值出来就是干净的衣领。
    用在整条长发上会出肉色横条 —— 长发横跨脸颊时，左边就是脸，插值把肉色拖到肩上（踩过）。
    """
    a = np.asarray(im.convert("RGB")).astype(np.float32)
    m = np.asarray(mask) > 100
    skip = m.copy()
    if skin is not None:
        skip = skip | skin
    if skip_dark:
        lum = a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114
        skip = skip | (lum < 105)     # 别把残留的头发当取色源
    out = a.copy()
    H, W, _ = a.shape
    idx = np.arange(W)
    for y in range(H):
        row = m[y]
        if not row.any():
            continue
        good = ~skip[y]
        if not good.any():
            continue
        left = np.maximum.accumulate(np.where(good, idx, -1))
        right = np.minimum.accumulate(np.where(good, idx, W)[::-1])[::-1]
        xs = idx[row]
        li = np.clip(left[row], 0, W - 1)
        ri = np.clip(right[row], 0, W - 1)
        ok_l = left[row] >= 0
        ok_r = right[row] <= W - 1
        lv = a[y, li]
        rv = a[y, ri]
        span = np.maximum((ri - li).astype(np.float32), 1.0)
        k = ((xs - li).astype(np.float32) / span)[:, None]
        mixed = lv * (1 - k) + rv * k
        mixed = np.where(ok_l[:, None] & ok_r[:, None], mixed,
                         np.where(ok_l[:, None], lv, rv))
        out[y, xs] = mixed
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def vfill(im: Image.Image, mask: Image.Image, skip_dark=True) -> Image.Image:
    """竖直方向的行插值（转置后复用 linear_fill）。

    长发是"竖着垂下来"的两撮，上下邻居是背景和衣服 —— 竖直插值取色最准。
    横向插值在衣领上会跨过整件衣服，糊出一片灰。
    """
    t = im.transpose(Image.TRANSPOSE)
    m = mask.transpose(Image.TRANSPOSE)
    return linear_fill(t, m, skip_dark=skip_dark).transpose(Image.TRANSPOSE)


def composite_hair(orig: Image.Image, gen_crop: Image.Image, rect, base_img: Image.Image,
                   patch_mask: Image.Image, target_lab=None) -> Image.Image:
    """扎发用：只把「生成图里真正是头发的那部分」贴回去。

    直接按 patch 贴会把 patch 边缘那圈"几何填充色"一起糊上去 ——
    头周围一圈灰晕（实测，一眼假）。这里按亮度判一下：
    比这张图整体背景暗一大截的才算新头发，其余留在底图上（底图已经是干净的）。
    """
    x0, y0, w, h = rect
    if base_img.size != orig.size:
        base_img = base_img.resize(orig.size, Image.LANCZOS)
    gen = gen_crop.resize((w, h), Image.LANCZOS)
    base = base_img.crop((x0, y0, x0 + w, y0 + h))
    gen = _tint_ab(gen, target_lab)

    pm = patch_mask.resize((w, h), Image.LANCZOS)
    L = _lab(gen)[..., 0]
    bg = float(np.percentile(L, 88))
    hair = np.clip((bg - 32.0 - L) / 26.0, 0.0, 1.0)
    a = (hair * 255.0) * (np.asarray(pm) / 255.0)
    alpha = Image.fromarray(a.astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(max(1, w // 300)))

    out = base_img.copy()
    out.paste(Image.composite(gen, base, alpha), (x0, y0))
    reg = out.crop((x0, y0, x0 + w, y0 + h)).filter(
        ImageFilter.UnsharpMask(radius=1.4, percent=40, threshold=3))
    out.paste(Image.composite(reg, base, alpha), (x0, y0))
    return out


def paste_patch(base: Image.Image, gen: Image.Image, mask: Image.Image) -> Image.Image:
    """按羽化 mask 把 gen 贴到 base 上（同尺寸）。用于"清完底再接着画"的多段流程。"""
    if gen.size != base.size:
        gen = gen.resize(base.size, Image.LANCZOS)
    a = mask.filter(ImageFilter.GaussianBlur(max(4, base.width // 120)))
    return Image.composite(gen, base, a)


def plate_fill(im: Image.Image, mask: Image.Image, avoid: Image.Image = None,
               vblur=13) -> Image.Image:
    """按行取"这一行的背景色/衣服色"，整块铺平。

    为什么不用 linear_fill：长发抹掉以后要填的区域很大（从耳侧一直到胸口），
    左右两个取色源经常一边是墙、一边是衣服，逐像素混合就会在脖子两侧拉出一片
    横向扫描线（实测，长卷发那组直接废）。
    这里每行只取**一个**颜色（该行在头部区域之外的像素中位数），整行铺同一个色，
    再沿竖直方向 blur —— 墙面/衣服的渐变本来就是平滑的，填出来看不出接缝。
    """
    a = np.asarray(im.convert("RGB")).astype(np.float32)
    m = np.asarray(mask) > 60
    av = (np.asarray(avoid) > 60) if avoid is not None else np.zeros(m.shape, bool)
    if not m.any():
        return im
    H, W, _ = a.shape
    out = a.copy()
    last = None
    for y in range(H):
        xs = np.where(m[y])[0]
        if len(xs) == 0:
            continue
        good = (~m[y]) & (~av[y])
        if good.sum() >= 12:
            col = np.median(a[y][good], axis=0)
            last = col
        elif last is not None:
            col = last
        else:
            continue
        out[y, xs] = col
    # 竖直方向抹平：逐行取色会有 1~2 级的跳变，竖着 blur 一下就看不出来了
    if vblur and vblur > 1:
        arr = out.astype(np.uint8)
        arr = cv2.blur(arr, (1, int(vblur) | 1)).astype(np.float32)
        out[m] = arr[m]
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(1.5))


def erase_region(img: Image.Image, mask: Image.Image, radius=14, rounds=4,
                 mode="telea", avoid: Image.Image = None, smooth_below=None) -> Image.Image:
    """把 mask 区整块换成「周围像素的延伸」，得到一张"这块头发不存在"的底图。

    mode="telea"  ：先 Telea 再沿水平方向反复 blur 抹平。适合**下方**那一大片
                    （脖子/衣领）：左右邻居就是衣服和背景。
    mode="linear" ：按行线性插值，取色源跳过皮肤和暗像素（跳过剩下的旧头发）。
                    适合**脸两侧**那两条竖着的披发：那里左边是背景、右边是脸，
                    Telea 会沿着上方的黑头发往下洇成一团黑影（实测），
                    行插值则只从背景取色，填出来就是干净的一片墙。
    avoid         ：额外"不能当取色源"的区域（脸框、要重画的目标剪影）。
                    不加的话，行插值会跨过整张脸去取唇色，脖子上一道粉色横条。
    """
    km = mask.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(4))
    if not (np.asarray(km) > 60).any():
        return img
    if mode == "linear":
        from .mask import _skin_mask
        if avoid is not None:
            # 用几何 avoid 当"不许取色"的区域，**不要**再并上肤色 mask：
            # 粉色衣服常常被肤色阈值误判，一并进 skip 就只剩背景可取了，
            # 衣领上会糊出一片背景灰（实测）。
            sk = np.asarray(avoid) > 60
        else:
            sk = _skin_mask(img)
        out = linear_fill(img, km, skin=sk, skip_dark=True)
        # 残留的暗块（取不到源的角落）交给 Telea 补
        a = np.asarray(out).astype(np.float32)
        lum = a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114
        dark = ((np.asarray(km) > 60) & (lum < 95))
        if dark.sum() > 200:
            out = erase_fill(out, Image.fromarray((dark * 255).astype(np.uint8)), radius=12)
        # 下巴以下（衣领那一段）再横扫几遍：行插值会留下横向的条纹和硬边，
        # 这块本来该是"衣服/皮肤的一片"，抹匀比保结构重要得多。
        if smooth_below is not None:
            arr = np.asarray(out).astype(np.float32)
            mk2 = np.asarray(km) > 60
            mk2[:max(0, int(smooth_below)), :] = False
            for _ in range(3):
                b = cv2.blur(arr.astype(np.uint8), (41, 1)).astype(np.float32)
                arr[mk2] = b[mk2]
            out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        return out.filter(ImageFilter.GaussianBlur(2.0))
    out = erase_fill(img, km, radius=radius)
    arr = np.asarray(out).astype(np.float32)
    mk2 = np.asarray(km) > 60
    for _ in range(rounds):
        b = cv2.blur(arr.astype(np.uint8), (45, 1)).astype(np.float32)
        arr[mk2] = b[mk2]
    out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    return out.filter(ImageFilter.GaussianBlur(1.2))


def cut_hair(img: Image.Image, hair: Image.Image, y_line: int, feather=8,
             fh_split=16):
    """几何剪短：把 y_line 以下的头发整段换成背景/衣领。

    长度由 y_line 精确决定，**不依赖模型理解"short"** —— SD1.5 用 prompt 控制长度
    十次里只有两三次听话（实测 8 张里 2 张短），业务上没法用。先几何剪到位，
    再让模型只修发尾那一小条，长度就锁死了。

    返回 (剪短后的底图, 需要模型重画的区域)。
    """
    a = np.asarray(hair) > 100
    kill = a.copy()
    kill[:max(0, y_line - int(feather))] = False
    if not kill.any():
        return img, Image.new("L", img.size, 0)
    km = Image.fromarray((kill * 255).astype(np.uint8))
    out = erase_region(img, km, radius=14, rounds=4)
    # 交给模型重画的区域 = 被抹掉的旧头发（含脖子/衣领那一段）+ 发尾过渡带
    patch = Image.fromarray((kill * 255).astype(np.uint8))
    patch = patch.filter(ImageFilter.MaxFilter(21)).filter(ImageFilter.GaussianBlur(6))
    return out, patch


def tail_band(hair: Image.Image, y_line: int, fh: float, grow=22) -> Image.Image:
    """发尾带：只把这一条交给模型去修自然，别的区域一律不动。"""
    W, H = hair.size
    band = np.zeros((H, W), np.uint8)
    y0 = max(0, int(y_line - fh * 0.12))
    y1 = min(H, int(y_line + fh * 0.22))   # 下沿要盖住 cut_hair 抹出来的过渡带，不然下巴下面一条灰影
    band[y0:y1, :] = 255
    k = int(grow) | 1                     # MaxFilter 只吃奇数
    hb = np.asarray(hair.filter(ImageFilter.MaxFilter(max(3, k))))
    return Image.fromarray(np.minimum(band, hb))


def erase_fill(im: Image.Image, mask: Image.Image, radius=9) -> Image.Image:
    """「剪短」第一步：把旧长发直接填成背景/脖子/衣领，得到一张"没有长发"的底图。

    为什么不用模型抹：SD1.5 inpainting 拿 "buzz cut" 去重画整块长发，出来经常是
    寸头 + 两条长鬓角（脸两侧那撮它认为是"背面的头发"），或者干脆原样不动。
    几何填充是确定性的、0 秒、绝不残留 —— 底图干净了，第二段生成才不会把长度补回去。
    """
    a = np.asarray(im.convert("RGB"))
    m = (np.asarray(mask) > 100).astype(np.uint8)
    if int(m.sum()) < 50:
        return im
    if os.environ.get("ERASE_MODE", "telea") == "linear":
        from .mask import _skin_mask
        return linear_fill(im, mask, skin=_skin_mask(im)).filter(ImageFilter.GaussianBlur(2))
    bgr = cv2.cvtColor(a, cv2.COLOR_RGB2BGR)
    out = cv2.inpaint(bgr, m, int(radius), cv2.INPAINT_TELEA)
    return Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))


def hair_ratio_below(im: Image.Image, y_line: int) -> float:
    """目标发尾线以下还留着多少"头发色的像素"。剪短时用它挑候选，越小越像剪短了。"""
    g = np.asarray(im.convert("L")).astype(np.float32)
    if y_line >= g.shape[0] - 8:
        return 0.0
    below = g[y_line:, :]
    bg = float(np.percentile(g, 85))
    return float((below < bg - 45).mean())


def _tint_ab(gen: Image.Image, target_lab=None, upper=0.62) -> Image.Image:
    """只动 a/b（发色），不动 L。剪短时 L 必须信生成图 —— 用原长发区域统计会压成黑纱。"""
    if target_lab is None:
        return gen
    g = _lab(gen)
    L = g[..., 0]
    h, w = L.shape
    bg = float(np.percentile(L, 88))
    hm = L < (bg - 45.0)
    hm[int(h * upper):, :] = False
    if hm.sum() < w * h * 0.008:
        return gen
    out = g.copy()
    for c in (1, 2):
        gm = float(g[..., c][hm].mean())
        out[..., c] = g[..., c] + (target_lab[c] - gm) * 0.85
    return _from_lab(out)


def _bg_align(gen: Image.Image, src: Image.Image, m: Image.Image) -> Image.Image:
    """把生成图里面那层"重画过的背景"拉回原图的亮度和色温，消掉头周围一圈灰环。

    只在 mask 内、且偏亮（=背景）的像素上统计差值，再按平滑权重叠回去：
    直接整图平移会把脸也染黄（踩过），所以权重跟着 mask 走、边界自然收敛到 0。
    """
    mi = np.asarray(m) > 96
    if mi.sum() < 5000:
        return gen
    g, s = _lab(gen), _lab(src)
    L = g[..., 0]
    bg = mi & (L > np.percentile(L[mi], 55))
    if bg.sum() < 2000:
        return gen
    d = [float((s[..., c][bg] - g[..., c][bg]).mean()) for c in range(3)]
    h, w = L.shape
    k = cv2.GaussianBlur(mi.astype(np.float32), (0, 0), max(3.0, w / 60.0))
    k = np.clip(k, 0.0, 1.0)
    out = g.copy()
    out[..., 0] = g[..., 0] + d[0] * 0.9 * k
    out[..., 1] = g[..., 1] + d[1] * 0.55 * k
    out[..., 2] = g[..., 2] + d[2] * 0.55 * k
    return _from_lab(out)


def composite_block(orig: Image.Image, gen_crop: Image.Image, rect, feather=None,
                    target_lab=None, pad=None, side=None) -> Image.Image:
    """云端整头编辑（gemini / gpt-image / apimart）专用：整块贴回，只羽化四边。

    为什么不用 composite()：那条路按"只贴暗像素=头发"筛选，云端模型重画的是
    **整颗头 + 背景 + 衣服**，筛完只剩一撮头发，脸和脖子还留着原图 —— 出来就是
    "头发换了、脖子还是旧的"，或者露出灰块。云端模型构图与人脸保持得很好，
    整块替换才是对的做法，接缝靠边缘羽化消掉。

    pad/side：送模型的图是「原图 + 补边成正方形」。这里先把补的边裁掉，
    再贴回原图位置 —— 不然补边那圈白条会被一起贴进去，或者只能贴中间一块、
    四周留着旧图（头顶浮一团头发、底下留一条旧水印，就是这么来的）。
    """
    x0, y0, w, h = rect
    gen = gen_crop.convert("RGB")
    if pad and side:
        pl, pt, pr, pb = pad
        gw, gh = gen.size
        box = (pl / float(side) * gw, pt / float(side) * gh,
               (side - pr) / float(side) * gw, (side - pb) / float(side) * gh)
        # 往里缩几像素：补边那一圈被模型画成浅色过渡，缩掉就看不到白框
        k = max(2, int(min(gw, gh) * 0.012))
        l, t, r, b = (int(round(v)) for v in box)
        gen = gen.crop((l + k, t + k, r - k, b - k))
    if gen.size != (w, h):
        gen = gen.resize((w, h), Image.LANCZOS)
    src = orig.crop((x0, y0, x0 + w, y0 + h))
    if target_lab is not None:
        gen = _tint_ab(gen, target_lab, upper=0.72)

    # 羽化带必须窄：带一宽，边上原图残留的头髮/文字就会半透明地透出来（"头顶浮一团鬼影"）。
    # 8~14px 足够盖住 1px 级错位，又不会把原图内容掺进来。
    f = feather or max(6, min(14, int(min(w, h) * 0.016)))
    yy, xx = np.mgrid[0:h, 0:w]
    d = np.minimum(np.minimum(xx, w - 1 - xx), np.minimum(yy, h - 1 - yy)).astype(np.float32)
    a = (np.clip(d / float(f), 0.0, 1.0) * 255).astype(np.uint8)
    alpha = Image.fromarray(a).filter(ImageFilter.GaussianBlur(max(1.0, f / 2.0)))

    out = orig.copy()
    out.paste(Image.composite(gen, src, alpha), (x0, y0))
    # 只对头肩那块锐化，别把整张图锐出塑料感
    region = out.crop((x0, y0, x0 + w, y0 + h)).filter(
        ImageFilter.UnsharpMask(radius=1.3, percent=35, threshold=4))
    out.paste(Image.composite(region, src, alpha), (x0, y0))
    return out


def composite_patch(orig: Image.Image, gen_crop: Image.Image, rect, base_img: Image.Image,
                    patch_mask: Image.Image, target_lab=None) -> Image.Image:
    """只在 patch_mask（发尾那一条）里贴模型结果，其余全部保留 base_img。

    剪短的几何部分（cut_hair）已经把长度锁死了，模型只负责把"刀切发尾"改成自然发尾。
    如果整块贴，模型手一抖又把长度补回去 —— 那就白剪了。
    """
    x0, y0, w, h = rect
    if base_img.size != orig.size:      # cut_hair 是 RENDER_SIZE 的，原图可能是别的尺寸
        base_img = base_img.resize(orig.size, Image.LANCZOS)
    gen = gen_crop.resize((w, h), Image.LANCZOS)
    base = base_img.crop((x0, y0, x0 + w, y0 + h))
    gen = _tint_ab(gen, target_lab)
    a = patch_mask.resize((w, h), Image.LANCZOS).filter(
        ImageFilter.GaussianBlur(max(4, w // 110)))
    out = base_img.copy()          # 底必须是"已剪短"的那张，patch 之外才不会再冒出长发
    out.paste(Image.composite(gen, base, a), (x0, y0))
    reg = out.crop((x0, y0, x0 + w, y0 + h)).filter(
        ImageFilter.UnsharpMask(radius=1.4, percent=40, threshold=3))
    out.paste(Image.composite(reg, base, a), (x0, y0))
    return out


def composite_short(orig: Image.Image, gen_crop: Image.Image, rect, base_img: Image.Image,
                    mask_crop: Image.Image, target_lab=None) -> Image.Image:
    """剪短专用合成：整块替换 mask 区（旧长发位置必须被顶掉），
    但先把 mask 内的背景亮度对齐原图，避免头周围那圈灰晕。

    base_img 保留参数（现在不直接用），留作以后做"只贴新头发"的实验口子。
    """
    x0, y0, w, h = rect
    gen = gen_crop.resize((w, h), Image.LANCZOS)
    src = orig.crop((x0, y0, x0 + w, y0 + h))
    m = mask_crop.resize((w, h), Image.LANCZOS).filter(
        ImageFilter.GaussianBlur(max(5, w // 90)))

    gen = _tint_ab(gen, target_lab)
    gen = _bg_align(gen, src, m)

    out = orig.copy()
    out.paste(Image.composite(gen, src, m), (x0, y0))
    reg = out.crop((x0, y0, x0 + w, y0 + h)).filter(
        ImageFilter.UnsharpMask(radius=1.4, percent=45, threshold=3))
    out.paste(Image.composite(reg, src, m), (x0, y0))
    return out


def composite_replace(orig: Image.Image, gen_crop: Image.Image, rect, target_lab=None,
                      tweak_l=True) -> Image.Image:
    """「剪短」用：整块替换，不做 L 迁移。

    剪短时生成图里原来的长发位置已经变成背景/衣领，还用原图头发区域统计亮度去压，
    整张脸和背景一起被压成黑纱（踩过，输出像盖了层黑网）。这里只动发色（a/b），
    L 完全相信生成图 —— 它的曝光本来就来自原图（第一阶段在原图上做的）。
    """
    x0, y0, w, h = rect
    gen = gen_crop.resize((w, h), Image.LANCZOS)
    if target_lab is not None or tweak_l:
        g = _lab(gen)
        L = g[..., 0]
        # 头发 = 比这张图整体亮度明显暗的像素（背景亮、衣服暗，取中间偏暗一大截的那批）
        bg_l = float(np.percentile(L, 88))
        hm = L < (bg_l - 45.0)
        # 只看上半部分（头），别把深色衣服算进发色
        hm[int(h * 0.62):, :] = False
        if hm.sum() > w * h * 0.01:
            out = g.copy()
            for c in (1, 2):
                gm = float(g[..., c][hm].mean())
                tm = target_lab[c] if target_lab is not None else gm
                out[..., c] = g[..., c] + (tm - gm) * (0.85 if target_lab is not None else 0.0)
            gen = _from_lab(out)
    out = orig.copy()
    out.paste(gen, (x0, y0))
    if w * h > 0:
        reg = out.crop((x0, y0, x0 + w, y0 + h)).filter(
            ImageFilter.UnsharpMask(radius=1.4, percent=45, threshold=3))
        out.paste(reg, (x0, y0))
    return out


def composite(orig: Image.Image, gen_crop: Image.Image, rect, mask_crop: Image.Image,
              target_lab=None, stats_mask=None, feather=None, full=False) -> Image.Image:
    """gen_crop: 与 rect 同尺寸的生成结果；mask_crop: 同尺寸羽化 mask（用紧贴发丝的那张，
    不要用给模型看的膨胀 mask —— 否则背景上会留一圈"头发光晕"）。"""
    x0, y0, w, h = rect
    gen = gen_crop.resize((w, h), Image.LANCZOS)
    src = orig.crop((x0, y0, x0 + w, y0 + h))
    m = mask_crop.resize((w, h), Image.LANCZOS).filter(
        ImageFilter.GaussianBlur(feather if feather else max(2, w // 200)))

    # 调色统计只用「原图真有头发」的那块（stats_mask）。
    # 用整块目标区域统计会把背景灰度算进发色，新长出来的头发会被提亮成灰白。
    stats = m
    if stats_mask is not None:
        cand = stats_mask.resize((w, h), Image.LANCZOS)
        if (np.asarray(cand) > 128).sum() > (w * h * 0.01):
            stats = cand
    gen = match_color(gen, src, stats, target_lab=target_lab)

    # 贴之前把"背景像素"从 mask 里剔掉（否则新长出来的头发外面套一圈亮光晕）
    if full:
        # "剪短"：原来的长发必须被整块替换掉（包括新露出来的背景），
        # 这时不能再按"只贴暗像素"筛，否则旧长发会留在原地 —— 看起来就像没剪。
        # 但整块贴会把模型重画的背景也贴上来（背景偏灰 → 头周围一圈灰晕），
        # 所以先把 mask 里的**背景部分**亮度对齐到原图背景。
        # 背景对齐只在整块都是背景时靠谱；mask 里还夹着额头/脸颊，按均值移会整片发黄，
        # 所以默认不开（COMFY_FULL_BG=1 可试）。宁留一点灰圈，不要黄脸。
        if os.environ.get("COMPOSE_FULL_BG") == "1":
            gen = _match_bg(gen, src, m, stats.resize((w, h), Image.LANCZOS))
        alpha = m
        out = orig.copy()
        out.paste(Image.composite(gen, src, alpha), (x0, y0))
        region = out.crop((x0, y0, x0 + w, y0 + h)).filter(
            ImageFilter.UnsharpMask(radius=1.6, percent=60, threshold=3))
        out.paste(Image.composite(region, src, alpha), (x0, y0))
        return out

    ms = np.asarray(stats.resize((w, h), Image.LANCZOS)) > 128
    hair_l = float(_lab(gen)[..., 0][ms].mean()) if ms.sum() > 100 else 60.0
    a = hair_only_alpha(gen, m, hair_l)
    # 彩色伪影门限：SD1.5 在 mask 里偶尔会冒出饱和色块（实测蓝斑糊在额头上），
    # 它比背景暗、于是被 hair_only_alpha 当成头发贴上去了。用"生成图自己那片发区
    # 的中位色"当基准，LAB 距离过大的像素一律不贴 —— 发色整体变了也不受影响。
    if ms.sum() > 200:
        gl = _lab(gen)
        ref = np.median(gl[ms], axis=0)
        d = np.sqrt(((gl - ref.reshape(1, 1, 3)) ** 2).sum(-1))
        a = a * np.clip((52.0 - d) / 18.0, 0.0, 1.0)
    alpha = Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8))
    alpha = _drop_islands(alpha, stats.resize((w, h), Image.LANCZOS), w, h)
    # 收 1px 再羽化：生成图的新发边缘常带一圈亮边，不收掉就是"白描边"
    alpha = alpha.filter(ImageFilter.MinFilter(3)).filter(
        ImageFilter.GaussianBlur(max(1, w // 400)))

    out = orig.copy()
    out.paste(Image.composite(gen, src, alpha), (x0, y0))

    # 只锐化头区，别把整张脸锐化出塑料感
    region = out.crop((x0, y0, x0 + w, y0 + h)).filter(
        ImageFilter.UnsharpMask(radius=1.6, percent=60, threshold=3))
    out.paste(Image.composite(region, src, alpha), (x0, y0))
    return out


def side_by_side(a: Image.Image, b: Image.Image, label=("原图", "效果")) -> Image.Image:
    h = min(a.height, b.height, 1024)
    def fit(im):
        k = h / im.height
        return im.resize((int(im.width * k), h), Image.LANCZOS)
    a, b = fit(a), fit(b)
    canvas = Image.new("RGB", (a.width + b.width, h), (255, 255, 255))
    canvas.paste(a, (0, 0))
    canvas.paste(b, (a.width, 0))
    return canvas
