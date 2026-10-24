FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg нужен yt-dlp для склейки видео+аудио и remux в mp4.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium --only-shell \
    && rm -rf /var/lib/apt/lists/*

COPY animedl/ ./animedl/
COPY bot.py .

# данные (кэш file_id, rate-файл) и временные файлы — на томах
RUN mkdir -p /app/data /app/tmp

CMD ["python", "bot.py"]
