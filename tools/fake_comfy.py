#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""假 ComfyUI（只验链路，不出真图）：真模型还在下载时，用它把 app→provider→候选→择优 整条链跑通。

它做的事比"返回一张图"多一点——会按 ComfyUI 的真实规则校验 graph：
  · class_type 必须在 /object_info 里存在
  · 连线 ["节点id", 槽位] 必须指向存在的节点
  · required 入参不能缺
  · 认 RepeatLatentBatch.amount，返回对应张数（否则 n=4 只回 1 张的 bug 会被吞掉）

    python tools/fake_comfy.py            # 127.0.0.1:8199
    set COMFY_HOST=http://127.0.0.1:8199
    python tools/check_comfy.py test_face.jpg 4
"""
import io
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from PIL import Image, ImageEnhance

INFO = {
    "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["sd-v1-5-inpainting.ckpt",
        "Realistic_Vision_V5.1-inpainting.safetensors"]]}}},
    "LoadImage": {"input": {"required": {"image": [["x.png"]]}}},
    "LoadImageMask": {"input": {"required": {"image": [[]], "channel": [["red", "green", "blue"]]}}},
    "CLIPTextEncode": {"input": {"required": {"clip": ["CLIP"], "text": ["STRING"]}}},
    "VAEEncodeForInpaint": {"input": {"required": {"pixels": ["IMAGE"], "vae": ["VAE"],
                                                   "mask": ["MASK"]},
                                     "optional": {"grow_mask_by": ["INT"]}}},
    "KSampler": {"input": {"required": {
        "model": ["MODEL"], "positive": ["CONDITIONING"], "negative": ["CONDITIONING"],
        "latent_image": ["LATENT"], "seed": ["INT"], "steps": ["INT"], "cfg": ["FLOAT"],
        "sampler_name": [["dpmpp_2m", "euler"]], "scheduler": [["karras", "normal"]],
        "denoise": ["FLOAT"]}}},
    "VAEDecode": {"input": {"required": {"samples": ["LATENT"], "vae": ["VAE"]}}},
    "SaveImage": {"input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING"]}}},
    "RepeatLatentBatch": {"input": {"required": {"samples": ["LATENT"], "amount": ["INT"]}}},
    "IPAdapterUnifiedLoader": {"input": {"required": {"model": ["MODEL"], "preset": [[
        "LIGHT - SD1.5 only (low strength)", "STANDARD (medium strength)",
        "PLUS (high strength)", "PLUS FACE (portraits)", "FACEID PLUS V2"]]}}},
    "IPAdapterAdvanced": {"input": {"required": {
        "model": ["MODEL"], "ipadapter": ["IPADAPTER"], "image": ["IMAGE"], "weight": ["FLOAT"],
        "weight_type": [["linear", "ease out"]], "combine_embeds": [["concat"]],
        "start_at": ["FLOAT"], "end_at": ["FLOAT"], "embeds_scaling": [["V only"]]}}},
}

STORE = {}      # prompt_id -> {"graph":..., "t":..., "amount":n}
FILES = {}      # filename -> bytes
OUT = {}        # filename -> bytes


def validate(graph):
    errs = []
    for nid, node in graph.items():
        cls = node.get("class_type")
        if cls not in INFO:
            errs.append(f"节点 {nid}: 未知 class_type {cls}")
            continue
        spec = INFO[cls].get("input", {})
        req = set(spec.get("required", {}))
        allowed = req | set(spec.get("optional", {}))
        for k in node.get("inputs", {}):
            if k not in allowed:
                errs.append(f"节点 {nid}({cls}): 入参 {k} 不存在，允许 {sorted(allowed)}")
        for k in req:
            if k not in node.get("inputs", {}):
                errs.append(f"节点 {nid}({cls}): 缺 required 入参 {k}")
        for k, v in node.get("inputs", {}).items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0].isdigit():
                if v[0] not in graph:
                    errs.append(f"节点 {nid}.{k} 指向不存在的节点 {v[0]}")
    for nid, node in graph.items():
        if node["class_type"] == "SaveImage":
            src = node["inputs"].get("images")
            if not (isinstance(src, list) and src[0] in graph):
                errs.append("SaveImage 没接到图")
    return errs


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, o, code=200):
        b = json.dumps(o).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _raw(self, b, ct="application/octet-stream"):
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/system_stats":
            return self._json({"system": {"os": "nt", "comfyui_version": "fake"},
                               "devices": [{"name": "FAKE-4060 (mock)", "type": "cuda",
                                            "vram_total": 8589934592,
                                            "vram_free": 6442450944}]})
        if u.path == "/object_info":
            return self._json(INFO)
        if u.path.startswith("/history/"):
            pid = u.path.rsplit("/", 1)[-1]
            rec = STORE.get(pid)
            if not rec:
                return self._json({})
            if time.time() - rec["t"] < 1.2:      # 模拟一点点耗时，验证轮询分支
                return self._json({})
            imgs = []
            base = Image.open(io.BytesIO(FILES[rec["crop"]]))
            for k in range(rec["amount"]):
                im = ImageEnhance.Brightness(base).enhance(1.0 + 0.06 * k)
                im = ImageEnhance.Color(im).enhance(0.9 + 0.1 * k)
                fn = f"hair_fake_{k}.png"
                b = io.BytesIO()
                im.save(b, "PNG")
                OUT[fn] = b.getvalue()
                imgs.append({"filename": fn, "subfolder": "", "type": "output"})
            return self._json({pid: {"outputs": {"11": {"images": imgs}}, "status": {"completed": True}}})
        if u.path == "/view":
            fn = (q.get("filename") or [""])[0]
            if fn in OUT:
                return self._raw(OUT[fn], "image/png")
            return self._json({"error": "no such file"}, 404)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        if u.path == "/upload/image":
            name = f"up_{uuid.uuid4().hex[:8]}.png"
            # 多段体里最后一个 PNG 块就是图；够用了
            i = body.rfind(b"\x89PNG\r\n\x1a\n")
            if i >= 0:
                FILES[name] = body[i:body.rfind(b"\r\n--")]
            return self._json({"name": name, "subfolder": "", "type": "input"})
        if u.path == "/prompt":
            try:
                payload = json.loads(body)
            except Exception:
                return self._json({"error": "bad json"}, 400)
            g = payload.get("prompt") or {}
            errs = validate(g)
            if errs:
                return self._json({"error": {"type": "prompt_outputs_failed_validation",
                                             "message": "；".join(errs)}, "node_errors": errs}, 400)
            amount = 1
            crop = None
            for node in g.values():
                if node["class_type"] == "RepeatLatentBatch":
                    amount = int(node["inputs"]["amount"])
                if node["class_type"] == "LoadImage":
                    crop = node["inputs"]["image"]
            pid = uuid.uuid4().hex
            STORE[pid] = {"t": time.time(), "amount": amount, "crop": crop, "graph": g}
            return self._json({"prompt_id": pid, "number": len(STORE)})
        return self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    port = int(os.environ.get("FAKE_COMFY_PORT", "8199"))
    print(f"fake ComfyUI on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
