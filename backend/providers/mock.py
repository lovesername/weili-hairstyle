# -*- coding: utf-8 -*-
"""MOCK provider：不联网、不装模型，只按 mask 做「换发型占位渲染」。

用途：跑通上传→遮罩→回贴→前端对比整条链路，验证构图/羽化/调色是否正确。
它不会真的长出新发型，出图角上会打 MOCK 标记 —— 上线前必须切 comfy 或 gemini。
"""
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .base import Provider, to_image

COLORS = {
    "黑": (26, 22, 20), "深棕": (58, 36, 24), "栗": (92, 56, 36), "浅棕": (128, 88, 54),
    "亚麻": (176, 142, 96), "红": (110, 44, 34), "酒红": (96, 26, 40), "灰": (108, 106, 104),
    "奶茶": (146, 112, 82), "青木": (86, 96, 80), "金": (188, 156, 106),
}


class Mock(Provider):
    name = "mock"
    supports_n = True

    def available(self):
        return True

    def generate_n(self, crop_bytes, mask_bytes, prompt, negative="", size=768, n=1,
                   ref_bytes=None):
        """模拟真实出图的多样性：每张的发色/纹理/高光略有不同；
        最后一张故意带整图色偏（模拟"脸也被染色"的翻车样本），用来验证择优能拦住它。"""
        out = []
        for k in range(max(1, n)):
            out.append(self.generate(crop_bytes, mask_bytes, prompt, negative, size,
                                     variant=k, n=n, ref_bytes=ref_bytes))
        return out

    def generate(self, crop_bytes, mask_bytes, prompt, negative="", size=768,
                 variant=0, n=1, ref_bytes=None):
        im = to_image(crop_bytes).resize((size, size), Image.LANCZOS)
        mask = to_image(mask_bytes).convert("L").resize((size, size), Image.LANCZOS)

        color = next((v for k, v in COLORS.items() if k in prompt), (48, 32, 26))
        arr = np.asarray(im).astype(np.float32)
        m = (np.asarray(mask).astype(np.float32) / 255.0)[..., None]

        # 变体抖动：发色在 ±8% 内浮动、纹理强度在 0.45~0.7 之间
        rng = np.random.RandomState(1000 + variant)
        jitter = 1.0 + (rng.rand() - 0.5) * 0.16
        tint = np.clip(np.array(color, np.float32) * jitter, 0, 255)

        # 1) 染色 + 压暗（模拟新发色）
        arr = arr * (1 - 0.72 * m) + tint * (0.72 * m)

        # 2) 加发丝纹理：沿纵向的细噪声
        noise = np.random.normal(0, 9, (size, size)).astype(np.float32)
        noise = np.repeat(noise, 1, axis=0).reshape(size, size)
        tex = np.asarray(Image.fromarray(((noise + 128).clip(0, 255)).astype(np.uint8))
                         .filter(ImageFilter.GaussianBlur(0.6))).astype(np.float32) - 128
        arr += (tex[..., None] * m * (0.45 + 0.25 * (variant % 3) / 2))

        out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

        # 3) 高光：顺着头顶方向一条柔光带
        hl = Image.new("L", (size, size), 0)
        d = ImageDraw.Draw(hl)
        d.ellipse((size * 0.18, -size * 0.15, size * 0.82, size * 0.55), fill=70)
        hl = hl.filter(ImageFilter.GaussianBlur(size // 12))
        hl = Image.composite(hl, Image.new("L", (size, size), 0), mask)
        out = Image.composite(Image.blend(out, Image.new("RGB", (size, size), (255, 250, 240)), 0.18),
                              out, hl)

        # 最后一个变体：整图色偏（模拟把脸也改了的那种翻车样本）
        if n > 1 and variant == n - 1:
            out = Image.fromarray(np.clip(np.asarray(out).astype(np.float32) * 0.88 + 18, 0, 255)
                                  .astype(np.uint8))

        d = ImageDraw.Draw(out)
        d.text((10, 8), "MOCK" if variant == 0 else f"MOCK-{variant}", fill=(255, 80, 80))
        return out
