#!/usr/bin/env python3
"""Run one long-polling worker: python3 bot.py."""
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time

from config import Config, ROOT
from shop import Shop, load_catalog
from store import Store, process_outbox
from telegram_api import Telegram, ApiError, TransportError

log = logging.getLogger("shop")


@contextmanager
def instance_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / "worker.lock").open("a+b")
    try:
        try:
            if os.name == "nt":
                import msvcrt
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Этот магазин уже запущен. Остановите второй экземпляр.") from None
        yield
    finally:
        lock.close()


def build(config):
    config.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(config.data_dir / "shop.db")
    api = Telegram(config.token)
    catalog = load_catalog(ROOT / "catalog.json")
    terms = (ROOT / "terms.txt").read_text(encoding="utf-8")
    if not 50 <= len(terms) <= 1400:
        raise ValueError("terms.txt: ожидается 50–1400 символов")
    return store, api, Shop(config, store, api, catalog, terms)


def run(config):
    stop = threading.Event()
    worker_failed = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with instance_lock(config.data_dir):
        store, api, app = build(config)
        worker = None
        try:
            info = api.call("getMe")
            if api.call("getWebhookInfo").get("url"):
                raise RuntimeError("У этого бота настроен webhook. Используйте новый бот или сначала отключите старый сервер.")
            api.call("setMyCommands", commands=[
                {"command": "catalog", "description": "Каталог подписок"},
                {"command": "search", "description": "Поиск подписки"},
                {"command": "favorites", "description": "Избранное"},
                {"command": "orders", "description": "Мои заказы"},
                {"command": "paysupport", "description": "Поддержка и вопросы об оплате"},
                {"command": "terms", "description": "Условия заказа"},
                {"command": "privacy", "description": "Обработка данных"},
            ])
            log.info("Бот @%s запущен. Оплата: %s", info["username"], config.payments_enabled)
            with store.db:
                store.run("UPDATE orders SET status='delivery_queued' WHERE status='delivery_sending'")

            def send_worker():
                outgoing = Store(config.data_dir / "shop.db")
                try:
                    while not stop.is_set():
                        process_outbox(outgoing, Telegram(config.token), config.owner_id, limit=1)
                        stop.wait(0.1)
                except Exception as exc:
                    log.error("Остановлена отправка сообщений: %s", type(exc).__name__)
                    worker_failed.set()
                    stop.set()
                finally:
                    outgoing.close()

            worker = threading.Thread(target=send_worker, name="outbox", daemon=True)
            worker.start()
            backoff = 1
            while not stop.is_set():
                try:
                    updates = api.call("getUpdates", offset=int(store.meta("offset")), timeout=15,
                                       allowed_updates=["message", "callback_query", "pre_checkout_query"])
                    updates.sort(key=lambda u: (0 if "pre_checkout_query" in u else 1, u["update_id"]))
                    for update in updates:
                        app.handle(update)
                    with store.db:
                        if updates:
                            store.set_meta("offset", max(u["update_id"] for u in updates) + 1)
                        store.set_meta("heartbeat", int(time.time()))
                    backoff = 1
                except (ApiError, TransportError) as exc:
                    if isinstance(exc, ApiError) and exc.code in (401, 409):
                        raise RuntimeError("Проверьте токен и убедитесь, что бот работает в одном экземпляре.") from None
                    log.warning("Связь с Telegram: %s. Повтор через %s с.", type(exc).__name__, backoff)
                    stop.wait(max(backoff, getattr(exc, "retry_after", 0)))
                    backoff = min(30, backoff * 2)
            if worker_failed.is_set():
                raise RuntimeError("Отправка остановлена. Перезапустите бот; заказы сохранены.")
        finally:
            stop.set()
            if worker:
                worker.join(timeout=10)
            store.close()


if __name__ == "__main__":
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        run(Config.load())
    except (ValueError, RuntimeError, ApiError, TransportError) as exc:
        log.error("%s", exc)
        sys.exit(1)
