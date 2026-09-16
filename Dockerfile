FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/app/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends unzip && rm -rf /var/lib/apt/lists/*
COPY telegram-subscriptions-bot.zip /tmp/app.zip
RUN unzip -q /tmp/app.zip -d /tmp/src \
    && cp -a /tmp/src/subscription-shop/. /app/ \
    && rm -rf /tmp/app.zip /tmp/src \
    && groupadd --gid 10001 shop \
    && useradd --uid 10001 --gid shop --no-create-home shop \
    && mkdir -p /app/data \
    && chown -R shop:shop /app
USER shop
CMD ["python", "cloud_start.py"]
