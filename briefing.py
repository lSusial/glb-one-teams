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


def ensure_table(conn) -> None:
    conn.execute(_CREATE)
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
) -> dict:
    """국가별로 ai_score 상위 기사를 모아 브리핑 생성·upsert."""
    ensure_table(conn)
    provider = provider or get_provider("smart")
    bdate = briefing_date or date.today().isoformat()
    ccs = countries or _target_countries(conn)

    stats = dict(countries=0, written=0)
    cur = conn.cursor()
    for cc in ccs:
        arts = conn.execute(
            """
            SELECT a.title, a.summary_ko, a.ai_score, a.link
            FROM articles_raw a
            JOIN media_sources m ON m.source_id = a.source_id
            WHERE m.primary_country_code = ?
              AND a.ai_score IS NOT NULL
              AND a.duplicate_of IS NULL
            ORDER BY a.ai_score DESC
            LIMIT ?
            """,
            (cc, config.BRIEFING_MAX_ARTICLES),
        ).fetchall()
        stats["countries"] += 1
        if not arts:
            continue

        bullets = "\n".join(
            f"- ({a['ai_score']}) {a['title']} :: {(a['summary_ko'] or '')[:160]}"
            for a in arts
        )
        user = f"국가: {cc} ({kb_network.context_for(cc)})\n기사 목록:\n{bullets}"
        data = provider.complete_json(_SYSTEM, user, max_tokens=900)
        if not data:
            continue

        cur.execute(
            """
            INSERT INTO country_briefings
                (cc, briefing_date, briefing_type, generated_at, summary, issues,
                 outlook, keywords, key_stat, model, article_count, source_articles)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cc, briefing_date, briefing_type) DO UPDATE SET
                generated_at    = CURRENT_TIMESTAMP,
                summary         = excluded.summary,
                issues          = excluded.issues,
                outlook         = excluded.outlook,
                keywords        = excluded.keywords,
                key_stat        = excluded.key_stat,
                model           = excluded.model,
                article_count   = excluded.article_count,
                source_articles = excluded.source_articles
            """,
            (
                cc, bdate, briefing_type,
                str(data.get("summary", ""))[:2000],
                json.dumps(data.get("issues", []), ensure_ascii=False),
                str(data.get("outlook", ""))[:1000],
                json.dumps(data.get("keywords", []), ensure_ascii=False),
                str(data.get("key_stat", ""))[:200],
                provider.model_id,
                len(arts),
                json.dumps([a["link"] for a in arts], ensure_ascii=False),
            ),
        )
        conn.commit()
        stats["written"] += 1

    log.info(
        "브리핑 완료 — 국가=%d  작성=%d  (%s / %s)",
        stats["countries"], stats["written"], bdate, briefing_type,
    )
    return stats
