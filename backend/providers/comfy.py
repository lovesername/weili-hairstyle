# -*- coding: utf-8 -*-
"""ComfyUI 本地出图（RTX 4060 8G：SD1.5-inpainting + IP-Adapter Plus 刚好够，1024 会爆显存）。

链路：头肩裁剪图 + 头发 mask → 局部重绘（只动头发区域）→ 出 N 张候选交给 pipeline/select 择优。

安装（全部落 D 盘，断点续传）：
    python tools/setup_comfy.py
启动：
    D:\\AI\\ComfyUI\\run_comfy.bat          # 127.0.0.1:8188
环境变量（都有默认值，一般不填）：
    COMFY_HOST=http://127.0.0.1:8188
    COMFY_CKPT=Realistic_Vision_V5.1-inpainting.safetensors
    COMFY_IPA=0.72       # IP-Adapter 权重，0 = 关掉（不装自定义节点也能跑）
    COMFY_STEPS=28  COMFY_CFG=5.0  COMFY_DENOISE=0.95  COMFY_TIMEOUT=300
"""
import os
import time
import uuid

import httpx
from PIL import Image

from .base import Provider, to_image

HOST = os.environ.get("COMFY_HOST", "http://127.0.0.1:8188")
CKPT = os.environ.get("COMFY_CKPT", "Realistic_Vision_V5.1-inpainting.safetensors")
IPA = float(os.environ.get("COMFY_IPA", "0.72"))
STEPS = int(os.environ.get("COMFY_STEPS", "32"))
CFG = float(os.environ.get("COMFY_CFG", "4.5"))
DENOISE = float(os.environ.get("COMFY_DENOISE", "0.86"))
TIMEOUT = int(os.environ.get("COMFY_TIMEOUT", "300"))

# 头发重绘的通用前后缀：让 SD1.5 别把额头、脸、背景一起重画
POS_SUFFIX = ("hair, detailed individual hair strands, natural hairline, soft studio lighting, "
              "photorealistic, 8k, sharp focus")
NEG = ("blurry hair, plastic hair, helmet hair, wig, cartoon, anime, painting, oversaturated, "
       "blue hair sheen, purple hair, green hair, colored highlights, glossy plastic sheen, "
       "glowing hair edge, halo around head, harsh mask seam, deformed face, extra ears, "
       "text, watermark, low quality")


def _preset(info, faceid=None):
    """按 ComfyUI 实际提供的 preset 列表挑一个，避免硬编码版本差异。

    默认只挑非 FaceID 的（FaceID 要 insightface，Windows 上经常编译不出来；
    我们装的是 ip-adapter-plus_sd15）。装了 insightface 就设 COMFY_FACEID=1。
    """
    try:
        opts = info["IPAdapterUnifiedLoader"]["input"]["required"]["preset"][0]
    except Exception:
        return None
    if faceid is None:
        faceid = os.environ.get("COMFY_FACEID", "0") == "1"
    if faceid:
        prefer = ("FACEID PLUS V2", "PLUS FACE", "PLUS", "STANDARD")
        pool = opts
    else:
        prefer = ("PLUS (HIGH STRENGTH)", "PLUS", "STANDARD", "LIGHT")
        pool = [o for o in opts if "FACE" not in o.upper()] or opts
    for want in prefer:
        for o in pool:
            if want in o.upper():
                return o
    return pool[0] if pool else None


def build_workflow(info, photo_name, mask_name, prompt, negative, n=1, ref_name=None,
                   seed=None, steps=STEPS, cfg=CFG, denoise=DENOISE, ipa=IPA, grow=6):
    """返回 API 格式 graph。节点号固定，方便对着日志排查。"""
    seed = int(seed if seed is not None else time.time() * 1000) % (2 ** 31)
    prompt = f"{prompt}, {POS_SUFFIX}"
    negative = f"{negative}, {NEG}" if negative else NEG

    g = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT}},
        "2": {"class_type": "LoadImage", "inputs": {"image": photo_name}},
        "3": {"class_type": "LoadImageMask", "inputs": {"image": mask_name, "channel": "red"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": prompt}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": negative}},
        "8": {"class_type": "VAEEncodeForInpaint",
              "inputs": {"pixels": ["2", 0], "vae": ["1", 2], "mask": ["3", 0],
                         "grow_mask_by": grow}},
        "9": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0],
                         "latent_image": ["8", 0], "seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": "dpmpp_2m", "scheduler": "karras",
                         "denoise": denoise}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["1", 2]}},
        "11": {"class_type": "SaveImage",
               "inputs": {"images": ["10", 0], "filename_prefix": "hair"}},
    }
    # 多候选：一份 latent 复制 n 份，一次采样出 n 张（同 seed 下批内噪声不同，结果才不一样）
    if n > 1 and "RepeatLatentBatch" in info:
        g["8b"] = {"class_type": "RepeatLatentBatch",
                   "inputs": {"samples": ["8", 0], "amount": int(n)}}
        g["9"]["inputs"]["latent_image"] = ["8b", 0]

    # IP-Adapter：把参考发型的"样子"带进模型，比纯 prompt 稳得多
    if ref_name and ipa > 0 and "IPAdapterUnifiedLoader" in info:
        ps = _preset(info)
        if ps:
            g["6"] = {"class_type": "IPAdapterUnifiedLoader",
                      "inputs": {"model": ["1", 0], "preset": ps}}
            node = "IPAdapterAdvanced" if "IPAdapterAdvanced" in info else "IPAdapter"
            g["7"] = {"class_type": node,
                      "inputs": {"model": ["6", 0], "ipadapter": ["6", 1],
                                 "image": ["12", 0], "weight": ipa,
                                 "weight_type": "ease out", "combine_embeds": "concat",
                                 "start_at": 0.0, "end_at": 0.85, "embeds_scaling": "V only"}}
            g["12"] = {"class_type": "LoadImage", "inputs": {"image": ref_name}}
            g["9"]["inputs"]["model"] = ["7", 0]
    return g


