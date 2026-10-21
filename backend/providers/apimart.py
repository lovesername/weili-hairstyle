# -*- coding: utf-8 -*-
"""APIMart（apimart.ai）图像模型网关 —— 一个 key 打通 gpt-image-1 / Nano Banana / Seedream。

协议：OpenAI 兼容，但**异步**：
    POST {base}/images/generations  ->  {"data": {"id": "<task_id>"}}
    GET  {base}/tasks/<id>          ->  {"data": {"status": "completed",
                                                  "result": {"images": [{"url": ...}]},
                                                  "cost": 0.0125}}
任务态：pending -> processing -> completed | failed（失败退款）。

换发型不需要 mask：把「人物图 + 发型参考图」一起喂进去，模型整头重画、脸不变 ——
这正是原版小程序的做法，也是本地 SD1.5 做不到的地方。

.env 配置：
    APIMART_API_KEY=sk-...
    APIMART_IMAGE_MODEL=gemini-2.5-flash-image-preview   # 默认（nano banana，最便宜最像）
    # APIMART_IMAGE_MODEL=gpt-image-1                    # 更贵，细节更稳
    # APIMART_IMAGE_MODEL=seedream-4.5
    APIMART_BASE=https://api.apimart.ai/v1
    APIMART_RESOLUTION=1K            # 1K / 2K / 4K，2K 起更清晰也更贵
    APIMART_MODE=images              # images（默认）| chat（个别中转只开 chat 通道）
"""
import base64
import os
import socket
import time

import httpx
from PIL import Image

from .base import Provider, to_image

DEFAULT_BASE = "https://api.apimart.ai/v1"
DEFAULT_MODEL = "gemini-2.5-flash-image-preview"

# 国内直连 apimart 不通，得走本机梯子。没配 APIMART_PROXY 就猜一下常见端口。
_PROXY_GUESS = ((10808, "socks5"), (10809, "http"), (7890, "http"), (7897, "http"),
                (1080, "socks5"), (8889, "http"))


def _auto_proxy() -> str:
    # 云上（DEPLOY=1）直连出网就是通的，别浪费时间探本机梯子端口
    if os.environ.get("DEPLOY", "") in ("1", "true", "yes"):
        return ""
    for port, kind in _PROXY_GUESS:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.12):
                return "%s://127.0.0.1:%d" % (kind, port)
        except OSError:
            continue
    return ""


