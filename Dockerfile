FROM python:3.12-slim

# 安装 ffmpeg（yt-dlp 合并音视频需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Railway 会注入 $PORT 环境变量，用 shell 形式的 CMD 来读取它
EXPOSE 8080
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
