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


def _prev_value(conn, cc: str, kind: str, symbol: str, before_date: str) -> float | None:
    row = conn.execute(
        """SELECT value FROM indicators
           WHERE country = ? AND kind = ? AND symbol = ? AND date < ?
           ORDER BY date DESC LIMIT 1""",
        (cc, kind, symbol, before_date),
    ).fetchone()
    return row["value"] if row else None


def _upsert(cur, snap_date, cc, kind, symbol, label, value, prev_value, note, fetched_at):
    change = (value - prev_value) if prev_value is not None else None
    change_pct = (change / prev_value * 100) if prev_value else None
    cur.execute(
        """
        INSERT INTO indicators
            (date, country, kind, symbol, label, value, prev_value, change, change_pct, note, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, country, kind, symbol) DO UPDATE SET
            value = excluded.value, prev_value = excluded.prev_value,
            change = excluded.change, change_pct = excluded.change_pct,
            note = excluded.note, fetched_at = excluded.fetched_at
        """,
        (snap_date, cc, kind, symbol, label, value, prev_value, change, change_pct, note, fetched_at),
    )


def _write_policy_rates(conn, snap_date: str, fetched_at: str) -> int:
    """config.POLICY_RATES 표를 그대로 스냅샷에 기록 — API 호출 없음, 새 수집원 없음.
    표의 값이 바뀌면(수동 갱신) 다음날 export에 등락으로 자동 반영된다."""
    cur = conn.cursor()
    n = 0
    for cc, spec in config.POLICY_RATES.items():
        prev_value = _prev_value(conn, cc, "policy_rate", cc, snap_date)
        note = f"기준일 {spec['as_of']}"
        _upsert(cur, snap_date, cc, "policy_rate", cc, spec["label"], spec["value"], prev_value, note, fetched_at)
        n += 1
    return n


def fetch_indicators(conn, snapshot_date: str | None = None) -> dict:
    """config.INDICATOR_MAP·POLICY_RATES·BOND10Y_MAP 기준으로 환율·지수·정책금리·
    국채금리를 수집해 indicators 테이블에 upsert."""
    ensure_table(conn)
    snap_date = snapshot_date or date.today().isoformat()
    fetched_at = datetime.now(timezone.utc).isoformat()
    stats = dict(fx=0, index=0, index_skipped=0, policy_rate=0, bond10y=0, bond10y_skipped=0)
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
        prev_value = _prev_value(conn, cc, "fx", currency, snap_date)
        note = "미얀마 중앙은행 공식환율 — 시장환율과 괴리 있음" if currency == "MMK" else None
        _upsert(cur, snap_date, cc, "fx", currency, currency, value, prev_value, note, fetched_at)
        stats["fx"] += 1

    stats["policy_rate"] = _write_policy_rates(conn, snap_date, fetched_at)

    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance 미설치 — 주가지수·국채금리 스킵(환율·정책금리만 수집됨)")
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
                except Exception as e:  # noqa: BLE001 — 야후 실패는 조용히 스킵, 파이프라인 계속
                    log.warning("지수 조회 실패(스킵): %s %s — %r", cc, symbol, e)
                    stats["index_skipped"] += 1
                    continue
                # prev_value는 yfinance 자체 히스토리가 아니라 DB에 저장된 전일 스냅샷 값을
                # 쓴다(fx와 동일 패턴) — 하루 안에 여러 번 재수집해도 등락 배지와 스파크라인이
                # 같은 기준점을 봐서 서로 어긋나지 않도록.
                prev_value = _prev_value(conn, cc, "index", symbol, snap_date)
                _upsert(cur, snap_date, cc, "index", symbol, label, value, prev_value, None, fetched_at)
                stats["index"] += 1

        # 10년물 국채금리 — 무료·무키 커버리지 실측 결과 Yahoo Finance(yfinance)는
        # 미국(^TNX)만 제공(config.BOND10Y_MAP 참조). 값은 이미 %(연리) 단위.
        for cc, spec in config.BOND10Y_MAP.items():
            symbol, label = spec["symbol"], spec["label"]
            try:
                hist = yf.Ticker(symbol).history(period="5d")
                if hist.empty:
                    raise ValueError("empty history")
                value = float(hist["Close"].iloc[-1])
                prev_value = float(hist["Close"].iloc[-2]) if len(hist) > 1 else None
            except Exception as e:  # noqa: BLE001
                log.warning("국채금리 조회 실패(스킵): %s %s — %r", cc, symbol, e)
                stats["bond10y_skipped"] += 1
                continue
            _upsert(cur, snap_date, cc, "bond10y", symbol, label, value, prev_value, None, fetched_at)
            stats["bond10y"] += 1

    conn.commit()
    log.info(
        "지표 수집 완료 — 환율=%d  지수=%d(스킵=%d)  정책금리=%d  국채금리=%d(스킵=%d)",
        stats["fx"], stats["index"], stats["index_skipped"],
        stats["policy_rate"], stats["bond10y"], stats["bond10y_skipped"],
    )
    return stats
