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
from datetime import date

import config
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
    "당신은 KB금융그룹 글로벌 인텔리전스 애널리스트다. 한 국가의 최근 기사 묶음을 바탕으로 "
    "거점 경영진용 브리핑을 작성한다. 반드시 JSON만 출력:\n"
    '{"summary": "3~4문장 종합", '
    '"issues": ["핵심 이슈 2~4개"], '
    '"outlook": "향후 전망 1~2문장", '
    '"keywords": ["키워드 3~6개"], '
    '"key_stat": "대표 수치 1개(예: BI-Rate 5.75%)"}'
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


def ensure_table(conn) -> None:
    conn.execute(_CREATE)
    # 구 DB 호환: 일일 브리핑 영어본 컬럼 보강
    cols = [r[1] for r in conn.execute("PRAGMA table_info(country_briefings)")]
    if "summary_en" not in cols:
        conn.execute("ALTER TABLE country_briefings ADD COLUMN summary_en TEXT")
    conn.commit()


def _target_countries(conn) -> list[str]:
    return [
        r["cc"] for r in conn.execute(
            """SELECT DISTINCT m.primary_country_code AS cc
               FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
               WHERE a.ai_score IS NOT NULL"""
        )
    ]


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
    days: 지정 시 최근 N일 게시분만(데이터 기준 앵커). daily 는 미지정 시 1.
    use_batch: None=배치(50% 할인, 기본) / False=동기.
    """
    ensure_table(conn)
    daily = (briefing_type == "daily")
    if daily and days is None:
        days = 1  # 전일+당일
    provider = provider or get_provider("smart", use_batch=use_batch)
    bdate = briefing_date or date.today().isoformat()
    ccs = countries or _target_countries(conn)
    system = _SYSTEM_DAILY if daily else _SYSTEM

    dc, dp = "", []
    if days:
        anchor = "(SELECT MAX(substr(published_at,1,10)) FROM articles_raw)"
        dc = f" AND substr(a.published_at,1,10) >= date({anchor}, ?)"
        dp = [f"-{int(days)} days"]

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
        requests.append((cc, system, user, 900))
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
            issues = outlook = keywords = key_stat = ""
        else:
            summary    = str(data.get("summary", ""))[:2000]
            summary_en = ""
            issues   = json.dumps(data.get("issues", []), ensure_ascii=False)
            outlook  = str(data.get("outlook", ""))[:1000]
            keywords = json.dumps(data.get("keywords", []), ensure_ascii=False)
            key_stat = str(data.get("key_stat", ""))[:200]

        cur.execute(
            """
            INSERT INTO country_briefings
                (cc, briefing_date, briefing_type, generated_at, summary, summary_en, issues,
                 outlook, keywords, key_stat, model, article_count, source_articles)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cc, briefing_date, briefing_type) DO UPDATE SET
                generated_at    = CURRENT_TIMESTAMP,
                summary         = excluded.summary,
                summary_en      = excluded.summary_en,
                issues          = excluded.issues,
                outlook         = excluded.outlook,
                keywords        = excluded.keywords,
                key_stat        = excluded.key_stat,
                model           = excluded.model,
                article_count   = excluded.article_count,
                source_articles = excluded.source_articles
            """,
            (
                cc, bdate, briefing_type, summary, summary_en, issues,
                outlook, keywords, key_stat, provider.model_id,
                len(arts), json.dumps([a["link"] for a in arts], ensure_ascii=False),
            ),
        )
        stats["written"] += 1
    conn.commit()

    log.info(
        "브리핑 완료 — 국가=%d  작성=%d  (%s / %s)",
        stats["countries"], stats["written"], bdate, briefing_type,
    )
    return stats
