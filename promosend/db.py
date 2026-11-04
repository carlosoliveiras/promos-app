import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("PROMOSEND_DB", "promosend.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS terms (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    term TEXT NOT NULL,
    grupo TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(provider, term)
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    name TEXT,
    image TEXT,
    url TEXT,
    affiliate_url TEXT,
    price REAL,
    price_original REAL,
    discount TEXT,
    commission REAL,
    sales INTEGER,
    grupo TEXT,
    status TEXT NOT NULL DEFAULT 'PENDENTE',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(provider, external_id)
);
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    token TEXT NOT NULL,
    posts INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS bot_groups (
    bot_id INTEGER NOT NULL,
    jid TEXT NOT NULL,
    name TEXT,
    PRIMARY KEY (bot_id, jid)
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    job TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT
);
CREATE INDEX IF NOT EXISTS logs_created ON logs (created_at);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    cron TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    last_run TEXT,
    last_status TEXT
);
"""


@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def rows(sql, *args):
    with connect() as con:
        return con.execute(sql, args).fetchall()


def row(sql, *args):
    with connect() as con:
        return con.execute(sql, args).fetchone()


def execute(sql, *args):
    with connect() as con:
        return con.execute(sql, args).rowcount


def init():
    with connect() as con:
        con.executescript(SCHEMA)


LOG_DAYS = 7
LOG_LEVELS = ("info", "ok", "aviso", "erro")


def log(job, level, message, details=None):
    with connect() as con:
        return con.execute("INSERT INTO logs (job, level, message, details) VALUES (?, ?, ?, ?)",
                           (job, level, message[:2000], details)).lastrowid


def prune_logs():
    return execute(f"DELETE FROM logs WHERE created_at < datetime('now', 'localtime', '-{LOG_DAYS} days')")


def get_setting(key, default=""):
    r = row("SELECT value FROM settings WHERE key = ?", key)
    return r["value"] if r else default


def set_setting(key, value):
    execute("INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, value)


def provider_config(pkey, fields):
    return {f["key"]: get_setting(f"{pkey}.{f['key']}", f.get("default", "")) for f in fields}


def upsert_products(provider, grupo, items):
    # status e link de afiliado já gerados não são sobrescritos numa nova busca
    with connect() as con:
        con.executemany(
            """INSERT INTO products (provider, external_id, name, image, url, affiliate_url, price,
                   price_original, discount, commission, sales, grupo, status)
               VALUES (:provider, :external_id, :name, :image, :url, :affiliate_url, :price,
                   :price_original, :discount, :commission, :sales, :grupo, :status)
               ON CONFLICT(provider, external_id) DO UPDATE SET
                   name = excluded.name, image = excluded.image, url = excluded.url,
                   price = excluded.price, price_original = excluded.price_original,
                   discount = excluded.discount, commission = excluded.commission,
                   sales = excluded.sales, grupo = excluded.grupo,
                   affiliate_url = COALESCE(excluded.affiliate_url, products.affiliate_url)""",
            [{"provider": provider, "grupo": grupo, "affiliate_url": None, "price_original": None,
              "discount": None, "commission": None, "sales": None, "url": None, **it,
              "status": "PRONTO" if it.get("affiliate_url") else "PENDENTE"} for it in items])
