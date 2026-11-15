FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SENTINEL_HOST=0.0.0.0 \
    SENTINEL_PORT=8000 \
    SENTINEL_DB_PATH=/data/sentinel.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY sentinel ./sentinel
COPY static ./static

# Run as an unprivileged user; /data holds the SQLite history.
RUN useradd --system --uid 10001 sentinel && mkdir /data && chown sentinel /data
USER sentinel
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["python", "main.py"]