def _fit(info, g):
    """按 /object_info 里各节点的真实入参裁剪，兼容不同版本的自定义节点。"""
    out = {}
    for nid, node in g.items():
        cls, inputs = node["class_type"], node["inputs"]
        spec = (info.get(cls) or {}).get("input") or {}
        allowed = set(spec.get("required", {})) | set(spec.get("optional", {}))
        out[nid] = {"class_type": cls,
                    "inputs": {k: v for k, v in inputs.items() if not allowed or k in allowed}}
    return out


class Comfy(Provider):
    name = "comfy"
    supports_n = True

    def __init__(self):
        self.host = HOST.rstrip("/")
        self.client_id = uuid.uuid4().hex
        self._info = None

    # ---------- 基础设施 ----------
    def available(self):
        try:
            r = httpx.get(f"{self.host}/system_stats", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def object_info(self, c):
        if self._info is None:
            self._info = c.get(f"{self.host}/object_info", timeout=60).json()
        return self._info

    def _upload(self, c, name, data):
        r = c.post(f"{self.host}/upload/image",
                   files={"image": (name, data, "image/png")},
                   data={"overwrite": "true", "type": "input"})
        r.raise_for_status()
        j = r.json()
        return j.get("name", name)

    # ---------- 出图 ----------
    def generate(self, crop_bytes, mask_bytes, prompt, negative="", size=768, ref_bytes=None,
                 denoise=None, cfg=None, steps=None):
        return self.generate_n(crop_bytes, mask_bytes, prompt, negative, size, n=1,
                               ref_bytes=ref_bytes, denoise=denoise, cfg=cfg, steps=steps)[0]

    def generate_n(self, crop_bytes, mask_bytes, prompt, negative="", size=768, n=1,
                   ref_bytes=None, denoise=None, cfg=None, steps=None):
        """denoise 不给就用默认 0.86；"剪短"这种要重画轮廓的场合由调用方提到 0.95/1.0。"""
        dn = DENOISE if denoise is None else float(denoise)
        cf = CFG if cfg is None else float(cfg)
        stp = STEPS if steps is None else int(steps)
        n = max(1, int(n or 1))
        with httpx.Client(timeout=120) as c:
            info = self.object_info(c)
            if "CheckpointLoaderSimple" not in info:
                raise RuntimeError("ComfyUI 没响应 /object_info，检查 8188 是否起来")
            tag = uuid.uuid4().hex[:8]
            photo = self._upload(c, f"crop_{tag}.png", crop_bytes)
            mask = self._upload(c, f"mask_{tag}.png", mask_bytes)
            ref = self._upload(c, f"ref_{tag}.png", ref_bytes) if ref_bytes else None

            graph = _fit(info, build_workflow(info, photo, mask, prompt, negative, n=n,
                                              ref_name=ref, denoise=dn, cfg=cf, steps=stp))
            r = c.post(f"{self.host}/prompt",
                       json={"prompt": graph, "client_id": self.client_id})
            if r.status_code != 200:
                raise RuntimeError(f"comfy /prompt {r.status_code}: {r.text[:600]}")
            pid = r.json()["prompt_id"]

            t0 = time.time()
            while time.time() - t0 < TIMEOUT:
                h = c.get(f"{self.host}/history/{pid}", timeout=30).json()
                if pid in h:
                    imgs = []
                    for node in h[pid].get("outputs", {}).values():
                        imgs += node.get("images", [])
                    if not imgs:
                        raise RuntimeError(f"comfy 跑完但没输出图: {str(h[pid])[:300]}")
                    outs = []
                    for im in imgs:
                        data = c.get(f"{self.host}/view", params={
                            "filename": im["filename"],
                            "subfolder": im.get("subfolder", ""),
                            "type": im.get("type", "output")}, timeout=60).content
                        outs.append(to_image(data).resize((size, size), Image.LANCZOS))
                    return outs
                time.sleep(1.5)
        raise RuntimeError(f"comfy 超时({TIMEOUT}s)，看 ComfyUI 控制台是否在跑第一张（首次加载模型要 20~40s）")
