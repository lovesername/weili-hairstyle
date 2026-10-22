# -*- coding: utf-8 -*-
"""自动到账支付：支付宝当面付 / 微信支付 v3 Native / 易支付（个人可接的聚合）。

为什么需要它：微信、支付宝的「个人经营码」只是一张图片，钱进你手机，
**没有回调**，所以只能人工确认。要「用户付完自动加积分」，必须走有
异步通知（notify）的通道：

    alipay  支付宝当面付 precreate：个体户/企业可签，生成 qr_code，notify 验签 RSA2
    wechat  微信支付 v3 Native：个体户/企业可签，code_url + 平台证书验签 + AES-GCM 解密
    epay    易支付类聚合：个人可接（第三方），MD5 签名，最快能跑起来
    manual  兜底：还是经营码 + 人工确认（后台点一下）

配置存在 data/pay.json（不对外），后台 ⑥ 填。密钥只落本地文件，接口只回掩码。
"""
import base64
import hashlib
import hmac
import json
import os
import random
import string
import time
import urllib.parse

import requests
from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 跟 app.py 同一个开关：容器里把运行时配置落到持久卷，重新部署不丢。
_RT = os.environ.get("WEILI_RUNTIME_DIR", "").strip()
PAY_FILE = os.path.join(_RT, "pay.json") if _RT else os.path.join(ROOT, "data", "pay.json")
QR_DIR = os.path.join(ROOT, "storage", "qr")
os.makedirs(QR_DIR, exist_ok=True)

DEFAULT = {
    "mode": "manual",
    "base_url": "",                 # 公网地址，如 https://weili.example.com（拼 notify_url 用）
    "alipay": {"appid": "", "private_key": "", "public_key": "",
               "gateway": "https://openapi.alipay.com/gateway.do"},
    "wechat": {"appid": "", "mchid": "", "serial_no": "", "private_key": "",
               "apiv3_key": "", "platform_cert": ""},
    "epay": {"gateway": "", "pid": "", "key": "", "type": "alipay"},
}


