#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ComfyUI 本地出图环境一键安装（全部落在 D 盘，零边际成本出图）。

做四件事：
  1) git clone ComfyUI 到 D:\\AI\\ComfyUI（--depth 1）
  2) 建独立 venv（默认用 Python 3.12，torch cu126，RTX 4060 8G 够用）
  3) 装 ComfyUI_IPAdapter_plus 自定义节点（Plus 变体不需要 insightface，避开 Windows 编译）
  4) 从 hf-mirror 断点续传拉模型到 D:\\AI\\ComfyUI\\models

用法（任选）：
  python tools/setup_comfy.py                 # 全量安装
  python tools/setup_comfy.py --models-only   # 只补模型，断了就再跑一遍
  python tools/setup_comfy.py --with-faceid    # 额外装 FaceID（要 insightface，能编译才有用）

装完：
  D:\\AI\\ComfyUI\\run_comfy.bat              # 起 ComfyUI（127.0.0.1:8188）
  ai-hairstyle\\start.bat                      # 起项目后端（8899），provider 选 comfy
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.environ.get("COMFY_ROOT", r"D:\AI\ComfyUI")
PY312 = os.environ.get("COMFY_PYTHON", r"C:\ProgramData\Accio\pre-install\2186c6e1bbb5\python\python.exe")
MIRRORS = ("https://hf-mirror.com", "https://huggingface.co")

# (本地相对路径, repo/文件, 期望字节数≈, 说明)
MODELS = [
    ("models/checkpoints/Realistic_Vision_V5.1-inpainting.safetensors",
     "SG161222/Realistic_Vision_V5.1_noVAE/resolve/main/Realistic_Vision_V5.1-inpainting.safetensors",
     2_130_000_000, "SD1.5 写实 inpainting 主模型"),
    ("models/clip_vision/CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors",
     "h94/IP-Adapter/resolve/main/models/image_encoder/model.safetensors",
     2_520_000_000, "IP-Adapter 用的 CLIP-ViT-H 图像编码器"),
    ("models/ipadapter/ip-adapter-plus_sd15.safetensors",
     "h94/IP-Adapter/resolve/main/models/ip-adapter-plus_sd15.safetensors",
     98_000_000, "IP-Adapter Plus（把参考发型的样子带过来）"),
]

FACEID = [
    ("models/ipadapter/ip-adapter-faceid-plusv2_sd15.bin",
     "h94/IP-Adapter-FaceID/resolve/main/ip-adapter-faceid-plusv2_sd15.bin",
     330_000_000, "FaceID PlusV2（锁人脸身份）"),
    ("models/loras/ip-adapter-faceid-plusv2_sd15_lora.safetensors",
     "h94/IP-Adapter-FaceID/resolve/main/ip-adapter-faceid-plusv2_sd15_lora.safetensors",
     70_000_000, "FaceID PlusV2 LoRA"),
]
ANTELOPE = ["1k3d68.onnx", "2d106det.onnx", "genderage.onnx", "glintr100.onnx",
            "scrfd_10g_bnkps.onnx"]


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def sh(cmd, cwd=None, env=None):
    log("$", " ".join(cmd) if isinstance(cmd, list) else cmd)
    p = subprocess.run(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str))
    if p.returncode != 0:
        raise SystemExit(f"命令失败({p.returncode})")


