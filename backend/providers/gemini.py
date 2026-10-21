# -*- coding: utf-8 -*-
"""Gemini image-edit（nano banana 系）：效果最像「一键换发」的一条路。

一次调用 = 一次换发，身份保持最好，不需要 mask；成本按张计。
配好 GEMINI_API_KEY 即可，模型名可换 GEMINI_IMAGE_MODEL。
"""
import base64
import io
import os

import httpx
from PIL import Image

from .base import Provider, to_image

BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class Gemini(Provider):
    name = "gemini"

    def __init__(self):
        self.key = os.environ.get("GEMINI_API_KEY", "")
        self.model = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

    def available(self):
        return bool(self.key)

    def generate(self, crop_bytes, mask_bytes, prompt, negative="", size=768, ref_bytes=None):
        if not self.key:
            raise RuntimeError("GEMINI_API_KEY 未配置")
        parts = [{"text": prompt}]
        if ref_bytes:
            # 有发型库封面就一起喂进去：模型看图比读 prompt 准得多（这就是"复刻该发型"）
            parts.append({"text": "图1 是目标发型参考（只看发型：长度、卷度、发色、刘海，别看脸）。"
                                  "图2 是要换发型的人。只改头发，脸、肤色、表情、衣服、背景保持完全不变。"})
            parts.append({"inline_data": {"mime_type": "image/jpeg",
                                         "data": base64.b64encode(ref_bytes).decode()}})
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                     "data": base64.b64encode(crop_bytes).decode()}})
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
        url = f"{BASE}/{self.model}:generateContent"
        with httpx.Client(timeout=180) as c:
            r = c.post(url, params={"key": self.key}, json=body)
        if r.status_code != 200:
            raise RuntimeError(f"gemini {r.status_code}: {r.text[:300]}")
        j = r.json()
        parts = (j.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        for p in parts:
            inline = p.get("inlineData") or p.get("inline_data")
            if inline and inline.get("data"):
                return to_image(base64.b64decode(inline["data"])).resize((size, size), Image.LANCZOS)
        raise RuntimeError("gemini 未返回图像: " + str(j)[:300])
