FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    PORT=8000

RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data && chown app:app /data

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini bc_deploy.py ./
COPY migrations ./migrations
COPY app ./app

USER app
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=4)"

# Un solo worker: scheduler, lock per environment ed eventi live vivono nel processo.
# --proxy-headers: lo schema https e l'IP del client arrivano dal proxy dell'hosting.
CMD ["sh", "-c", "exec uvicorn app.asgi:app --host 0.0.0.0 --port \"$PORT\" --workers 1 --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 30"]
