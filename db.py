"""
glb-one-teams 공용 DB 헬퍼.

기존 main.py의 `_open()` 과 각 모듈의 `ensure_*_columns()` ALTER TABLE 패턴을
한곳으로 모아 중복을 줄인다(리팩토링). 동작(PRAGMA, row_factory)은 기존과 동일.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import config

log = logging.getLogger("db")


def open_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    """표준 PRAGMA가 적용된 SQLite 연결을 연다(기존 main._open 과 동일 설정)."""
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -8000")
    return conn


def table_columns(conn: sqlite3.Connection, table: str = "articles_raw") -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    migrations: list[tuple[str, str]],
) -> list[str]:
    """없는 컬럼만 ADD COLUMN 으로 추가하는 멱등 마이그레이션.

    migrations: (컬럼명, "ALTER TABLE ... ADD COLUMN ...") 튜플 리스트.
    추가된 컬럼명 리스트를 반환한다. 기존 ensure_*_columns 패턴과 호환.
    """
    existing = table_columns(conn, table)
    added: list[str] = []
    for col, sql in migrations:
        if col not in existing:
            conn.execute(sql)
            added.append(col)
    if added:
        conn.commit()
        log.info("마이그레이션 완료: %s 컬럼 추가 %s", table, added)
    return added
