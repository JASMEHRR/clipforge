# ClipForge — python:3.11 pinned (MediaPipe wheel compatibility).
# FFmpeg comes from Debian bookworm (5.1.x) and is verified at build time.
FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_INPUT=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    # pin check: bookworm ships FFmpeg 5.x — fail the build on drift
    && ffmpeg -version | head -1 \
    && ffmpeg -version | grep -q "ffmpeg version 5\." \
    && fc-list >/dev/null 2>&1 || true

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --no-input -r requirements.txt

# bundled OFL fonts ship with the image — captions never depend on system fonts
COPY . .

ENV CLIPFORGE_HOST=0.0.0.0 \
    CLIPFORGE_PORT=7860

EXPOSE 7860

VOLUME ["/app/output", "/app/cache", "/app/samples", "/app/inbox"]

CMD ["python", "-m", "server.main"]
