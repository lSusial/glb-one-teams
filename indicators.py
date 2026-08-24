"""
국가별 거시지표(환율·주가지수) 수집 (indicators.py)

무료 소스만 사용, API 키 불필요:
  - 환율: open.er-api.com/v6/latest/USD — 1회 호출로 전체 통화(rates[통화코드] = 1 USD 당 현지통화)
  - 주가지수: yfinance(야후) — 종목별 조회. 실패해도 해당 카드만 스킵하고 파이프라인은 계속.

일자별 스냅샷을 indicators 테이블에 저장(국가별 config.INDICATOR_MAP 매핑 기준).
  - 환율 등락: DB에 저장된 직전 스냅샷과 비교(최초 실행은 prev_value/change가 없음 → null).
  - 지수 등락: yfinance가 주는 전일 종가를 그대로 사용(별도 DB 조회 불필요).

MMK 주의: open.er-api의 MMK는 미얀마 중앙은행 공식환율이며 시장환율과 괴리가 크다 —
해당 행에만 note를 남긴다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import requests

import config
import db

log = logging.getLogger("indicators")

_FX_API = "https://open.er-api.com/v6/latest/USD"
_FX_TIMEOUT = 15

_CREATE = """
CREATE TABLE IF NOT EXISTS indicators (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    country     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    label       TEXT,
    value       REAL,
    prev_value  REAL,
    change      REAL,
    change_pct  REAL,
    note        TEXT,
    fetched_at  TEXT,
    UNIQUE(date, country, kind, symbol)
)
"""


def ensure_table(conn) -> None:
    conn.execute(_CREATE)
    conn.commit()


def _fetch_fx_rates() -> dict:
    """open.er-api.com 1회 호출로 전체 통화 환율을 가져온다. 실패 시 빈 dict."""
    try:
        resp = requests.get(_FX_API, timeout=_FX_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        log.warning("환율 API 호출 실패: %r", e)
        return {}
    if data.get("result") != "success":
        log.warning("환율 API 응답 이상: result=%s", data.get("result"))
        return {}
    return data.get("rates", {})


def _prev_fx_value(conn, cc: str, symbol: str, before_date: str) -> float | None:
    row = conn.execute(
        """SELECT value FROM indicators
           WHERE country = ? AND kind = 'fx' AND symbol = ? AND date < ?
           ORDER BY date DESC LIMIT 1""",
        (cc, symbol, before_date),
    ).fetchone()
    return row["value"] if row else None


def fetch_indicators(conn, snapshot_date: str | None = None) -> dict:
    """config.INDICATOR_MAP 기준으로 환율·지수를 수집해 indicators 테이블에 upsert."""
    ensure_table(conn)
    snap_date = snapshot_date or date.today().isoformat()
    fetched_at = datetime.now(timezone.utc).isoformat()
    stats = dict(fx=0, index=0, index_skipped=0)
    cur = conn.cursor()

    fx_rates = _fetch_fx_rates()

    for cc, spec in config.INDICATOR_MAP.items():
        currency = spec.get("fx")
        if not currency:
            continue
        value = fx_rates.get(currency)
        if value is None:
            log.warning("환율 없음(스킵): %s(%s)", cc, currency)
            continue
        prev_value = _prev_fx_value(conn, cc, currency, snap_date)
        change = (value - prev_value) if prev_value is not None else None
        change_pct = (change / prev_value * 100) if prev_value else None
        note = "미얀마 중앙은행 공식환율 — 시장환율과 괴리 있음" if currency == "MMK" else None
        cur.execute(
            """
            INSERT INTO indicators
                (date, country, kind, symbol, label, value, prev_value, change, change_pct, note, fetched_at)
            VALUES (?, ?, 'fx', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, country, kind, symbol) DO UPDATE SET
                value = excluded.value, prev_value = excluded.prev_value,
                change = excluded.change, change_pct = excluded.change_pct,
                note = excluded.note, fetched_at = excluded.fetched_at
            """,
            (snap_date, cc, currency, currency, value, prev_value, change, change_pct, note, fetched_at),
        )
        stats["fx"] += 1

    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance 미설치 — 주가지수 스킵(환율만 수집됨)")
        yf = None

    if yf is not None:
        for cc, spec in config.INDICATOR_MAP.items():
            for idx in spec.get("indices", []):
                symbol, label = idx["symbol"], idx["label"]
                try:
                    hist = yf.Ticker(symbol).history(period="5d")
                    if hist.empty:
                        raise ValueError("empty history")
                    value = float(hist["Close"].iloc[-1])
                    prev_value = float(hist["Close"].iloc[-2]) if len(hist) > 1 else None
                except Exception as e:  # noqa: BLE001 — 야후 실패는 조용히 스킵, 파이프라인 계속
                    log.warning("지수 조회 실패(스킵): %s %s — %r", cc, symbol, e)
                    stats["index_skipped"] += 1
                    continue
                change = (value - prev_value) if prev_value is not None else None
                change_pct = (change / prev_value * 100) if prev_value else None
                cur.execute(
                    """
                    INSERT INTO indicators
                        (date, country, kind, symbol, label, value, prev_value, change, change_pct, note, fetched_at)
                    VALUES (?, ?, 'index', ?, ?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(date, country, kind, symbol) DO UPDATE SET
                        value = excluded.value, prev_value = excluded.prev_value,
                        change = excluded.change, change_pct = excluded.change_pct,
                        fetched_at = excluded.fetched_at
                    """,
                    (snap_date, cc, symbol, label, value, prev_value, change, change_pct, fetched_at),
                )
                stats["index"] += 1

    conn.commit()
    log.info("지표 수집 완료 — 환율=%d  지수=%d(스킵=%d)", stats["fx"], stats["index"], stats["index_skipped"])
    return stats
