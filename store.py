"""Durable orders, payment ledger and transactional outbox. Single worker."""
import json
import os
import sqlite3
import time


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS handled (id INTEGER PRIMARY KEY, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS drafts (
 user_id INTEGER PRIMARY KEY, product_id TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS selections (
 user_id INTEGER PRIMARY KEY, data TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS favorites (
 user_id INTEGER NOT NULL, product_id TEXT NOT NULL, PRIMARY KEY(user_id, product_id)
);
CREATE TABLE IF NOT EXISTS operator_drafts (
 user_id INTEGER PRIMARY KEY, data TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, order_id TEXT,
 status TEXT NOT NULL DEFAULT 'open', created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL REFERENCES tickets(id),
 author TEXT NOT NULL, body TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, product_id TEXT NOT NULL,
 title TEXT NOT NULL, details TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'requested',
 created INTEGER NOT NULL, price INTEGER, offer TEXT, expires INTEGER,
 accepted_terms TEXT, checkout_id TEXT, checkout_at INTEGER, delivery TEXT,
 delivered_at INTEGER
);
CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id, created DESC);
CREATE TABLE IF NOT EXISTS payments (
 charge TEXT PRIMARY KEY, order_id TEXT REFERENCES orders(id), user_id INTEGER NOT NULL,
 amount INTEGER NOT NULL, currency TEXT NOT NULL, payload TEXT NOT NULL,
 status TEXT NOT NULL, is_primary INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_primary_payment
 ON payments(order_id) WHERE is_primary=1;
CREATE TABLE IF NOT EXISTS checkouts (
 id TEXT PRIMARY KEY, approved INTEGER NOT NULL, error TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe TEXT UNIQUE NOT NULL,
 method TEXT NOT NULL, params TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'message',
 ref TEXT, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_try INTEGER NOT NULL DEFAULT 0, last_error TEXT
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(state, next_try, id);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if str(path) != ":memory:":
            os.chmod(path, 0o600)

    def one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def all(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()

    def run(self, sql, args=()):
        return self.db.execute(sql, args)

    def meta(self, key, default="0"):
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return row[0] if row else default

    def set_meta(self, key, value):
        self.run("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))

    def job(self, key, method, params, kind="message", ref=None):
        self.run("INSERT OR IGNORE INTO outbox(dedupe,method,params,kind,ref) VALUES (?,?,?,?,?)",
                 (key, method, json.dumps(params, ensure_ascii=False), kind, ref))

    def order(self, oid):
        return self.one("SELECT * FROM orders WHERE id=?", (oid,))

    def finish_refund(self, charge):
        payment = self.one("SELECT * FROM payments WHERE charge=?", (charge,))
        if not payment:
            return
        self.run("UPDATE payments SET status='refunded' WHERE charge=?", (charge,))
        if payment["is_primary"]:
            self.run("UPDATE orders SET status='refunded' WHERE id=?", (payment["order_id"],))
        self.job(f"refund-notice:{charge}", "sendMessage", {
            "chat_id": payment["user_id"],
            "text": f"Возврат выполнен: {payment['amount']} ⭐. Заказ {payment['order_id'] or '—'}.",
        })

    def close(self):
        self.db.close()


def process_outbox(store, api, owner_id, now=None, limit=2):
    """Retries money operations by charge ID; delivery retries the same saved code."""
    from telegram_api import ApiError, TransportError
    now = int(time.time()) if now is None else now
    jobs = store.all("SELECT * FROM outbox WHERE state='pending' AND next_try<=? ORDER BY id LIMIT ?",
                     (now, limit))
    for job in jobs:
        with store.db:
            if job["kind"] in ("invoice", "delivery"):
                order = store.order(job["ref"])
                valid = ("quoted", "checkout") if job["kind"] == "invoice" else ("delivery_queued", "delivered")
                if not order or order["status"] not in valid:
                    store.run("UPDATE outbox SET state='cancelled' WHERE id=?", (job["id"],))
                    continue
                if job["kind"] == "delivery" and order["status"] == "delivery_queued":
                    store.run("UPDATE orders SET status='delivery_sending' WHERE id=?", (job["ref"],))
            if job["kind"] == "refund":
                payment = store.one("SELECT * FROM payments WHERE charge=?", (job["ref"],))
                if payment["status"] == "refunded":
                    store.run("UPDATE outbox SET state='sent' WHERE id=?", (job["id"],))
                    continue
        try:
            api.call(job["method"], **json.loads(job["params"]))
        except (ApiError, TransportError) as exc:
            if job["kind"] == "screen" and isinstance(exc, ApiError) and exc.code == 400:
                description = exc.description.casefold()
                if "message is not modified" in description:
                    with store.db:
                        store.run("UPDATE outbox SET state='sent' WHERE id=?", (job["id"],))
                    continue
                if any(phrase in description for phrase in (
                        "message to edit not found", "message can't be edited", "message can not be edited")):
                    params = json.loads(job["params"])
                    params.pop("message_id", None)
                    with store.db:
                        store.run("UPDATE outbox SET method='sendMessage',params=?,kind='message',next_try=0 WHERE id=?",
                                  (json.dumps(params, ensure_ascii=False), job["id"]))
                    continue
            refunded = (isinstance(exc, ApiError) and job["kind"] == "refund" and
                        ("PAYMENT_ALREADY_REFUNDED" in exc.description.upper() or
                         "ALREADY REFUNDED" in exc.description.upper()))
            if not refunded:
                temporary = isinstance(exc, TransportError) or exc.code == 429 or exc.code >= 500
                delay = max(getattr(exc, "retry_after", 0), min(300, 2 ** min(job["attempts"] + 1, 8)))
                state = "pending" if temporary else "failed"
                with store.db:
                    if job["kind"] == "delivery":
                        store.run("UPDATE orders SET status='delivery_queued' WHERE id=? AND status='delivery_sending'",
                                  (job["ref"],))
                    store.run("UPDATE outbox SET state=?, attempts=attempts+1, next_try=?, last_error=? WHERE id=?",
                              (state, now + delay, type(exc).__name__ + ": " + str(exc)[:300], job["id"]))
                    if state == "failed" and job["kind"] in ("refund", "delivery", "invoice"):
                        store.job(f"failed-alert:{job['id']}", "sendMessage", {
                            "chat_id": owner_id,
                            "text": f"Не выполнена операция №{job['id']} ({job['kind']}). "
                                    "Проверьте /failures. Статус заказа сохранён; успех не подтверждён.",
                        })
                continue
        with store.db:
            store.run("UPDATE outbox SET state='sent',last_error=NULL WHERE id=?", (job["id"],))
            if job["kind"] == "delivery":
                store.run("UPDATE orders SET status='delivered',delivered_at=? WHERE id=? AND status='delivery_sending'",
                          (now, job["ref"]))
            elif job["kind"] == "refund":
                store.finish_refund(job["ref"])
