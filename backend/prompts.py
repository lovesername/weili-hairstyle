# -*- coding: utf-8 -*-
"""Prompt 构造：效果好不好，一半在 prompt 的"不许动什么"。

核心约束（云端 image-edit 大模型路线最关键）：
  1. 只改头发，五官/肤色/表情/光照/背景/姿势一律不动 —— 否则出图"像假人"
  2. 明确"保留发际线"，不然头发和额头会糊在一起
  3. 明确"不要戴帽子/假发套感"，直接把它变成理发效果而不是套头套
"""

GUARD_ZH = (
    "保持人物身份完全不变：五官形状、眼睛、鼻子、嘴巴、肤色、表情、光照方向、背景、服装、姿势全部保持不变，"
    "只替换头发。保留原有发际线位置与额头、耳朵、鬓角的皮肤边缘，头发要自然生长在头皮上，"
    "发丝有真实质感与光泽，与头型贴合，不要出现帽子感、发套感、贴片感。输出与原图同尺寸、同构图的照片级真实图像。"
)

GUARD_EN = (
    "keep the exact same person, same facial features, eyes, nose, mouth, skin tone, expression, lighting, "
    "background, clothing and pose; change ONLY the hair. preserve the original hairline, forehead, ears and "
    "sideburn skin edges. hair must look naturally grown from the scalp with realistic strand texture and "
    "specular highlights, fitting the head shape. no hat, no wig-cap look, no pasted-on edges. "
    "photorealistic, same framing and size as input."
)

NEGATIVE_EN = (
    "different person, altered face, changed eyes, changed skin tone, changed expression, changed background, "
    "hat, wig cap, helmet, bald patches, blurred hairline, plastic hair, cartoon, cgi, deformed ears, "
    "extra fingers, watermark, text"
)


def _fields(style):
    """兼容内置元组 (id,name,name_en,tags,zh,en) 与外部 dict。"""
    if isinstance(style, dict):
        return style.get("prompt_zh", ""), style.get("prompt_en", ""), style.get("name", "")
    return style[4], style[5], style[1]


# 产品库的文案是"卖假发"的：wig / lace topper base / hairpiece 这些都是电商词，
# 直接喂给模型会真的画出一顶假发，或者被负向词"wig"打架。入库前先洗一遍。
_RETAIL = (
    ("thin lace topper base", ""), ("lace topper base", ""), ("lace front", ""),
    ("hairpiece", "hairstyle"), ("hair piece", "hairstyle"), ("topper", ""),
    ("human hair wig", "hair"), ("full lace wig", "hair"), ("wig", "hairstyle"),
    ("发片", "发型"), ("假发", "发型"), ("补发", "发型"),
)


def _clean(text: str) -> str:
    out = text or ""
    for a, b in _RETAIL:
        out = out.replace(a, b)
    while ", ," in out or ",," in out:
        out = out.replace(", ,", ",").replace(",,", ",")
    out = " ".join(out.split())
    return out.replace(" ,", ",").strip(" ,.")


def build(style, lang="zh"):
    zh, en, _ = _fields(style)
    if lang == "en":
        return f"{_clean(en)}. {GUARD_EN}"
    return f"把照片中人物的发型改成：{_clean(zh)}。{GUARD_ZH}"


def build_neutral(lang="zh"):
    """让用户只上传参考图时用：以参考图的发型为准。"""
    if lang == "en":
        return f"replace the hair with the hairstyle in the reference image. {GUARD_EN}"
    return f"以参考图中的发型为准替换照片中人物的头发。{GUARD_ZH}"


