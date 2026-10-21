# -*- coding: utf-8 -*-
"""同一张图 / 同一个款式，直接跑一遍 _run_job（不经 HTTP），用来对比改动前后。

用法：python tools/rerun_one.py <photo_id> <style_id> [n] [provider]
      python tools/rerun_one.py --job <job_id>          # 照原任务重跑（含 provider）
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import app  # noqa: E402
import db  # noqa: E402
import styles  # noqa: E402


def main():
    args = sys.argv[1:]
    if args and args[0] == "--job":
        old = dict(db.get_job(args[1]))
        photo_id, style_id = old["photo_id"], old["style_id"]
        provider = old.get("provider") or app.default_provider()
        n = int(args[2]) if len(args) > 2 else 1
    else:
        photo_id, style_id = args[0], args[1]
        n = int(args[2]) if len(args) > 2 else 1
        provider = args[3] if len(args) > 3 else app.default_provider()

    photo = db.get_photo(photo_id)
    style = styles.BY_ID[style_id]
    jid = uuid.uuid4().hex[:16]
    app._run_job(jid, photo, style, provider, n)
    row = db.get_job(jid) if hasattr(db, "get_job") else None
    print("JOB", jid, "provider=%s" % provider,
          dict(row).get("status") if row else "?", dict(row).get("err") if row else "")
    print("OUT", os.path.join(app.RES, "%s.jpg" % jid))


main()
