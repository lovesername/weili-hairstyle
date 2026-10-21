# -*- coding: utf-8 -*-
"""发知发同款「AI 一键换发」后端。

    uvicorn app:app --host 0.0.0.0 --port 8899 --reload

链路：上传 → 人脸/头型裁剪 → 头发 mask → 局部重绘（provider） → 调色回贴 → 结果
额度：openid + 日期，默认每天 30 次（FREE_PER_DAY 可调），运营加次数走 bonus。
     本地自用嫌少：页面右上角「重置额度 ↺」，或 POST /api/admin/quota {all:true,reset:true}。
"""
import base64
import copy
import io
import json
import os
import re
import sqlite3
import sys
import threading
import uuid
import time
import traceback

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import numpy as np
from PIL import Image, ImageFilter, ImageOps

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import auth
import billing
import prompts
import pay
import styles as stylelib
from pipeline import compose, mask as maskmod, preprocess, updo
from pipeline import select as picker
from providers.base import registry as build_registry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 公网部署开关（云上必须设 DEPLOY=1）。两处作用：
# 1) 关掉"本机访问免管理员口令"——云平台的反代跟 uvicorn 同机，client.host 就是 127.0.0.1
# 2) ADMIN_TOKEN 不再用 dev-token 兜底，没配就谁都过不了
DEPLOY = os.environ.get("DEPLOY", "") in ("1", "true", "yes")

# 运行时可写目录。容器里设 WEILI_RUNTIME_DIR=/app/storage（挂持久卷）就能把
# 发型库改动 / 参考图 / 收款码 / 站点设置 跟代码镜像解耦，重新部署不丢。
# 本地不设 → _rt() 落回 data/，行为跟以前一模一样。
RUNTIME_DIR = os.environ.get("WEILI_RUNTIME_DIR", "").strip()


def _rt(*parts):
    """运行时可写路径：容器里统一收进 WEILI_RUNTIME_DIR 指向的持久卷。"""
    if RUNTIME_DIR:
        return os.path.join(RUNTIME_DIR, *parts)
    return os.path.join(ROOT, "data", *parts)


def _seed_runtime():
    """容器首次挂上空卷时，把镜像里的种子文件（发型库 json / 站点设置 / 支付配置）
    和实拍图目录拷进持久卷，之后代码重新部署就不再动它们。"""
    if not RUNTIME_DIR:
        return
    import shutil
    for name in ("styles.json", "site.json", "pay.json"):
        dst, src = os.path.join(RUNTIME_DIR, name), os.path.join(ROOT, "data", name)
        if not os.path.exists(dst) and os.path.exists(src):
            try:
                shutil.copyfile(src, dst)
            except Exception:
                pass
    gsrc = os.path.join(ROOT, "data", "gallery")
    gdst = os.path.join(RUNTIME_DIR, "gallery")
    if os.path.isdir(gsrc) and not os.path.exists(gdst):
        try:
            shutil.copytree(gsrc, gdst)
        except Exception:
            pass


def _load_env():
    """读项目根目录 .env（uvicorn 自己不加载）。已存在的环境变量优先。"""
    p = os.path.join(ROOT, ".env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = re.split(r"\s+#", v, 1)[0]        # 去掉行尾注释：A=1   # 说明
        v = v.strip().strip('"').strip("'")
        if not v:                     # 空值等于没写，交给代码里的自动判断
            continue
        os.environ.setdefault(k.strip(), v)


_load_env()

UP = os.path.join(ROOT, "storage", "uploads")
RES = os.path.join(ROOT, "storage", "results")
DBG = os.path.join(ROOT, "storage", "debug")
for d in (UP, RES, DBG):
    os.makedirs(d, exist_ok=True)
_seed_runtime()          # 必须在 StaticFiles 挂载之前：挂载时目录得存在

PROVIDERS = build_registry()
# 这几个是"整头重画"型后端（云端图像大模型）：不遮 mask、不做几何、
# 直接把整张人像 + 发型参考图喂进去。本地 comfy 走另一条几何流水线。
EDIT_PROVIDERS = {"apimart", "gemini", "openai"}
DEFAULT_PROVIDER = os.environ.get("PROVIDER", "mock")
if DEFAULT_PROVIDER not in PROVIDERS:
    DEFAULT_PROVIDER = "mock"

_dp = {"t": 0.0, "v": DEFAULT_PROVIDER}


def reload_providers():
    """改完 .env（填 key / 换模型）当场生效，不用重启服务。"""
    global PROVIDERS, DEFAULT_PROVIDER
    PROVIDERS = build_registry()
    DEFAULT_PROVIDER = os.environ.get("PROVIDER", "") or "mock"
    if DEFAULT_PROVIDER not in PROVIDERS:
        DEFAULT_PROVIDER = "mock"
    _dp.update(t=0.0, v=DEFAULT_PROVIDER)
    return PROVIDERS


def default_provider():
    """没指定后端时，自动挑一个**当场真能用**的：comfy > gemini > openai > mock。

    结果缓存 5s，避免每次请求都去探 8188。
    """
    if os.environ.get("PROVIDER"):        # 显式指定就听指定的
        return DEFAULT_PROVIDER
    now = time.time()
    if now - _dp["t"] < 5:
        return _dp["v"]
    pick = "mock"
    for k in ("apimart", "comfy", "gemini", "openai"):
        prov = PROVIDERS.get(k)
        if prov is not None and prov_available(k):
            pick = k
            break
    _dp.update(t=now, v=pick)
    return pick
SIZE = int(os.environ.get("RENDER_SIZE", "768"))

app = FastAPI(title="AI 一键换发")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
# 只把"该给人看的图"挂出去。以前是整包 storage，等于把 app.db（密码哈希/订单）
# 和 secret.key（能伪造任意登录 token）挂在公网上，谁都能 wget 下来。
for _sub in ("uploads", "results", "candidates"):
    _d = os.path.join(ROOT, "storage", _sub)
    os.makedirs(_d, exist_ok=True)
    app.mount("/files/" + _sub, StaticFiles(directory=_d), name="files_" + _sub)
app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "frontend")), name="static")
app.mount("/covers", StaticFiles(directory=os.path.join(ROOT, "data", "covers")), name="covers")
app.mount("/gallery", StaticFiles(directory=_rt("gallery")), name="gallery")
# 收款码（后台老板上传的微信 / 支付宝经营码）—— 公开只读，里面只有一张二维码图片
PAYQR = _rt("payqr")
os.makedirs(PAYQR, exist_ok=True)
app.mount("/payqr", StaticFiles(directory=PAYQR), name="payqr")
# 用户上传的发型参考图：按 uid 分目录存，只有本人接口会读
REFS = _rt("refs")
os.makedirs(REFS, exist_ok=True)


def uid(openid: str) -> str:
    return (openid or "demo")[:64]


# ---------------- 登录 / 积分 ----------------
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "weili8888")


def me(request: Request, openid: str = "") -> dict:
    """当前是谁：优先 Bearer token；没有再退回 openid（本地自用）。"""
    tok = request.headers.get("Authorization", "") if request is not None else ""
    a = auth.read_token(tok)
    if a:
        return {"account": a, "uid": a["id"], "role": a["role"], "guest": False}
    return {"account": None, "uid": uid(openid), "role": "", "guest": True}


def need_user(request: Request, openid: str = ""):
    """生成接口用：没登录就 401（本机 + admin token 例外，方便老板调试）。"""
    m = me(request, openid)
    if not m["guest"]:
        return m
    if _need_admin(request.headers.get("X-Admin-Token", ""), request, soft=True):
        return m
    raise HTTPException(401, "请先注册 / 登录后使用（注册送 %d 次免费）" % billing.FREE_GENS)


def _is_admin_ctx(m) -> bool:
    return m.get("role") == "admin"


@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "frontend", "index.html"))


@app.get("/admin")
def admin_page():
    """老板用的后台：填 key、加发型、删发型。"""
    return FileResponse(os.path.join(ROOT, "frontend", "admin.html"))


# ---------------- 设置：把 key 写进 .env 并当场生效 ----------------
ENV_PATH = os.path.join(ROOT, ".env")


def _write_env(updates: dict):
    """改写 .env：已有的键就覆盖（顺手取消 # 注释），没有就追加；值为 None 表示注释掉。"""
    lines = []
    if os.path.exists(ENV_PATH):
        lines = open(ENV_PATH, encoding="utf-8").read().splitlines()
    keys = set(updates)
    out, seen = [], set()
    for line in lines:
        m = re.match(r"^#?\s*([A-Z0-9_]+)\s*=", line.strip())
        if m and m.group(1) in keys:
            k = m.group(1)
            seen.add(k)
            v = updates[k]
            if v is None:
                out.append("# %s=" % k)
            else:
                out.append("%s=%s" % (k, v))
        else:
            out.append(line)
    for k in keys - seen:
        v = updates[k]
        if v is not None:
            out.append("%s=%s" % (k, v))
    open(ENV_PATH, "w", encoding="utf-8").write("\n".join(out) + "\n")


def _env_apply(updates: dict):
    for k, v in updates.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _write_env(updates)
    reload_providers()


# provider 探活缓存：comfy 探一次 2 秒+，别每次开后台都让用户等
_AVAIL_CACHE = {}

def prov_available(k):
    v = PROVIDERS[k]
    now = time.time()
    hit = _AVAIL_CACHE.get(k)
    ttl = 300 if (hit and hit[1]) else 30   # 可用缓存 5 分钟；不可用 30 秒后重探（ComfyUI 随时可能被启动）
    if hit and now - hit[0] < ttl:
        return hit[1]
    ok = bool(v.available())
    _AVAIL_CACHE[k] = (now, ok)
    return ok


