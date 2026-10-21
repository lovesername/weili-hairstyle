# -*- coding: utf-8 -*-
"""图片存放：配了 Supabase 就存对象存储（永久），没配就还是本地盘。

Render 免费层磁盘是易失的 —— 重新部署一次，顾客之前生成的图就全没了。
配了 SUPABASE_URL + SUPABASE_SERVICE_KEY 之后，结果图会额外传一份到对象存储，
库里存外链，重启/重新部署都不丢。没配这两个变量时行为完全不变（本地盘）。
"""
import os

URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
KEY = (os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
       or os.environ.get("SUPABASE_ANON_KEY", "").strip())
BUCKET = os.environ.get("SUPABASE_BUCKET", "weili").strip()
ON = bool(URL and KEY)


def put(kind, name, data, ctype="image/jpeg"):
    """上传 bytes 到 {bucket}/{kind}/{name}，返回公开外链。没配 Supabase 返回 None。"""
    if not ON:
        return None
    import httpx
    path = "%s/%s" % (kind, name)
    r = httpx.post("%s/storage/v1/object/%s/%s" % (URL, BUCKET, path),
                   headers={"Authorization": "Bearer " + KEY,
                            "Content-Type": ctype,
                            "x-upsert": "true"},
                   content=data, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError("supabase storage %s: %s" % (r.status_code, r.text[:200]))
    return "%s/storage/v1/object/public/%s/%s" % (URL, BUCKET, path)


def del_obj(kind, name):
    """删掉一张（后台删除生成记录时用）。没配就静默跳过。"""
    if not ON:
        return False
    import httpx
    r = httpx.delete("%s/storage/v1/object/%s/%s/%s" % (URL, BUCKET, kind, name),
                     headers={"Authorization": "Bearer " + KEY}, timeout=30)
    return r.status_code < 300


def is_remote(v):
    return bool(v) and str(v).startswith("http")
