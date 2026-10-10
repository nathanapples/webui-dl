FROM python:3.12-slim

# ffmpeg: merging formats and embedding tags/thumbnails. deno: yt-dlp needs a JS runtime for YouTube.
# tzdata: TZ support.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ARG GIT_SHA=""
ENV GIT_SHA=$GIT_SHA \
    CONFIG_DIR=/config \
    DOWNLOAD_DIR=/downloads \
    PUID=1000 \
    PGID=1000 \
    TZ=Etc/UTC \
    MAX_CONCURRENT=2 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
VOLUME ["/config", "/downloads"]
EXPOSE 7341
ENTRYPOINT ["/entrypoint.sh"]