@app.get("/api/config")
def api_config(x_admin_token: str = Header(default=""), request: Request = None):
    """后台配置。含 key 掩码 / 模型 / 后端状态 —— 这些全是老板的私事，
    绝不能让普通用户或爬子拿到，所以整个接口只对管理员开。"""
    _need_admin(x_admin_token, request)
    key = os.environ.get("APIMART_API_KEY", "")
    return {
        "provider": os.environ.get("PROVIDER", "") or "",
        "default": default_provider(),
        "free_per_day": db.free_per_day(),
        "apimart": {
            "key_set": bool(key),
            "key_mask": (key[:5] + "..." + key[-4:]) if len(key) > 10 else ("" if not key else "***"),
            "model": os.environ.get("APIMART_IMAGE_MODEL") or "gemini-2.5-flash-image-preview",
            "resolution": os.environ.get("APIMART_RESOLUTION") or "1K",
            "proxy": os.environ.get("APIMART_PROXY") or "(自动探测)",
        },
        "providers": {k: {"available": prov_available(k), "reason": "" if prov_available(k)
                          else REASON.get(k, "未就绪")} for k in PROVIDERS},
        "billing": billing.profile(),
    }


@app.post("/api/config")
def api_config_set(payload: dict, request: Request, x_admin_token: str = Header(default="")):
    """{apimart_key?, apimart_model?, apimart_proxy?, provider?} → 写 .env + 立即可用。"""
    _need_admin(x_admin_token, request)
    upd = {}
    if "apimart_key" in payload:
        upd["APIMART_API_KEY"] = (payload.get("apimart_key") or "").strip() or None
    if "apimart_model" in payload:
        upd["APIMART_IMAGE_MODEL"] = (payload.get("apimart_model") or "").strip() or None
    if "apimart_proxy" in payload:
        upd["APIMART_PROXY"] = (payload.get("apimart_proxy") or "").strip() or None
    if "apimart_resolution" in payload:
        upd["APIMART_RESOLUTION"] = (payload.get("apimart_resolution") or "").strip() or None
    if "provider" in payload:
        upd["PROVIDER"] = (payload.get("provider") or "").strip() or None
    if not upd:
        raise HTTPException(400, "没有要保存的字段")
    _env_apply(upd)
    return {"ok": True, "saved": sorted(upd.keys()), "config": api_config()}


# ---------------- 站点设置（客服微信 / 版权 / 收款码）：后台可改，前台公开读 ----------------
SITE_FILE = _rt("site.json")
_SITE_DEFAULT = {"kf_wechat": "", "copyright": "© 2026 微粒 · WEILI",
                 "notice": "", "wxpay_qr": "", "alipay_qr": ""}


def _site():
    try:
        d = json.load(open(SITE_FILE, encoding="utf-8"))
    except Exception:
        d = {}
    out = dict(_SITE_DEFAULT)
    out.update({k: d[k] for k in _SITE_DEFAULT if d.get(k)})
    return out


def _save_site(d):
    os.makedirs(os.path.dirname(SITE_FILE), exist_ok=True)
    json.dump(d, open(SITE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


@app.get("/api/site")
def api_site():
    """前台公开的站点信息。只给这几样，多的都不给 —— 防爬。"""
    s = _site()
    return {"kf_wechat": s["kf_wechat"], "copyright": s["copyright"],
            "notice": s["notice"],
            "wxpay_qr": "/payqr/wxpay.png" if os.path.exists(os.path.join(PAYQR, "wxpay.png")) else "",
            "alipay_qr": "/payqr/alipay.png" if os.path.exists(os.path.join(PAYQR, "alipay.png")) else "",
            "free_gens": billing.FREE_GENS, "gen_credits": billing.GEN_CREDITS}


@app.post("/api/admin/site")
async def api_admin_site(payload: dict, request: Request):
    """{kf_wechat?, copyright?, notice?, wxpay_qr_b64?, alipay_qr_b64?}"""
    m = me(request, "")
    _need_role(request, m)
    s = _site()
    for k in ("kf_wechat", "copyright", "notice"):
        if k in payload:
            s[k] = str(payload[k] or "").strip()
    for k, fname in (("wxpay_qr_b64", "wxpay.png"), ("alipay_qr_b64", "alipay.png")):
        b64 = str(payload.get(k) or "")
        if b64:
            if "," in b64:                      # data:image/png;base64,xxxx
                b64 = b64.split(",", 1)[1]
            try:
                raw = base64.b64decode(b64)
                im = Image.open(io.BytesIO(raw)).convert("RGB")
                im.thumbnail((480, 480))
                im.save(os.path.join(PAYQR, fname))
            except Exception as e:
                raise HTTPException(400, "收款码图片解析失败：%s" % e)
            s["wxpay_qr" if "wx" in k else "alipay_qr"] = "/payqr/" + fname
    _save_site(s)
    return {"ok": True, "site": api_site()}


# ---------------- 自动支付配置（支付宝当面付 / 微信 v3 / 易支付） ----------------
@app.get("/api/admin/pay")
def api_admin_pay(request: Request):
    m = me(request, "")
    _need_role(request, m)
    return pay.public_cfg()


@app.post("/api/admin/pay")
def api_admin_pay_set(payload: dict, request: Request):
    """{mode, base_url, alipay:{appid,private_key,public_key,gateway},
        wechat:{appid,mchid,serial_no,private_key,apiv3_key,platform_cert},
        epay:{gateway,pid,key,type}}
    密钥只写不进返回；mode 决定走哪条（manual 就回经营码人工确认）。"""
    m = me(request, "")
    _need_role(request, m)
    if payload.get("mode") not in (None, "manual", "alipay", "wechat", "epay"):
        raise HTTPException(400, "mode 只能是 manual / alipay / wechat / epay")
    pay.save(payload)
    c = pay.public_cfg()
    if c["mode"] != "manual" and not c["ready"]:
        c["hint"] = "配置还不全，暂时仍按经营码收款 —— 看下面每项是否配齐"
    return c


@app.get("/api/admin/pay/test")
def api_admin_pay_test(request: Request):
    """自检：通道是否齐、notify 地址拼出来是什么（本地也要能看到）。"""
    m = me(request, "")
    _need_role(request, m)
    c = pay.cfg()
    return {"mode": c["mode"], "ready": pay.ready(),
            "notify": {"alipay": pay.notify_url("alipay"), "wechat": pay.notify_url("wechat"),
                       "epay": pay.notify_url("epay")},
            "base_url": c["base_url"]}


# ---------------- 用户上传的发型参考图（和发型库完全隔离） ----------------
def _ref_public(r):
    return {"id": r["id"], "name": r["name"],
            "url": "/refs-file/%s/%s" % (r["uid"], r["path"]),
            "created_at": r["created_at"]}


@app.get("/api/ref/list")
def api_ref_list(request: Request, openid: str = Header(default="")):
    """只回登录者自己的参考图。不登录看不到任何人的 —— 隐私和数据隔离都在这。"""
    m = me(request, openid)
    if not m["account"]:
        return {"items": []}
    with db.conn() as c:
        rows = c.execute("select * from refs where uid=? order by created_at desc",
                         (m["account"]["id"],)).fetchall()
    return {"items": [_ref_public(r) for r in rows]}


@app.post("/api/ref/upload")
async def api_ref_upload(file: UploadFile = File(...), request: Request = None,
                         openid: str = Header(default="")):
    m = need_user(request, openid)
    if not m["account"]:
        raise HTTPException(401, "登录后才能上传参考图")
    raw = await file.read()
    if len(raw) > 12 * 1024 * 1024:
        raise HTTPException(400, "图片太大了（>12MB）")
    try:
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im).convert("RGB")
    except Exception:
        raise HTTPException(400, "不是有效图片")
    # 参考图是给模型看的：长边压到 960，细节足够，传输也快
    im.thumbnail((960, 960))
    rid = "r" + uuid.uuid4().hex[:10]
    udir = os.path.join(REFS, m["account"]["id"])
    os.makedirs(udir, exist_ok=True)
    fn = rid + ".jpg"
    im.save(os.path.join(udir, fn), quality=90)
    with db._lock, db.conn() as c:
        c.execute("insert into refs(id,uid,name,path,created_at) values(?,?,?,?,?)",
                  (rid, m["account"]["id"], (file.filename or "参考图")[:40], fn,
                   int(time.time())))
    return {"ok": True, "item": _ref_public(dict(
        id=rid, uid=m["account"]["id"], name=(file.filename or "参考图")[:40],
        path=fn, created_at=int(time.time())))}


@app.delete("/api/ref/{rid}")
def api_ref_del(rid: str, request: Request, openid: str = Header(default="")):
    m = need_user(request, openid)
    with db.conn() as c:
        r = c.execute("select * from refs where id=?", (rid,)).fetchone()
        if not r or (m["account"] and r["uid"] != m["account"]["id"] and not _is_admin_ctx(m)):
            raise HTTPException(404, "参考图不存在")
        c.execute("delete from refs where id=?", (rid,))
    p = os.path.join(REFS, r["uid"], r["path"])
    if os.path.exists(p):
        os.remove(p)
    return {"ok": True}


@app.get("/refs-file/{uid}/{fname}")
def refs_file(uid: str, fname: str):
    """参考图文件。路径由后端拼，uid+fname 都过白名单校验，防目录穿越。"""
    if not re.fullmatch(r"[A-Za-z0-9_\-]{4,64}", uid) or "/" in fname or ".." in fname:
        raise HTTPException(404)
    p = os.path.join(REFS, uid, fname)
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/styles")
def api_styles(tag: str = "", q: str = ""):
    items = stylelib.ALL
    if tag:
        items = [x for x in items if tag in x.get("tags", [])]
    if q:
        items = [x for x in items if q.lower() in (x.get("name", "") + x.get("name_en", "")).lower()]
    # 有实拍封面的排前面：用户一进来先看到的是有图的款，
    # 内置 100 款（无图占位）沉底，不然满屏 NO IMG 像坏了一样
    items = sorted(items, key=lambda x: 0 if x.get("cover_url") else 1)
    return {"total": stylelib.TOTAL, "shown": len(items), "items": items,
            "palette": prompts.color_choices({})}


# ---------------- 后台：加/删发型（发型库怎么上新，见 README） ----------------
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "" if DEPLOY else "dev-token")
COVERS = os.path.join(ROOT, "data", "covers")
GALLERY = _rt("gallery")


