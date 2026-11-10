FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Asia/Shanghai
ENV MALLOC_ARENA_MAX=2
LABEL org.opencontainers.image.title="MusicHub · 音乐汇" \
      org.opencontainers.image.description="Multi-source music server with Subsonic support" \
      org.opencontainers.image.source="https://github.com/quyanchun/music-hub" \
      org.opencontainers.image.licenses="MIT"
WORKDIR /experiment
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --disable-pip-version-check -r requirements.txt
COPY --chmod=644 *.py ./
COPY --chmod=755 providers/ ./providers/
COPY LICENSE THIRD_PARTY_NOTICES.md ./
RUN mkdir -p /experiment/data && chown 10001:10001 /experiment/data
USER 10001:10001
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -S -c "import socket; s=socket.create_connection(('127.0.0.1',8766),3); s.sendall(b'GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n'); assert b' 200 ' in s.recv(64); s.close()"
ENTRYPOINT ["python", "/experiment/service.py"]
CMD ["--host", "0.0.0.0", "--port", "8766"]
