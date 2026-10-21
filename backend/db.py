# -*- coding: utf-8 -*-
"""SQLite 存储：用户 / 照片 / 任务 / 每日额度。单文件，零运维。"""
import os
import sqlite3
import threading
import time
import uuid

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "storage", "app.db")
_lock = threading.Lock()

SCHEMA = """
create table if not exists users(
  openid text primary key,
  created_at integer,
  bonus integer default 0
);
create table if not exists photos(
  id text primary key,
  openid text,
  path text,
  rect text,
  face text,
  created_at integer
);
create table if not exists jobs(
  id text primary key,
  openid text,
  photo_id text,
  style_id text,
  provider text,
  status integer default 0,      -- 0 排队 1 生成中 2 成功 3 失败
  result_path text,
  err text,
  created_at integer,
  updated_at integer
);
create table if not exists quota(
  openid text,
  day text,
  used integer default 0,
  primary key(openid, day)
);
-- 用户自己上传的发型参考图。和 styles / gallery 完全两个世界：
-- 只属于上传者本人，生成时单独走 custom: 分支，绝不混进发型库。
create table if not exists refs(
  id text primary key,
  uid text,
  name text,
  path text,
  created_at integer
);
create table if not exists accounts(
  id text primary key,
  username text unique,
  phone text,
  pass_hash text,
  salt text,
  role text default 'user',        -- user | admin
  credits integer default 0,       -- 积分余额
  free_left integer default 0,     -- 注册送的免费次数
  status text default 'ok',        -- ok | banned
  created_at integer,
  last_at integer
);
create table if not exists orders(
  id text primary key,
  uid text,
  pkg text,
  amount_cents integer,
  credits integer,
  status text default 'created',   -- created | paid | closed
  created_at integer,
  paid_at integer
);
create table if not exists ledger(
  id integer primary key autoincrement,
  uid text,
  delta integer,
  credits_after integer,
  reason text,
  created_at integer
);
"""

# 老库加列：jobs 原来没有计费字段，直接 ALTER（已存在就忽略）
JOB_EXTRA_COLUMNS = (("credits_spent", "integer default 0"),
                     ("free_spent", "integer default 0"),
                     ("cost_usd", "real default 0"))


def migrate():
    with _lock, conn() as c:
        cols = [r["name"] for r in c.execute("pragma table_info(jobs)")]
        for name, ddl in JOB_EXTRA_COLUMNS:
            if name not in cols:
                c.execute("alter table jobs add column %s %s" % (name, ddl))

def free_per_day():
    """每天免费次数。读环境变量要放到函数里 —— db 比 app 先 import，
    写成模块常量会读不到 .env（踩过：.env 里 FREE_PER_DAY=0 不生效）。"""
    try:
        return int(os.environ.get("FREE_PER_DAY", "30"))
    except ValueError:
        return 30


def unlimited():
    """<=0 表示完全不限次数（本地自用）。线上想限流就填 2 这种正数。"""
    return free_per_day() <= 0


def conn():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, conn() as c:
        c.executescript(SCHEMA)
    migrate()


def today():
    return time.strftime("%Y-%m-%d")


def touch_user(openid):
    with _lock, conn() as c:
        c.execute("insert or ignore into users(openid, created_at) values(?,?)", (openid, int(time.time())))


def quota_info(openid):
    touch_user(openid)
    with _lock, conn() as c:
        u = c.execute("select bonus from users where openid=?", (openid,)).fetchone()
        q = c.execute("select used from quota where openid=? and day=?", (openid, today())).fetchone()
    used = q["used"] if q else 0
    if unlimited():
        return {"day": today(), "free": -1, "used": used, "bonus": 0,
                "remain": -1, "unlimited": True}
    free = free_per_day()
    remain = max(0, free + (u["bonus"] if u else 0) - used)
    return {"day": today(), "free": free, "used": used, "bonus": (u["bonus"] if u else 0),
            "remain": remain, "unlimited": False}


def consume(openid, n=1):
    """原子扣额度，返回 True 表示扣成功。不限次数时只记一笔流水，从不拦。"""
    touch_user(openid)
    if unlimited():
        with _lock, conn() as c:
            c.execute("insert or ignore into quota(openid, day, used) values(?,?,0)", (openid, today()))
            c.execute("update quota set used=used+? where openid=? and day=?", (n, openid, today()))
        return True
    with _lock, conn() as c:
        c.execute("insert or ignore into quota(openid, day, used) values(?,?,0)", (openid, today()))
        row = c.execute("select q.used, u.bonus from quota q join users u on u.openid=q.openid "
                        "where q.openid=? and q.day=?", (openid, today())).fetchone()
        if row["used"] + n > free_per_day() + row["bonus"]:
            return False
        c.execute("update quota set used=used+? where openid=? and day=?", (n, openid, today()))
    return True


def quota_reset(openid, day=None):
    """把某人某天（默认今天）的用量清零。"""
    day = day or today()
    with _lock, conn() as c:
        c.execute("update quota set used=0 where openid=? and day=?", (openid, day))
    return quota_info(openid)


def quota_reset_all(day=None):
    """全表清零（所有 openid）。"""
    day = day or today()
    with _lock, conn() as c:
        c.execute("update quota set used=0 where day=?", (day,))
    return {"day": day, "reset": "all"}


def add_bonus(openid, n=1):
    touch_user(openid)
    with _lock, conn() as c:
        c.execute("update users set bonus=bonus+? where openid=?", (n, openid))
    return quota_info(openid)


def save_photo(openid, path, rect, face):
    pid = uuid.uuid4().hex[:16]
    with _lock, conn() as c:
        c.execute("insert into photos(id,openid,path,rect,face,created_at) values(?,?,?,?,?,?)",
                  (pid, openid, path, str(rect), str(face), int(time.time())))
    return pid


def get_photo(pid):
    with conn() as c:
        return c.execute("select * from photos where id=?", (pid,)).fetchone()


def create_job(openid, photo_id, style_id, provider):
    jid = uuid.uuid4().hex[:16]
    now = int(time.time())
    with _lock, conn() as c:
        c.execute("insert into jobs(id,openid,photo_id,style_id,provider,status,created_at,updated_at) "
                  "values(?,?,?,?,?,0,?,?)", (jid, openid, photo_id, style_id, provider, now, now))
    return jid


def update_job(jid, status=None, result_path=None, err=None,
               credits_spent=None, free_spent=None, cost_usd=None):
    sets, vals = ["updated_at=?"], [int(time.time())]
    if status is not None:
        sets.append("status=?"); vals.append(status)
    if result_path is not None:
        sets.append("result_path=?"); vals.append(result_path)
    if err is not None:
        sets.append("err=?"); vals.append(err[:400])
    for name, v in (("credits_spent", credits_spent), ("free_spent", free_spent),
                    ("cost_usd", cost_usd)):
        if v is not None:
            sets.append("%s=?" % name); vals.append(v)
    vals.append(jid)
    with _lock, conn() as c:
        c.execute(f"update jobs set {','.join(sets)} where id=?", vals)


def get_job(jid):
    with conn() as c:
        return c.execute("select * from jobs where id=?", (jid,)).fetchone()


def history(openid, limit=20):
    with conn() as c:
        rows = c.execute("select id,style_id,status,result_path,created_at from jobs "
                         "where openid=? order by created_at desc limit ?", (openid, limit)).fetchall()
    return [dict(r) for r in rows]
