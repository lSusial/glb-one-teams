"""
국가별 브리핑 생성 (briefing.py) → country_briefings

분석 완료(ai_score 보유) 기사를 국가별로 묶어 LLM이 거점 경영진용 브리핑을 만든다.
출력 필드: summary, issues, outlook, keywords, key_stat (+ article_count, source_articles)

* 분석 모델(role='smart') 사용. upsert 키: (cc, briefing_date, briefing_type)
* UI '글로벌 동향'의 빅넘버(key_stat)·거점 브리핑 근거로 연결.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import config
import db
import kb_network
from llm_provider import LLMProvider, get_provider

log = logging.getLogger("briefing")

# schema.sql 의 country_briefings 와 동일 — 구 DB 호환을 위해 멱등 생성
_CREATE = """
CREATE TABLE IF NOT EXISTS country_briefings (
    briefing_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    cc              TEXT NOT NULL,
    briefing_date   TEXT NOT NULL,
    briefing_type   TEXT NOT NULL DEFAULT 'weekly',
    generated_at    TEXT,
    summary         TEXT,
    issues          TEXT,
    outlook         TEXT,
    keywords        TEXT,
    key_stat        TEXT,
    model           TEXT,
    article_count   INTEGER,
    source_articles TEXT,
    UNIQUE(cc, briefing_date, briefing_type)
)
"""

_SYSTEM = (
    "You are a global intelligence analyst at KB Financial Group. Given one country's news "
    "from the past week, write a WEEKLY briefing for branch executives (macro / markets / "
    "banking / regulation / risk relevant to KB's overseas operations). Base it strictly on "
    "the provided items; no speculation. Output ONLY JSON with BOTH Korean and English:\n"
    '{"summary_ko": "5~6문장 주간 종합", "summary_en": "5-6 sentence weekly summary", '
    '"issues_ko": ["핵심 이슈 3~4개"], "issues_en": ["3-4 key issues"], '
    '"outlook_ko": "향후 전망 1~2문장", "outlook_en": "1-2 sentence outlook", '
    '"keywords": ["키워드 4~6개"], "key_stat": "대표 수치 1개(예: BI-Rate 5.75%)"}'
)

# 일일(daily) 브리핑 — 현지언론 화면 상단용. 전일+당일 기사를 4~5문장으로 종합(한/영 동시).
_SYSTEM_DAILY = (
    "You are a global intelligence analyst at KB Financial Group. "
    "Given one country's news items from the last day or two, write a concise briefing "
    "for branch executives — what happened and why it matters to KB's overseas operations "
    "(macro / financial markets / banking / regulation / risk). "
    "Write 4-5 sentences, in BOTH Korean and English. Base it strictly on the provided items; "
    "no speculation. Output ONLY JSON:\n"
    '{"summary_ko": "4~5문장 한국어 브리핑", "summary_en": "4-5 sentence English briefing"}'
)

# 오늘의 글로벌 핵심 3줄 — 전 거점 횡단 종합(하루 1회 1콜). 겹치는 주제는 하나로 묶는다.
_SYSTEM_HIGHLIGHTS = (
    "You are a global intelligence analyst at KB Financial Group. Given today's top-scored "
    "news across ALL KB overseas hubs, synthesize EXACTLY 3 headline items a KB bank executive "
    "must read today — merge overlapping stories into a single item where relevant, and pick "
    "the 3 most important distinct developments overall (not one per hub). "
    "Base strictly on the provided items; no speculation. Output ONLY JSON:\n"
    '{"highlights": [{'
    '"category": "금리|FX|규제|시장|디지털|지정학 중 하나", '
    '"headline_ko": "건조한 신문 헤드라인 1줄(한국어, 설명체 금지)", '
    '"headline_en": "one-line dry newspaper headline (English)", '
    '"impact_ko": "어느 KB 거점/자회사에 어떤 영향인지 1줄(한국어)", '
    '"impact_en": "one-line note on which KB hub/subsidiary this affects and how (English)", '
    '"country_codes": ["관련 거점 코드(예: GB, US)"]'
    '}]}\n'
    "The \"highlights\" array must have exactly 3 items, ordered by importance."
)


def ensure_table(conn) -> None:
    conn.execute(_CREATE)
    # 구 DB 호환: 일일 브리핑 영어본 컬럼 보강
    cols = [r[1] for r in conn.execute("PRAGMA table_info(country_briefings)")]
    for col in ("summary_en", "issues_en", "outlook_en", "week_start", "week_end"):
        if col not in cols:
            conn.execute(f"ALTER TABLE country_briefings ADD COLUMN {col} TEXT")
    conn.commit()


def _target_countries(conn) -> list[str]:
    return [
        r["cc"] for r in conn.execute(
            """SELECT DISTINCT m.primary_country_code AS cc
               FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
               WHERE a.ai_score IS NOT NULL"""
        )
    ]


def _last_completed_week(today: date | None = None) -> tuple[str, str]:
    """직전에 완료된 월~일 주(월요일 시작) 범위를 (시작일, 종료일) ISO 문자열로 반환.

    스케줄이 월요일에 못 돌고 늦게(예: 수요일에) 실행돼도 같은 주를 가리키도록
    "오늘이 속한 주의 월요일"을 기준으로 그 직전 주를 계산한다(고정 달력 주 —
    실행 시각 기준 상대창(days_clause_*)과 달리 실행이 늦어져도 밀리지 않음).
    """
    today = today or date.today()
    this_monday = today - timedelta(days=today.weekday())  # weekday(): 월=0
    week_start = this_monday - timedelta(days=7)
    week_end = this_monday - timedelta(days=1)
    return week_start.isoformat(), week_end.isoformat()


def run_briefing(
    conn,
    provider: LLMProvider | None = None,
    briefing_date: str | None = None,
    briefing_type: str = "weekly",
    countries: list[str] | None = None,
    days: int | None = None,
    use_batch: bool | None = None,
) -> dict:
    """국가별로 ai_score 상위 기사를 모아 브리핑 생성·upsert.

    briefing_type='daily': 현지언론 화면 상단용. 전일+당일(days 기본 1) 기사를
      4~5문장 한/영 동시로 종합해 summary·summary_en 에 저장.
    briefing_type='weekly': days 미지정 시 "직전에 완료된 월~일 주"를 고정 사용
      (매주 월요일 실행 전제 — 스케줄이 늦게 돌아도 같은 주를 요약).
      days를 명시하면 그 대신 실행 시점 기준 최근 N일(상대창)을 쓴다.
    use_batch: None=배치(50% 할인, 기본) / False=동기.
    """
    ensure_table(conn)
    daily = (briefing_type == "daily")
    week_start = week_end = None
    if daily:
        if days is None:
            days = 1  # 전일+당일
        dc, dp = db.days_clause_data(days)
    elif days is not None:
        dc, dp = db.days_clause_data(days)
        week_end = (date.today() - timedelta(days=1)).isoformat()
        week_start = (date.today() - timedelta(days=days)).isoformat()
    else:
        week_start, week_end = _last_completed_week()
        dc, dp = db.date_range_clause(week_start, week_end)

    provider = provider or get_provider("smart", use_batch=use_batch)
    bdate = briefing_date or date.today().isoformat()
    ccs = countries or _target_countries(conn)
    system = _SYSTEM_DAILY if daily else _SYSTEM

    # 국가별 기사 수집 → 요청 일괄 구성(배치 제출) → custom_id=cc 로 결과 수거
    stats = dict(countries=0, written=0)
    requests, meta = [], {}
    for cc in ccs:
        arts = conn.execute(
            f"""
            SELECT a.title, a.summary_ko, a.summary_en, a.ai_score, a.link
            FROM articles_raw a
            JOIN media_sources m ON m.source_id = a.source_id
            WHERE m.primary_country_code = ?
              AND a.ai_score IS NOT NULL
              AND a.duplicate_of IS NULL{dc}
            ORDER BY a.ai_score DESC
            LIMIT ?
            """,
            (cc, *dp, config.BRIEFING_MAX_ARTICLES),
        ).fetchall()
        stats["countries"] += 1
        if not arts:
            continue
        bullets = "\n".join(
            f"- ({a['ai_score']}) {a['title']} :: {((a['summary_ko'] or a['summary_en']) or '')[:160]}"
            for a in arts
        )
        user = f"국가: {cc} ({kb_network.context_for(cc)})\n기사 목록:\n{bullets}"
        # weekly는 한/영 요약+이슈 3~4개+전망+키워드까지 daily보다 필드가 훨씬 많아
        # 900으로는 잘려서 JSON 파싱이 깨진다(생성 도중 max_tokens 도달) — 여유를 둔다.
        requests.append((cc, system, user, 900 if daily else 2200))
        meta[cc] = arts

    results = provider.complete_json_batch(requests) if requests else {}

    cur = conn.cursor()
    for cc, arts in meta.items():
        data = results.get(cc) or {}
        if not data:
            continue
        if daily:
            summary    = str(data.get("summary_ko") or data.get("summary") or "")[:2000]
            summary_en = str(data.get("summary_en") or "")[:2000]
            issues = issues_en = outlook = outlook_en = keywords = key_stat = ""
        else:  # weekly — 이중언어
            summary    = str(data.get("summary_ko") or data.get("summary") or "")[:2000]
            summary_en = str(data.get("summary_en") or "")[:2000]
            issues     = json.dumps(data.get("issues_ko") or data.get("issues") or [], ensure_ascii=False)
            issues_en  = json.dumps(data.get("issues_en") or [], ensure_ascii=False)
            outlook    = str(data.get("outlook_ko") or data.get("outlook") or "")[:1000]
            outlook_en = str(data.get("outlook_en") or "")[:1000]
            keywords   = json.dumps(data.get("keywords", []), ensure_ascii=False)
            key_stat   = str(data.get("key_stat", ""))[:200]

        cur.execute(
            """
            INSERT INTO country_briefings
                (cc, briefing_date, briefing_type, generated_at, summary, summary_en,
                 issues, issues_en, outlook, outlook_en, keywords, key_stat,
                 model, article_count, source_articles, week_start, week_end)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cc, briefing_date, briefing_type) DO UPDATE SET
                generated_at    = CURRENT_TIMESTAMP,
                summary         = excluded.summary,
                summary_en      = excluded.summary_en,
                issues          = excluded.issues,
                issues_en       = excluded.issues_en,
                outlook         = excluded.outlook,
                outlook_en      = excluded.outlook_en,
                keywords        = excluded.keywords,
                key_stat        = excluded.key_stat,
                model           = excluded.model,
                article_count   = excluded.article_count,
                source_articles = excluded.source_articles,
                week_start      = excluded.week_start,
                week_end        = excluded.week_end
            """,
            (
                cc, bdate, briefing_type, summary, summary_en,
                issues, issues_en, outlook, outlook_en, keywords, key_stat,
                provider.model_id, len(arts),
                json.dumps([a["link"] for a in arts], ensure_ascii=False),
                week_start, week_end,
            ),
        )
        stats["written"] += 1
    conn.commit()

    log.info(
        "브리핑 완료 — 국가=%d  작성=%d  (%s / %s)",
        stats["countries"], stats["written"], bdate, briefing_type,
    )
    return stats


_CREATE_HIGHLIGHTS = """
CREATE TABLE IF NOT EXISTS daily_highlights (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,
    items        TEXT NOT NULL,
    model        TEXT,
    generated_at TEXT,
    UNIQUE(date)
)
"""


def ensure_highlights_table(conn) -> None:
    conn.execute(_CREATE_HIGHLIGHTS)
    conn.commit()


def generate_daily_highlights(
    conn,
    provider: LLMProvider | None = None,
    target_date: str | None = None,
) -> dict:
    """당일 ACTIVE 상위 기사를 전 거점 횡단으로 종합해 '오늘의 글로벌 핵심 3줄' 생성.

    LLM은 하루 1회 1콜만 사용(배치 아님 — 요청 1건은 배치 이득이 없음).
    기사가 없거나 LLM이 0개를 반환하면 아무것도 저장하지 않는다(화면은 블록을 숨김).
    """
    ensure_highlights_table(conn)
    tdate = target_date or date.today().isoformat()
    dc, dp = db.days_clause_data(1)

    rows = conn.execute(
        f"""
        SELECT a.title, a.summary_ko, a.summary_en, a.kb_implication, a.kb_implication_en,
               a.topics, a.ai_score, m.primary_country_code AS cc
        FROM articles_raw a
        JOIN media_sources m ON m.source_id = a.source_id
        WHERE a.ai_score >= ? AND a.duplicate_of IS NULL{dc}
        ORDER BY a.ai_score DESC
        LIMIT ?
        """,
        (config.AI_SCORE_ACTIVE_THRESHOLD, *dp, config.HIGHLIGHTS_MAX_ARTICLES),
    ).fetchall()

    if not rows:
        log.info("글로벌 핵심 3줄 — 대상 기사 없음, 스킵")
        return {"written": 0}

    bullets = "\n".join(
        f"- [{r['cc']}] ({r['ai_score']}) {r['title']} :: "
        f"{((r['summary_ko'] or r['summary_en']) or '')[:160]} "
        f"| KB 시사점: {((r['kb_implication'] or r['kb_implication_en']) or '')[:120]}"
        for r in rows
    )
    user = f"KB 거점 네트워크: {kb_network.all_context()}\n\n오늘의 상위 기사:\n{bullets}"

    provider = provider or get_provider("smart", use_batch=False)
    data = provider.complete_json(_SYSTEM_HIGHLIGHTS, user, max_tokens=1200)
    items = (data.get("highlights") or [])[:3]

    if not items:
        log.info("글로벌 핵심 3줄 — LLM이 0개 반환, 스킵")
        return {"written": 0}

    conn.execute(
        """
        INSERT INTO daily_highlights (date, items, model, generated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(date) DO UPDATE SET
            items = excluded.items, model = excluded.model, generated_at = CURRENT_TIMESTAMP
        """,
        (tdate, json.dumps(items, ensure_ascii=False), provider.model_id),
    )
    conn.commit()

    log.info("글로벌 핵심 3줄 완료 — 항목=%d  (%s)", len(items), tdate)
    return {"written": len(items)}
