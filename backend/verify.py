# -*- coding: utf-8 -*-
"""验证码：邮箱（SMTP 标准库）/ 手机（阿里云短信，手搓签名）。

为什么不用第三方 SDK：smtplib、hmac、hashlib 都是标准库。云端构建少两个依赖，
就少两处 "pip install 失败导致整个服务起不来" 的可能。

成本事实（老板要知道）：
    邮箱  0 元。QQ / 163 开个 SMTP 授权码就能发，每天几百封随便用。
    手机  ≈0.045 元/条。阿里云/腾讯云都要实名 + 短信签名审核，
          个人很难过审（签名通常要求已备案域名或 App），所以默认关闭。

配置（环境变量优先，其次 runtime 目录的 mail.json —— 后台 ⑨ 可填）：
    WEILI_MAIL_HOST   smtp.qq.com
    WEILI_MAIL_PORT   465          # 465=SSL, 587=STARTTLS
    WEILI_MAIL_USER   xxx@qq.com
    WEILI_MAIL_PASS   SMTP 授权码（不是邮箱登录密码！）
    WEILI_MAIL_FROM   显示名，留空同 USER

    WEILI_SMS_PROVIDER aliyun
    WEILI_SMS_KEY      阿里云 AccessKeyId
    WEILI_SMS_SECRET   AccessKeySecret
    WEILI_SMS_SIGN     短信签名，如「微粒科技」
    WEILI_SMS_TEMPLATE 模板 CODE，如 SMS_123456789
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import time
import urllib.parse
import uuid
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RT = os.environ.get("WEILI_RUNTIME_DIR", "").strip()
MAIL_FILE = os.path.join(_RT, "mail.json") if _RT else os.path.join(ROOT, "data", "mail.json")

TTL = 600                 # 验证码 10 分钟有效
MAX_TRIES = 5             # 错 5 次作废
RESEND_GAP = 60           # 同一目标 60 秒内不准重发
DAY_CAP = 10              # 同一目标一天最多 10 条
IP_CAP = 30               # 同一 IP 一小时最多 30 条

MAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

DEFAULT = {
    "host": "", "port": 465, "user": "", "pass": "", "from": "",
    "sms_provider": "", "sms_key": "", "sms_secret": "",
    "sms_sign": "", "sms_template": "",
}


# ------------------------------------------------------------------ 配置
def cfg():
    d = {}
    try:
        d = json.load(open(MAIL_FILE, encoding="utf-8")) or {}
    except Exception:
        d = {}
    out = dict(DEFAULT)
    for k in DEFAULT:
        env = "WEILI_MAIL_" + k.upper() if k in ("host", "port", "user", "pass", "from") \
            else "WEILI_SMS_" + k.replace("sms_", "").upper()
        v = os.environ.get(env, "").strip()
        if v:
            out[k] = int(v) if k == "port" else v
        elif d.get(k) not in (None, ""):
            out[k] = d[k]
    return out


def save(d):
    cur = cfg()
    for k, v in (d or {}).items():
        if k in DEFAULT and v not in (None, ""):
            cur[k] = v
    os.makedirs(os.path.dirname(MAIL_FILE), exist_ok=True)
    json.dump(cur, open(MAIL_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return cur


def public_cfg():
    """后台看的：密码只回「配没配」。"""
    c = cfg()
    return {"host": c["host"], "port": c["port"], "user": c["user"],
            "from": c["from"], "pass_set": bool(c["pass"]),
            "email_ready": bool(c["host"] and c["user"] and c["pass"]),
            "sms_provider": c["sms_provider"], "sms_sign": c["sms_sign"],
            "sms_ready": bool(c["sms_provider"] and c["sms_key"] and c["sms_secret"]
                              and c["sms_sign"] and c["sms_template"]),
            "sms_template": c["sms_template"]}


# ------------------------------------------------------------------ 发信
def _mail_body(code, purpose):
    what = {"register": "注册账号", "reset": "重置密码", "bind": "绑定账号"}.get(purpose, "验证")
    return ("""<div style="font-family:-apple-system,BlinkMacSystemFont,'Microsoft YaHei',sans-serif;
