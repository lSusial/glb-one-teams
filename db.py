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


def days_clause_now(days: int | None, alias: str = "a") -> tuple[str, list]:
    """최근 N일 창(SQLite 서버 시각 'now' 기준) SQL 절. days 없으면 전체(빈 절).

    수집 파이프라인(prefilter/ranker/translate/fulltext)에서 사용 — 실행 시점 기준
    최신 N일 백로그만 처리해 비용을 절감한다. alias="" 이면 컬럼에 접두사를 붙이지 않는다.
    """
    if not days:
        return "", []
    p = f"{alias}." if alias else ""
    return f" AND substr({p}published_at, 1, 10) >= date('now', ?)", [f"-{int(days)} days"]


def days_clause_data(days: int | None, alias: str = "a") -> tuple[str, list]:
    """최근 N일 창(DB 내 최신 게시일 기준) SQL 절. days 없으면 전체(빈 절).

    export/briefing에서 사용 — 수집이 지연돼도 "최근 N일"이 빈 결과가 되지 않도록
    실행 시각이 아니라 데이터 자체의 최신 게시일을 기준으로 삼는다(스냅샷 재생성에도 안전).
    """
    if not days:
        return "", []
    p = f"{alias}." if alias else ""
    anchor = "(SELECT MAX(substr(published_at, 1, 10)) FROM articles_raw)"
    return f" AND substr({p}published_at, 1, 10) >= date({anchor}, ?)", [f"-{int(days)} days"]


def date_range_clause(start: str, end: str, alias: str = "a") -> tuple[str, list]:
    """[start, end] 날짜(YYYY-MM-DD, 양끝 포함) 범위 SQL 절.

    주간 브리핑처럼 "실행 시각과 무관하게 항상 같은 달력 주(월~일)"를 고정하고
    싶을 때 사용 — days_clause_*는 실행 시점 기준 상대창이라 스케줄이 늦게
    돌면 대상 기간이 밀리는데, 이건 절대 날짜 범위라 밀리지 않는다.
    """
    p = f"{alias}." if alias else ""
    return f" AND substr({p}published_at, 1, 10) BETWEEN ? AND ?", [start, end]


def exclude_countries_clause(codes, alias: str = "m") -> tuple[str, list]:
    """codes(국가코드 목록)를 제외하는 SQL 절(NOT IN). codes가 비면 빈 절.

    KB 미진출국(config.NON_PRESENCE_CODES) 기사를 기존 국가횡단 집계
    (온도·핵심뉴스·모니터링·국가브리핑)에서 제외할 때 사용 — 미진출국은
    countries.html의 별도 통합피드에서만 노출한다(docs/design_미진출국.md).
    """
    codes = tuple(codes)
    if not codes:
        return "", []
    ph = ",".join("?" * len(codes))
    p = f"{alias}." if alias else ""
    return f" AND {p}primary_country_code NOT IN ({ph})", list(codes)


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