# ---------------- 云端图像大模型：整头重画（gemini / gpt-image / apimart） ----------------
# 这一档的成败几乎全在「图1/图2 是谁」和「照抄哪几项」：
#  - 送图顺序必须和文案里的图1/图2 一致（apimart 曾经反着送 → 出来是参考图那个人的
#    衣服和背景 + 用户自己的发型，用户一眼就知道"完全不对版"）；
#  - 款式文案是电商站的规格文本（"齐耳到下颌…顺直无卷度"），经常和主图上的真发不一样
#    （同一个商品有 15/20/30cm 多个规格，文案写一个、图拍另一个）。文案一掺进去，
#    模型就照着字画，把图1 的长卷发刘海全丢了。所以有参考图时**一个字文案都不喂**。
EDIT_ZH = (
    "图1 是发型参考（商品实拍，里面是另一个人）；图2 是要换发型的人。\n"
    "任务：把图2 这个人的头发整个换成图1 的头发。只换头发，别的一律不动。\n"
    "必须逐项照抄图1，抄错一项就算失败：\n"
    "1. 长度：图1 的头发到下巴/脖子/肩膀/锁骨/胸口就画到哪，不许自己改长改短；\n"
    "2. 卷度：图1 是直发就直发，是波浪就波浪，是小卷/羊毛卷就照着小卷画，卷的大小和密度一致；\n"
    "3. 刘海：图1 有刘海就必须长出一样的刘海（齐眉直刘海 / 空气刘海 / 侧分），"
    "图1 没有刘海就让额头露出来；\n"
    "4. 发量、蓬松度、分缝位置、发色、光泽，全部和图1 一致。\n"
    "不要把图1 那个人的东西带过来：她的脸、五官、肤色、妆容、表情、衣服、首饰、背景、光线、姿势。"
    "图2 的这些人像属性必须保持原样，逐像素一致。\n"
    "原来的发型不能残留：不能一边留着原来的披发/半扎，一边又叠一层新发型。"
    "新头发要自然长在头皮上，发际线、鬓角、耳后衔接自然；脖子和肩膀该露出来的地方照原样露出来。\n"
    "整张照片不要出现任何文字、水印、标题、标签、色块或边框，任何字都不要写。\n"
    "输出真实人像照片质感，发丝清晰有光泽，不要插画感、不要过度磨皮、不要美颜滤镜。"
)

EDIT_EN = (
    "Image 1 is the hairstyle reference (a product photo of a different person); image 2 is the person "
    "whose hair will be changed.\n"
    "Task: replace the hair of the person in image 2 with the hair from image 1. Hair only, nothing else.\n"
    "Copy these from image 1 exactly - missing any of them is a failure://n"
    "1. length: chin / neck / shoulder / collarbone / chest - whatever image 1 shows, do not shorten or "
    "lengthen it;\n"
    "2. curl pattern: straight stays straight, waves stay waves, tight curls stay tight curls, same size and "
    "density;\n"
    "3. bangs: if image 1 has bangs, draw the same bangs (blunt, see-through, side-swept); if image 1 has no "
    "bangs, keep the forehead bare;\n"
    "4. density, volume, parting line, hair color and shine must match image 1.\n"
    "Do NOT bring over from image 1: that person's face, features, skin tone, makeup, expression, clothing, "
    "jewelry, background, lighting, pose. Those belong to image 2 and must stay pixel-identical.\n"
    "No remnant of the old hairstyle (do not keep the old loose hair or half-up style underneath the new one). "
    "The new hair must sit naturally on the scalp with clean hairline, sideburns and behind the ears.\n"
    "No text, watermark, caption, label, colored band or frame anywhere in the image.\n"
    "Photorealistic portrait, sharp individual strands, no illustration look, no beauty-filter skin."
)

# 没有参考图时（用户只选了个文字款）才退回文案驱动
EDIT_TEXT_ZH = (
    "把图2 这个人的头发整个换成这个发型：{TGT}。\n"
    "必须真的替换，不是叠加：先去掉原来的头发，再按这个发型的长度、卷度、刘海、发量、发色重新长一遍，"
    "原来的发型不能残留。\n"
    "以下一律不动，和原图逐像素一致：脸型、五官、眼睛、鼻子、嘴、表情、肤色、耳朵、脖颈、锁骨、"
    "衣服、首饰、背景、光线、机位、构图、画幅比例。\n"
    "新头发要自然生长在头皮上，发际线与额头、鬓角、耳后衔接自然。\n"
    "整张照片不要出现任何文字、水印、标题、标签、色块或边框，任何字都不要写。\n"
    "输出真实人像照片质感，发丝清晰有光泽，不要插画感、不要过度磨皮、不要美颜滤镜。"
)
EDIT_TEXT_EN = (
    "Replace the hair of the person in image 2 with this hairstyle: {TGT}.\n"
    "It must be a real replacement, not an addition. Everything else (face, features, skin, expression, "
    "clothing, background, lighting, framing) must stay pixel-identical.\n"
    "No text, watermark, caption, label, colored band or frame anywhere in the image.\n"
    "Photorealistic portrait, sharp individual strands."
)


