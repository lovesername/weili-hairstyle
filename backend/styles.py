# -*- coding: utf-8 -*-
"""发型库：100 款。每款含中文 prompt（云端 image-edit 大模型用）、
英文 prompt（SD/IPAdapter 用）与标签（前端筛选：短发/中长/长发/卷发/扎发/男士/发色）。
"""

STYLES = [
    # ---------- 短发 ----------
    ("s001", "法式碎短发", "French textured short bob", ["短发", "女"],
     "干净利落的法式碎短发，长度到下颌，颧骨处打薄出层次，发尾自然外翻，自然黑发色",
     "clean French textured short bob, jaw length, thinned layers at cheekbone, ends flicked outward, natural black"),
    ("s002", "齐耳Bob", "ear-length bob", ["短发", "女"],
     "齐耳长度内扣Bob，前额八字刘海，发丝顺直有光泽，深棕发色",
     "ear-length inward-curling bob, curtain bangs, straight glossy hair, dark brown"),
    ("s003", "日系空气短发", "Japanese airy short", ["短发", "女"],
     "日系空气感短发，发丝轻盈蓬松，露耳，浅棕发色，刘海薄而碎",
     "Japanese airy short hair, light and fluffy, ears exposed, light brown, wispy bangs"),
    ("s004", "精灵短发", "pixie cut", ["短发", "女"],
     "精灵短发，两侧贴头皮修剪，顶部短层次有纹理感，自然黑",
     "pixie cut, closely cropped sides, short textured top layers, natural black"),
    ("s005", "低层次鲍勃", "low-layer bob", ["短发", "女"],
     "低层次鲍勃头，长度在嘴角，发尾整齐，中分，栗棕色",
     "low-layer bob, mouth-length, blunt ends, center part, chestnut brown"),
    ("s006", "港风碎短", "Hong Kong layered short", ["短发", "女"],
     "复古港风碎短发，层次高，发梢微卷外翘，深黑发色，蓬松",
     "retro Hong Kong layered short hair, high layers, slightly curled outward tips, jet black, voluminous"),
    ("s007", "侧分短波波", "side-part short bob", ["短发", "女"],
     "三七侧分短波波，一侧别在耳后，另一侧盖住脸颊，自然黑",
     "side-parted short bob, one side behind ear, other side covering cheek, natural black"),
    ("s008", "蛋卷短发", "egg-roll short perm", ["短发", "卷发", "女"],
     "蛋卷短发，S形小卷均匀排列，长度到下颌，深棕",
     "egg-roll short perm, even S-shaped small curls, jaw length, dark brown"),
    ("s009", "内扣短Bob", "inward bob", ["短发", "女"],
     "内扣短Bob，发尾向颈部内卷，齐刘海，深黑发色",
     "inward-curling short bob, ends curling toward neck, blunt bangs, jet black"),
    ("s010", "锁骨发", "collarbone lob", ["中长", "女"],
     "锁骨长度Lob，发尾微外翻，中分八字刘海，浅栗色",
     "collarbone-length lob, slightly flicked ends, center-parted curtain bangs, light chestnut"),
    ("s011", "齐肩层次发", "shoulder layered hair", ["中长", "女"],
     "齐肩层次发，脸颊两侧有修饰脸型的层次，自然黑",
     "shoulder-length layered hair, face-framing layers, natural black"),
    ("s012", "锁骨微卷", "collarbone wavy lob", ["中长", "卷发", "女"],
     "锁骨长度微波浪卷，卷度松散自然，深棕发色，侧分",
     "collarbone wavy lob, loose natural waves, dark brown, side part"),
    ("s013", "法式慵懒中长", "French effortless mid-length", ["中长", "卷发", "女"],
     "法式慵懒中长发，不规则大波浪，发丝有空气感，栗棕",
     "French effortless mid-length, irregular large waves, airy texture, chestnut brown"),
    ("s014", "翻翘Lob", "flipped lob", ["中长", "女"],
     "发尾外翻的Lob，长度过颌，造型感强，浅棕",
     "flipped lob, below-jaw length, strong styling, light brown"),
    ("s015", "锁骨羊毛卷", "collarbone wool curls", ["中长", "卷发", "女"],
     "锁骨长度羊毛卷，小卷密集蓬松，刘海同卷度，深棕",
     "collarbone-length wool curls, dense fluffy small curls, curly bangs, dark brown"),
    ("s016", "层次中长直发", "layered mid-length straight", ["中长", "女"],
     "层次中长直发，发尾打薄，自然垂顺，深黑",
     "layered mid-length straight hair, thinned ends, natural fall, deep black"),

    # ---------- 长发 ----------
    ("s017", "黑长直", "long straight black hair", ["长发", "女"],
     "及腰黑长直，中分，发丝顺直有光泽，自然黑",
     "waist-length straight black hair, center part, glossy sleek, natural black"),
    ("s018", "大波浪长发", "long big waves", ["长发", "卷发", "女"],
     "及胸大波浪长发，卷度饱满，侧分，深棕色",
     "chest-length big waves, full curls, side part, dark brown"),
    ("s019", "空气感长卷", "airy long curls", ["长发", "卷发", "女"],
     "空气感长卷发，卷度松而不散，发尾轻盈，栗棕",
     "airy long curls, loose waves, light ends, chestnut brown"),
    ("s020", "公主切长发", "hime-cut long hair", ["长发", "女"],
     "公主切长发，两侧齐颌直线发束修饰脸型，后发及腰，黑发",
     "hime-cut long hair, blunt jaw-level side locks, waist-length back, black"),
    ("s021", "及腰微卷", "waist-length soft curls", ["长发", "卷发", "女"],
     "及腰微卷长发，从下三分之一开始有卷度，深黑",
     "waist-length soft curls from lower third, deep black"),
    ("s022", "层次长直", "long layered straight", ["长发", "女"],
     "层次感长直发，脸颊两侧短发层次修饰，自然黑",
     "long layered straight hair, short face-framing layers, natural black"),
    ("s023", "复古大卷", "retro big curls", ["长发", "卷发", "女"],
     "复古大卷长发，卷度规则饱满，发量蓬松，深棕",
     "retro big curls, regular full curls, voluminous, dark brown"),
    ("s024", "水波纹长发", "water-wave long hair", ["长发", "卷发", "女"],
     "水波纹长发，波纹均匀柔和，中分，浅棕",
     "water-wave long hair, even soft waves, center part, light brown"),
    ("s025", "湿发感长直", "wet-look long straight", ["长发", "女"],
     "湿发质感长直发，发丝成束，高光泽，深黑",
     "wet-look long straight hair, clumped strands, high shine, deep black"),

    # ---------- 卷发 ----------
    ("s026", "法式卷", "French curls", ["卷发", "中长", "女"],
     "法式卷，卷度从耳下开始，松散不规整，深棕",
     "French curls starting below ears, loose irregular, dark brown"),
    ("s027", "绵羊卷", "sheep curls", ["卷发", "中长", "女"],
     "绵羊卷，小而密的卷，蓬松有体积感，黑发",
     "sheep curls, small dense curls, voluminous, black"),
    ("s028", "蛋卷长发", "egg-roll long perm", ["卷发", "长发", "女"],
     "蛋卷长发，S形卷整齐排列，中分，深棕",
     "egg-roll long perm, even S-curls, center part, dark brown"),
    ("s029", "空气刘海卷发", "curls with airy bangs", ["卷发", "中长", "女"],
     "空气刘海配中长卷发，卷度柔和，浅棕",
     "mid-length curls with airy bangs, soft waves, light brown"),
    ("s030", "螺旋卷", "spiral curls", ["卷发", "长发", "女"],
     "螺旋卷长发，卷筒清晰，发量蓬松，深棕",
     "spiral curls, defined ringlets, voluminous, dark brown"),
    ("s031", "外翻卷中长", "outward curled mid-length", ["卷发", "中长", "女"],
     "中长发，发尾统一外翻卷，层次分明，栗色",
     "mid-length hair with uniform outward curls, clear layers, chestnut"),
    ("s032", "慵懒大卷", "lazy big waves", ["卷发", "中长", "女"],
     "慵懒大卷，卷度大而松，随手感，自然黑",
     "lazy big waves, large loose curls, effortless, natural black"),
    ("s033", "羊毛卷中长", "wool curls mid-length", ["卷发", "中长", "女"],
     "羊毛卷中长发，小卷密集，蓬松，深棕",
     "wool curls mid-length, dense small curls, fluffy, dark brown"),
    ("s034", "云朵卷", "cloud curls", ["卷发", "长发", "女"],
     "云朵卷长发，卷度柔软蓬松像云，浅棕",
     "cloud curls, soft fluffy waves, light brown"),
    ("s035", "复古烫短发", "retro perm short", ["卷发", "短发", "女"],
     "复古烫短发，卷度紧实有弹性，蓬松，黑发",
     "retro perm short hair, tight bouncy curls, voluminous, black"),

    # ---------- 扎发 ----------
    ("s036", "高马尾", "high ponytail", ["扎发", "女"],
     "利落高马尾，头顶蓬起，发尾顺直，自然黑",
     "sleek high ponytail, lifted crown, straight ends, natural black"),
    ("s037", "低马尾", "low ponytail", ["扎发", "女"],
     "低马尾，中分，脸颊两侧留碎发，深棕",
     "low ponytail, center part, loose face-framing strands, dark brown"),
    ("s038", "丸子头", "top bun", ["扎发", "女"],
     "高丸子头，发丝整齐收紧，自然黑",
     "high top bun, neatly pulled back, natural black"),
    ("s039", "半丸子头", "half-up bun", ["扎发", "女"],
     "半丸子头，上半扎起下半披落，发丝松散，深棕",
     "half-up bun, loose texture, dark brown"),
    ("s040", "侧编发", "side braid", ["扎发", "女"],
     "侧边松散麻花辫，发丝有空气感，栗棕",
     "loose side braid, airy texture, chestnut brown"),
    ("s041", "法式低盘发", "French low updo", ["扎发", "女"],
     "法式低盘发，颈后松散发髻，两侧留碎发，深黑",
     "French low updo, loose chignon at nape, face-framing strands, deep black"),
    ("s042", "双麻花辫", "twin braids", ["扎发", "女"],
     "双麻花辫垂在胸前，辫子松散自然，深棕",
     "twin braids over the chest, loose natural braids, dark brown"),
    ("s043", "高马尾卷尾", "high ponytail with curls", ["扎发", "卷发", "女"],
     "高马尾，马尾部分带大卷，头顶蓬松，深棕",
     "high ponytail with big curls in the tail, voluminous crown, dark brown"),

    # ---------- 发色 ----------
    ("s044", "亚麻浅金", "flaxen blonde", ["发色", "长发", "女"],
     "及胸长直发，亚麻浅金色，发丝通透有光泽",
     "chest-length straight hair, flaxen light blonde, translucent glossy strands"),
    ("s045", "栗棕", "chestnut brown", ["发色", "长发", "女"],
     "栗棕色长卷发，卷度自然，发尾内扣，及胸",
     "chestnut brown long curls, natural waves, inward ends, chest length"),
    ("s046", "红棕", "auburn red-brown", ["发色", "长发", "女"],
     "红棕色长发，偏暗调，微卷，及胸",
     "auburn red-brown long hair, slightly wavy, chest length"),
    ("s047", "奶茶棕", "milk-tea brown", ["发色", "长发", "女"],
     "奶茶棕长直发，中分，发丝顺滑，及肩以下",
     "milk-tea brown long straight hair, center part, sleek, below shoulder"),
    ("s048", "灰棕", "ash brown", ["发色", "长发", "女"],
     "灰棕色长发，冷调，微波浪，蓬松",
     "ash brown long hair, cool tone, soft waves, airy"),
    ("s049", "渐层挑染", "balayage", ["发色", "长发", "女"],
     "深发根渐层挑染长发，发尾浅金，大波浪",
     "balayage long hair, dark roots fading to light gold ends, big waves"),
    ("s050", "酒红", "burgundy", ["发色", "长发", "女"],
     "酒红色长发，微卷，及胸，发丝有光泽",
     "burgundy long hair, slightly wavy, chest length, glossy"),
    ("s051", "青木亚麻", "ash green", ["发色", "长发", "女"],
     "青木亚麻色长发，冷调灰绿，直发，及胸",
     "ash green long hair, cool grey-green, straight, chest length"),
    ("s052", "深棕内扣", "dark brown inward bob", ["发色", "短发", "女"],
     "深棕色内扣短Bob，齐刘海，发丝顺滑",
     "dark brown inward short bob, blunt bangs, sleek"),
    ("s053", "黑茶色", "black-tea brown", ["发色", "中长", "女"],
     "黑茶色中长发，几乎看不出染，光线下发暖，自然垂落",
     "black-tea brown mid-length hair, subtle warm tone in light, natural fall"),

    # ---------- 男士 ----------
    ("s054", "男士三七分", "men side part", ["男士"],
     "男士三七分短发，发丝有纹理，露出额头，自然黑",
     "men side-part short hair, textured, forehead exposed, natural black"),
    ("s055", "男士短碎", "men textured crop", ["男士"],
     "男士短碎发，顶部有层次纹理，两侧短，自然黑",
     "men textured crop, layered top, short sides, natural black"),
    ("s056", "男士寸头", "men buzz cut", ["男士"],
     "男士寸头，头发极短贴合头型，自然黑",
     "men buzz cut, very short, close to scalp, natural black"),
    ("s057", "男士背头", "men slicked back", ["男士"],
     "男士背头，发丝向后梳顺，有光泽，深黑",
     "men slicked-back hair, combed back, glossy, deep black"),
    ("s058", "男士微卷中分", "men wavy center part", ["男士", "卷发"],
     "男士微卷中分，长度到耳朵，发丝自然弯，深棕",
     "men wavy center-part hair, ear length, natural bend, dark brown"),
    ("s059", "男士狼尾", "men wolf cut", ["男士"],
     "男士狼尾,顶部蓬松有层次，后颈留长，深黑",
     "men wolf cut, voluminous layered top, longer at nape, deep black"),
    ("s060", "男士短卷", "men short curls", ["男士", "卷发"],
     "男士短卷发，顶部小卷蓬松，两侧短，自然黑",
     "men short curls, fluffy curls on top, short sides, natural black"),
]

