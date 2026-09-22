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
import re
from datetime import date, timedelta

import config
import db
import kb_network
import ranking
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
    '"keywords_ko": ["한국어 키워드 4~6개"], "keywords_en": ["same 4-6 keywords in English"], '
    '"key_stat_ko": "대표 수치 1개, 한국어(예: BI-Rate 5.75%)", '
    '"key_stat_en": "the same key stat, in English"}'
)

# 일일(daily) 브리핑 — 현지언론 화면 상단용. 전일+당일 기사를 4~5문장으로 종합(한/영 동시).
_SYSTEM_DAILY = (
    "You are a global intelligence analyst at KB Financial Group. "
    "Given one country's news items from the last day or two, write a concise briefing "
    "for branch executives — what happened and why it matters to KB's overseas operations "
    "(macro / financial markets / banking / regulation / risk). "
    "Write 4-5 sentences, in BOTH Korean and English. Base it strictly on the provided items; "
    "no speculation. If evidence is sparse, write fewer sentences; do not pad. Output ONLY JSON:\n"
    '{"summary_ko": "4~5문장 한국어 브리핑", "summary_en": "4-5 sentence English briefing"}'
)

# 오늘의 글로벌 핵심 — 전 거점 횡단 종합(하루 1회 1콜). 겹치는 주제는 하나로 묶는다.
# 항목 수는 config.HIGHLIGHTS_COUNT (2026-08-26: 3 → 10, 전체 진출국 커버리지 확대).
def _system_highlights(count: int) -> str:
    return (
        "You are a global intelligence analyst at KB Financial Group. Given today's top-scored "
        f"news across ALL KB overseas hubs, synthesize EXACTLY {count} headline items a KB bank "
        "executive must read today — merge overlapping stories into a single item where relevant, "
        f"and pick the {count} most important distinct developments overall (not one per hub; "
        "spread across as many different hubs/topics as the material genuinely supports). "
        "Base strictly on the provided items; no speculation. Output ONLY JSON:\n"
        '{"highlights": [{'
        '"category": "금리|FX|규제|시장|디지털|지정학|인사 중 하나. '
        '중앙은행 총재·금융감독기관장·은행 CEO 등 인물의 임명·취임·사임·교체가 '
        '기사의 핵심이면 반드시 인사(그 인물의 정책 성향 언급은 이유가 되지 않는다)", '
        '"headline_ko": "건조한 신문 헤드라인 1줄(한국어, 설명체 금지)", '
        '"headline_en": "one-line dry newspaper headline (English)", '
        '"country_codes": ["관련 거점 코드(예: GB, US)"], '
        '"source_article_ids": ["이 항목의 직접 근거가 된 입력 기사의 article_id. '
        '입력에 표시된 정수 ID만 1개 이상 그대로 복사"]'
        '}]}\n'
        f'The "highlights" array must have exactly {count} items, ordered by importance. '
        'Every item must cite at least one source_article_id; never invent an ID. '
        'Copy every number and monetary unit exactly from the cited source; do not convert units.'
    )


def ensure_table(conn) -> None:
    conn.execute(_CREATE)
    # 구 DB 호환: 일일 브리핑 영어본 컬럼 보강
    cols = [r[1] for r in conn.execute("PRAGMA table_info(country_briefings)")]
    for col in ("summary_en", "issues_en", "outlook_en", "week_start", "week_end", "keywords_en", "key_stat_en"):
        if col not in cols:
            conn.execute(f"ALTER TABLE country_briefings ADD COLUMN {col} TEXT")
    conn.commit()


