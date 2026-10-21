# 后端镜像：FastAPI + OpenCV(人脸) + PIL + 支付回调
# 部署到 Render / Railway / Fly / Koyeb 都用这一个文件。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEPLOY=1 \
    PORT=8899

# opencv-headless 在 slim 上只需要这两个
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 发型库 / 封面 / 实拍图 / 站点配置
COPY data ./data
# YuNet 人脸检测模型
COPY models ./models
COPY backend ./backend
COPY frontend ./frontend

# 运行时数据（SQLite + 上传 + 结果）：挂持久卷就挂到 /app/storage
RUN mkdir -p /app/storage/uploads /app/storage/results /app/storage/candidates \
             /app/storage/debug /app/storage/qr

EXPOSE 8899

CMD ["sh", "-c", "cd /app/backend && exec python -m uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1"]
