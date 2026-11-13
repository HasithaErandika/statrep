# Pinned by digest for reproducible builds; Dependabot (.github/dependabot.yml) proposes updates
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TLDEXTRACT_CACHE=/tmp/tldextract

WORKDIR /app

# ping, for config/ip.yml
# Cache mounts keep downloaded apt/pip packages between builds (not in the image),
# so a rebuild after a base-image or requirements bump doesn't re-download everything
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update && apt-get install -y --no-install-recommends iputils-ping

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

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
