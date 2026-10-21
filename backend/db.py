# -*- coding: utf-8 -*-
"""存储层：本地 SQLite（零运维）或云端 Postgres（配了 DATABASE_URL 就走它）。

云端（Render 免费层）磁盘是易失的，容器一休眠重启账号/订单/积分就没了，
所以上线必须配 DATABASE_URL（Supabase 免费 Postgres 就够）。
本地不配这个变量，行为跟以前完全一样，还是 SQLite。
"""
import os
import sqlite3
import threading
import time
import uuid

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "storage", "app.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
PG = bool(DATABASE_URL)
_lock = threading.RLock()

if PG:
    import psycopg2
    import psycopg2.extras
    # Supabase 强制 SSL，连接串里没写就补上
    if "sslmode" not in DATABASE_URL:
        DATABASE_URL += ("&" if "?" in DATABASE_URL else "?") + "sslmode=require"

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


# ---------------------------------------------------------------- Postgres
# 全部业务代码都按 SQLite 写（? 占位、insert or ignore）。这里做一层翻译，
# 免得为了上云把 45 处 ? 全改一遍。
def _pg_ddl(ddl):
    return ddl.replace("integer primary key autoincrement", "bigserial primary key")


def _pg_sql(sql):
    s = sql.strip()
    low = s.lower()
    # pragma table_info(t) -> information_schema
    if low.startswith("pragma table_info"):
        t = s[s.find("(") + 1:s.rfind(")")].strip().strip("'\"")
        return ("select column_name as name from information_schema.columns "
                "where table_name='%s' order by ordinal_position" % t)
    # % 先转义（psycopg2 的参数占位就是 %s），再把 ? 换成 %s
    s = s.replace("%", "%%").replace("?", "%s")
    if low.startswith("insert or ignore"):
        # 留着空格：s[16:] 是 " into ..."，写成 len+1 会拼成 "insertinto"
        s = "insert" + s[len("insert or ignore"):]
        if s.rstrip().endswith(";"):
            s = s.rstrip()[:-1]
        s += " on conflict do nothing"
    return s


class _PgConn(object):
    """一条长连接 + 一把锁。Supabase 免费档连接数有限，绝不能每次查询都新建。"""

    def __init__(self):
        self.conn = None
        self._open()

    def _open(self):
        self.conn = psycopg2.connect(DATABASE_URL, connect_timeout=15)
        self.conn.autocommit = False

    def _reset(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        self.conn = None
        self._open()

    def execute(self, sql, params=None):
        sql2 = _pg_sql(sql)
        try:
            cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql2, params)
            return cur
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            # Supabase 免费项目闲置 7 天会暂停，连接会断 —— 重连一次再试
            self._reset()
            cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql2, params)
            return cur

    def executescript(self, script):
        for stmt in script.split(";"):
            # 只去掉开头的注释行；建表语句后面还挂着中文注释，不能整段丢掉
            lines = stmt.strip().splitlines()
            while lines and (not lines[0].strip() or lines[0].strip().startswith("--")):
                lines.pop(0)
            s = "\n".join(lines).strip()
            if not s:
                continue
            self.execute(s)

    def commit(self):
        try:
            self.conn.commit()
        except Exception:
            pass

    def rollback(self):
        try:
            self.conn.rollback()
        except Exception:
            pass

    def __enter__(self):
        _lock.acquire()
        return self

    def __exit__(self, *exc):
        try:
            if exc[0] is None:
                self.commit()
            else:
                self.rollback()
        finally:
            _lock.release()
        return False


_pg_singleton = None


def _pg():
    global _pg_singleton
    if _pg_singleton is None:
        _pg_singleton = _PgConn()
    return _pg_singleton


# ---------------------------------------------------------------- 建表 / 连接
SCHEMA_PG = _pg_ddl(SCHEMA)


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
    if PG:
        return _pg()
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def ping():
    """给健康检查/保活用：真的打一次库，顺带让 Supabase 不至于被判闲置。"""
    with conn() as c:
        c.execute("select 1 as ok").fetchone()
    return True


def init():
    if PG:
        with conn() as c:
            c.executescript(SCHEMA_PG)
        migrate()
        return
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