max-width:520px;margin:0 auto;padding:28px 24px;color:#1a1a1a">
<div style="font-size:13px;letter-spacing:3px;color:#888">WEILI · 微粒</div>
<h2 style="font-size:20px;margin:14px 0 18px;font-weight:600">%s验证码</h2>
<div style="background:#f5f7fa;border-radius:12px;padding:22px;text-align:center;
font-size:34px;font-weight:700;letter-spacing:10px;color:#0a7d72">%s</div>
<p style="color:#666;font-size:13px;line-height:1.9;margin-top:18px">
10 分钟内有效，只能使用一次。<br>如果不是你本人操作，忽略这封邮件即可，账号不会有任何变化。</p>
<p style="color:#aaa;font-size:12px;margin-top:22px">微粒 AI 换发型 · 此邮件由系统自动发出</p>
</div>""" % (what, code))


def send_email(to, code, purpose="register"):
    c = cfg()
    if not (c["host"] and c["user"] and c["pass"]):
        raise RuntimeError("邮箱验证码还没配置（缺 SMTP 服务器/账号/授权码）")
    what = {"register": "注册", "reset": "重置密码", "bind": "绑定"}.get(purpose, "验证")
    msg = MIMEText(_mail_body(code, purpose), "html", "utf-8")
    msg["Subject"] = Header("【微粒】%s验证码 %s" % (what, code), "utf-8")
    # formataddr 第 2 个参数必须是纯邮箱地址（中文显示名放第 1 个），
    # 参数写反会直接抛 UnicodeEncodeError ascii codec。
    msg["From"] = formataddr((c["from"] or "微粒 WEILI", c["user"]))
    msg["To"] = to
    port = int(c["port"] or 465)
    if port == 465:
        srv = smtplib.SMTP_SSL(c["host"], port, timeout=15)
    else:
        srv = smtplib.SMTP(c["host"], port, timeout=15)
        try:
            srv.starttls()
        except Exception:
            pass
    try:
        srv.login(c["user"], c["pass"])
        # envelope sender 必须是纯地址 —— 传显示名（"微粒 WEILI"）会在
        # smtplib 里 ascii 编码时直接抛 UnicodeEncodeError。显示名只放头里。
        srv.sendmail(c["user"], [to], msg.as_string())
    finally:
        try:
            srv.quit()
        except Exception:
            pass


def _sms_sign(params, secret):
    """阿里云 RPC 签名：HMAC-SHA1(StringToSign, AccessSecret + '&')。"""
    q = "&".join("%s=%s" % (urllib.parse.quote(k, safe=""), urllib.parse.quote(str(v), safe=""))
                 for k, v in sorted(params.items()))
    msg = "GET&%2F&" + urllib.parse.quote(q, safe="")
    sig = hmac.new((secret + "&").encode(), msg.encode(), hashlib.sha1).digest()
    return base64.b64encode(sig).decode()


def send_sms(phone, code, purpose="register"):
    c = cfg()
    if not public_cfg()["sms_ready"]:
        raise RuntimeError("手机验证码还没开通（短信服务商要实名+签名审核），先用邮箱注册")
    if c["sms_provider"] != "aliyun":
        raise RuntimeError("暂只支持阿里云短信，当前配置是 %s" % c["sms_provider"])
    import requests
    p = {
        "AccessKeyId": c["sms_key"], "Action": "SendSms", "Format": "JSON",
        "PhoneNumbers": phone, "RegionId": "cn-hangzhou", "SignName": c["sms_sign"],
        "SignatureMethod": "HMAC-SHA1", "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0", "TemplateCode": c["sms_template"],
        "TemplateParam": json.dumps({"code": code}, separators=(",", ":")),
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Version": "2017-05-25",
    }
    p["Signature"] = _sms_sign(p, c["sms_secret"])
    r = requests.get("https://dysmsapi.aliyuncs.com/", params=p, timeout=15,
                     proxies={"http": None, "https": None})
    j = r.json()
    if j.get("Code") != "OK":
        raise RuntimeError("短信发送失败：%s %s" % (j.get("Code"), j.get("Message")))


# ------------------------------------------------------------------ 发码 / 验码
def norm(target, kind=""):
    t = (target or "").strip()
    if kind == "email" or (not kind and "@" in t):
        return t.lower(), "email"
    return t, "phone"


def bad(t, kind):
    if kind == "email":
        return not MAIL_RE.match(t)
    return not PHONE_RE.match(t)


def _count(target, since, ip=""):
    with db.conn() as c:
        if ip:
            r = c.execute("select count(*) n from vcodes where ip=? and created_at>=?",
                          (ip, since)).fetchone()
        else:
            r = c.execute("select count(*) n from vcodes where target=? and created_at>=?",
                          (target, since)).fetchone()
    return int((r or {})["n"] or 0)


def issue(target, kind="", purpose="register", ip=""):
    """生成并发送验证码。返回 {ok, target, kind, wait}；异常抛 RuntimeError（文案直接给用户看）。"""
    t, kind = norm(target, kind)
    if bad(t, kind):
        raise RuntimeError("邮箱格式不对" if kind == "email" else "手机号格式不对")

    now = int(time.time())
    with db.conn() as c:
        last = c.execute("select created_at from vcodes where target=? order by created_at desc limit 1",
                         (t,)).fetchone()
    if last and now - int(last["created_at"] or 0) < RESEND_GAP:
        left = RESEND_GAP - (now - int(last["created_at"]))
        raise RuntimeError("请等 %d 秒后再发" % left)
    if _count(t, now - 86400) >= DAY_CAP:
        raise RuntimeError("这个%s今天验证码发太多次了，明天再试" % ("邮箱" if kind == "email" else "手机号"))
    if ip and _count("", now - 3600, ip=ip) >= IP_CAP:
        raise RuntimeError("操作太频繁，等一小时再试")

    code = "%06d" % secrets.randbelow(1000000)
    with db._lock, db.conn() as c:
        c.execute("insert into vcodes(id,target,kind,code,purpose,tries,used,ip,created_at,expires_at)"
                  " values(?,?,?,?,?,0,0,?,?,?)",
                  ("v" + uuid.uuid4().hex[:12], t, kind, code, purpose, ip or "", now, now + TTL))

    if kind == "email":
        send_email(t, code, purpose)
    else:
        send_sms(t, code, purpose)
    return {"ok": True, "target": t, "kind": kind, "wait": RESEND_GAP, "ttl": TTL}


def verify(target, code, purpose="register"):
    """校验并消耗。成功返回 True，失败抛 RuntimeError。"""
    t, _ = norm(target)
    code = str(code or "").strip()
    if not code:
        raise RuntimeError("填一下验证码")
    now = int(time.time())
    with db.conn() as c:
        r = c.execute("select * from vcodes where target=? and purpose=? and used=0 "
                      "order by created_at desc limit 1", (t, purpose)).fetchone()
    if not r:
        raise RuntimeError("先点「获取验证码」")
    if int(r["expires_at"] or 0) < now:
        raise RuntimeError("验证码过期了，重新获取")
    if int(r["tries"] or 0) >= MAX_TRIES:
        raise RuntimeError("错太多次了，重新获取验证码")
    if r["code"] != code:
        with db._lock, db.conn() as c:
            c.execute("update vcodes set tries=tries+1 where id=?", (r["id"],))
        raise RuntimeError("验证码不对")
    with db._lock, db.conn() as c:
        c.execute("update vcodes set used=1 where id=?", (r["id"],))
    return True


def cooldown_left(target):
    t, _ = norm(target)
    with db.conn() as c:
        r = c.execute("select created_at from vcodes where target=? order by created_at desc limit 1",
                      (t,)).fetchone()
    if not r:
        return 0
    return max(0, RESEND_GAP - (int(time.time()) - int(r["created_at"] or 0)))
