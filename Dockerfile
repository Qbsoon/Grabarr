FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py config.yaml ./
COPY static ./static

RUN useradd -r -s /usr/sbin/nologin grabarr \
    && mkdir -p /data /plex/movies /plex/series \
    && chown -R grabarr /data /plex

USER grabarr

ENV DB_PATH=/data/grabarr.db

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://127.0.0.1:8000/api/health', timeout=4).raise_for_status()"

CMD ["hypercorn", "app:app", "--bind", "0.0.0.0:8000"]
