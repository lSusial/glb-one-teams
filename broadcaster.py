"""
Telegram 브로드캐스트 발송 모듈.

  python main.py broadcast                      # 최신 daily 브리핑 전 거점 발송
  python main.py broadcast --date 2026-08-06    # 특정 날짜
  python main.py broadcast --cc JP              # 특정 거점만
  python main.py broadcast --dry-run            # 실제 발송 없이 메시지 출력
  python main.py broadcast --type weekly        # 주간 브리핑

환경변수 (.env):
  TELEGRAM_BOT_TOKEN   — BotFather에서 발급한 봇 토큰
  TELEGRAM_CHANNEL_ID  — 채널/그룹 chat_id (예: @kb_global_daily 또는 -100xxxxxxxxxx)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

log = logging.getLogger(__name__)

CC_META: dict[str, tuple[str, str]] = {
    "GB": ("🇬🇧", "영국"),
    "US": ("🇺🇸", "미국"),
    "HK": ("🇭🇰", "홍콩"),
    "CN": ("🇨🇳", "중국"),
    "JP": ("🇯🇵", "일본"),
    "SG": ("🇸🇬", "싱가포르"),
    "IN": ("🇮🇳", "인도"),
    "VN": ("🇻🇳", "베트남"),
    "MM": ("🇲🇲", "미얀마"),
    "ID": ("🇮🇩", "인도네시아"),
    "KH": ("🇰🇭", "캄보디아"),
}

CC_ORDER = ["GB", "US", "HK", "CN", "JP", "SG", "IN", "VN", "MM", "ID", "KH"]


# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------

def _tg_api(token: str, method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Telegram API {method} HTTP {e.code}: {body}") from e


# ---------------------------------------------------------------------------
# 메시지 포맷터
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    """Telegram HTML 최소 이스케이프."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _load_json(raw) -> list | dict:
    if not raw:
        return []
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return []


