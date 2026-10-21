# 后端镜像：FastAPI + OpenCV(人脸) + PIL + 支付回调
# Render / Railway / Fly / Koyeb / 自有 VPS 都用这一个文件。
# 镜像层是只读的：发型库改动 / 参考图 / 收款码 / 站点设置 / SQLite 全落在
# WEILI_RUNTIME_DIR（容器里 = /app/storage，挂持久卷即可跨部署保留）。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEPLOY=1 \
    PORT=8899 \
    WEILI_RUNTIME_DIR=/app/storage

# opencv-headless 在 slim 上只需要这两个
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 libgl1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 发型库 / 封面 / 站点配置（只读种子，首次启动会拷进 WEILI_RUNTIME_DIR）
COPY data ./data
# YuNet 人脸检测模型
COPY models ./models
COPY backend ./backend
COPY frontend ./frontend

RUN mkdir -p /app/storage/uploads /app/storage/results /app/storage/candidates \
             /app/storage/debug /app/storage/qr

EXPOSE 8899

HEALTHCHECK --interval=30s --timeout=8s --start-period=25s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT}/api/health || exit 1

CMD ["sh", "-c", "cd /app/backend && exec python -m uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1"]