def _data_url(b: bytes) -> str:
    mime = "image/png" if b[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return "data:%s;base64,%s" % (mime, base64.b64encode(b).decode())


def _find_task_id(node, depth=0):
    """从各种返回形状里挖出 task id。"""
    if depth > 6:
        return None
    if isinstance(node, dict):
        for k in ("task_id", "taskId", "id"):
            v = node.get(k)
            if isinstance(v, str) and len(v) >= 8:
                return v
        for v in node.values():
            got = _find_task_id(v, depth + 1)
            if got:
                return got
    elif isinstance(node, list):
        for v in node:
            got = _find_task_id(v, depth + 1)
            if got:
                return got
    return None


_IMG_KEYS = ("url", "b64_json", "base64", "image_url", "imageUrl")
_SKIP_KEYS = ("image_urls", "input", "request", "prompt", "messages", "params", "usage")


def _find_images(node, depth=0):
    """挖出图片数组：可能是 [{"url":..}] / [[{"url":[..]}]] / ["https://.."] / [{"b64_json":..}]。"""
    if depth > 8:
        return None
    if isinstance(node, str):
        s = node.strip()
        return [s] if (s.startswith("http") or s.startswith("data:")) else None
    if isinstance(node, list):
        if not node:
            return None
        if all(isinstance(x, str) for x in node):
            return node if any(x.startswith("http") or x.startswith("data:")
                               for x in node) else None
        if all(isinstance(x, dict) for x in node) and any(
                set(_IMG_KEYS) & set(x) for x in node):
            return node
        for v in node:
            got = _find_images(v, depth + 1)
            if got:
                return got
        return None
    if isinstance(node, dict):
        for k in ("images", "output", "result", "data"):
            if k in node:
                got = _find_images(node[k], depth + 1)
                if got:
                    return got
        if set(_IMG_KEYS) & set(node):
            return [node]
        for k, v in node.items():
            if k in _SKIP_KEYS:
                continue
            got = _find_images(v, depth + 1)
            if got:
                return got
    return None


def _first_url(v):
    """返回形状里第一个像 URL / data-URL 的字符串 —— 中转常把 url 包成 ["https://.."]。"""
    if isinstance(v, str):
        return v if v.strip() else None
    if isinstance(v, (list, tuple)):
        for x in v:
            got = _first_url(x)
            if got:
                return got
        return None
    if isinstance(v, dict):
        for k in ("url", "image_url", "imageUrl", "src", "href", "path"):
            if k in v:
                got = _first_url(v[k])
                if got:
                    return got
    return None


def _flat(items):
    out = []
    for it in items or []:
        if isinstance(it, (list, tuple)):
            out.extend(_flat(it))
        else:
            out.append(it)
    return out


def _fetch(url, client=None) -> bytes:
    if client is not None:
        r = client.get(url)
        r.raise_for_status()
        return r.content
    r = httpx.get(url, timeout=120, follow_redirects=True)
    r.raise_for_status()
    return r.content


def _item_to_bytes(item, client=None) -> bytes:
    # url 被包成 list / dict / [[..]] 是常态，先压平成单个字符串
    if isinstance(item, (list, tuple)):
        flat = _flat(item)
        if len(flat) == 1:
            return _item_to_bytes(flat[0], client)
        for x in flat:
            try:
                return _item_to_bytes(x, client)
            except Exception:
                continue
        raise RuntimeError("图片数组里没有可用项: %r" % (item,))
    if isinstance(item, str):
        if item.startswith("data:") or len(item) > 2000:
            return base64.b64decode(item.split(",", 1)[-1])
        return _fetch(item, client)
    if isinstance(item, dict):
        for k in ("b64_json", "base64", "data"):
            v = item.get(k)
            if isinstance(v, str) and v:
                return base64.b64decode(v.split(",", 1)[-1])
        url = _first_url(item.get("url")) or _first_url(item.get("image_url")) \
            or _first_url(item.get("imageUrl"))
        if not url:
            url = _first_url(item)
        if url:
            return _item_to_bytes(url, client)
    raise RuntimeError("无法识别的图片项: %r" % (item,))


class APIMart(Provider):
    name = "apimart"
    supports_n = True

    def __init__(self):
        self.last_cost = 0.0      # 这一单真实花了多少美元（毛利台账用）
        self.key = os.environ.get("APIMART_API_KEY", "")
        self.base = (os.environ.get("APIMART_BASE") or DEFAULT_BASE).rstrip("/")
        self.model = os.environ.get("APIMART_IMAGE_MODEL") or DEFAULT_MODEL
        self.resolution = os.environ.get("APIMART_RESOLUTION") or "1K"
        self.mode = (os.environ.get("APIMART_MODE") or "images").lower()
        self.timeout = float(os.environ.get("APIMART_TIMEOUT") or 300)
        self.poll = float(os.environ.get("APIMART_POLL") or 2.0)
        # 国内直连不通，得走本机梯子。v2rayN 默认 socks5://127.0.0.1:10808
        self.proxy = os.environ.get("APIMART_PROXY") or _auto_proxy()

    def available(self):
        return bool(self.key)

    # ------------------------------------------------------------------ 底层
    def _headers(self):
        return {"Authorization": "Bearer %s" % self.key, "Content-Type": "application/json"}

    def _client(self, timeout=90, proxy=None):
        # trust_env=False：不要被系统里那些 WorkBuddy/CI 的 HTTP_PROXY 带偏，
        # 代理只用 APIMART_PROXY 显式指定的那个。proxy="" 表示这次强制直连。
        p = self.proxy if proxy is None else proxy
        kw = {"timeout": timeout, "trust_env": False, "follow_redirects": True}
        if p:
            kw["proxy"] = p
        try:
            return httpx.Client(**kw)
        except TypeError:                      # httpx < 0.26 用 proxies=
            kw.pop("proxy", None)
            if p:
                kw["proxies"] = p
            return httpx.Client(**kw)

    def _grab(self, items):
        """把图项全变成 bytes。中转的图片域名经常只通一边，代理/直连各试一次。"""
        out = []
        for it in items:
            last = None
            tries = (self.proxy, "") if self.proxy else ("",)
            for p in tries:
                try:
                    with self._client(120, proxy=p) as c:
                        out.append(_item_to_bytes(it, c))
                    last = None
                    break
                except Exception as e:          # noqa: BLE001
                    last = e
            if last is not None:
                raise RuntimeError("下载出图失败（代理/直连都试过）: %s" % last)
        return out

    def _post(self, path, payload, timeout=90):
        with self._client(timeout) as c:
            r = c.post(self.base + path, headers=self._headers(), json=payload)
        if r.status_code >= 400:
            raise RuntimeError("apimart %s %s: %s" % (path, r.status_code, r.text[:400]))
        return r.json()

    def _get(self, path, timeout=90):
        with self._client(timeout) as c:
            r = c.get(self.base + path, headers=self._headers())
        if r.status_code >= 400:
            raise RuntimeError("apimart %s %s: %s" % (path, r.status_code, r.text[:400]))
        return r.json()

    def _wait(self, task_id):
        t0 = time.time()
        last = ""
        while time.time() - t0 < self.timeout:
            j = self._get("/tasks/%s" % task_id)
            data = j.get("data") if isinstance(j.get("data"), dict) else j
            status = (data or {}).get("status") or (data or {}).get("state") or ""
            status = str(status).lower()
            if status in ("completed", "succeeded", "success", "done"):
                return data
            if status in ("failed", "error", "canceled", "cancelled"):
                raise RuntimeError("apimart 任务失败: %s" % str(data)[:400])
            if not status and _find_images(data):
                return data
            last = status
            time.sleep(self.poll)
        raise RuntimeError("apimart 任务超时（%ss，最后状态 %s）" % (int(self.timeout), last or "unknown"))

    def _run(self, payload):
        """提交 + 轮询 + 取图，返回原始 bytes 列表。"""
        if not self.key:
            raise RuntimeError("APIMART_API_KEY 未配置")
        job = self._post("/images/generations", payload)
        # 有些路由直接同步返回图片（不排队）
        imgs = _flat(_find_images(job))
        if imgs:
            return self._grab(imgs)
        tid = _find_task_id(job)
        if not tid:
            raise RuntimeError("apimart 未返回 task id: %s" % str(job)[:400])
        data = self._wait(tid)
        imgs = _flat(_find_images(data))
        if not imgs:
            raise RuntimeError("apimart 任务完成但没有图片: %s" % str(data)[:400])
        cost = (data or {}).get("cost") or (data or {}).get("credits_cost")
        try:
            self.last_cost = float(cost or 0)
        except (TypeError, ValueError):
            self.last_cost = 0.0
        print("apimart 出图 成本=%s 张数=%d 模型=%s" % (cost, len(imgs), self.model))
        return self._grab(imgs)

    # ------------------------------------------------------------------ 接口
    def _payload(self, crop_bytes, prompt, ref_bytes, n):
        # 顺序必须和 prompts.build_edit 里的「图1=发型参考、图2=本人」一致。
        # 反着送过一次：模型把参考图那个人的衣服/背景当成本人，头发却用了本人的 ——
        # 出来就是"完全不对版"。gemini 那条路一直是 ref 在前。
        urls = []
        if ref_bytes:
            urls.append(_data_url(ref_bytes))
        urls.append(_data_url(crop_bytes))
        return {
            "model": self.model,
            "prompt": prompt,
            "image_urls": urls,
            "size": "1:1",
            "resolution": self.resolution,
            "n": max(1, int(n)),
        }

    def generate(self, crop_bytes, mask_bytes, prompt, negative="", size=768, ref_bytes=None):
        outs = self.generate_n(crop_bytes, mask_bytes, prompt, negative, size,
                               n=1, ref_bytes=ref_bytes)
        return outs[0]

    def generate_n(self, crop_bytes, mask_bytes, prompt, negative="", size=768, n=1,
                   ref_bytes=None, **kw):
        if self.mode == "chat":
            return self._chat_n(crop_bytes, prompt, ref_bytes, size, n)
        raws = self._run(self._payload(crop_bytes, prompt, ref_bytes, n))
        out = []
        for b in raws:
            im = to_image(b)
            if im.size != (size, size):
                im = im.resize((size, size), Image.LANCZOS)
            out.append(im)
        return out[:max(1, int(n))]

    def _chat_n(self, crop_bytes, prompt, ref_bytes, size, n):
        """个别中转只开 chat/completions（多模态 in / image out）。"""
        content = [{"type": "text", "text": prompt}]
        if ref_bytes:
            content.append({"type": "image_url", "image_url": {"url": _data_url(ref_bytes)}})
        content.append({"type": "image_url", "image_url": {"url": _data_url(crop_bytes)}})
        body = {"model": self.model, "stream": False,
                "messages": [{"role": "user", "content": content}],
                "n": max(1, int(n))}
        j = self._post("/chat/completions", body, timeout=self.timeout)
        imgs = _flat(_find_images(j))
        if not imgs:
            raise RuntimeError("apimart chat 未返回图片: %s" % str(j)[:400])
        out = []
        for b in self._grab(imgs):
            im = to_image(b)
            if im.size != (size, size):
                im = im.resize((size, size), Image.LANCZOS)
            out.append(im)
        return out[:max(1, int(n))]