# ---- 换发色：客户在控制台选的色号 ----
# 色号只做"颜色"这一件事。写太松模型会顺手把长度/卷度也重画一遍（换色变成换发型），
# 写太死又会把整头压成一坨平色。所以固定三句：全头统一、只改颜色、要真实层次。
COLOR_ZH = (
    "发色（客户本次指定，优先于图1）：把整头头发染成「{C}」，发根到发梢同一个色系，"
    "可以有自然的深浅和光泽，但不能出现色块、断层、一坨死色。"
    "只改颜色，不改形态：长度、卷度、刘海、发量、分缝完全照图1。"
    "不要把发色染到额头、鬓角皮肤、耳朵、衣服和背景上。\n"
)
COLOR_EN = (
    "Hair color (client-specified this time, overrides image 1): color the whole head of hair \"{C}\" "
    "from root to tip in one tonal family, with realistic depth and shine - no flat block, no banding. "
    "Color only, never the shape: length, curl pattern, bangs, density and parting stay exactly as image 1. "
    "Do not tint the forehead, skin, ears, clothing or background.\n"
)

# 色号 → 英文（模型对英文色名更稳；不认的字面值就当色名直接透传）
COLOR_EN_MAP = {
    "自然黑": "natural black",
    "自然色": "natural off-black",
    "深棕": "dark brown",
    "浅棕": "light brown",
    "栗棕": "chestnut brown",
    "酒红": "burgundy red",
    "亚麻金": "flaxen golden blonde",
    "亚麻灰": "ash grey",
    "奶茶棕": "milk-tea brown",
    "冷棕": "cool ash brown",
    "焦糖": "caramel brown",
}


def _color_block(color, lang):
    c = str(color or "").strip()
    if not c:
        return ""
    if lang == "en":
        return COLOR_EN.replace("{C}", COLOR_EN_MAP.get(c, c))
    return COLOR_ZH.replace("{C}", c)


def build_edit(style, lang="zh", has_ref=False, color=""):
    """整头编辑指令。has_ref=True → 图1=发型参考、图2=本人，和 provider 送图顺序强绑定。

    有参考图时**不拼任何款式文案**：电商文案和主图经常不是一套（规格 15/20/30cm 混拍），
    文案会把图上的真发型带跑偏。
    color 非空 → 追加一段换色指令（形态仍然全照图1，只动颜色）。
    """
    if has_ref:
        base = EDIT_EN if lang == "en" else EDIT_ZH
        if str(color or "").strip():
            # 换色时要把「发色照抄图1」这条摘掉，否则同一段里出现两个互相打架的指令，
            # 模型经常折中成一半新色一半旧色。
            base = base.replace("4. 发量、蓬松度、分缝位置、发色、光泽，全部和图1 一致。",
                                "4. 发量、蓬松度、分缝位置、光泽，全部和图1 一致（发色由客户另指定，见最后一段）。")
            base = base.replace("4. density, volume, parting line, hair color and shine must match image 1.",
                                "4. density, volume, parting line and shine must match image 1 (hair color is "
                                "client-specified, see the last paragraph).")
    else:
        zh, en, name = _fields(style)
        tgt = _clean(zh if lang != "en" else en) or name or ""
        tpl = EDIT_TEXT_EN if lang == "en" else EDIT_TEXT_ZH
        base = tpl.replace("{TGT}", tgt.strip("。. "))
    return base + _color_block(color, lang)


def color_choices(style):
    """这款能选哪些色：主色 / 商品可选色 / 通用调色板，去重后给前台做 chip。"""
    out = []
    for c in [style.get("color")] + list(style.get("colors") or []) + list(COLOR_EN_MAP):
        c = str(c or "").strip()
        if c and c not in out:
            out.append(c)
    return out