# 补足到 100 款：以「基础型 + 发色/长度变体」组合生成（同一套 prompt 规则，前端按 tag 归类）
_BASES = [
    ("法式碎短发", "French textured short bob", "短发", "clean French textured short bob, jaw length"),
    ("锁骨Lob", "collarbone lob", "中长", "collarbone-length lob with soft ends"),
    ("层次长发", "layered long hair", "长发", "long layered hair, face-framing layers"),
    ("大波浪", "big waves", "卷发", "big loose waves, voluminous"),
    ("低马尾", "low ponytail", "扎发", "low ponytail, loose strands"),
    ("丸子头", "top bun", "扎发", "high top bun, neat"),
    ("男士短碎", "men textured crop", "男士", "men textured crop, short sides"),
    ("黑长直", "long straight black", "长发", "waist-length straight black hair"),
]
_COLORS = [
    ("自然黑", "natural black", "自然黑发色"),
    ("深棕", "dark brown", "深棕色"),
    ("栗棕", "chestnut", "栗棕色"),
    ("浅棕", "light brown", "浅棕色"),
    ("亚麻金", "flaxen blonde", "亚麻浅金色"),
    ("红棕", "auburn", "红棕色"),
]

_i = 61
for bname, ben, btag, ben_full in _BASES:
    for cname, cen, czh in _COLORS:
        if _i > 100:
            break
        STYLES.append((
            f"s{_i:03d}", f"{bname}·{cname}", f"{ben_full}, {cen}", [btag, "发色"],
            f"{bname}，{czh}，自然光泽，发丝质感真实",
            f"{ben_full}, {cen}, natural shine, realistic strand texture",
        ))
        _i += 1

