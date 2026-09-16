FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/app/data
WORKDIR /app
COPY bot.py cloud_start.py config.py operator_ui.py store.py storefront.py telegram_api.py catalog.json terms.txt shop.part1 shop.part2 shop.part3 ./
RUN cat shop.part1 shop.part2 shop.part3 > shop.py \
    && python -m py_compile bot.py cloud_start.py config.py operator_ui.py shop.py store.py storefront.py telegram_api.py \
    && rm shop.part1 shop.part2 shop.part3 \
    && groupadd --gid 10001 shop \
    && useradd --uid 10001 --gid shop --no-create-home shop \
    && mkdir -p /app/data \
    && chown -R shop:shop /app
USER shop
CMD ["python", "cloud_start.py"]
