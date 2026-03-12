FROM python:3.12-slim

# 安装 ffmpeg（yt-dlp 合并音视频需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 如需语音转文字，取消下一行注释：
# RUN pip install --no-cache-dir faster-whisper

COPY main.py .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