def dl(url_path, dst, expect=0):
    """断点续传下载；先试 hf-mirror，全部镜像失败才报错。"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst) and expect and os.path.getsize(dst) >= expect * 0.97:
        log("跳过(已存在)", os.path.basename(dst), f"{os.path.getsize(dst)/2**30:.2f}G")
        return dst

    last = None
    for host in MIRRORS:
        url = f"{host}/{url_path}"
        for attempt in (1, 2, 3):
            have = os.path.getsize(dst) if os.path.exists(dst) else 0
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            if have:
                req.add_header("Range", f"bytes={have}-")
            try:
                t0, tlast = time.time(), time.time()
                with urllib.request.urlopen(req, timeout=60) as r:
                    total = int(r.headers.get("Content-Length") or 0) + have
                    mode = "ab" if have and r.status == 206 else "wb"
                    if mode == "wb":
                        have = 0
                    done = have
                    with open(dst, mode) as f:
                        while True:
                            chunk = r.read(1 << 20)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            if time.time() - tlast > 3:
                                tlast = time.time()
                                sp = done / max(1e-6, time.time() - t0) / 2 ** 20
                                pct = f"{done*100/total:.1f}%" if total else "?"
                                log(f"  {os.path.basename(dst)} {done/2**20:.0f}M {pct} {sp:.1f}MB/s")
                size = os.path.getsize(dst)
                if expect and size < expect * 0.97:
                    log("  尺寸偏小，重试", size, "期望", expect)
                    continue
                log("完成", os.path.basename(dst), f"{size/2**30:.2f}G")
                return dst
            except Exception as e:  # noqa: BLE001
                last = e
                log("  失败", type(e).__name__, str(e)[:120], "→ 重试")
                time.sleep(2 * attempt)
    raise SystemExit(f"下载失败 {url_path}: {last}")


def ensure_python():
    if os.path.exists(PY312):
        return PY312
    cand = shutil.which("python")
    if cand:
        log("用系统 python:", cand)
        return cand
    raise SystemExit("找不到可用的 python（设置 COMFY_PYTHON 指向 3.11/3.12 的 python.exe）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--models-only", action="store_true")
    ap.add_argument("--with-faceid", action="store_true")
    ap.add_argument("--skip-torch", action="store_true")
    a = ap.parse_args()
    root = a.root
    # 下载缓存也放 D 盘，别占 C（pip 的 wheel 缓存 + HF 模型缓存）
    os.environ.setdefault("PIP_CACHE_DIR", os.path.join(root, "cache", "pip"))
    os.environ.setdefault("HF_HOME", os.path.join(root, "cache", "hf"))
    os.makedirs(os.environ["PIP_CACHE_DIR"], exist_ok=True)
    venv_py = os.path.join(root, ".venv", "Scripts", "python.exe")
    py = ensure_python()

    if not a.models_only:
        # 1) ComfyUI 源码
        if not os.path.exists(os.path.join(root, "main.py")):
            os.makedirs(root, exist_ok=True)
            sh(["git", "clone", "--depth", "1",
                "https://github.com/comfyanonymous/ComfyUI.git", root])
        else:
            log("ComfyUI 已存在，跳过 clone")

        # 2) venv
        if not os.path.exists(venv_py):
            log("[2/4] 建 venv:", root)
            sh([py, "-m", "venv", os.path.join(root, ".venv")])
        sh([venv_py, "-m", "pip", "install", "-q", "--upgrade", "pip", "setuptools", "wheel"])

        # 3) torch cu126 + ComfyUI 依赖
        if not a.skip_torch:
            log("[3/4] 装 torch cu126（约 2.5G，慢）")
            sh([venv_py, "-m", "pip", "install", "torch", "torchvision", "torchaudio",
                "--index-url", "https://download.pytorch.org/whl/cu126"])
        sh([venv_py, "-m", "pip", "install", "-r",
            os.path.join(root, "requirements.txt")])

        # 4) IPAdapter_plus（Plus 变体不需要 insightface）
        ipa = os.path.join(root, "custom_nodes", "ComfyUI_IPAdapter_plus")
        if not os.path.exists(ipa):
            log("[4/4] 装 ComfyUI_IPAdapter_plus")
            sh(["git", "clone", "--depth", "1",
                "https://github.com/cubiq/ComfyUI_IPAdapter_plus.git", ipa])
        if a.with_faceid:
            try:
                sh([venv_py, "-m", "pip", "install", "insightface", "onnxruntime-gpu"])
            except SystemExit:
                log("!! insightface 装不上（缺 MSVC），退回 Plus 方案，不影响出图")

    log("下载模型 →", os.path.join(root, "models"))
    for rel, src, expect, why in MODELS:
        log("·", why)
        dl(src, os.path.join(root, rel), expect)
    if a.with_faceid:
        for rel, src, expect, why in FACEID:
            log("·", why)
            dl(src, os.path.join(root, rel), expect)
        for f in ANTELOPE:
            dl(f"DIAMONIK7777/antelopev2/resolve/main/{f}",
               os.path.join(root, "models", "insightface", "models", "antelopev2", f))

    write_launcher(root, venv_py)
    log("=" * 60)
    log("装完了。起 ComfyUI：", os.path.join(root, "run_comfy.bat"))
    log("自检：curl --noproxy * http://127.0.0.1:8188/system_stats")
    log("=" * 60)


def write_launcher(root, venv_py):
    bat = os.path.join(root, "run_comfy.bat")
    body = f"""@echo off
chcp 65001 >nul
set HF_ENDPOINT=https://hf-mirror.com
set HF_HUB_ENDPOINT=https://hf-mirror.com
cd /d "{root}"
"{venv_py}" main.py --listen 127.0.0.1 --port 8188 --disable-auto-launch --preview-method none
"""
    open(bat, "w", encoding="utf-8").write(body)
    log("写启动器", bat)


if __name__ == "__main__":
    main()