def format_message(row: sqlite3.Row, briefing_date: str) -> str:
    """country_briefings 행 → Telegram HTML 메시지."""
    cc = row["cc"]
    flag, name = CC_META.get(cc, ("🌐", cc))

    lines: list[str] = [f"{flag} <b>{name}</b>  <i>{briefing_date}</i>", ""]

    summary = (row["summary"] or "").strip()
    if summary:
        lines += [_esc(summary), ""]

    issues = _load_json(row["issues"])
    if issues:
        lines.append("<b>핵심 이슈</b>")
        for i, iss in enumerate(issues[:5], 1):
            cat = iss.get("category", "")
            title = _esc(iss.get("title", ""))
            detail = _esc(iss.get("detail", ""))
            prefix = f"[{cat}] " if cat else ""
            lines.append(f"{i}. {prefix}<b>{title}</b>")
            if detail:
                lines.append(f"   {detail}")
        lines.append("")

    keywords = _load_json(row["keywords"])
    if keywords:
        kw_str = " ".join(f"#{kw.replace(' ', '_')}" for kw in keywords[:8])
        lines += [kw_str, ""]

    articles = _load_json(row["source_articles"])
    if articles:
        lines.append("<b>주요 기사</b>")
        for art in articles[:4]:
            if isinstance(art, dict):
                title = _esc((art.get("title") or "")[:60])
                link = art.get("link", "")
                source = _esc(art.get("source") or "")
                if link:
                    lines.append(f'• <a href="{link}">{title}</a> <i>({source})</i>')
                else:
                    lines.append(f"• {title} ({source})")
            else:
                # URL 문자열만 있는 경우
                lines.append(f"• {art}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 테이블 마이그레이션 (멱등)
# ---------------------------------------------------------------------------

def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS broadcast_targets (
            cc        TEXT NOT NULL,
            channel   TEXT NOT NULL DEFAULT 'telegram',
            target_id TEXT NOT NULL,
            language  TEXT NOT NULL DEFAULT 'ko',
            enabled   INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (cc, channel)
        );
        CREATE TABLE IF NOT EXISTS broadcast_log (
            cc            TEXT NOT NULL,
            briefing_date TEXT NOT NULL,
            briefing_type TEXT NOT NULL DEFAULT 'daily',
            channel       TEXT NOT NULL DEFAULT 'telegram',
            status        TEXT NOT NULL,
            sent_at       TEXT,
            error         TEXT,
            UNIQUE (cc, briefing_date, briefing_type, channel)
        );
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# 메인 실행
# ---------------------------------------------------------------------------

def run_broadcast(
    conn: sqlite3.Connection,
    *,
    briefing_date: str | None = None,
    briefing_type: str = "daily",
    cc_filter: str | None = None,
    dry_run: bool = False,
) -> dict:
    """브리핑 → Telegram 채널 발송.

    Returns: {sent, skipped, failed, date}
    """
    _ensure_tables(conn)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    channel_id = os.environ.get("TELEGRAM_CHANNEL_ID", "")

    if not dry_run and (not token or not channel_id):
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHANNEL_ID 환경변수 없음. .env 확인."
        )

    # 날짜 미지정 시 최신 브리핑 날짜 사용
    if briefing_date is None:
        row = conn.execute(
            "SELECT briefing_date FROM country_briefings "
            "WHERE briefing_type=? ORDER BY briefing_date DESC LIMIT 1",
            (briefing_type,),
        ).fetchone()
        if not row:
            log.warning("브리핑 데이터 없음 (type=%s)", briefing_type)
            return {"sent": 0, "skipped": 0, "failed": 0, "date": None}
        briefing_date = row["briefing_date"]

    log.info("broadcast 시작 — date=%s type=%s dry_run=%s", briefing_date, briefing_type, dry_run)

    rows: dict[str, sqlite3.Row] = {
        r["cc"]: r
        for r in conn.execute(
            "SELECT * FROM country_briefings WHERE briefing_date=? AND briefing_type=?",
            (briefing_date, briefing_type),
        ).fetchall()
    }

    targets = [cc for cc in CC_ORDER if cc in rows]
    if cc_filter:
        targets = [cc for cc in targets if cc == cc_filter.upper()]

    sent = skipped = failed = 0
    now = datetime.now(timezone.utc).isoformat()

    for cc in targets:
        # 멱등: 이미 발송한 건 스킵
        already = conn.execute(
            "SELECT 1 FROM broadcast_log "
            "WHERE cc=? AND briefing_date=? AND briefing_type=? AND channel='telegram' AND status='sent'",
            (cc, briefing_date, briefing_type),
        ).fetchone()
        if already:
            log.info("skip (already sent) %s %s", cc, briefing_date)
            skipped += 1
            continue

        text = format_message(rows[cc], briefing_date)

        if dry_run:
            print(f"\n{'─'*60}")
            print(text)
            sent += 1
            continue

        try:
            _tg_api(token, "sendMessage", {
                "chat_id": channel_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            conn.execute(
                "INSERT OR REPLACE INTO broadcast_log"
                "(cc,briefing_date,briefing_type,channel,status,sent_at)"
                " VALUES(?,?,?,'telegram','sent',?)",
                (cc, briefing_date, briefing_type, now),
            )
            conn.commit()
            log.info("sent %s %s", cc, briefing_date)
            sent += 1
        except Exception as exc:
            log.error("failed %s: %s", cc, exc)
            conn.execute(
                "INSERT OR REPLACE INTO broadcast_log"
                "(cc,briefing_date,briefing_type,channel,status,sent_at,error)"
                " VALUES(?,?,?,'telegram','failed',?,?)",
                (cc, briefing_date, briefing_type, now, str(exc)),
            )
            conn.commit()
            failed += 1

    return {"sent": sent, "skipped": skipped, "failed": failed, "date": briefing_date}
