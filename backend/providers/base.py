# -*- coding: utf-8 -*-
"""生成后端统一接口。

任何 provider 拿到：裁剪后的头肩图 bytes、可选的头发 mask bytes、prompt、目标尺寸，
返回一张 size×size 的 PIL 图（只保证头发区域正确，其余由 compose 阶段贴回）。
"""
import io

from PIL import Image


class Provider:
    name = "base"
    supports_n = False          # 支持一次生成 N 个候选（供 pipeline/select 择优）

    def available(self) -> bool:
        raise NotImplementedError

    def generate(self, crop_bytes: bytes, mask_bytes: bytes, prompt: str,
                 negative: str = "", size: int = 768,
                 ref_bytes: bytes | None = None) -> Image.Image:
        """ref_bytes = 目标发型的参考图（发型库封面），有就用，没有退化成纯 prompt。"""
        raise NotImplementedError

    def generate_n(self, crop_bytes: bytes, mask_bytes: bytes, prompt: str,
                   negative: str = "", size: int = 768, n: int = 1,
                   ref_bytes: bytes | None = None):
        """默认退化为重复调用 generate；支持多样本的 provider 覆盖它。"""
        return [self.generate(crop_bytes, mask_bytes, prompt, negative, size,
                              ref_bytes=ref_bytes) for _ in range(max(1, n))]


def to_image(b: bytes) -> Image.Image:
    return Image.open(io.BytesIO(b)).convert("RGB")


def registry():
    from . import mock, gemini, openai_edit, comfy, apimart
    return {"mock": mock.Mock(), "gemini": gemini.Gemini(), "openai": openai_edit.OpenAIEdit(),
            "apimart": apimart.APIMart(), "comfy": comfy.Comfy()}