TOTAL = len(STYLES)

BY_ID = {s[0]: s for s in STYLES}


def as_dict(s):
    return {"id": s[0], "name": s[1], "name_en": s[2], "tags": s[3], "prompt_zh": s[4], "prompt_en": s[5]}


# ---------------- 外部库（抓取/后台新增） ----------------
import json as _json  # noqa: E402
import os as _os  # noqa: E402

_DATA = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "data")
_EXT_JSON = _os.path.join(_DATA, "styles.json")

ALL = []
EXT = []


def reload():
    """重新读 data/styles.json，与内置 100 款合并。后台加款后调用。"""
    global ALL, BY_ID, TOTAL, EXT
    builtin = [as_dict(s) for s in STYLES]
    EXT = []
    if _os.path.exists(_EXT_JSON):
        try:
            for e in _json.load(open(_EXT_JSON, encoding="utf-8")):
                e = dict(e)
                e.setdefault("prompt_zh", "")
                e.setdefault("prompt_en", "")
                e.setdefault("tags", ["新增"])
                e["source"] = "external"
                e["cover_url"] = f"/covers/{e['cover']}" if e.get("cover") else None
                # 实拍图（主图/正面/侧面/背面）：目录里真有的才给 URL，别让前端挂一堆 404
                imgs = []
                for it in (e.get("images") or []):
                    f = (it or {}).get("file") if isinstance(it, dict) else it
                    if not f:
                        continue
                    p = _os.path.join(_DATA, "gallery", e["id"], f)
                    if _os.path.exists(p):
                        imgs.append({"label": (it.get("label") if isinstance(it, dict) else "") or "实拍",
                                     "file": f, "url": f"/gallery/{e['id']}/{f}"})
                e["images"] = imgs
                e["image_count"] = len(imgs)
                if not e["cover_url"] and imgs:
                    e["cover_url"] = imgs[0]["url"]
                EXT.append(e)
        except Exception as ex:  # 坏 json 不影响内置库
            print("styles.json 读取失败:", ex)
    ALL = builtin + EXT
    BY_ID = {x["id"]: x for x in ALL}
    TOTAL = len(ALL)
    return TOTAL


def add_external(entry):
    items = []
    if _os.path.exists(_EXT_JSON):
        items = _json.load(open(_EXT_JSON, encoding="utf-8"))
    items = [x for x in items if x["id"] != entry["id"]] + [entry]
    _os.makedirs(_DATA, exist_ok=True)
    _json.dump(items, open(_EXT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    reload()
    return entry


def del_external(sid):
    if not _os.path.exists(_EXT_JSON):
        return False
    items = [x for x in _json.load(open(_EXT_JSON, encoding="utf-8")) if x["id"] != sid]
    _json.dump(items, open(_EXT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    reload()
    return True


reload()