def cfg():
    try:
        d = json.load(open(PAY_FILE, encoding="utf-8"))
    except Exception:
        d = {}
    out = json.loads(json.dumps(DEFAULT))
    for k, v in (d or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def save(d):
    cur = cfg()
    for k, v in d.items():
        if isinstance(v, dict) and isinstance(cur.get(k), dict):
            cur[k].update({kk: vv for kk, vv in v.items() if vv is not None})
        elif v is not None:
            cur[k] = v
    os.makedirs(os.path.dirname(PAY_FILE), exist_ok=True)
    json.dump(cur, open(PAY_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return cur


def public_cfg():
    """给后台看的：密钥只回「配没配」，不回内容。"""
    c = cfg()
    return {
        "mode": c["mode"], "base_url": c["base_url"],
        "alipay": {"appid": c["alipay"]["appid"], "gateway": c["alipay"]["gateway"],
                   "private_key_set": bool(c["alipay"]["private_key"]),
                   "public_key_set": bool(c["alipay"]["public_key"])},
        "wechat": {"appid": c["wechat"]["appid"], "mchid": c["wechat"]["mchid"],
                   "serial_no": c["wechat"]["serial_no"],
                   "private_key_set": bool(c["wechat"]["private_key"]),
                   "apiv3_key_set": bool(c["wechat"]["apiv3_key"]),
                   "platform_cert_set": bool(c["wechat"]["platform_cert"])},
        "epay": {"gateway": c["epay"]["gateway"], "pid": c["epay"]["pid"],
                 "key_set": bool(c["epay"]["key"]), "type": c["epay"]["type"]},
        "ready": ready(c),
        "channels": channels(c),
    }


def channels(c=None):
    """当前可用通道。auto=True 表示「付完自动到账」。"""
    c = c or cfg()
    m = c["mode"]
    a = c["alipay"]; w = c["wechat"]; e = c["epay"]
    out = []
    if m == "alipay" and a["appid"] and a["private_key"] and a["public_key"]:
        out.append({"k": "alipay", "name": "支付宝", "auto": True})
    if m == "wechat" and w["mchid"] and w["private_key"] and w["apiv3_key"]:
        out.append({"k": "wechat", "name": "微信支付", "auto": True})
    if m == "epay" and e["gateway"] and e["pid"] and e["key"]:
        out.append({"k": "epay", "name": "在线支付", "auto": True})
    if not out:                      # 兜底：经营码人工确认
        out.append({"k": "manual", "name": "扫码转账", "auto": False})
    return out


def ready(c=None):
    return any(x["auto"] for x in channels(c))


def notify_url(kind):
    c = cfg()
    base = (c.get("base_url") or os.environ.get("PUBLIC_BASE_URL", "")).rstrip("/")
    if not base:
        return ""
    return "%s/api/pay/notify/%s" % (base, kind)


def _nonce(n=16):
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


# ---------------- 支付宝当面付 ----------------
ALI_GATEWAY = "https://openapi.alipay.com/gateway.do"


def _ali_sign(params, private_key_pem):
    msg = "&".join("%s=%s" % (k, params[k]) for k in sorted(params) if params[k] not in ("", None))
    key = RSA.import_key(private_key_pem)
    return base64.b64encode(pkcs1_15.new(key).sign(SHA256.new(msg.encode("utf-8")))).decode()


def _ali_verify(params, public_key_pem):
    sign = params.get("sign", "")
    clean = {k: v for k, v in params.items() if k not in ("sign", "sign_type") and v not in ("", None)}
    msg = "&".join("%s=%s" % (k, clean[k]) for k in sorted(clean))
    try:
        key = RSA.import_key(public_key_pem)
        pkcs1_15.new(key).verify(SHA256.new(msg.encode("utf-8")), base64.b64decode(sign))
        return True
    except Exception:
        return False


def alipay_create(oid, amount_cents, subject):
    c = cfg()["alipay"]
    p = {
        "app_id": c["appid"], "method": "alipay.trade.precreate", "format": "JSON",
        "charset": "utf-8", "sign_type": "RSA2", "version": "1.0",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "notify_url": notify_url("alipay") or "",
        "biz_content": json.dumps({
            "out_trade_no": oid, "total_amount": "%.2f" % (amount_cents / 100.0),
            "subject": subject, "timeout_express": "30m",
        }, ensure_ascii=False, separators=(",", ":")),
    }
    p = {k: v for k, v in p.items() if v}
    p["sign"] = _ali_sign(p, c["private_key"])
    r = requests.post(c["gateway"] or ALI_GATEWAY, data=p,
                      headers={"Content-Type": "application/x-www-form-urlencoded"},
                      timeout=20, proxies={"http": None, "https": None})
    j = r.json()
    resp = j.get("alipay_trade_precreate_response") or {}
    if resp.get("code") != "10000" or not resp.get("qr_code"):
        raise RuntimeError("支付宝下单失败：%s %s" % (resp.get("code"), resp.get("sub_msg") or resp.get("msg")))
    return {"qr": resp["qr_code"], "kind": "alipay"}


def alipay_notify(form):
    c = cfg()["alipay"]
    if form.get("app_id") and c["appid"] and form["app_id"] != c["appid"]:
        return None
    if not _ali_verify(form, c["public_key"]):
        return None
    if form.get("trade_status") not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        return None
    return {"oid": form.get("out_trade_no"), "amount_cents": int(round(float(form.get("total_amount", 0)) * 100)),
            "trade_no": form.get("trade_no")}


# ---------------- 微信支付 v3 Native ----------------
def _wx_sign(method, path, body, private_pem):
    msg = "%s\n%s\n%d\n%s\n" % (method, path, int(time.time()), body)
    key = RSA.import_key(private_pem)
    return base64.b64encode(pkcs1_15.new(key).sign(SHA256.new(msg.encode("utf-8")))).decode(), int(time.time())


def wechat_create(oid, amount_cents, subject):
    c = cfg()["wechat"]
    body = json.dumps({
        "appid": c["appid"], "mchid": c["mchid"], "description": subject[:60],
        "out_trade_no": oid, "notify_url": notify_url("wechat"),
        "amount": {"total": int(amount_cents), "currency": "CNY"},
    }, ensure_ascii=False, separators=(",", ":"))
    path = "/v3/pay/transactions/native"
    sig, ts = _wx_sign("POST", path, body, c["private_key"])
    auth = ('WECHATPAY2-SHA256-RSA2048 mchid="%s",nonce_str="%s",signature="%s",'
            'timestamp="%d",serial_no="%s"') % (c["mchid"], _nonce(), sig, ts, c["serial_no"])
    r = requests.post("https://api.mch.weixin.qq.com" + path, data=body.encode("utf-8"),
                      headers={"Authorization": auth, "Content-Type": "application/json",
                               "Accept": "application/json"},
                      timeout=20, proxies={"http": None, "https": None})
    if r.status_code not in (200, 201):
        raise RuntimeError("微信下单失败：%s %s" % (r.status_code, r.text[:200]))
    j = r.json()
    if not j.get("code_url"):
        raise RuntimeError("微信未返回 code_url：%s" % str(j)[:200])
    return {"qr": j["code_url"], "kind": "wechat"}


def wechat_notify(headers, raw_body):
    """验签（平台证书）→ AES-256-GCM 解密 → 返回订单号。"""
    c = cfg()["wechat"]
    ts = headers.get("Wechatpay-Timestamp", "")
    nonce = headers.get("Wechatpay-Nonce", "")
    sig = headers.get("Wechatpay-Signature", "")
    if not (ts and nonce and sig and c["platform_cert"]):
        return None
    msg = "%s\n%s\n%s\n" % (ts, nonce, raw_body)
    try:
        key = RSA.import_key(c["platform_cert"])
        pkcs1_15.new(key).verify(SHA256.new(msg.encode("utf-8")), base64.b64decode(sig))
    except Exception:
        return None
    j = json.loads(raw_body)
    res = j.get("resource") or {}
    if not res.get("ciphertext"):
        return None
    try:
        aes = AES.new(c["apiv3_key"].encode("utf-8"), AES.MODE_GCM,
                      nonce=res["nonce"].encode("utf-8"))
        # 微信把 associated_data 当 AAD 喂给 GCM；ciphertext 里末尾 16 字节是 tag
        aes.update((res.get("associated_data") or "").encode("utf-8"))
        blob = base64.b64decode(res["ciphertext"])
        plain = aes.decrypt_and_verify(blob[:-16], blob[-16:])
        d = json.loads(plain.decode("utf-8"))
    except Exception as e:
        print("微信回调解密失败:", e)
        return None
    if d.get("trade_state") != "SUCCESS":
        return None
    amt = (d.get("amount") or {}).get("total") or 0
    return {"oid": d.get("out_trade_no"), "amount_cents": int(amt),
            "trade_no": d.get("transaction_id")}


# ---------------- 易支付 / 码支付（个人可接） ----------------
def _epay_sign(params, key):
    items = sorted((k, v) for k, v in params.items()
                   if k not in ("sign", "sign_type") and v not in ("", None))
    s = "&".join("%s=%s" % (k, v) for k, v in items) + key
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def epay_create(oid, amount_cents, subject, pay_type=""):
    c = cfg()["epay"]
    t = pay_type or c.get("type") or "alipay"
    p = {"pid": c["pid"], "type": t, "out_trade_no": oid,
         "notify_url": notify_url("epay"), "return_url": (cfg().get("base_url") or "") + "/",
         "name": subject[:60], "money": "%.2f" % (amount_cents / 100.0)}
    p["sign"] = _epay_sign(p, c["key"])
    p["sign_type"] = "MD5"
    r = requests.post(c["gateway"].rstrip("/") + "/submit.php", data=p, timeout=20,
                      proxies={"http": None, "https": None})
    txt = r.text.strip()
    # 有的站直接回 JSON，有的回一段带二维码链接的 HTML
    try:
        j = r.json()
        qr = j.get("qrcode") or j.get("qr") or j.get("url") or j.get("payurl")
        if qr:
            return {"qr": qr, "kind": "epay", "type": t}
    except Exception:
        pass
    import re as _re
    m = _re.search(r'(https?://[^"\'\s<>]+)', txt)
    if m:
        return {"qr": m.group(1), "kind": "epay", "type": t}
    raise RuntimeError("易支付没有返回二维码：%s" % txt[:160])


def epay_notify(params):
    c = cfg()["epay"]
    if str(params.get("pid") or "") and c["pid"] and str(params["pid"]) != str(c["pid"]):
        return None
    sign = (params.get("sign") or "").lower()
    if not sign or sign != _epay_sign(params, c["key"]).lower():
        return None
    if (params.get("trade_status") or "TRADE_SUCCESS") not in ("TRADE_SUCCESS", "SUCCESS"):
        return None
    try:
        cents = int(round(float(params.get("money", 0)) * 100))
    except ValueError:
        return None
    return {"oid": params.get("out_trade_no"), "amount_cents": cents,
            "trade_no": params.get("trade_no")}


def epay_query(oid):
    """主动查单：回调丢了（容器重启 / 出站抖动 / 网关重试耗尽）时的兜底。

    没有这个，用户付了钱、回调没到，就只能在后台手动点确认 —— 那不叫自动充值。
    标准易支付协议：GET {gateway}/api.php?act=order&pid=&key=&out_trade_no=
    返回 code=1、status=1 表示已支付。
    """
    c = cfg()["epay"]
    if not (c.get("gateway") and c.get("pid") and c.get("key")):
        return None
    try:
        r = requests.get(c["gateway"].rstrip("/") + "/api.php",
                         params={"act": "order", "pid": c["pid"], "key": c["key"],
                                 "out_trade_no": oid},
                         timeout=15, proxies={"http": None, "https": None})
        j = r.json()
    except Exception as e:
        print("易支付查单失败:", oid, e)
        return None
    if str(j.get("code")) != "1" or str(j.get("status")) != "1":
        return None
    try:
        cents = int(round(float(j.get("money") or 0) * 100))
    except (TypeError, ValueError):
        return None
    return {"oid": j.get("out_trade_no") or oid, "amount_cents": cents,
            "trade_no": j.get("trade_no")}


def query(oid):
    """按当前通道主动查单；manual/未配置一律返回 None。"""
    if cfg().get("mode") == "epay":
        return epay_query(oid)
    return None


# ---------------- 统一下单 ----------------
def create(oid, amount_cents, subject, channel=""):
    """返回 {mode:'auto', qr, kind} 或抛异常（调用方退回 manual）。"""
    c = cfg()
    m = c["mode"]
    if m == "alipay":
        return dict(alipay_create(oid, amount_cents, subject), mode="auto")
    if m == "wechat":
        return dict(wechat_create(oid, amount_cents, subject), mode="auto")
    if m == "epay":
        return dict(epay_create(oid, amount_cents, subject, channel), mode="auto")
    raise RuntimeError("当前是经营码模式")


def qr_png(text, name):
    """把二维码内容画成 PNG（微信/alipay 的 qr 文本都能直接画）。"""
    import qrcode
    p = os.path.join(QR_DIR, name + ".png")
    if not os.path.exists(p):
        img = qrcode.make(text, box_size=8, border=2)
        img.save(p)
    return p