def _is_local(request) -> bool:
    # 云上部署必须设 DEPLOY=1：Render/Fly 的反向代理跟 uvicorn 在同一台机器上，
    # 请求打过来 client.host 就是 127.0.0.1 —— 不关掉的话，互联网上任何人
    # 直接请求 /api/admin/* 都会被当成"老板本机打开"直接放行。
    if DEPLOY:
        return False
    try:
        host = request.client.host if request is not None else ""
    except Exception:
        host = ""
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


def _need_admin(token: str, request=None, soft=False):
    """本机打开后台不用填口令；从别的机器访问才要 X-Admin-Token。

    老板在自己电脑上点「设置」就该能直接进，不然又是一个进不去的入口。
    soft=True：只判断不抛错（给"没登录但本机调试"放行用）。
    """
    if token and token == ADMIN_TOKEN:
        return True
    if _is_local(request):
        return True
    if soft:
        return False
    raise HTTPException(401, "X-Admin-Token 不对（或换本机打开）")


def _need_role(request: Request, m: dict):
    """后台接口：登录账号是 admin，或带 X-Admin-Token / 本机访问，都放行。"""
    if _is_admin_ctx(m):
        return m
    if _need_admin(request.headers.get("X-Admin-Token", ""), request, soft=True):
        return m
    raise HTTPException(403, "要管理员账号（或本机带 X-Admin-Token）")


@app.get("/api/admin/styles")
def admin_list(request: Request, x_admin_token: str = Header(default="")):
    _need_admin(x_admin_token, request)
    return {"external": stylelib.EXT, "total": stylelib.TOTAL}


@app.post("/api/admin/styles")
def admin_add(payload: dict, request: Request, x_admin_token: str = Header(default="")):
    """加一款新发型。cover_b64 传图片 base64（可带 data:image/jpeg;base64, 前缀）。

    必填 name；tags 建议带一个主要类目（短发/中长/长发/卷发/扎发/男士），
    prompt_zh 不填就用 tags + color 自动套模板。
    """
    _need_admin(x_admin_token, request)
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name 必填")
    tags = payload.get("tags") or ["新增"]
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]

    sid = payload.get("id") or ("a" + uuid.uuid4().hex[:10])
    cover_file = None
    b64 = payload.get("cover_b64")
    if b64:
        import base64
        import io as _io
        from PIL import Image as _Image
        raw = base64.b64decode(b64.split(",")[-1])
        im = _Image.open(_io.BytesIO(raw)).convert("RGB")
        w, h = im.size
        side = min(w, h)
        im = im.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        im = im.resize((512, 512), _Image.LANCZOS)
        os.makedirs(COVERS, exist_ok=True)
        cover_file = sid + ".jpg"
        im.save(os.path.join(COVERS, cover_file), "JPEG", quality=88)

    color = payload.get("color") or "自然黑"
    zh = payload.get("prompt_zh") or f"{color}{name}，发丝质感真实，自然光泽"
    en = payload.get("prompt_en") or f"{color} {name} hairstyle, realistic strands"

    try:
        length = int(payload.get("length") or 0)
    except Exception:
        length = 0
    entry = {"id": sid, "name": name, "name_en": payload.get("name_en", name), "tags": tags,
             "prompt_zh": zh, "prompt_en": en, "cover": cover_file,
             "color": color, "hot": bool(payload.get("hot", False)),
             "length": length, "created_at": int(time.time())}
    stylelib.add_external(entry)
    return {"ok": True, "style": entry, "total": stylelib.TOTAL}


# ---------------- 后台：改款 / 换封面 / 加删实拍图 ----------------
EXT_JSON = _rt("styles.json")


def _seed_runtime():
    """容器首次挂载空卷时，把镜像里的种子文件（发型库等）拷进持久卷。"""
    if not RUNTIME_DIR:
        return
    for name in ("styles.json", "site.json", "pay.json"):
        dst = os.path.join(RUNTIME_DIR, name)
        src = os.path.join(ROOT, "data", name)
        if not os.path.exists(dst) and os.path.exists(src):
            try:
                open(dst, "wb").write(open(src, "rb").read())
            except Exception:
                pass


_seed_runtime()


def _ext_items():
    if not os.path.exists(EXT_JSON):
        return []
    return json.load(open(EXT_JSON, encoding="utf-8"))


