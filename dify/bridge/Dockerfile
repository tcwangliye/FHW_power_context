FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LETHE_BRIDGE_HOST=0.0.0.0 \
    LETHE_BRIDGE_PORT=8080 \
    POWERCONTEXT_URL=http://lethe-powercontext:8000

WORKDIR /app
COPY app.py pc_client.py tavern.py ./

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/livez', timeout=4)"]

CMD ["python", "app.py"]
