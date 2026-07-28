FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY pyproject.toml README.md ./
COPY backend ./backend

RUN pip install --upgrade pip \
    && pip install . \
    && playwright install --with-deps chromium \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin faraflow \
    && mkdir -p /app/data /app/artifacts /app/browser-state \
    && chown -R faraflow:faraflow /app /ms-playwright

USER 10001

EXPOSE 8080

CMD ["uvicorn", "faraflow.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
