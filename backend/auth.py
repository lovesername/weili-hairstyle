# -*- coding: utf-8 -*-
"""账号 / 登录 / 积分。

- 密码：pbkdf2_hmac(sha256, 12 万轮) + 每账号随机盐，库里不存明文。
- 登录态：HMAC 签名 token（uid.exp.sig），服务端不存 session，重启不丢登录。
- 扣费顺序：免费次数 → 积分余额；失败自动退（见 refund）。
  扣的是"这一单实际花掉的"，不是"点按钮时预估的"。
"""
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid

import db

SECRET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "storage", "secret.key")
TOKEN_TTL = 60 * 60 * 24 * 30          # 30 天
ROUNDS = 120000


def _secret():
    try:
        s = open(SECRET_PATH, encoding="utf-8").read().strip()
        if s:
            return s
    except Exception:
        pass
    s = uuid.uuid4().hex + uuid.uuid4().hex
    os.makedirs(os.path.dirname(SECRET_PATH), exist_ok=True)
    open(SECRET_PATH, "w", encoding="utf-8").write(s)
    return s


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), ROUNDS).hex()


def _b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s):
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode())


def make_token(uid, ttl=TOKEN_TTL):
    exp = int(time.time()) + ttl
    body = _b64(json.dumps({"u": uid, "e": exp}).encode())
    sig = hmac.new(_secret().encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return body + "." + sig


def read_token(tok):
    """返回 account 行；token 不对 / 过期返回 None。"""
    if not tok:
        return None
    tok = tok[7:].strip() if tok.lower().startswith("bearer ") else tok.strip()
    if "." not in tok:
        return None
    body, sig = tok.rsplit(".", 1)
    if not hmac.compare_digest(hmac.new(_secret().encode(), body.encode(),
                                        hashlib.sha256).hexdigest()[:32], sig):
        return None
    try:
        d = json.loads(_unb64(body))
    except Exception:
        return None
    if int(d.get("e", 0)) < time.time():
        return None
    return get(d.get("u"))


def get(aid):
    with db.conn() as c:
        r = c.execute("select * from accounts where id=?", (aid,)).fetchone()
    return dict(r) if r else None


def by_name(name):
    with db.conn() as c:
        r = c.execute("select * from accounts where username=? or phone=?", (name, name)).fetchone()
    return dict(r) if r else None


def public(a):
    if not a:
        return None
    return {"id": a["id"], "username": a["username"], "phone": a["phone"] or "",
            "role": a["role"], "credits": a["credits"], "free_left": a["free_left"],
            "gens_left": a["free_left"] + int(a["credits"] / max(1, _gen_credits()))}


def _gen_credits():
    import billing
    return billing.GEN_CREDITS


def register(username, password, phone="", role="user", free=None):
    import billing
    username = (username or "").strip()
    if len(username) < 3 or len(username) > 24:
        raise ValueError("账号要 3~24 个字符")
    if len(password or "") < 6:
        raise ValueError("密码至少 6 位")
    if by_name(username):
        raise ValueError("这个账号已经被注册了")
    if phone and by_name(phone):
        raise ValueError("这个手机号已经注册过")
    aid = "u" + uuid.uuid4().hex[:12]
    salt = uuid.uuid4().hex
    now = int(time.time())
    free = billing.FREE_GENS if free is None else int(free)
    with db._lock, db.conn() as c:
        c.execute("insert into accounts(id,username,phone,pass_hash,salt,role,credits,"
                  "free_left,status,created_at,last_at) values(?,?,?,?,?,?,0,?,?,?,?)",
                  (aid, username, phone or "", hash_pw(password, salt), salt, role, free,
                   "ok", now, now))
        c.execute("insert into ledger(uid,delta,credits_after,reason,created_at) "
                  "values(?,?,?,?,?)", (aid, 0, 0, "register: 赠 %d 次免费" % free, now))
    return get(aid)


def login(name, password):
    a = by_name((name or "").strip())
    if not a:
        raise ValueError("账号不存在")
    if a["status"] != "ok":
        raise ValueError("账号已被停用")
    if not hmac.compare_digest(a["pass_hash"], hash_pw(password or "", a["salt"])):
        raise ValueError("密码不对")
    with db._lock, db.conn() as c:
        c.execute("update accounts set last_at=? where id=?", (int(time.time()), a["id"]))
    return get(a["id"])


def set_role(aid, role):
    with db._lock, db.conn() as c:
        c.execute("update accounts set role=? where id=?", (role, aid))
    return get(aid)


# ---------------- 积分 ----------------
def add_credits(aid, n, reason=""):
    now = int(time.time())
    with db._lock, db.conn() as c:
        c.execute("update accounts set credits=credits+? where id=?", (n, aid))
        r = c.execute("select credits from accounts where id=?", (aid,)).fetchone()
        c.execute("insert into ledger(uid,delta,credits_after,reason,created_at) "
                  "values(?,?,?,?,?)", (aid, n, r["credits"], reason, now))
    return get(aid)


def set_free(aid, n):
    with db._lock, db.conn() as c:
        c.execute("update accounts set free_left=? where id=?", (int(n), aid))
    return get(aid)


def charge(aid, credits, n=1):
    """扣一次：先用免费次数，不够再扣积分。

    返回 {'ok':True, 'free':k, 'credits':c} 或 {'ok':False,'need':c,'have':c}
    """
    a = get(aid) or {}
    if a.get("role") == "admin":
        return {"ok": True, "free": 0, "credits": 0, "admin": True}
    now = int(time.time())
    with db._lock, db.conn() as c:
        row = c.execute("select credits,free_left from accounts where id=?", (aid,)).fetchone()
        if not row:
            return {"ok": False, "need": credits, "have": 0, "free_left": 0}
        n = max(1, int(n or 1))
        free, credit = int(row["free_left"]), int(row["credits"])
        if free >= n:
            c.execute("update accounts set free_left=free_left-? where id=?", (n, aid))
            return {"ok": True, "free": n, "credits": 0}
        # 免费次数不够 cover 全部张数：剩下的几张按积分算。
        # 注意顺序 —— 必须先确认积分够，再把免费次数清零，
        # 否则"免费 1 次 + 积分不够"会把那 1 次也白白吃掉。
        per = credits / float(n)
        rest = n - free
        want = int(round(per * rest))
        if credit < want:
            return {"ok": False, "need": want, "have": credit, "free_left": free}
        if free > 0:
            c.execute("update accounts set free_left=0 where id=?", (aid,))
        c.execute("update accounts set credits=credits-? where id=?", (want, aid))
        c.execute("insert into ledger(uid,delta,credits_after,reason,created_at) "
                  "values(?,?,?,?,?)", (aid, -want, credit - want,
                                        "generate ×%d（免费 %d + 积分 %d）" % (n, free, want), now))
        return {"ok": True, "free": free, "credits": want}


def refund(aid, free_n, credits, reason="job failed"):
    """生成失败：把这一单扣掉的免费次数 / 积分原路退回。"""
    if not aid:
        return None
    now = int(time.time())
    with db._lock, db.conn() as c:
        if free_n:
            c.execute("update accounts set free_left=free_left+? where id=?", (int(free_n), aid))
        if credits:
            c.execute("update accounts set credits=credits+? where id=?", (int(credits), aid))
        if free_n or credits:
            r = c.execute("select credits from accounts where id=?", (aid,)).fetchone()
            c.execute("insert into ledger(uid,delta,credits_after,reason,created_at) "
                      "values(?,?,?,?,?)", (aid, int(credits), r["credits"] if r else 0,
                                            reason, now))
    return get(aid)


# ---------------- 订单（线下/模拟支付；接微信支付宝时把 pay() 换成回调） ----------------
def create_order(aid, pkg_id, amount_cents, credits):
    oid = "o" + uuid.uuid4().hex[:12]
    with db._lock, db.conn() as c:
        c.execute("insert into orders(id,uid,pkg,amount_cents,credits,status,created_at) "
                  "values(?,?,?,?,?,'created',?)", (oid, aid, pkg_id, int(amount_cents),
                                                    int(credits), int(time.time())))
    return oid


def get_order(oid):
    with db.conn() as c:
        r = c.execute("select * from orders where id=?", (oid,)).fetchone()
    return dict(r) if r else None


def pay_order(oid, aid=None):
    o = get_order(oid)
    if not o:
        raise ValueError("订单不存在")
    if aid and o["uid"] != aid:
        raise ValueError("不是你的订单")
    if o["status"] == "paid":
        return o
    now = int(time.time())
    with db._lock, db.conn() as c:
        c.execute("update orders set status='paid', paid_at=? where id=?", (now, oid))
    add_credits(o["uid"], o["credits"], "recharge %s ¥%.2f" % (o["pkg"], o["amount_cents"] / 100.0))
    return get_order(oid)


def claim_order(oid, aid):
    """用户扫经营码转账后点「我已完成转账」：订单进入待确认（checking）。

    经营码没有回调，钱到没到只有老板看手机才知道 —— 所以这里绝不直接加积分，
    等后台「确认到账」才走 pay_order。
    """
    o = get_order(oid)
    if not o:
        raise ValueError("订单不存在")
    if aid and o["uid"] != aid:
        raise ValueError("不是你的订单")
    if o["status"] == "paid":
        return o
    if o["status"] not in ("created", "checking"):
        raise ValueError("订单状态不可支付")
    with db._lock, db.conn() as c:
        c.execute("update orders set status='checking' where id=?", (oid,))
    return get_order(oid)


def orders_of(aid, limit=20):
    with db.conn() as c:
        rows = c.execute("select * from orders where uid=? order by created_at desc limit ?",
                         (aid, limit)).fetchall()
    return [dict(r) for r in rows]


def all_orders(limit=100):
    with db.conn() as c:
        rows = c.execute("select o.*, a.username from orders o left join accounts a "
                         "on a.id=o.uid order by o.created_at desc limit ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def list_users(limit=500):
    with db.conn() as c:
        rows = c.execute("select * from accounts order by created_at desc limit ?",
                         (limit,)).fetchall()
    return [dict(r) for r in rows]


def ledger_of(aid, limit=30):
    with db.conn() as c:
        rows = c.execute("select * from ledger where uid=? order by id desc limit ?",
                         (aid, limit)).fetchall()
    return [dict(r) for r in rows]


def ensure_admin(username="admin", password="weili8888"):
    """第一次跑起来给老板留一个管理员账号。已存在就不动。"""
    if by_name(username):
        return get(by_name(username)["id"])
    a = register(username, password, role="admin", free=999999)
    with db._lock, db.conn() as c:
        c.execute("update accounts set credits=999999 where id=?", (a["id"],))
    print("管理员账号：%s / %s（登录后前台能选模型、看全部功能）" % (username, password))
    return get(a["id"])
