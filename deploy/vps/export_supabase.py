# -*- coding: utf-8 -*-
"""把线上 Supabase 的全部数据搬到本地（账号 / 订单 / 积分 / 历史 / 成品图）。

用法（在项目根目录 ai-hairstyle/ 下跑）：
    python deploy/vps/export_supabase.py

产出：
    deploy/vps/dump/app.db          —— 全部表，SQLite，直接给服务器用
    deploy/vps/dump/results/*.jpg   —— Supabase 对象存储里的成品图
    deploy/vps/dump/manifest.json   —— 每张表多少行的核对清单

三件顺带处理的事（不处理的话搬过去就是一堆死链）：
    1. jobs.result_path 里的 https://…supabase.co/…/results/xxx.jpg → 只留 xxx.jpg，
       前端改成 /files/results/xxx.jpg（store 没配 Supabase 时自动走本地盘）。
    2. photos.path 里的 /app/storage/uploads/xxx.jpg → __ROOT__/storage/uploads/xxx.jpg，
       服务器解包时把 __ROOT__ 换成 /opt/weili。
    3. 对象存储里的图全部下载到 dump/results/，一张不落。
"""
import io
import json
import os
import sqlite3
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DUMP = os.path.join(HERE, "dump")
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Postgres 类型 → SQLite 类型
def _sqtype(t):
    t = (t or "").lower()
    if any(k in t for k in ("int", "serial")):
        return "INTEGER"
    if any(k in t for k in ("numeric", "real", "double", "float", "decimal")):
        return "REAL"
    if "bool" in t:
        return "INTEGER"
    if "bytea" in t:
        return "BLOB"
    return "TEXT"


def read_env():
    env = {}
    p = os.path.join(HERE, "env.server")
    for line in io.open(p, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def main():
    env = read_env()
    url = env.get("DATABASE_URL", "")
    if not url:
        sys.exit("env.server 里没有 DATABASE_URL")
    if "sslmode" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    import psycopg2

    os.makedirs(DUMP, exist_ok=True)
    db_path = os.path.join(DUMP, "app.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    print("连接 Supabase …")
    pg = psycopg2.connect(url)
    cur = pg.cursor()
    cur.execute("select table_name from information_schema.tables "
                "where table_schema='public' and table_type='BASE TABLE' order by 1")
    tables = [r[0] for r in cur.fetchall()]
    print("表:", ", ".join(tables))

    lite = sqlite3.connect(db_path)
    lite.execute("PRAGMA journal_mode=WAL")
    manifest = {}

    for t in tables:
        cur.execute("""select column_name, data_type, is_nullable, column_default
                       from information_schema.columns
                       where table_schema='public' and table_name=%s
                       order by ordinal_position""", (t,))
        cols = cur.fetchall()
        if not cols:
            continue
        names = [c[0] for c in cols]
        ddl = ", ".join('"%s" %s' % (c[0], _sqtype(c[1])) for c in cols)
        lite.execute('drop table if exists "%s"' % t)
        lite.execute('create table "%s" (%s)' % (t, ddl))

        cur.execute('select %s from "%s"' % (", ".join('"%s"' % n for n in names), t))
        rows = cur.fetchall()
        clean = []
        for row in rows:
            out = []
            for v in row:
                if isinstance(v, bool):
                    out.append(1 if v else 0)
                elif isinstance(v, (dict, list)):
                    out.append(json.dumps(v, ensure_ascii=False))
                elif isinstance(v, memoryview):
                    out.append(bytes(v))
                elif v is not None and v.__class__.__name__ in ("Decimal", "datetime", "date", "time"):
                    out.append(str(v))
                elif isinstance(v, (bytes, str, int, float)) or v is None:
                    out.append(v)
                else:
                    out.append(str(v))
            clean.append(tuple(out))
        if clean:
            lite.executemany('insert into "%s" (%s) values (%s)' % (
                t, ", ".join('"%s"' % n for n in names), ", ".join("?" * len(names))), clean)
        manifest[t] = len(clean)
        print("  %-10s %4d 行" % (t, len(clean)))

    # ---- 改写两类路径 ----
    for (jid, rp) in lite.execute("select id, result_path from jobs where result_path like 'http%'").fetchall():
        base = rp.rstrip("/").rsplit("/", 1)[-1]
        lite.execute("update jobs set result_path=? where id=?", (base, jid))

    for (pid, p) in lite.execute("select id, path from photos").fetchall():
        if p and p.startswith("/"):
            base = p.rsplit("/", 1)[-1]
            lite.execute("update photos set path=? where id=?",
                         ("__ROOT__/storage/uploads/" + base, pid))
    lite.commit()

    # ---- 下载对象存储里的成品图 ----
    SU = env.get("SUPABASE_URL", "").rstrip("/")
    SK = (env.get("SUPABASE_SERVICE_KEY") or env.get("SUPABASE_ANON_KEY") or "").strip()
    BUCKET = env.get("SUPABASE_BUCKET", "weili").strip()
    got = 0
    if SU and SK:
        out_dir = os.path.join(DUMP, "results")
        os.makedirs(out_dir, exist_ok=True)
        H = {"Authorization": "Bearer " + SK, "apikey": SK,
             "Content-Type": "application/json"}
        body = json.dumps({"prefix": "results", "limit": 1000, "offset": 0}).encode()
        r = urllib.request.Request("%s/storage/v1/object/list/%s" % (SU, BUCKET),
                                   data=body, headers=H, method="POST")
        items = json.loads(OP.open(r, timeout=60).read())
        for it in items:
            if not it.get("id"):
                continue
            name = it["name"]
            src = "%s/storage/v1/object/public/%s/results/%s" % (SU, BUCKET, name)
            dst = os.path.join(out_dir, name)
            try:
                with OP.open(urllib.request.Request(src, headers={"User-Agent": "weili-migrate"}),
                             timeout=120) as resp, open(dst, "wb") as f:
                    f.write(resp.read())
                got += 1
            except Exception as e:
                print("  下载失败", name, e)
        print("成品图下载 %d 张 → %s" % (got, out_dir))
    else:
        print("没配 SUPABASE_URL/KEY，跳过图片下载")

    manifest["_results_downloaded"] = got
    manifest["_db_bytes"] = os.path.getsize(db_path)
    with io.open(os.path.join(DUMP, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    lite.close()
    pg.close()
    print("\n完成：", db_path, "%.1f KB" % (os.path.getsize(db_path) / 1024))
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
