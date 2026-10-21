# -*- coding: utf-8 -*-
"""OpenAI 兼容的 images/edits（gpt-image-1 / 各类中转站同协议）。

带 mask 才能只改头发：OpenAI 语义是「mask 透明处 = 要重绘的区域」，
所以把我们的 mask（255=头发）反转成 alpha。
"""
import base64
import io
import os

import httpx
from PIL import Image

from .base import Provider, to_image


def _mask_for_openai(mask_bytes, size):
    m = to_image(mask_bytes).convert("L").resize((size, size), Image.LANCZOS)
    inv = Image.eval(m, lambda v: 255 - v)          # 头发区变 0(透明)
    rgba = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    rgba.putalpha(inv.point(lambda v: 255 if v > 8 else 0))
    b = io.BytesIO()
    rgba.save(b, "PNG")
    return b.getvalue()


class OpenAIEdit(Provider):
    name = "openai"

    def __init__(self):
        self.key = os.environ.get("OPENAI_API_KEY", "")
        self.base = os.environ.get("OPENAI_BASE", "https://api.openai.com/v1").rstrip("/")
        self.model = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")

    def available(self):
        return bool(self.key)

    def generate(self, crop_bytes, mask_bytes, prompt, negative="", size=768, ref_bytes=None):
        if not self.key:
            raise RuntimeError("OPENAI_API_KEY 未配置")
        files = {
            "image": ("crop.png", crop_bytes, "image/png"),
            "mask": ("mask.png", _mask_for_openai(mask_bytes, size), "image/png"),
        }
        data = {"model": self.model, "prompt": prompt + ("  negative: " + negative if negative else ""),
                "size": f"{size}x{size}", "n": "1"}
        with httpx.Client(timeout=300) as c:
            r = c.post(f"{self.base}/images/edits", headers={"Authorization": f"Bearer {self.key}"},
                       files=files, data=data)
        if r.status_code != 200:
            raise RuntimeError(f"openai {r.status_code}: {r.text[:300]}")
        j = r.json()
        d = j["data"][0]
        raw = base64.b64decode(d["b64_json"]) if d.get("b64_json") else httpx.get(d["url"]).content
        return to_image(raw).resize((size, size), Image.LANCZOS)
