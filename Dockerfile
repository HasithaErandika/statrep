FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config

RUN useradd --create-home statrep
USER statrep

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

# One worker so each scheduled check runs exactly once
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--timeout", "180", \
     "--bind", "0.0.0.0:8080", "app.main:create_app()"]
