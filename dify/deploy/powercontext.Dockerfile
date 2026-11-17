FROM python:3.12-slim

# Pinned to the PowerContext release that the competition baseline tracks.
ARG POWERCONTEXT_VERSION=1.2.0

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POWERCONTEXT_HOME=/data \
    POWERCONTEXT_SERVER_HTTP_HOST=0.0.0.0 \
    POWERCONTEXT_SERVER_HTTP_PORT=8000 \
    POWERCONTEXT_SERVER_ACCESS_MODE=disabled \
    POWERCONTEXT_SERVER_ALLOW_UNAUTHENTICATED_NON_LOOPBACK=true \
    POWERCONTEXT_SERVER_LOGGING_LEVEL=INFO

RUN pip install --no-cache-dir "powercontext[cli,server]==${POWERCONTEXT_VERSION}"

WORKDIR /data
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=12 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4)"]

CMD ["powercontext", "server", "run"]
