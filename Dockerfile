FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY . /app/savedbot

RUN mkdir -p /data/sessions /data/media /data/logs

ENV BASE_DIR=/data

CMD ["python", "-m", "savedbot.handlers"]
