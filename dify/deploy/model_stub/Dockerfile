FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    STUB_PORT=8080 \
    STUB_MODEL_NAME=lethe-npc

WORKDIR /app
COPY server.py ./

EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=5s --start-period=3s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/v1/models', timeout=3)"]

CMD ["python", "server.py"]
