# Pinned by digest for reproducible builds; Dependabot (.github/dependabot.yml) proposes updates
FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TLDEXTRACT_CACHE=/tmp/tldextract

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config

RUN useradd --no-create-home --shell /usr/sbin/nologin statrep
USER statrep

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

# One worker so each scheduled check runs exactly once
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "180", \
     "--worker-tmp-dir", "/dev/shm", "--limit-request-line", "4096", \
     "--bind", "0.0.0.0:8080", "app.main:create_app()"]
