from __future__ import annotations
import os
import sqlite3
from pathlib import Path
from typing import Any
import pandas as pd

try:
    import psycopg
except Exception:
    psycopg = None

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / 'data' / 'competency_dss.db'
DB_BACKEND = os.getenv('COMPETENCY_DB_BACKEND', 'sqlite').lower()
DATABASE_URL = os.getenv('COMPETENCY_DATABASE_URL', '') or os.getenv('DATABASE_URL', '')


def normalize_backend() -> str:
    if DB_BACKEND in {'postgres', 'postgresql'}:
        return 'postgres'
    return 'sqlite'


def backend_info() -> dict[str, Any]:
    b = normalize_backend()
    return {
        'backend': b,
        'configured_url': bool(DATABASE_URL) if b == 'postgres' else True,
        'sqlite_path': str(DB_PATH),
        'sqlite_exists': DB_PATH.exists(),
        'postgres_driver_installed': psycopg is not None,
    }


def get_connection():
    b = normalize_backend()
    if b == 'sqlite':
        con = sqlite3.connect(DB_PATH)
        con.execute('PRAGMA foreign_keys = ON')
        return con
    if not DATABASE_URL:
        raise RuntimeError('COMPETENCY_DATABASE_URL/DATABASE_URL belum diset untuk PostgreSQL.')
    if psycopg is None:
        raise RuntimeError('psycopg belum terpasang. Jalankan pip install psycopg[binary].')
    return psycopg.connect(DATABASE_URL)


def read_sql(sql: str, params: tuple = ()) -> pd.DataFrame:
    b = normalize_backend()
    if b == 'sqlite':
        with get_connection() as con:
            return pd.read_sql_query(sql, con, params=params)
    # psycopg uses %s placeholders; existing application SQL uses ?. Keep a
    # simple compatibility conversion for the common parameter style.
    sql_pg = sql.replace('?', '%s')
    with get_connection() as con:
        return pd.read_sql_query(sql_pg, con, params=params)


def execute(sql: str, params: tuple = (), commit: bool = True) -> None:
    b = normalize_backend()
    if b == 'sqlite':
        with get_connection() as con:
            con.execute(sql, params)
            if commit:
                con.commit()
        return
    sql_pg = sql.replace('?', '%s')
    with get_connection() as con:
        cur = con.cursor()
        cur.execute(sql_pg, params)
        if commit:
            con.commit()


def production_preflight() -> dict[str, Any]:
    info = backend_info()
    result = dict(info)
    result['ready'] = False
    result['message'] = ''
    try:
        if info['backend'] == 'sqlite':
            if not DB_PATH.exists():
                result['message'] = 'SQLite database tidak ditemukan.'
                return result
            with sqlite3.connect(DB_PATH) as con:
                con.execute('SELECT 1')
            result['ready'] = True
            result['message'] = 'SQLite siap digunakan.'
            return result
        if not info['configured_url']:
            result['message'] = 'DATABASE_URL belum diset.'
            return result
        if not info['postgres_driver_installed']:
            result['message'] = 'psycopg belum terpasang.'
            return result
        with psycopg.connect(DATABASE_URL) as con:
            with con.cursor() as cur:
                cur.execute('SELECT 1')
                cur.fetchone()
        result['ready'] = True
        result['message'] = 'PostgreSQL dapat diakses.'
        return result
    except Exception as exc:
        result['message'] = str(exc)
        return result
