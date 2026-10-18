FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FS_DB=/data/franticsearch.db

WORKDIR /app
COPY pyproject.toml ./
COPY franticsearch ./franticsearch
RUN pip install --no-cache-dir ".[serve]"

VOLUME /data
EXPOSE 6602

# One worker on purpose: the background Scryfall sync thread must run once per
# container, and SQLite prefers a single writer. Threads cover concurrency.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:6602", \
     "--access-logfile", "-", "franticsearch.wsgi:app"]