def _target_countries(conn) -> list[str]:
    """국가 브리핑 대상 국가 목록. KB 미진출국은 국가 브리핑을 만들지 않는다
    (docs/design_미진출국.md — 통합 피드만 제공, countries.html에서 처리)."""
    return [
        r["cc"] for r in conn.execute(
            f"""SELECT DISTINCT COALESCE(NULLIF(a.primary_country, ''), m.primary_country_code) AS cc
                FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
                WHERE a.ai_score IS NOT NULL
                  AND COALESCE(NULLIF(a.primary_country, ''), m.primary_country_code)
                      IN ({','.join('?' for _ in kb_network.KB_NETWORK)})""",
            tuple(kb_network.KB_NETWORK),
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
    """주제국가별 적격 기사를 rank_score순으로 모아 브리핑 생성·upsert.

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
    cm = ranking.cluster_sizes(conn)
    for cc in ccs:
        arts = conn.execute(
            f"""
            SELECT a.article_id, a.title, a.summary_ko, a.summary_en, a.ai_score, a.link,
                   a.published_at, a.event_type, a.korean_fi, a.personnel_move, m.tier,
                   COALESCE(NULLIF(a.primary_country, ''), m.primary_country_code) AS cc
            FROM articles_raw a
            JOIN media_sources m ON m.source_id = a.source_id
            WHERE COALESCE(NULLIF(a.primary_country, ''), m.primary_country_code) = ?
              AND a.ai_score >= ?
              AND COALESCE(NULLIF(a.summary_ko, ''), a.summary_en, '') != ''
              AND a.duplicate_of IS NULL{dc}
            ORDER BY a.published_at DESC, a.article_id ASC
            """,
            (cc, config.AI_SCORE_ACTIVE_THRESHOLD, *dp),
        ).fetchall()
        arts = ranking.order(conn, arts, cluster_map=cm)[:config.BRIEFING_MAX_ARTICLES]
        stats["countries"] += 1
        meta[cc] = arts
        if not arts:
            continue
        bullets = "\n".join(
            f"- [{a['published_at']}] ({a['ai_score']}) {a['title']} :: {((a['summary_ko'] or a['summary_en']) or '')}"
            for a in arts
        )
        user = f"국가: {cc} ({kb_network.context_for(cc)})\n기사 목록:\n{bullets}"
        # weekly는 한/영 요약+이슈 3~4개+전망+키워드까지 daily보다 필드가 훨씬 많아
        # 900으로는 잘려서 JSON 파싱이 깨진다(생성 도중 max_tokens 도달) — 여유를 둔다.
        requests.append((cc, system, user, 900 if daily else 2200))

    results = provider.complete_json_batch(requests) if requests else {}

    cur = conn.cursor()
    for cc, arts in meta.items():
        data = results.get(cc) or {}
        if not arts:
            data = {"summary_ko": "해당 기간에 브리핑 기준을 충족한 기사가 없습니다.",
                    "summary_en": "No articles met the briefing criteria for this period."}
        if not data:
            continue
        if daily:
            summary    = str(data.get("summary_ko") or data.get("summary") or "")[:2000]
            summary_en = str(data.get("summary_en") or "")[:2000]
            issues = issues_en = outlook = outlook_en = keywords = keywords_en = key_stat = key_stat_en = ""
        else:  # weekly — 이중언어
            summary    = str(data.get("summary_ko") or data.get("summary") or "")[:2000]
            summary_en = str(data.get("summary_en") or "")[:2000]
            issues     = json.dumps(data.get("issues_ko") or data.get("issues") or [], ensure_ascii=False)
            issues_en  = json.dumps(data.get("issues_en") or [], ensure_ascii=False)
            outlook    = str(data.get("outlook_ko") or data.get("outlook") or "")[:1000]
            outlook_en = str(data.get("outlook_en") or "")[:1000]
            keywords   = json.dumps(data.get("keywords_ko") or data.get("keywords") or [], ensure_ascii=False)
            keywords_en = json.dumps(data.get("keywords_en") or [], ensure_ascii=False)
            key_stat   = str(data.get("key_stat_ko") or data.get("key_stat") or "")[:200]
            key_stat_en = str(data.get("key_stat_en") or "")[:200]

        cur.execute(
            """
            INSERT INTO country_briefings
                (cc, briefing_date, briefing_type, generated_at, summary, summary_en,
                 issues, issues_en, outlook, outlook_en, keywords, keywords_en, key_stat, key_stat_en,
                 model, article_count, source_articles, week_start, week_end)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cc, briefing_date, briefing_type) DO UPDATE SET
                generated_at    = CURRENT_TIMESTAMP,
                summary         = excluded.summary,
                summary_en      = excluded.summary_en,
                issues          = excluded.issues,
                issues_en       = excluded.issues_en,
                outlook         = excluded.outlook,
                outlook_en      = excluded.outlook_en,
                keywords        = excluded.keywords,
                keywords_en     = excluded.keywords_en,
                key_stat        = excluded.key_stat,
                key_stat_en     = excluded.key_stat_en,
                model           = excluded.model,
                article_count   = excluded.article_count,
                source_articles = excluded.source_articles,
                week_start      = excluded.week_start,
                week_end        = excluded.week_end
            """,
            (
                cc, bdate, briefing_type, summary, summary_en,
                issues, issues_en, outlook, outlook_en, keywords, keywords_en, key_stat, key_stat_en,
                provider.model_id if arts else "rules:insufficient-evidence", len(arts),
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


def _validate_highlight_sources(items: list, rows, limit: int) -> list[dict]:
    """LLM 출처 ID를 입력 후보 집합에 대해 검증한다.

    잘못되거나 출처가 없는 항목은 저장하지 않는다. 화면에서 국가·제목으로 원문을
    추측해 붙이는 것보다 항목을 누락시키는 편이 출처 신뢰성 면에서 안전하다.
    """
    allowed = {int(r["article_id"]): r for r in rows}

    def usd_values(text: str) -> list[float]:
        values = []
        units = {"": 1, "m": 1e6, "million": 1e6, "bn": 1e9,
                 "billion": 1e9, "trillion": 1e12}
        for m in re.finditer(r"\$\s*([\d,.]+)\s*(trillion|billion|million|bn|m)?\b", text, re.I):
            values.append(float(m.group(1).replace(",", "")) * units[(m.group(2) or "").lower()])
        for m in re.finditer(
                r"([\d,.]+)\s*[- ]?(trillion|billion|million|bn|m)\s*[- ]?(?:USD|US dollars?)\b",
                text, re.I):
            values.append(float(m.group(1).replace(",", "")) * units[m.group(2).lower()])
        for m in re.finditer(r"([\d,.]+)\s*(조|억)\s*달러", text):
            values.append(float(m.group(1).replace(",", "")) * (1e12 if m.group(2) == "조" else 1e8))
        return values

    def row_text(r) -> str:
        def val(key):
            try:
                return r[key] or ""
            except (KeyError, TypeError, IndexError):
                return ""
        return " ".join(val(k) for k in ("title", "title_ko", "summary_ko", "summary_en"))

    out = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        valid = []
        raw_ids = item.get("source_article_ids")
        if not isinstance(raw_ids, list):
            raw_ids = []
        for value in raw_ids:
            if isinstance(value, bool):
                continue
            try:
                aid = int(value)
            except (TypeError, ValueError):
                continue
            if aid in allowed and aid not in valid:
                valid.append(aid)
        if not valid:
            log.warning("글로벌 핵심 출처 누락/무효 — 항목 제외: %s", item.get("headline_ko", "")[:80])
            continue
        generated_usd = usd_values((item.get("headline_ko") or "") + " " + (item.get("headline_en") or ""))
        source_usd = []
        for aid in valid:
            source_usd.extend(usd_values(row_text(allowed[aid])))
        if generated_usd and source_usd and any(
                not any(abs(g - s) <= max(1, abs(s)) * 0.02 for s in source_usd)
                for g in generated_usd):
            log.warning("글로벌 핵심 금액 불일치 — 항목 제외: %s", item.get("headline_ko", "")[:80])
            continue
        clean = dict(item)
        clean["source_article_ids"] = valid
        out.append(clean)
    return out


def generate_daily_highlights(
    conn,
    provider: LLMProvider | None = None,
    target_date: str | None = None,
) -> dict:
    """당일 ACTIVE 상위 기사를 전 거점 횡단으로 종합해 '오늘의 글로벌 핵심' 생성(개수는
    config.HIGHLIGHTS_COUNT).

    LLM은 하루 1회 1콜만 사용(배치 아님 — 요청 1건은 배치 이득이 없음).
    기사가 없거나 LLM이 0개를 반환하면 아무것도 저장하지 않는다(화면은 블록을 숨김).
    """
    ensure_highlights_table(conn)
    tdate = target_date or date.today().isoformat()
    dc, dp = db.days_clause_data(1)
    # KB 미진출국 제외 — 전 거점 횡단 요약은 KB 진출 거점 기준으로만 구성.
    exc, exp = db.exclude_countries_clause(config.NON_PRESENCE_CODES)

    rows = conn.execute(
        f"""
        SELECT a.article_id, a.title, a.title_ko, a.summary_ko, a.summary_en,
               a.topics, a.ai_score, a.published_at, a.event_type, a.korean_fi, a.personnel_move,
               m.tier, m.primary_country_code AS cc
        FROM articles_raw a
        JOIN media_sources m ON m.source_id = a.source_id
        WHERE a.ai_score >= ? AND a.duplicate_of IS NULL{dc}{exc}
        ORDER BY a.ai_score DESC
        LIMIT ?
        """,
        (config.AI_SCORE_ACTIVE_THRESHOLD, *dp, *exp, config.HIGHLIGHTS_MAX_ARTICLES * 3),
    ).fetchall()
    # LLM에 넘길 후보풀을 복합 rank_score(ranking.py) 순으로 — ai_score 양자화(동점) 보완.
    rows = ranking.order(conn, rows)[:config.HIGHLIGHTS_MAX_ARTICLES]

    if not rows:
        log.info("글로벌 핵심 — 대상 기사 없음, 스킵")
        return {"written": 0}

    bullets = "\n".join(
        f"- [article_id={r['article_id']}] [{r['cc']}] ({r['ai_score']}) {r['title']} :: "
        f"{((r['summary_ko'] or r['summary_en']) or '')[:160]}"
        for r in rows
    )
    user = f"KB 거점 네트워크: {kb_network.all_context()}\n\n오늘의 상위 기사:\n{bullets}"

    count = config.HIGHLIGHTS_COUNT
    provider = provider or get_provider("smart", use_batch=False)
    data = provider.complete_json(_system_highlights(count), user, max_tokens=3200)
    items = _validate_highlight_sources(data.get("highlights") or [], rows, count)

    if not items:
        log.info("글로벌 핵심 — LLM이 0개 반환, 스킵")
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

    log.info("글로벌 핵심 완료 — 항목=%d  (%s)", len(items), tdate)
    return {"written": len(items)}
