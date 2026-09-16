#!/usr/bin/env python3
"""Cloud entry point: securely pair the owner, then run the existing shop."""
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time

from bot import instance_lock, run
from config import Config, ROOT, read_env
from store import Store
from telegram_api import Telegram, ApiError, TransportError


def paired_user(message, secret):
    chat, user = message.get("chat", {}), message.get("from", {})
    expected = f"/start setup_{secret}".encode()
    actual = str(message.get("text", "")).encode()
    if (chat.get("type") == "private" and user.get("id") == chat.get("id")
            and isinstance(user.get("id"), int) and user["id"] > 0 and not user.get("is_bot")
            and secrets.compare_digest(actual, expected)):
        return user
    return None


def save_owner(directory, bot_id, owner_id, support, offset):
    """Never save a bot token. Pairing state contains only public identity fields."""
    payload = {"bot_id": bot_id, "owner_id": owner_id, "support": support}
    temporary = directory / f"owner-{secrets.token_hex(8)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        db = Store(directory / "shop.db")
        try:
            with db.db:
                db.set_meta("offset", offset)
        finally:
            db.close()
        os.replace(temporary, directory / "owner.json")
    finally:
        temporary.unlink(missing_ok=True)


def bootstrap():
    values = {**read_env(ROOT / ".env"), **os.environ}
    directory = Path(values.get("DATA_DIR") or str(ROOT / "data"))
    if (values.get("OWNER_ID") and values.get("SUPPORT_USERNAME")) or (directory / "owner.json").exists():
        return Config.load()
    token = values.get("BOT_TOKEN", "").strip()
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
        raise ValueError("Добавьте BOT_TOKEN в защищённые переменные сервера")
    with instance_lock(directory):
        api = Telegram(token)
        me = api.call("getMe")
        expected = values.get("EXPECTED_BOT_USERNAME", "").lstrip("@").lower()
        if expected and expected != me["username"].lower():
            raise ValueError("Токен относится к другому боту. Проверьте EXPECTED_BOT_USERNAME")
        if api.call("getWebhookInfo").get("url"):
            raise ValueError("Бот уже подключён к webhook. Сначала отключите прежний сервер")
        db = Store(directory / "shop.db")
        try:
            if db.one("SELECT count(*) FROM orders")[0]:
                raise ValueError("В базе уже есть заказы. Восстановите OWNER_ID вместо повторной привязки")
            offset = int(db.meta("offset"))
        finally:
            db.close()
        secret = secrets.token_hex(24)
        print("Для привязки администратора откройте эту личную ссылку в течение 10 минут:", flush=True)
        print(f"https://t.me/{me['username']}?start=setup_{secret}", flush=True)
        print("Ссылку передавайте только владельцу магазина. Токен бота не выводится.", flush=True)
        deadline, owner = time.monotonic() + 600, None
        while time.monotonic() < deadline and owner is None:
            updates = api.call("getUpdates", offset=offset, timeout=15, allowed_updates=["message"])
            for update in updates:
                offset = max(offset, update["update_id"] + 1)
                if owner is not None:
                    continue
                candidate = paired_user(update.get("message", {}), secret)
                if candidate:
                    support = (values.get("SUPPORT_USERNAME") or candidate.get("username", "")).lstrip("@")
                    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", support):
                        api.call("sendMessage", chat_id=candidate["id"], text="Добавьте имя пользователя "
                                 "в настройках своего Telegram-профиля и ещё раз откройте ссылку подключения. "
                                 "Оно будет контактом поддержки магазина.")
                    else:
                        owner = candidate
            if owner:
                save_owner(directory, me["id"], owner["id"], support, offset)
        if owner is None:
            raise ValueError("Время привязки истекло. Перезапустите сервис и откройте новую ссылку из журнала")
        print("Администратор привязан. Запускаем магазин.", flush=True)
    return Config.load()


if __name__ == "__main__":
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        run(bootstrap())
    except (ValueError, RuntimeError, ApiError, TransportError) as exc:
        raise SystemExit(str(exc)) from None
