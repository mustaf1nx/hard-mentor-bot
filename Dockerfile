FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot
RUN useradd --create-home app
USER app

# Long polling: порт не слушаем, healthcheck не нужен.
CMD ["python", "-m", "bot"]