# ---------------- 剪短：必须显式把"长度"钉死 ----------------
# 款式文案里的 "ear-to-jaw length"、"发片 15cm" 这类词 SD1.5 读不懂，
# 加上 POS_SUFFIX 的发量词，模型默认会往"及肩"画。剪短时两头都要压：
SHORT_POS = "very short hair, above the shoulders, cropped bob, short, "
SHORT_NEG = ("long hair, hair past shoulders, shoulder length hair, hair below the chin, "
             "long strands falling on shoulders, hair on chest, flowing long hair")


def build_short(style, lang="en"):
    """剪短专用：长度词放最前面，权重最高。"""
    return SHORT_POS + build(style, lang)


def build_short_neg(base=""):
    return f"{base}, {SHORT_NEG}" if base else SHORT_NEG


# ---------------- 扎发 / 盘发 ----------------
# 扎发的关键是"头发全部往上走、脖子和肩膀空出来"。这个语义必须写在最前面，
# 否则后面一大串发量/光泽词会把它盖掉，出来还是披发。
# 2026-10-21 补：光有"扎起来"不够，必须显式写「从额头往后梳、发际线露出、
# 没有刘海、两侧收到耳后」—— 否则模型会把原来的侧分刘海原样画回来，
# 头发梳上去了、前面还是披的，看着就像"加了个髻"。
UPDO_HIGH_POS = ("hair pulled up into a high top bun on top of the head, all hair gathered "
                 "up from the scalp, slicked back from the forehead, hairline and forehead "
                 "fully visible, no bangs, no fringe, both sides swept back behind the ears, "
                 "bare neck and shoulders, clean hairline, ")
UPDO_LOW_POS = ("hair pulled back tight and tucked behind the ears into a low bun at the nape, "
                "slicked back from the forehead, hairline and forehead fully visible, no bangs, "
                "smooth crown, no volume at the sides, nothing hanging beside the cheeks, "
                "bare neck and collarbone, no hair on shoulders, ")
UPDO_NEG = ("hair down, loose long hair, bob, wavy bob, loose curls, voluminous sides, "
            "hair falling over the shoulders, shoulder length hair, "
            "hair covering the neck, hair on chest, long strands beside the cheeks, "
            "hair hanging down, bangs, fringe, hair covering forehead, hair over the ears, "
            "wig, halo, blur, two hairstyles, hair bun floating above the head")

# 扎发第一步：先把扎点以下的旧长发清成「脖子/衣领/背景」。
# 模型画头发很积极，所以这一步的 prompt 必须只谈皮肤和衣服，一个字都不提头发。
UPDO_CLEAN_POS = ("bare neck and collarbone skin, keep the same clothes and collar unchanged, "
                  "same background, natural skin tone, no hair on the neck, "
                  "no long hair on the shoulders, photorealistic portrait")
UPDO_CLEAN_NEG = ("long hair, shoulder length hair, hair on shoulders, strands on neck, "
                  "hair covering the collar, blur, halo, distorted neck, seam")


def build_updo(style, kind="low", lang="en"):
    """扎发专用：先钉"扎起来/脖子空"，再说具体款式。"""
    pre = UPDO_HIGH_POS if kind == "high" else UPDO_LOW_POS
    return pre + build(style, lang)


def build_updo_neg(base=""):
    return f"{base}, {UPDO_NEG}" if base else UPDO_NEG


# 剪短那一步已经改成 compose.erase_fill()（Telea 确定性填充，0 秒且绝不残留），
# 不再用模型去"抹头发"。下面两个常量保留给想走模型抹除的调试路径。
ERASE_EN = (
    "very short buzz cut, shaved sides, cropped hair above the ears, ears and neck fully visible, "
    "black stubble on scalp"
)

ERASE_NEG = (
    "long hair, shoulder length hair, hair falling on shoulders, hair strands over neck, "
    "hair covering ears, hair on clothes, fur collar, wig, messy strands, halo, blurry"
)