def _ext_save(items):
    json.dump(items, open(EXT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    stylelib.reload()


def _find_ext(items, sid):
    for i, x in enumerate(items):
        if x.get("id") == sid:
            return i
    return None


def _save_cover(sid: str, b64: str) -> str:
    import base64
    import io as _io
    from PIL import Image as _Image
    raw = base64.b64decode(b64.split(",")[-1])
    im = _Image.open(_io.BytesIO(raw)).convert("RGB")
    w, h = im.size
    side = min(w, h)
    im = im.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
    im = im.resize((512, 512), _Image.LANCZOS)
    os.makedirs(COVERS, exist_ok=True)
    name = sid + ".jpg"
    im.save(os.path.join(COVERS, name), "JPEG", quality=88)
    return name


@app.post("/api/admin/styles/{sid}")
def admin_update(sid: str, payload: dict, request: Request,
                 x_admin_token: str = Header(default="")):
    """改自己加的款：名字 / 类目 / 长度 / 发色 / 提示词 / 换封面。内置 100 款改不了。"""
    _need_admin(x_admin_token, request)
    items = _ext_items()
    i = _find_ext(items, sid)
    if i is None:
        raise HTTPException(400, "只有自己加的款能改（内置 100 款写在代码里）")
    e = dict(items[i])
    if "name" in payload:
        n = (payload.get("name") or "").strip()
        if not n:
            raise HTTPException(400, "名字不能空")
        e["name"] = n
    for k in ("name_en", "color", "prompt_zh", "prompt_en", "category"):
        if k in payload:
            e[k] = payload[k]
    if "tags" in payload:
        tags = payload["tags"]
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
        e["tags"] = tags or e.get("tags") or ["新增"]
    if "length" in payload:
        try:
            e["length"] = int(payload["length"] or 0)
        except Exception:
            e["length"] = 0
    if "hot" in payload:
        e["hot"] = bool(payload["hot"])
    if payload.get("cover_b64"):
        e["cover"] = _save_cover(sid, payload["cover_b64"])
    items[i] = e
    _ext_save(items)
    return {"ok": True, "style": stylelib.BY_ID.get(sid), "total": stylelib.TOTAL}


@app.post("/api/admin/styles/{sid}/images")
def admin_add_image(sid: str, payload: dict, request: Request,
                    x_admin_token: str = Header(default="")):
    """给某一款加一张实拍图。{file_b64, label}（label 写 正面/侧面/背面/主图）。"""
    _need_admin(x_admin_token, request)
    items = _ext_items()
    i = _find_ext(items, sid)
    if i is None:
        raise HTTPException(400, "只有自己加的款能加图")
    b64 = payload.get("file_b64") or payload.get("cover_b64")
    if not b64:
        raise HTTPException(400, "缺 file_b64")
    import base64
    import io as _io
    from PIL import Image as _Image
    folder = os.path.join(GALLERY, sid)
    os.makedirs(folder, exist_ok=True)
    n = 1
    while os.path.exists(os.path.join(folder, "extra%d.jpg" % n)):
        n += 1
    fname = "extra%d.jpg" % n
    im = _Image.open(_io.BytesIO(base64.b64decode(b64.split(",")[-1]))).convert("RGB")
    if max(im.size) > 1200:
        k = 1200 / float(max(im.size))
        im = im.resize((int(im.width * k), int(im.height * k)), _Image.LANCZOS)
    im.save(os.path.join(folder, fname), "JPEG", quality=88, optimize=True)
    e = dict(items[i])
    e.setdefault("images", []).append({"label": (payload.get("label") or "实拍").strip(),
                                       "file": fname})
    items[i] = e
    _ext_save(items)
    return {"ok": True, "style": stylelib.BY_ID.get(sid)}


@app.delete("/api/admin/styles/{sid}/images/{fname}")
def admin_del_image(sid: str, fname: str, request: Request,
                    x_admin_token: str = Header(default="")):
    _need_admin(x_admin_token, request)
    if "/" in fname or "\\" in fname or ".." in fname:
        raise HTTPException(400, "文件名不合法")
    items = _ext_items()
    i = _find_ext(items, sid)
    if i is None:
        raise HTTPException(400, "只有自己加的款能删图")
    e = dict(items[i])
    e["images"] = [x for x in (e.get("images") or [])
                   if (x.get("file") if isinstance(x, dict) else x) != fname]
    items[i] = e
    _ext_save(items)
    p = os.path.join(GALLERY, sid, fname)
    if os.path.exists(p):
        os.remove(p)
    return {"ok": True, "style": stylelib.BY_ID.get(sid)}


@app.get("/api/admin/quota")
def admin_quota(request: Request, openid: str = "", x_admin_token: str = Header(default="")):
    """查额度：不带 openid 就全表列出，带就只查这一个。"""
    _need_admin(x_admin_token, request)
    if openid:
        return db.quota_info(openid)
    with db.conn() as c:
        rows = c.execute("select openid, used from quota where day=? order by used desc",
                         (db.today(),)).fetchall()
    return {"day": db.today(), "free": db.free_per_day(),
            "rows": [dict(r) for r in rows]}


@app.post("/api/admin/quota")
def admin_quota_set(payload: dict, request: Request, x_admin_token: str = Header(default="")):
    """{openid, bonus: N} 加额度；{openid, reset: true} 清零；{all: true, reset: true} 全清。"""
    _need_admin(x_admin_token, request)
    if payload.get("all") and payload.get("reset"):
        return db.quota_reset_all()
    openid = (payload.get("openid") or "").strip()
    if not openid:
        raise HTTPException(400, "缺 openid")
    if payload.get("reset"):
        return db.quota_reset(openid)
    n = int(payload.get("bonus") or 0)
    if not n:
        raise HTTPException(400, "缺 bonus")
    return db.add_bonus(openid, n)


@app.delete("/api/admin/styles/{sid}")
def admin_del(sid: str, request: Request, x_admin_token: str = Header(default="")):
    _need_admin(x_admin_token, request)
    if sid not in stylelib.BY_ID:
        raise HTTPException(404, "没有这款")
    stylelib.del_external(sid)
    # 实拍图 / 封面一起清掉，别在磁盘上留孤儿文件
    import shutil
    g = os.path.join(GALLERY, sid)
    if os.path.isdir(g):
        shutil.rmtree(g, ignore_errors=True)
    c = os.path.join(COVERS, sid + ".jpg")
    if os.path.exists(c):
        os.remove(c)
    return {"ok": True, "total": stylelib.TOTAL}


@app.get("/api/health")
def api_health():
    return {"ok": True, "default": default_provider(),
            "providers": {k: prov_available(k) for k in PROVIDERS}}


REASON = {
    "mock": "占位渲染，仅用于验证链路",
    "apimart": "未配置 APIMART_API_KEY（apimart.ai 后台 → API Keys）",
    "gemini": "未配置 GEMINI_API_KEY",
    "openai": "未配置 OPENAI_API_KEY",
    "comfy": "ComfyUI 未启动（默认 127.0.0.1:8188）",
}


@app.get("/api/providers")
def api_providers(request: Request, x_admin_token: str = Header(default="")):
    """后端就绪状态 + 没接通的原因（含缺哪个 KEY）—— 管理员专属。
    用户端永远不该知道店里接了哪几家云。"""
    _need_admin(x_admin_token, request)
    items = {}
    for k in PROVIDERS:
        ok = prov_available(k)
        items[k] = {"available": ok, "reason": "" if ok else REASON.get(k, "未就绪")}
    return {"default": default_provider(), "items": items}


def _list_models(key=""):
    """拿 APIMart 的模型清单。key 传了就用临时的（还没保存也能先试试）。"""
    p = PROVIDERS.get("apimart")
    if p is None:
        raise HTTPException(503, "apimart provider 没注册")
    k = (key or "").strip() or getattr(p, "key", "")
    if not k:
        raise HTTPException(400, "还没有 key：先在框里粘贴 APIMart 的 key")
    old = getattr(p, "key", "")
    try:
        p.key = k
        j = p._get("/models")
    except Exception as e:
        raise HTTPException(502, str(e)[:300])
    finally:
        p.key = old
    raw = j.get("data") if isinstance(j.get("data"), list) else (j.get("models") or [])
    ids = []
    for it in raw:
        mid = (it.get("id") or it.get("model")) if isinstance(it, dict) else str(it)
        if mid:
            ids.append(mid)
    ids = sorted(set(ids))
    kw = ("image", "banana", "seedream", "imagen", "flux", "dall", "gpt-image", "kolors", "doubao")
    # 视频/语音/向量类的也会撞上 flux、image 这些词（比如 flux-3-video），前台选到就白扣一次钱
    bad = ("video", "tts", "audio", "speech", "voice", "embed", "whisper", "rerank",
           "moderation", "ocr", "upscale")
    img = [i for i in ids if any(x in i.lower() for x in kw)
           and not any(b in i.lower() for b in bad)]
    # 落盘缓存：前台每次刷新页面不该都去 /models 打一次（虽然不花钱，
    # 但一进来就卡两秒很不像样）。后台「拉取模型」时才刷新这份。
    try:
        json.dump({"current": getattr(p, "model", ""), "total": len(ids), "image": img,
                   "updated": time.strftime("%Y-%m-%d %H:%M")},
                  open(os.path.join(ROOT, "data", "models.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    except Exception as e:
        print("写 models.json 失败:", e)
    return {"current": getattr(p, "model", ""), "total": len(ids), "image": img, "all": ids}


@app.get("/api/models")
def api_models():
    """前台用的模型清单：只读本地缓存，不联网。缓存没有就只回当前这个。"""
    p = PROVIDERS.get("apimart")
    cur = getattr(p, "model", "") if p else ""
    try:
        d = json.load(open(os.path.join(ROOT, "data", "models.json"), encoding="utf-8"))
    except Exception:
        d = {}
    items = [x for x in (d.get("image") or []) if isinstance(x, str)]
    if cur and cur not in items:
        items.insert(0, cur)
    return {"current": cur, "items": items, "cached": bool(d.get("image")),
            "updated": d.get("updated") or "", "total": d.get("total") or len(items)}


@app.get("/api/apimart/models")
def api_apimart_models(key: str = ""):
    """列出 APIMart 上能用的模型（key 可选，带上就不必先保存）。"""
    return _list_models(key)


@app.post("/api/apimart/models")
def api_apimart_models_post(payload: dict):
    """{key} → 不保存也能拉模型清单（后台填 key 时用）。"""
    return _list_models((payload or {}).get("key") or "")


@app.post("/api/apimart/model")
def api_apimart_switch(payload: dict):
    """运行中切模型，不用重启：curl -XPOST /api/apimart/model -d '{"model":"gpt-image-1"}'"""
    p = PROVIDERS.get("apimart")
    mid = str((payload or {}).get("model") or "").strip()
    if p is None or not mid:
        raise HTTPException(400, "缺 model")
    p.model = mid
    return {"ok": True, "model": p.model}


@app.get("/api/quota")
def api_quota(openid: str = Header(default="", alias="openid")):
    return db.quota_info(uid(openid))


@app.post("/api/unlock")
def api_unlock(openid: str = Header(default=""), channel: str = "share"):
    if channel not in ("share", "invite", "cs"):
        raise HTTPException(400, "unknown channel")
    return db.add_bonus(uid(openid), 1)


# ---------------- 注册 / 登录 ----------------
@app.post("/api/auth/register")
def api_register(payload: dict):
    u = str(payload.get("username") or "").strip()
    p = str(payload.get("password") or "")
    phone = str(payload.get("phone") or "").strip()
    try:
        a = auth.register(u, p, phone)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except sqlite3.IntegrityError:
        raise HTTPException(400, "这个账号已经被注册了")
    return {"ok": True, "token": auth.make_token(a["id"]), "user": auth.public(a),
            "billing": billing.profile()}


@app.post("/api/auth/login")
def api_login(payload: dict):
    try:
        a = auth.login(str(payload.get("username") or "").strip(),
                       str(payload.get("password") or ""))
    except ValueError as e:
        raise HTTPException(401, str(e))
    return {"ok": True, "token": auth.make_token(a["id"]), "user": auth.public(a),
            "billing": billing.profile()}


@app.get("/api/auth/me")
def api_me(request: Request, openid: str = Header(default="")):
    m = me(request, openid)
    return {"user": auth.public(m["account"]) if m["account"] else None,
            "role": m["role"] or ("local" if _need_admin(
                request.headers.get("X-Admin-Token", ""), request, soft=True) else ""),
            "billing": billing.profile()}


@app.post("/api/auth/logout")
def api_logout():
    return {"ok": True}


# ---------------- 积分 / 套餐 / 订单 ----------------
@app.get("/api/billing")
def api_billing(request: Request, openid: str = Header(default="")):
    m = me(request, openid)
    return {"packages": billing.packages(), "profile": billing.profile(),
            "user": auth.public(m["account"]),
            "channels": pay.channels(), "pay_mode": pay.cfg()["mode"],
            "auto": pay.ready()}


@app.post("/api/billing/order")
def api_order(payload: dict, request: Request, openid: str = Header(default="")):
    """下单：{package_id, channel?} → 订单号 + 支付信息。

    配了自动通道（支付宝当面付 / 微信 Native / 易支付）就走真支付：回二维码内容，
    用户付完 notify 一进就自动加积分，老板不用点。
    没配就退回经营码：用户转账后点「我已完成转账」，后台确认。
    """
    m = need_user(request, openid)
    if not m["account"]:
        raise HTTPException(400, "登录账号才能充值（游客模式不记账）")
    p = billing.package(str(payload.get("package_id") or ""))
    if not p:
        raise HTTPException(400, "没有这个套餐")
    oid = auth.create_order(m["account"]["id"], p["id"], int(round(p["price"] * 100)),
                            p["credits"])
    s = _site()
    cents = int(round(p["price"] * 100))
    payinfo = {"mode": "manual", "amount": p["price"], "wxpay_qr": s["wxpay_qr"],
               "alipay_qr": s["alipay_qr"], "kf_wechat": s["kf_wechat"], "channels": pay.channels()}
    if pay.ready():
        try:
            r = pay.create(oid, cents, "微粒 AI 换发 · " + p["name"],
                           channel=str(payload.get("channel") or ""))
            payinfo.update({"mode": "auto", "kind": r.get("kind"), "qr": r.get("qr"),
                            "qr_url": "/api/billing/qr/%s.png" % oid})
            pay.qr_png(r["qr"], oid)        # 预生成，前端进弹窗就有图
        except Exception as e:
            print("自动下单失败，退回经营码：", e)
            payinfo["warn"] = str(e)[:160]
    return {"ok": True, "order": auth.get_order(oid), "package": p, "pay": payinfo}


@app.get("/api/billing/order/{oid}")
def api_order_status(oid: str, request: Request, openid: str = Header(default="")):
    """前端轮询用：支付回调到账后这里立刻变 paid，用户端自动跳「已到账」。"""
    m = need_user(request, openid)
    o = auth.get_order(oid)
    if not o:
        raise HTTPException(404, "订单不存在")
    if m["account"] and o["uid"] != m["account"]["id"] and not _is_admin_ctx(m):
        raise HTTPException(404, "订单不存在")
    return {"order": o, "paid": o["status"] == "paid",
            "user": auth.public(auth.get(o["uid"])) if o["status"] == "paid" else None}


@app.get("/api/billing/qr/{oid}.png")
def api_order_qr(oid: str):
    """订单二维码图片（内容由后端生成，用户端只拿图）。"""
    p = os.path.join(pay.QR_DIR, oid + ".png")
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p)


def _settle(oid, cents, trade_no=""):
    """回调统一落账：金额必须和订单一致，已付过的幂等返回。"""
    o = auth.get_order(oid or "")
    if not o:
        print("回调订单不存在:", oid)
        return False
    if o["status"] == "paid":
        return True
    if int(cents or 0) != int(o["amount_cents"]):
        print("回调金额不符 订单=%s 应付=%s 实付=%s" % (oid, o["amount_cents"], cents))
        return False
    auth.pay_order(oid, None)
    print("自动到账：订单 %s 金额 %s 分 交易号 %s" % (oid, cents, trade_no))
    return True


@app.post("/api/pay/notify/alipay")
async def notify_alipay(request: Request):
    form = dict(await request.form())
    got = pay.alipay_notify(form)
    if not got:
        print("支付宝回调验签失败:", form.get("out_trade_no"))
        return PlainTextResponse("fail", status_code=400)
    ok = _settle(got["oid"], got["amount_cents"], got.get("trade_no"))
    return PlainTextResponse("success" if ok else "fail")


@app.post("/api/pay/notify/wechat")
async def notify_wechat(request: Request):
    raw = (await request.body()).decode("utf-8")
    hdr = {k.lower(): v for k, v in request.headers.items()}
    got = pay.wechat_notify({"Wechatpay-Timestamp": hdr.get("wechatpay-timestamp", ""),
                             "Wechatpay-Nonce": hdr.get("wechatpay-nonce", ""),
                             "Wechatpay-Signature": hdr.get("wechatpay-signature", "")}, raw)
    if not got:
        print("微信回调验签失败")
        return JSONResponse({"code": "FAIL", "message": "verify failed"}, status_code=400)
    ok = _settle(got["oid"], got["amount_cents"], got.get("trade_no"))
    return JSONResponse({"code": "SUCCESS" if ok else "FAIL", "message": ""})


@app.api_route("/api/pay/notify/epay", methods=["GET", "POST"])
async def notify_epay(request: Request):
    """易支付：GET/POST 都可能，回 success 表示收到。"""
    q = dict(request.query_params)
    if request.method == "POST":
        try:
            q.update(dict(await request.form()))
        except Exception:
            pass
    got = pay.epay_notify(q)
    if not got:
        print("易支付回调验签失败:", q.get("out_trade_no"))
        return PlainTextResponse("fail", status_code=400)
    ok = _settle(got["oid"], got["amount_cents"], got.get("trade_no"))
    return PlainTextResponse("success" if ok else "fail")


@app.post("/api/billing/order/{oid}/claim")
def api_order_claim(oid: str, request: Request, openid: str = Header(default="")):
    """用户：我已扫码转账。订单进「待确认」，客服核对到账后在后台点确认。"""
    m = need_user(request, openid)
    try:
        o = auth.claim_order(oid, m["account"]["id"] if m["account"] else None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "order": o}


@app.post("/api/billing/order/{oid}/pay")
def api_order_pay(oid: str, request: Request, openid: str = Header(default="")):
    """确认到账 → 立即加积分。只给管理员（老板核对经营码账单后点）。
    真接上微信/支付宝官方回调时，回调验签后同样调 auth.pay_order。"""
    m = need_user(request, openid)
    if not _is_admin_ctx(m):
        raise HTTPException(403, "确认到账是管理员操作（用户端请点「我已完成转账」）")
    try:
        o = auth.pay_order(oid, None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "order": o, "user": auth.public(auth.get(o["uid"]))}


@app.get("/api/billing/orders")
def api_my_orders(request: Request, openid: str = Header(default="")):
    m = need_user(request, openid)
    return {"orders": auth.orders_of(m["account"]["id"]) if m["account"] else [],
            "ledger": auth.ledger_of(m["account"]["id"]) if m["account"] else []}


# ---------------- 后台：用户 / 充值 / 毛利 ----------------
@app.get("/api/admin/users")
def api_admin_users(request: Request, openid: str = Header(default=""),
                    x_admin_token: str = Header(default="")):
    m = me(request, openid)
    _need_role(request, m)
    return {"users": [dict(auth.public(u), created_at=u["created_at"], status=u["status"])
                      for u in auth.list_users()],
            "orders": auth.all_orders(50)}


@app.post("/api/admin/users/{aid}")
def api_admin_user_set(aid: str, payload: dict, request: Request,
                       openid: str = Header(default=""),
                       x_admin_token: str = Header(default="")):
    """{credits?:+N, free_left?:N, role?:'user'|'admin', status?}"""
    m = me(request, openid)
    _need_role(request, m)
    a = auth.get(aid)
    if not a:
        raise HTTPException(404, "没这个用户")
    if "credits" in payload:
        try:
            n = int(payload["credits"])
        except Exception:
            raise HTTPException(400, "credits 要是整数")
        auth.add_credits(aid, n, "admin: %s" % (payload.get("reason") or "手动调整"))
    if "free_left" in payload:
        auth.set_free(aid, int(payload["free_left"] or 0))
    if payload.get("role") in ("user", "admin"):
        auth.set_role(aid, payload["role"])
    if payload.get("status") in ("ok", "banned"):
        with db.conn() as c:
            c.execute("update accounts set status=? where id=?", (payload["status"], aid))
    return {"ok": True, "user": auth.public(auth.get(aid))}


@app.get("/api/admin/overview")
def api_admin_overview(request: Request, openid: str = Header(default=""),
                       x_admin_token: str = Header(default="")):
    """老板要看的那张表：卖了多少、花了多少、赚了多少。"""
    m = me(request, openid)
    _need_role(request, m)
    with db.conn() as c:
        users = c.execute("select count(*) n from accounts").fetchone()["n"]
        jobs = c.execute("select count(*) n, coalesce(sum(credits_spent),0) cr, "
                         "coalesce(sum(cost_usd),0) usd, "
                         "sum(case when status=2 then 1 else 0 end) ok, "
                         "sum(case when status=3 then 1 else 0 end) bad "
                         "from jobs").fetchone()
        paid = c.execute("select count(*) n, coalesce(sum(amount_cents),0) cents, "
                         "coalesce(sum(credits),0) cr from orders where status='paid'").fetchone()
    cur = os.environ.get("APIMART_IMAGE_MODEL") or ""
    mg = billing.margin(cur, 1)
    usd = float(jobs["usd"] or 0)
    return {
        "users": users,
        "jobs": {"total": jobs["n"], "ok": jobs["ok"], "failed": jobs["bad"],
                 "credits": jobs["cr"]},
        "recharge": {"orders": paid["n"], "cny": round(paid["cents"] / 100.0, 2),
                     "credits": paid["cr"]},
        "api": {"usd": round(usd, 4), "cny": round(usd * billing.USD_CNY, 2)},
        "unit": mg,
        "profile": billing.profile(),
        "model_table": [dict(m2, model=k, income=billing.cny_income(billing.credits_for(k, 1)),
                             gross=billing.margin(k, 1)["gross"])
                        for k, m2 in
                        ((k, billing.tier_of(k)) for k in sorted(billing.MODEL_USD))],
    }


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...), openid: str = Header(default="")):
    u = uid(openid)
    data = await file.read()
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(400, "图片超过 12MB")
    try:
        p = preprocess.prepare(data, size=SIZE)
    except Exception as e:
        raise HTTPException(400, f"图片解析失败: {e}")

    # 文件名必须唯一：原来用 int(time.time())，同一秒连传两张会互相覆盖，
    # 结果 photo A 的 id 指向了 B 的图（测 A 却出了一张 B 的成品，排查了两小时）。
    path = os.path.join(UP, f"{uuid.uuid4().hex[:12]}_{u[:8]}.jpg")
    open(path, "wb").write(data)
    pid = db.save_photo(u, path, p["rect"], p["face"])
    # 前端预览用裁剪图，保证和生成时看到的构图一致
    prev = os.path.join(UP, f"{pid}_crop.jpg")
    p["img"].save(prev, quality=92)
    return {"photo_id": pid, "preview": f"/files/uploads/{os.path.basename(prev)}",
            "face_found": bool(p["face"]), "rect": p["rect"]}


CAND = os.path.join(ROOT, "storage", "candidates")

# 发型库封面 → 目标发色 LAB 缓存（259 张封面，算一次就够）
_LAB_CACHE = {}


def _cover_hair_lab(path):
    """从封面里抠出头发，取 LAB 均值当"该款发型的标准发色"。取不到就返回 None。"""
    key = os.path.basename(path)
    if key in _LAB_CACHE:
        return _LAB_CACHE[key]
    lab = None
    try:
        p = preprocess.prepare(open(path, "rb").read(), size=512)
        fb = maskmod.face_box_in_crop(p["face"], p["rect"], 512)
        m = maskmod.hair_mask(p["img"], fb)
        lab = compose.hair_lab(p["img"], m)
    except Exception as e:  # 封面坏了不该让整单失败
        print("封面发色取不到:", key, e)
    _LAB_CACHE[key] = lab
    return lab


def _bun_score(im: Image.Image, fb) -> float:
    """头顶那一块有多少深色（= 长出了发髻）。用来在几张候选里挑真出了髻的那张。"""
    g = np.asarray(im.convert("L")).astype(np.float32)
    H, W = g.shape
    fx, fy, fw, fh = fb
    y0, y1 = int(max(0, fy - fh * 0.70)), int(min(H, fy + fh * 0.05))
    x0, x1 = int(max(0, fx + fw * 0.05)), int(min(W, fx + fw * 0.95))
    if y1 <= y0 or x1 <= x0:
        return 0.0
    patch = g[y0:y1, x0:x1]
    return float((patch < 95).mean())


def _updo_score(im: Image.Image, fb, tgt: Image.Image) -> float:
    """候选评分。

    + 剪影上半有头发（不能秃）
    - 剪影下半（耳下到下颌）头发太多 —— 那是 bob 的形状，扎发这里应该是脸颊和耳后的皮肤
    - 剪影下方还有头发（长发没收拾干净）
    """
    g = np.asarray(im.convert("L")).astype(np.float32)
    t = np.asarray(tgt.resize(im.size, Image.LANCZOS)) > 128
    if t.sum() < 500:
        return 0.0
    H = g.shape[0]
    ys = np.where(t.any(1))[0]
    if len(ys) < 10:
        return 0.0
    y0, y1 = ys[0], ys[-1]
    top = t.copy()
    top[int(y0 + (y1 - y0) * 0.62):, :] = False
    bottom = t & ~top
    have = float(((g < 100) & top).mean())
    low = float(((g < 100) & bottom).mean()) if bottom.any() else 0.0
    y = int(fb[1] + fb[3] * 0.95)
    stray = 0.0
    if y < H - 8:
        bg = float(np.percentile(g, 85))
        stray = float((g[y:, :] < bg - 45).mean())
    return have - 0.9 * low - 1.8 * stray


def _dark_in(im: Image.Image, mask: Image.Image) -> float:
    """mask 区里"头发色"的占比（越小越干净）。清底那一段用它挑候选。"""
    g = np.asarray(im.convert("L").resize(mask.size, Image.LANCZOS)).astype(np.float32)
    m = np.asarray(mask) > 100
    if m.sum() < 100:
        return 0.0
    bg = float(np.percentile(g, 85))
    return float((g[m] < bg - 45).mean())


def _restore_skin(out: Image.Image, p) -> Image.Image:
    """把原图的皮肤区域（脸/额头/脖子/手）原样贴回来。

    模型在剪影里重画时最容易把五官、肤色、脖子边缘带歪；这一步是硬保险 ——
    脸永远不是 AI 画的。刘海区域在原图里是头发（不是肤色），所以不会被"恢复"
    成原来的刘海，发际线仍然是模型新画的那条。
    """
    orig = p["orig"]
    sk = maskmod._skin_mask(orig)
    m = Image.fromarray((sk * 255).astype(np.uint8))
    # 必须**内缩**再羽化：外扩会把原图的脸边头发一起贴回来，
    # 脸周围出现一圈深色"鬼影"（第一次就是这么翻车的）。
    m = m.filter(ImageFilter.MinFilter(9)).filter(ImageFilter.GaussianBlur(4))
    return Image.composite(orig, out, m)


def _despeckle(base: Image.Image, kill: Image.Image, fb, size, hair=None,
               rounds=3) -> Image.Image:
    """把肩膀/衣领上剩下的发丝（卷发尾尤其多）抹成衣服色。

    判定用**发色距离**，不用亮度：卷发尾一半在阴影里、一半有高光，
    亮度阈值怎么调都会漏；而"跟头发中位色差多少"很稳，粉色衣服天然离得远。
    纯几何、确定性 —— 这段不交给模型，模型看到长发轮廓就会把长发画回来。
    脖子中间那一条不碰（下巴阴影本来就暗）。
    """
    W, H = size
    fx, fy, fw, fh = fb
    cx = fx + fw / 2
    xs = np.arange(W)[None, :]
    ys = np.arange(H)[:, None]
    side = (np.abs(xs - cx) > fw * 0.30) | (ys > fy + fh * 1.30)
    rows = (ys > fy + fh * 0.95)
    region = Image.fromarray(
        (np.asarray(kill.filter(ImageFilter.MaxFilter(81))) > 100).astype(np.uint8) * 255)
    reg = np.asarray(region) > 100

    arr0 = np.asarray(base.convert("RGB")).astype(np.float32)
    if hair is not None:
        hm = np.asarray(hair.resize(base.size, Image.LANCZOS)) > 120
        core = hm & (arr0.max(2) < 120)
    else:
        core = np.zeros(arr0.shape[:2], bool)
    hair_col = np.median(arr0[core], axis=0) if core.sum() > 300 else np.array([45, 38, 34])
    out = base
    for _ in range(rounds):
        arr = np.asarray(out.convert("RGB")).astype(np.float32)
        d = np.sqrt(((arr - hair_col[None, None, :]) ** 2).sum(2))
        dark = (d < 72) & reg & side & rows
        if dark.sum() < 40:
            break
        out = compose.plate_fill(out, Image.fromarray((dark * 255).astype(np.uint8)))
    return out


def _do_updo(prov, p, fb, m_hair, kind, job_id, target_lab=None, ref_b=None):
    """扎发/盘发：剪影法。

    1. 几何剪影（头骨帽 + 髻），剪影外的旧头发整块抹掉 —— 这一步决定"换发型"而不是
       "在原发型上加东西"
    2. 模型只在剪影里重画：收拢的发丝走向 + 髻的质感（denoise 0.95，轮廓已被锁死）
    3. 回贴 + 皮肤还原（脸不交给模型）
    """
    SIZE = p["img"].width
    r = updo.prepare(p["img"], fb, m_hair, kind)
    kind = r["kind"]      # 头顶没空间时自动降成后颈低盘
    canvas, tgt, kill, pad = r["canvas"], r["tgt"], r["kill"], r["pad"]
    canvas.save(os.path.join(DBG, f"{job_id}_updo_canvas.jpg"), quality=91)
    tgt.save(os.path.join(DBG, f"{job_id}_updo_tgt.png"))
    kill.save(os.path.join(DBG, f"{job_id}_updo_kill.png"))

    avoid = maskmod.union(maskmod.dilate(tgt, px=3), maskmod.head_rect_mask(canvas, r["fb"]))
    base = compose.plate_fill(canvas, kill, avoid=avoid)
    base = _despeckle(base, kill, r["fb"], canvas.size, hair=r["hair"])
    base.save(os.path.join(DBG, f"{job_id}_updo_base.jpg"), quality=92)

    # ---- 第一段：清掉**头侧**那一段旧披发（只给头部这一条，别给肩膀）----
    # 肩膀交给几何填充（plate_fill + _despeckle，确定性、绝不会长回头发）。
    # 把肩膀也交给模型时，mask 又长又宽，SD1.5 十次有八次把长发画回来（实测两次）。
    y_band = int(r["fb"][1] + r["fb"][3] * 1.02)
    ka = np.asarray(kill).copy()
    ka[y_band:, :] = 0
    kill_head = Image.fromarray(ka)
    m_clean = maskmod.dilate(kill_head, px=max(8, SIZE // 96))
    cleans = prov.generate_n(preprocess.to_bytes(base, "PNG"), preprocess.to_bytes(m_clean, "PNG"),
                             prompts.UPDO_CLEAN_POS, prompts.UPDO_CLEAN_NEG, SIZE, n=2,
                             denoise=1.0)
    clean = min(cleans, key=lambda g: _dark_in(g, kill_head))
    clean.save(os.path.join(DBG, f"{job_id}_updo_clean.jpg"), quality=92)
    base = compose.paste_patch(base, clean, kill_head)
    # 剪影内部也抹掉（只留发际线那一圈做锚点）：不抹的话模型会"顺着原发丝 refine"，
    # 卷发还是卷发、披肩还是披肩，只是短了一点。抹干净它才肯重新长一遍。
    if os.environ.get("UPDO_BARE", "0") == "1":
        inner = np.asarray(tgt).copy()
        inner[:int(r["fb"][1] + r["fb"][3] * 0.02), :] = 0
        base = compose.plate_fill(base, Image.fromarray(inner), avoid=kill_head)
    base.save(os.path.join(DBG, f"{job_id}_updo_base2.jpg"), quality=92)

    # ---- 第二段：只在剪影里画新发型（轮廓已被几何锁死）----
    patch = tgt
    m_g = maskmod.dilate(tgt, px=max(8, SIZE // 90))
    pos = prompts.UPDO_HIGH_POS if kind == "high" else prompts.UPDO_LOW_POS
    cands = prov.generate_n(preprocess.to_bytes(base, "PNG"), preprocess.to_bytes(m_g, "PNG"),
                            pos, prompts.UPDO_NEG, SIZE, n=3, denoise=0.92, ref_bytes=ref_b)
    for i, g in enumerate(cands):
        g.save(os.path.join(DBG, f"{job_id}_updo_gen{i}.jpg"), quality=91)
    best = max(cands, key=lambda g: _updo_score(g, r["fb"], tgt))
    best.save(os.path.join(DBG, f"{job_id}_updo_pick.jpg"), quality=92)

    if pad:
        best = updo.unpad(best, pad)
        base = updo.unpad(base, pad)
        tgt = updo.unpad(tgt, pad)
    # 模型重画的背景/皮肤和底图有一点点色差，贴上去就是一条"灰带"。
    # 先把 patch 里偏亮的那部分（背景/衣服）对齐到底图，再贴。
    # 用 patch（剪影）整块贴：按"只贴暗像素"筛会留下一个亮色方块（发丝 alpha 试过，
    # 剪影外那圈底图填充色露出来，比灰晕更难看）。灰晕交给 _bg_align 类手段，不要动这里。
    out = compose.composite_patch(p["orig"], best, p["rect"], base, patch, target_lab=target_lab)
    out = _restore_skin(out, p)
    return [out], best


def _edit_ref_bytes(style_row):
    """整头编辑用的发型参考图：优先库里的主图原图（640x960），没有才退回封面缩略图。

    封面是 1:1 缩略图（头发细节被压掉了），主图能看清刘海和卷度 —— 这类模型是看图办事的，
    参考图糊一分，出来的发型就模糊一分。
    """
    # 用户自己上传的参考图：直接用原图，不走发型库目录
    rp = style_row.get("_ref_path") or ""
    if rp and os.path.exists(rp):
        return open(rp, "rb").read(), rp
    sid = str(style_row.get("id") or "")
    for name in ("main.jpg", "front.jpg", "side.jpg"):
        rp = os.path.join(GALLERY, sid, name)
        if os.path.exists(rp):
            return open(rp, "rb").read(), rp
    cu = style_row.get("cover_url") or ""
    if cu.startswith("/covers/"):
        rp = os.path.join(COVERS, os.path.basename(cu))
        if os.path.exists(rp):
            return open(rp, "rb").read(), rp
    return None, ""


def _run_job(job_id, photo_row, style_row, provider_name, n=1, model=None, color=None):
    try:
        db.update_job(job_id, status=1)
        prov = PROVIDERS[provider_name]
        # 前台选了模型就只对这一单生效：copy 一份再改 model。
        # 直接改单例 provider 会把同时跑的另一单也换掉。
        if model and getattr(prov, "model", None) is not None:
            prov = copy.copy(prov)
            prov.model = model
            print("本次任务模型:", model)
        data = open(photo_row["path"], "rb").read()
        p = preprocess.prepare(data, size=SIZE)
        fb = maskmod.face_box_in_crop(p["face"], p["rect"], SIZE)

        # 目标发型长度：15 齐下颌 / 20 过下巴 / 30 到锁骨。
        # 只在"现有头发像素"里重绘，短发选长发出来还是短发 —— 用户看到的就是"没变化"。
        # 后台加款时手工选的 length 优先（比从文案里猜准），没填才走文案识别
        L = int(style_row.get("length") or 0) or maskmod.length_cm(
            style_row.get("name_en"), style_row.get("prompt_en"),
            style_row.get("prompt_zh"), " ".join(style_row.get("tags") or []))
        # 扎发 / 盘发不是长度问题，是"头发往哪儿走"：先认出来，走单独的几何分支
        updo = maskmod.updo_kind(style_row.get("name"), style_row.get("name_en"),
                                 style_row.get("prompt_en"), style_row.get("prompt_zh"),
                                 " ".join(style_row.get("tags") or []))
        if updo:
            L = 15 if updo == "high" else 20
        m_hair = maskmod.hair_mask(p["img"], fb)            # 现有头发（调色统计用）
        m = maskmod.style_mask(p["img"], fb, L)             # 目标发型可占区域（回贴用）
        m_g = maskmod.dilate(m, px=max(6, SIZE // 96))      # 给模型看的略扩版

        # 目标发尾比现有头发还短 → "剪短"分支：整块重画 + 提高 denoise，
        # 否则旧长发会留在原地（pipeline 里最容易"看起来没变"的就是这一档）
        short_case = False
        if fb:
            end_y = fb[1] + fb[3] * maskmod.LEN_END.get(int(L), 1.12)
            short_case = end_y < maskmod.hair_bottom(m_hair) - 0.02 * SIZE
        mask_path = os.path.join(DBG, f"{job_id}_mask.png")
        m_g.save(mask_path)

        prompt = prompts.build(style_row, "zh" if provider_name in ("gemini", "mock") else "en")
        neg = prompts.NEGATIVE_EN
        if updo:
            # 底下那层走「剪短」的文案（已验证干净），扎发的收拢词只用在发髻那一小块
            prompt = prompts.build_short(style_row, "en")
            neg = prompts.build_short_neg(prompts.NEGATIVE_EN)
        elif short_case:
            # 长度词必须顶到最前面，否则被后面一大串发量词盖掉，出来还是及肩
            prompt = prompts.build_short(style_row, "en")
            neg = prompts.build_short_neg(prompts.NEGATIVE_EN)
        crop_b, mask_b = preprocess.to_bytes(p["img"], "PNG"), preprocess.to_bytes(m_g, "PNG")

        # 目标发型有封面图就当参考图喂给模型（本地 IP-Adapter / gemini 多图都吃这个），
        # 顺便从封面头发取目标发色，回贴时按它上色 —— 发色不会漂成红棕
        ref_b, ref_path = _edit_ref_bytes(style_row)

        # 目标发色：文案里写了发色就按文案（模型自己爱加暖色，要压回来）；
        # 没写就留 None → 回贴时跟用户自己原发色对齐。
        ab = compose.color_from_prompt(
            (style_row.get("prompt_en") or "") + " " + (style_row.get("prompt_zh") or ""))
        target_lab = (0.0, ab[0], ab[1]) if ab else None

        # ---- 扎发 / 盘发：干净的短底 + 头顶一个髻 ----
        # 走单独一条路：几何剪短（把长度锁死）→ 模型把脖子/衣领补干净（顺带收成短底）
        # → 只在头顶一小块里长发髻。低马尾/低盘发只做前两步。
        # ---- 云端图像大模型：整头重画（apimart / gemini / openai）----
        # 这一档不遮 mask、不切几何、不做剪短：这些模型能理解"换成这个发型"，
        # 遮 mask 反而把它限死成"在原发范围里描一遍"——那正是本地 SD1.5 的死法。
        # 传图顺序：图1 = 发型参考（封面），图2 = 本人，和 prompts.build_edit 里的措辞一致。
        edit_outs = None
        if provider_name in EDIT_PROVIDERS:
            pos = prompts.build_edit(style_row, "zh", has_ref=bool(ref_b),
                                     color=color or "")
            # 喂「整图补边方图」，不是方框裁剪版：切掉的那部分回贴时没人能补，
            # 头上就会浮出一圈旧头发的鬼影、底下留一条旧衣领/水印。
            edit_b = preprocess.to_bytes(p["pad_img"], "PNG")
            gens = prov.generate_n(edit_b, mask_b, pos, "", SIZE,
                                   n=max(1, min(int(n or 1), 4)), ref_bytes=ref_b)
            W, H = p["orig"].size
            # 不调 _tint_ab：整图加 a/b 偏移会把衣服/背景一起染色（出来一片灰紫雾），
            # 深色发根还会被推成蓝块。云端模型看得见参考图，发色自己就对得上。
            outs = [compose.composite_block(p["orig"], g, (0, 0, W, H),
                                            pad=p["pad"], side=p["side"])
                    for g in gens]
            edit_outs = (gens, outs)
            print("云端整头编辑:", provider_name, len(gens), "张 | 图1=",
                  os.path.basename(ref_path or "-"), "图2=本人")
            print("   换发色:", color or "跟随款式原色")
            print("   prompt 前 80 字:", pos[:80].replace("\n", " "))

        updo_outs = None
        if edit_outs is None and updo and provider_name == "comfy" and fb:
            updo_outs, updo_gen = _do_updo(prov, p, fb, m_hair, updo, job_id, target_lab, ref_b)
            if updo_outs:
                print("扎发完成:", updo)

        if edit_outs is not None:
            gens, outs = edit_outs
            idx, scores = 0, []
        elif updo_outs:
            gens, outs, idx, scores = [updo_gen], updo_outs, 0, []
            patch = m_g
        else:
            # ---- 剪短：几何剪到位（长度锁死）→ 模型只修发尾那一小条 ----
            # SD1.5 用 prompt 控制长度十次里只有两三次听话，业务上没法用。
            # 所以长度交给几何（cut_hair，100% 确定），模型只负责把"刀切发尾"改成自然发尾。
            gen_src_b = crop_b
            gen_src_img = p["img"]
            base_img = p["img"]
            patch = m_g
            v2 = False
            if short_case and provider_name == "comfy":
                try:
                    y_line = int(end_y) if end_y else int(SIZE * 0.62)
                    cut, patch = compose.cut_hair(p["img"], m_hair, y_line)
                    cut.save(os.path.join(DBG, f"{job_id}_cut.jpg"), quality=91)
                    base_img = cut
                    gen_src_img = cut
                    gen_src_b = preprocess.to_bytes(cut, "PNG")
                    # 只给模型「发尾那一条」：给整块它一定把长度补回去
                    patch = compose.tail_band(m_hair, y_line, fb[3] if fb else SIZE * 0.3)
                    patch.save(mask_path)
                    v2 = True
                except Exception as e:
                    print("剪短几何处理失败，回退单段:", e)

            n = max(1, min(int(n or 1), 4))
            gen_kw = {"ref_bytes": ref_b}
            if short_case and provider_name == "comfy" and v2:
                gen_kw["denoise"] = 0.7   # 只修发尾，别让模型有空间把长度补回去
            if v2:
                mask_b = preprocess.to_bytes(patch, "PNG")
            if n > 1 and getattr(prov, "supports_n", False):
                gens = prov.generate_n(gen_src_b, mask_b, prompt, neg, SIZE, n=n, **gen_kw)
            else:
                gens = [prov.generate(gen_src_b, mask_b, prompt, neg, SIZE, **gen_kw)]
                n = 1

            if v2:
                outs = [compose.composite_patch(p["orig"], g, p["rect"], base_img, patch,
                                                target_lab=target_lab) for g in gens]
            elif short_case:
                outs = [compose.composite_short(p["orig"], g, p["rect"], base_img, m_g,
                                                target_lab=target_lab) for g in gens]
            else:
                outs = [compose.composite(p["orig"], g, p["rect"], m, target_lab=target_lab,
                                          stats_mask=m_hair, full=False) for g in gens]
                # 脸/脖子必须回到原图：这一档模型会在脸侧糊出一片暗斑，
                # 贴回去就是"脸上两块灰 + 硬竖边"（用户截图里那张）。
                # 皮肤不交给 AI，是这条流水线的硬保险，所有非扎发分支都要走。
                outs = [_restore_skin(o, p) for o in outs]

            idx, scores = (picker.best(gen_src_img, gens, patch) if len(gens) > 1 else (0, []))

        os.makedirs(CAND, exist_ok=True)
        cand_meta = []
        for k, o in enumerate(outs):
            cn = f"{job_id}_{k}.jpg"
            o.save(os.path.join(CAND, cn), quality=93)
            sc = scores[k] if k < len(scores) else None
            cand_meta.append({"url": f"/files/candidates/{cn}", "index": k,
                              "selected": k == idx, "score": sc})
        json.dump({"selected": idx, "n": len(outs), "candidates": cand_meta},
                  open(os.path.join(CAND, f"{job_id}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)

        gen = gens[idx]
        out = outs[idx]
        name = f"{job_id}.jpg"
        out.save(os.path.join(RES, name), quality=94)

        # 同时存一张 gen 原图，方便调参对比
        gen.save(os.path.join(DBG, f"{job_id}_raw.jpg"), quality=92)
        # 真实花掉的美元（apimart 任务回包里有 cost），写进台账 —— 老板要的是"这一单到底赚多少"
        db.update_job(job_id, status=2, result_path=name,
                      cost_usd=round(float(getattr(prov, "last_cost", 0) or 0), 6))
        print("本单模型成本: $%s ≈ ¥%.3f" % (getattr(prov, "last_cost", 0),
                                            float(getattr(prov, "last_cost", 0) or 0) * billing.USD_CNY))
    except Exception as e:
        db.update_job(job_id, status=3, err=f"{type(e).__name__}: {e}")
        # 失败必须退：扣的是真金白银（免费次数也一样），不退就是"点一次少一次"
        try:
            jr = db.get_job(job_id)
            if jr and (jr["credits_spent"] or jr["free_spent"]):
                auth.refund(jr["openid"], jr["free_spent"], jr["credits_spent"],
                            "job %s failed: %s" % (job_id, str(e)[:80]))
                print("已退还：免费 %s 次 / 积分 %s" % (jr["free_spent"], jr["credits_spent"]))
        except Exception as e2:
            print("退款失败:", e2)
        traceback.print_exc()


@app.post("/api/generate")
def api_generate(payload: dict, request: Request, openid: str = Header(default="")):
    m = need_user(request, openid)      # 没登录 → 401（本机 admin 除外）
    u = m["uid"]
    admin = _is_admin_ctx(m)
    pid = payload.get("photo_id")
    sid = payload.get("style_id")
    ref_id = str(payload.get("ref_id") or "").strip()
    provider_name = payload.get("provider") or default_provider()

    if not pid or (not sid and not ref_id):
        raise HTTPException(400, "photo_id / style_id 必填")

    # 自定义参考图：用户传的一张「我想要这个发型」图片。
    # 只认本人上传的那条记录，伪造 id 直接 404；构造的 style_row 不带任何
    # 发型库文案，prompt 走「照抄图1」分支，和发型库的数据互不沾边。
    if ref_id:
        with db.conn() as c:
            rr = c.execute("select * from refs where id=?", (ref_id,)).fetchone()
        if not rr or (m["account"] and rr["uid"] != m["account"]["id"] and not admin):
            raise HTTPException(404, "参考图不存在")
        style_row = {"id": "custom:" + ref_id, "name": "自定义参考 · " + (rr["name"] or ""),
                     "name_en": "", "prompt_en": "", "prompt_zh": "", "tags": [],
                     "cover_url": "", "curl": "", "cat": "自定义参考",
                     "_ref_path": os.path.join(REFS, rr["uid"], rr["path"])}
        sid = style_row["id"]
    if sid not in stylelib.BY_ID and not ref_id:
        raise HTTPException(404, "style 不存在")
    if provider_name not in PROVIDERS:
        raise HTTPException(400, "provider 不存在")
    if not prov_available(provider_name):
        raise HTTPException(503, f"后端 {provider_name} 未就绪：{REASON.get(provider_name, '缺 key 或服务未启动')}")

    photo = db.get_photo(pid)
    if not photo:
        raise HTTPException(404, "photo 不存在")

    # 张数：管理员 4 张，普通用户 USER_MAX_N（默认 2）—— 每多一张就是多一次 API 钱
    n = max(1, min(int(payload.get("n") or 1), billing.ADMIN_MAX_N if admin else billing.USER_MAX_N))

    # 选模型是管理员专属：普通用户传了也按默认模型跑，
    # 否则一个手贱选到 seedream-5-0-pro（$0.36/张 ≈ 2.6 元）就直接亏穿。
    model = str(payload.get("model") or "").strip() or None
    if model and not admin:
        t = billing.tier_of(model)
        if billing.BLOCK_MAX_FOR_USER and t["mul"] >= 5:
            raise HTTPException(403, "这个模型只对管理员开放（单价太高）")
        model = None

    # 换发色：只认调色板里的字面值。这个字段会原样拼进 prompt，
    # 不校验就等于让前端往模型指令里插任意文本。
    style_row = style_row if ref_id else stylelib.BY_ID[sid]
    color = str(payload.get("color") or "").strip()
    allowed = set(prompts.color_choices(style_row))
    color = color if color in allowed else ""

    # ---- 扣费：免费次数优先，用完扣积分。不够就 402，前端弹充值面板 ----
    eff_model = model or (os.environ.get("APIMART_IMAGE_MODEL") or "")
    cost = billing.credits_for(eff_model if provider_name == "apimart" else None, n)
    pay = auth.charge(m["uid"], cost, n) if m["account"] else {"ok": True, "free": 0, "credits": 0}
    if not pay.get("ok"):
        return JSONResponse(status_code=402, content={
            "error": "积分不足", "need": cost, "credits": pay.get("have", 0),
            "free_left": pay.get("free_left", 0), "packages": billing.packages(),
            "hint": "注册送 %d 次免费，用完按 %d 积分/张算，1 元 = 10 积分"
                    % (billing.FREE_GENS, billing.GEN_CREDITS)})

    job_id = db.create_job(u, pid, sid, provider_name)
    db.update_job(job_id, credits_spent=int(pay.get("credits") or 0),
                  free_spent=int(pay.get("free") or 0))
    t = threading.Thread(target=_run_job, daemon=True,
                         args=(job_id, photo, style_row, provider_name, n, model, color))
    t.start()
    left = auth.public(auth.get(m["uid"])) if m["account"] else None
    return {"job_id": job_id, "status": 0, "candidates": n, "model": model,
            "color": color or None, "spent": {"credits": int(pay.get("credits") or 0),
                                              "free": int(pay.get("free") or 0)},
            "user": left, "cost_cny": billing.cny_cost(eff_model, n)}


@app.get("/api/job/{job_id}")
def api_job(job_id: str):
    j = db.get_job(job_id)
    if not j:
        raise HTTPException(404, "job 不存在")
    meta = None
    mp = os.path.join(CAND, f"{j['id']}.json")
    if os.path.exists(mp):
        try:
            meta = json.load(open(mp, encoding="utf-8"))
        except Exception:
            meta = None
    return {"id": j["id"], "status": j["status"],
            "url": f"/files/results/{j['result_path']}" if j["result_path"] else None,
            "err": j["err"], "style_id": j["style_id"], "provider": j["provider"],
            "candidates": meta, "spent": {"credits": j["credits_spent"] or 0,
                                          "free": j["free_spent"] or 0},
            "cost_usd": j["cost_usd"] or 0}


@app.get("/api/history")
def api_history(request: Request, openid: str = Header(default=""), all: str = ""):
    """默认只回自己的。游客一律空（老板的历史数据只有管理员登录才看得到）。"""
    m = me(request, openid)
    if m["guest"]:
        return {"items": []}
    if all and _is_admin_ctx(m):
        with db.conn() as c:
            rows = c.execute("select id,openid,style_id,status,result_path,created_at "
                             "from jobs order by created_at desc limit 60").fetchall()
        items = [dict(r) for r in rows]
    else:
        items = db.history(m["uid"])
    for it in items:
        if it.get("result_path"):
            it["url"] = f"/files/results/{it['result_path']}"
    return {"items": items}


# ---------------- 管理员：生成记录管理（查看 / 删除） ----------------
def _job_file_paths(j):
    """一条任务占用的所有文件：结果图 + 候选图 + 候选清单 json。"""
    paths = []
    if j.get("result_path"):
        paths.append(os.path.join(RES, j["result_path"]))
    mp = os.path.join(CAND, f"{j['id']}.json")
    if os.path.exists(mp):
        paths.append(mp)
        try:
            meta = json.load(open(mp, encoding="utf-8"))
            for c in (meta if isinstance(meta, list) else meta.get("cands", [])):
                f = c.get("file") if isinstance(c, dict) else c
                if f:
                    paths.append(os.path.join(CAND, f))
        except Exception:
            pass
    return [p for p in paths if os.path.exists(p)]


@app.get("/api/admin/jobs")
def api_admin_jobs(request: Request, x_admin_token: str = Header(default="")):
    _need_admin(x_admin_token, request)
    with db.conn() as c:
        rows = c.execute(
            "select j.*, a.username from jobs j left join accounts a on a.id=j.openid "
            "order by j.created_at desc limit 200").fetchall()
    items = []
    for r in rows:
        j = dict(r)
        j.pop("photo_id", None)
        j["url"] = f"/files/results/{j['result_path']}" if j.get("result_path") else None
        items.append(j)
    return {"items": items,
            "disk": {"results": len(os.listdir(RES)) if os.path.isdir(RES) else 0,
                     "candidates": len(os.listdir(CAND)) if os.path.isdir(CAND) else 0}}


@app.delete("/api/admin/jobs/{jid}")
def api_admin_job_del(jid: str, request: Request, x_admin_token: str = Header(default="")):
    _need_admin(x_admin_token, request)
    with db.conn() as c:
        row = c.execute("select * from jobs where id=?", (jid,)).fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")
        n = 0
        for p in _job_file_paths(dict(row)):
            try:
                os.remove(p); n += 1
            except OSError:
                pass
        c.execute("delete from jobs where id=?", (jid,))
    return {"ok": True, "files_removed": n}


@app.post("/api/admin/jobs/clear")
def api_admin_jobs_clear(payload: dict, request: Request, x_admin_token: str = Header(default="")):
    """清空全部生成记录与成图文件（不动账号 / 积分 / 订单）。"""
    _need_admin(x_admin_token, request)
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("select * from jobs").fetchall()]
        n = 0
        for j in rows:
            for p in _job_file_paths(j):
                try:
                    os.remove(p); n += 1
                except OSError:
                    pass
        c.execute("delete from jobs")
    return {"ok": True, "jobs_removed": len(rows), "files_removed": n}


db.init()
auth.ensure_admin(ADMIN_USER, ADMIN_PASS)
