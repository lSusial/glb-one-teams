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
import numeric_guard
import text_quality
import export_json
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
    summary_en      TEXT,
    issues          TEXT,
    issues_en       TEXT,
    outlook         TEXT,
    outlook_en      TEXT,
    keywords        TEXT,
    keywords_en     TEXT,
    key_stat        TEXT,
    key_stat_en     TEXT,
    model           TEXT,
    article_count   INTEGER,
    source_articles TEXT,
    week_start      TEXT,
    week_end        TEXT,
    UNIQUE(cc, briefing_date, briefing_type)
)
"""

_SYSTEM = (
    "You are a global intelligence analyst at KB Financial Group. Given one country's news "
    "from the past week, write a WEEKLY briefing for branch executives (macro / markets / "
    "banking / regulation / risk relevant to KB's overseas operations). Base it strictly on "
    "the provided items; no speculation. Write English first, then a faithful Korean translation "
    "of every field, with the same facts, figures and issue order. Do not add or omit amounts "
    "in either language. Output ONLY JSON with BOTH Korean and English:\n"
    '{"summary_en": "5-6 sentence weekly summary", "issues_en": ["3-4 key issues"], '
    '"outlook_en": "1-2 sentence outlook", "keywords_en": ["4-6 keywords in English"], '
    '"key_stat_en": "one key statistic", "summary_ko": "영문 요약의 충실한 한국어 번역", '
    '"issues_ko": ["영문 이슈와 같은 순서·수치로 번역"], "outlook_ko": "영문 전망의 번역", '
    '"keywords_ko": ["영문과 같은 개수·순서로 번역"], "key_stat_ko": "영문과 같은 대표 수치"}'
    + numeric_guard.PRESERVE_MONEY_PROMPT
)

# 일일(daily) 브리핑 — 현지언론 화면 상단용. 전일+당일 기사를 4~5문장으로 종합(한/영 동시).
_SYSTEM_DAILY = (
    "You are a global intelligence analyst at KB Financial Group. "
    "Given one country's news items from the last day or two, write a concise briefing "
    "for branch executives — what happened and why it matters to KB's overseas operations "
    "(macro / financial markets / banking / regulation / risk). "
    "Write at most 5 sentences. Base it strictly on the provided items; "
    "no speculation. If evidence is sparse, write fewer sentences; do not pad. "
    "With only one source item, write at most two factual sentences. Omit generic business "
    "implications unless a specific impact is supported by that item. "
    "Write the English briefing first. The Korean briefing must be a faithful translation of the "
    "English one — same facts, figures and order, nothing added or dropped — in formal Korean "
    "(합쇼체: every sentence ends in '~습니다'), written in Hangul only — no Chinese characters or "
    "Japanese kana (write 증권감독관리위원회, not 证监会). Output ONLY JSON:\n"
    '{"summary_en": "1-5 sentence English briefing", "summary_ko": "영어 브리핑의 한국어 번역(~습니다체)"}'
    + numeric_guard.PRESERVE_MONEY_PROMPT
)

# 오늘의 글로벌 핵심 — 전 거점 횡단 종합(하루 1회 1콜). 겹치는 주제는 하나로 묶는다.
# 항목 수는 config.HIGHLIGHTS_COUNT (2026-08-26: 3 → 10, 전체 진출국 커버리지 확대).
def _system_highlights(count: int) -> str:
    return (
        "You are a global intelligence analyst at KB Financial Group. Given today's top-scored "
        f"news across ALL KB overseas hubs, synthesize EXACTLY {count} headline items a KB bank "
        "executive must read today — merge overlapping stories into a single item where relevant, "
        f"and pick the {count} most important distinct developments overall (not one per hub; "
        "spread across as many different hubs/topics as the material genuinely supports; "
        "include cross-hub global risks such as oil shocks, global market sell-offs or critical "
        "cyber incidents when they are material). "
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
        + numeric_guard.PRESERVE_MONEY_PROMPT
    )


# 오늘의 탑이슈 퀴즈 — daily_highlights 항목 중 하나를 골라 2~4지선다 1문항 생성(하루 1회 1콜).
_SYSTEM_QUIZ = (
    "You are creating ONE multiple-choice quiz question for KB bank employees based strictly on "
    "today's top global financial news items below. Goal: an employee who read today's headlines "
    "can answer it AND learns something useful for banking work.\n"
    "Rules:\n"
    "1. Prefer questions about WHO/WHERE/WHAT ACTION/WHY IT MATTERS (which country, which central "
    "bank or regulator, what measure was taken, what it means for banks) over recalling a number. "
    "Follow the numeric-question instruction in the input.\n"
    "2. Exactly one choice is correct and is stated in the source item. The question must have only "
    "one defensible answer.\n"
    "3. Distractors must be the SAME kind and unit as the answer (all countries, all institutions, "
    "all percentages, all amounts in the same currency and magnitude) and plausible — never reuse an "
    "unrelated number from the article with a different unit.\n"
    "4. Do not put a date or year in the question unless it appears in the source item.\n"
    "5. Korean fields must be natural Korean: write money as 조/억 원·달러 (never trillion/billion "
    "in Korean), no Japanese kana.\n"
    "6. Do not repeat or paraphrase any recently asked question listed in the input.\n"
    "7. 2-4 choices, the SAME order in Korean and English.\n"
    "Output ONLY JSON:\n"
    '{"question_ko": "퀴즈 질문(한국어)", "question_en": "the question (English)", '
    '"choices_ko": ["선택지1", "선택지2", "..."], "choices_en": ["choice1", "choice2", "..."], '
    '"correct_index": 0, '
    '"explanation_ko": "정답 근거 1문장 + KB(은행) 관점 시사점 1문장", '
    '"explanation_en": "1-sentence reason + 1-sentence implication for a bank like KB", '
    '"source_article_id": 123}\n'
    '"source_article_id" must be exactly one of the article_id values shown in the input.'
    + numeric_guard.PRESERVE_MONEY_PROMPT
)

# 퀴즈 검증용 — 정답을 숨긴 채 출처만 보고 풀게 한다(오답 보기·모호한 문제·사실 오류 차단).
_SYSTEM_QUIZ_CHECK = (
    "Answer the multiple-choice question using ONLY the SOURCE text. "
    "If the source does not state the answer, or more than one choice could be correct, say so. "
    'Output ONLY JSON: {"answer_index": 0, "answerable": true, "ambiguous": false, "reason": "short"}'
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
            f"""SELECT DISTINCT {db.effective_country_expr()} AS cc
                FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
                WHERE a.ai_score IS NOT NULL
                  AND {db.effective_country_expr()}
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


# 한국어 번역에 한자·가나가 섞이는 경우(2026-10-22 CN '证监会'·GB '전略'·ID 'ファ실리티') 재요청 판정·지시
_FOREIGN_SCRIPT_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")
_SCRIPT_RETRY_NOTE = (
    "\n\n[재요청] 직전 응답의 한국어 브리핑에 한자·일본어 가나가 섞여 폐기됐다. 한국어 브리핑은 한글로만 쓰고 "
    "고유명사는 한글 표기 또는 영문 그대로 쓴다."
)

# 영어 브리핑에 한국어 금액 힌트가 '(100억 파운드)'로 섞이는 경우(2026-10-22 GB) 재요청 판정·지시
_HANGUL_RE = re.compile(r"[가-힣]")
_EN_HANGUL_RETRY_NOTE = (
    "\n\n[재요청] 직전 응답의 영어 브리핑에 한글이 섞여 폐기됐다. 영어 필드에는 한글을 쓰지 말고 "
    "금액도 기사 목록의 영어 표기만 쓴다."
)

_AMOUNT_RETRY_NOTE = (
    "\n\n[재요청] 직전 응답의 금액이 기사 목록과 달라 폐기됐다. 금액은 기사 목록에 적힌 숫자·단위·통화를 "
    "그대로 옮겨 쓰고 직접 단위를 환산하지 마라. 한국어 브리핑에서도 금액은 영어 표기 그대로 쓴다"
    "(예: '₹30,213 crore', '$3.5 trillion' — 억/조 환산 금지, 괄호 병기도 금지)."
)

_SYSTEM_WEEKLY_KO = (
    "Provide a faithful Korean translation of the verified English weekly briefing JSON below. "
    "Translate every field and every array item in exactly the same order. Add or omit no facts "
    "or figures. Preserve monetary amounts exactly as written in English. "
    "Use formal Korean (~습니다), no Chinese characters or Japanese kana. "
    "Return ONLY JSON with summary_ko, issues_ko (array), outlook_ko, keywords_ko (array), key_stat_ko. "
    "Do not rewrite or return any English fields."
    + numeric_guard.PRESERVE_MONEY_PROMPT
)


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
    requests, meta, bullets_by_cc = [], {}, {}
    cm = ranking.cluster_sizes(conn)
    title_en_sql = "a.title_en" if "title_en" in db.table_columns(conn) else "NULL"
    for cc in ccs:
        # cc = 매체 국가(국가 탭과 같은 rank_score·국가 언급 판정용). 대상 국가는 WHERE의 주제국가 식으로 고른다.
        arts = conn.execute(
            f"""
            SELECT a.article_id, a.title, a.title_ko, {title_en_sql} AS title_en, a.summary_ko, a.summary_en,
                   a.ai_score, a.link, a.published_at, a.event_type, a.korean_fi, a.personnel_move, m.tier,
                   a.primary_country, m.primary_country_code AS cc
            FROM articles_raw a
            JOIN media_sources m ON m.source_id = a.source_id
            WHERE {db.effective_country_expr()} = ?
              AND a.ai_score >= ?
              AND COALESCE(NULLIF(a.summary_ko, ''), a.summary_en, '') != ''
              AND a.duplicate_of IS NULL{dc}{export_json._ARCHIVE_LINK_EXCL if daily else ''}
            ORDER BY a.published_at DESC, a.article_id ASC
            """,
            (cc, config.AI_SCORE_ACTIVE_THRESHOLD, *dp),
        ).fetchall()
        if daily:
            # 국가 탭 카드와 같은 기사·순서로 쓴다(2026-10-22 US 브리핑은 SpaceX, 탭 카드는 연준 1건)
            arts = [a for a in arts if export_json._on_country_tab(a, cc)]
            arts = export_json.country_tab_order(conn, arts, cm)[0][:config.COUNTRY_MAX_ARTICLES]
        else:
            arts = ranking.order(conn, arts, cluster_map=cm)[:config.BRIEFING_MAX_ARTICLES]
        before = len(arts)
        arts = [a for a in arts if text_quality.has_briefing_evidence(
            a['title_en'] or a['title'], a['summary_en'] or a['summary_ko'])]
        if before != len(arts):
            log.info("브리핑 정보 부족 기사 제외 — 국가=%s %d건", cc, before-len(arts))
        stats["countries"] += 1
        meta[cc] = arts
        if not arts:
            continue
        # 영어 기준본을 입력으로 쓰고 금액 표기를 유지한다. 환산된 한국어 입력·힌트는 넣지 않는다.
        bullets = "\n".join(
            f"- [{a['published_at']}] ({a['ai_score']}) {a['title']} :: {((a['summary_en'] or a['summary_ko']) or '')}"
            for a in arts
        )
        bullets_by_cc[cc] = bullets
        user = f"국가: {cc} ({kb_network.context_for(cc)})\n기사 목록:\n{bullets}"
        # weekly는 한/영 요약+이슈 3~4개+전망+키워드까지 daily보다 필드가 훨씬 많아
        # 900으로는 잘려서 JSON 파싱이 깨진다(생성 도중 max_tokens 도달) — 여유를 둔다.
        # daily 900은 영어 선작성+번역으로 길어진 응답이 잘려 JSON이 깨졌다(2026-10-22 CN) — 여유를 둔다.
        # weekly 2200도 2026-11-09 HK·ID 응답이 잘렸다(다른 국가도 1,600~2,145 사용) — 4000.
        requests.append((cc, system, user, 1600 if daily else 4000))

    results = provider.complete_json_batch(requests) if requests else {}

    cur = conn.cursor()
    # 금액 검증에서 거부된 국가는 '금액 그대로' 지시를 붙여 1회 재요청한다. 재요청 없이 버리면
    # 옛 브리핑이 계속 남았다(2026-10-07 IN 브리핑 6일째 미갱신).
    req_by_cc = {r[0]: r for r in requests}
    pending = [(cc, results.get(cc) or {}) for cc in meta]
    for attempt in (1, 2, 3):
        retry = []
        translation_retry = []
        for cc, data in pending:
            arts = meta[cc]
            if not arts:
                data = {"summary_ko": "해당 기간에 브리핑 기준을 충족할 충분한 근거가 없습니다.",
                        "summary_en": "There is insufficient evidence for a briefing in this period."}
            if not data:
                # 응답이 잘려 JSON을 못 읽으면 빈 결과다. 조용히 넘기면 옛 브리핑이 그대로 남는다.
                if attempt == 1:
                    log.warning("브리핑 응답 해석 실패 — 재요청(국가=%s, %s)", cc, briefing_type)
                    retry.append((cc, ""))
                else:
                    log.warning("브리핑 응답 해석 실패 — 저장 안 함(국가=%s, %s)", cc, briefing_type)
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

            if arts:
                generated = " ".join((summary, summary_en, issues, issues_en,
                                      outlook, outlook_en, key_stat, key_stat_en))
                en_fields, ko_fields = {"summary": summary_en}, {"summary": summary}
                if not daily:
                    en_fields.update(issues=json.loads(issues_en), outlook=outlook_en,
                                     keywords=json.loads(keywords_en), key_stat=key_stat_en)
                    ko_fields.update(issues=json.loads(issues), outlook=outlook,
                                     keywords=json.loads(keywords), key_stat=key_stat)
                differences = numeric_guard.bilingual_amount_issues(en_fields, ko_fields)
                unsupported = numeric_guard.unsupported_amounts(bullets_by_cc[cc], generated)
                en_text = json.dumps(en_fields, ensure_ascii=False)
                if unsupported or differences:
                    details = differences + ([f"원문에 없는 금액: {unsupported}"] if unsupported else [])
                    reason = "금액 불일치"
                    note = _AMOUNT_RETRY_NOTE + "\n불일치 필드·금액: " + "; ".join(details[:12])
                elif _FOREIGN_SCRIPT_RE.search(json.dumps(ko_fields, ensure_ascii=False)):
                    reason, note = "한국어에 한자·가나", _SCRIPT_RETRY_NOTE
                elif _HANGUL_RE.search(en_text):
                    reason, note = "영어에 한글", _EN_HANGUL_RETRY_NOTE
                else:
                    reason = None
                if reason:
                    if attempt == 1:
                        log.warning("브리핑 %s — 재요청(국가=%s, %s): %s",
                                    reason, cc, briefing_type, summary[:80])
                        retry.append((cc, note))
                        continue
                    # 전체 재요청도 실패하면 원문 검증을 통과한 영문은 고정하고 한국어만 1회 번역한다.
                    # 영문 자체가 잘못됐거나 한글이 섞였으면 번역 기준본으로 사용하지 않는다.
                    # daily도 같다 — 2026-11-11 VN 'VND 1.28 quadrillion'을 매번 '1.28경'으로 환산해 사흘째 미갱신.
                    if (attempt == 2 and summary_en
                            and not numeric_guard.unsupported_amounts(bullets_by_cc[cc], en_text)
                            and not _HANGUL_RE.search(en_text)):
                        translation_retry.append((cc, en_fields))
                        log.warning("%s 브리핑 — 검증된 영문 기준 한국어 재번역(국가=%s)", briefing_type, cc)
                        continue
                    log.warning("브리핑 %s — 저장 안 함(국가=%s, %s): %s",
                                reason, cc, briefing_type, summary[:80])
                    continue

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

        if attempt == 1 and retry:
            again = provider.complete_json_batch([
                (cc, system, req_by_cc[cc][2] + note, req_by_cc[cc][3]) for cc, note in retry
            ])
            pending = [(cc, again.get(cc) or {}) for cc, _ in retry]
        elif attempt == 2 and translation_retry:
            again = provider.complete_json_batch([
                (cc, _SYSTEM_WEEKLY_KO, json.dumps(fields, ensure_ascii=False), 2200)
                for cc, fields in translation_retry
            ])
            pending = [(cc, {**{k + "_en": v for k, v in fields.items()},
                             **{k + "_ko": (again.get(cc) or {}).get(k + "_ko") for k in fields}})
                       for cc, fields in translation_retry]
        else:
            break
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


def _route_text(r) -> str | None:
    """국가 언급 판정용 영문 텍스트(export_json._route_text와 같은 기준). 영문 요약이 없으면 None → 판단 보류."""
    keys = r.keys()
    if "summary_en" not in keys or not (r["summary_en"] or "").strip():
        return None
    title_en = r["title_en"] if "title_en" in keys else ""
    return f"{r['title'] or ''} {title_en or ''} {r['summary_en']}"


def _validate_highlight_sources(items: list, rows, limit: int) -> list[dict]:
    """LLM 출처 ID를 입력 후보 집합에 대해 검증한다.

    잘못되거나 출처가 없는 항목은 저장하지 않는다. 화면에서 국가·제목으로 원문을
    추측해 붙이는 것보다 항목을 누락시키는 편이 출처 신뢰성 면에서 안전하다.
    """
    allowed = {int(r["article_id"]): r for r in rows}

    def val_of(r, key) -> str:
        try:
            return r[key] or ""
        except (KeyError, TypeError, IndexError):
            return ""

    def row_text(r) -> str:
        return " ".join(val_of(r, k) for k in ("title", "title_ko", "summary_ko", "summary_en"))

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
        generated = (item.get("headline_ko") or "") + " " + (item.get("headline_en") or "")
        source_text = " ".join(row_text(allowed[aid]) for aid in valid)
        if numeric_guard.amount_mismatch(source_text, generated):
            log.warning("글로벌 핵심 금액 불일치 — 항목 제외: %s", item.get("headline_ko", "")[:80])
            continue
        en_head = item.get('headline_en') or ''
        ko_head = item.get('headline_ko') or ''
        if (text_quality.korean_issues(en_head, ko_head, headline=True)
                or text_quality.period_issues(source_text, en_head)
                or text_quality.period_issues(source_text, ko_head)):
            log.warning('글로벌 핵심 기간/언어 검증 실패 — 항목 제외: %s',ko_head[:80])
            continue
        clean = dict(item)
        clean["source_article_ids"] = valid
        # 국가 태그 = 근거 기사의 주제국가(LLM이 붙인 태그는 쓰지 않는다). 근거가 전부 GLOBAL이면 GLOBAL.
        # 2026-10-06 이탈리아 은행 M&A가 GB, 10/7 중국-라오스 군사훈련센터가 TH+LA(방콕포스트 매체국)로
        # 태그됨 — 겹치면 LLM 태그를 유지하던 규칙이 매체 국가를 통과시켰다.
        src_ccs = {kb_network.route_country(val_of(allowed[aid], "subject_cc") or val_of(allowed[aid], "cc"),
                                            _route_text(allowed[aid])) for aid in valid} - {""}
        codes = sorted(src_ccs - {"GLOBAL"}) or ["GLOBAL"]
        if codes != item.get("country_codes"):
            log.info("글로벌 핵심 국가 태그 %s → %s: %s", item.get("country_codes"), codes,
                     item.get("headline_ko", "")[:60])
        clean["country_codes"] = codes
        out.append(clean)
    return out


def _cap_pool(rows, limit: int, per_country: int) -> list:
    """rank 순 후보에서 국가(주제국가, GLOBAL 포함)당 per_country건까지만 담아 limit건 후보풀을 만든다."""
    out, used = [], {}
    for r in rows:
        cc = r["subject_cc"] or r["cc"]
        if used.get(cc, 0) >= per_country:
            continue
        used[cc] = used.get(cc, 0) + 1
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _select_highlights(items: list, count: int, per_country: int,
                       min_global: int, max_global: int) -> list:
    """LLM 중요도 순 항목에서 국가당 상한·GLOBAL 최소/최대 슬롯을 지켜 count건을 고른다.

    상한 때문에 count에 못 미치면 남은 항목을 순서대로 채운다(빈 슬롯보다 쏠림이 낫다).
    """
    def is_global(it):
        return it["country_codes"] == ["GLOBAL"]

    def cap_of(code):
        return max_global if code == "GLOBAL" else per_country

    chosen, used = [], {}
    for it in items:
        if len(chosen) >= count:
            break
        codes = it["country_codes"]
        if any(used.get(c, 0) >= cap_of(c) for c in codes):
            continue
        chosen.append(it)
        for c in codes:
            used[c] = used.get(c, 0) + 1

    spare = [it for it in items if is_global(it) and it not in chosen]
    need = min(min_global, max_global) - sum(map(is_global, chosen))
    while need > 0 and spare:
        if len(chosen) < count:
            chosen.append(spare.pop(0))
        else:
            idx = next((i for i in range(len(chosen) - 1, -1, -1) if not is_global(chosen[i])), None)
            if idx is None:
                break
            chosen[idx] = spare.pop(0)
        need -= 1

    for it in items:
        if len(chosen) >= count:
            break
        if it not in chosen:
            chosen.append(it)
    return chosen


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
               m.tier, m.primary_country_code AS cc,
               {db.effective_country_expr()} AS subject_cc
        FROM articles_raw a
        JOIN media_sources m ON m.source_id = a.source_id
        WHERE a.ai_score >= ? AND a.duplicate_of IS NULL{dc}{exc}
        ORDER BY a.ai_score DESC
        LIMIT ?
        """,
        (config.AI_SCORE_ACTIVE_THRESHOLD, *dp, *exp, config.HIGHLIGHTS_MAX_ARTICLES * 3),
    ).fetchall()
    # LLM에 넘길 후보풀을 복합 rank_score(ranking.py) 순으로 — ai_score 양자화(동점) 보완.
    # 국가당 상한으로 후보풀 쏠림 방지(현지 피드 물량이 많은 IN·ID가 풀을 채우던 문제).
    rows = _cap_pool(ranking.order(conn, rows), config.HIGHLIGHTS_MAX_ARTICLES,
                     config.HIGHLIGHTS_POOL_PER_COUNTRY)

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
    asked = count + config.HIGHLIGHTS_EXTRA
    provider = provider or get_provider("smart", use_batch=False)
    # 15개 항목(한·영) 응답이 길어 4000토큰에서 잘리거나 JSON이 깨지면 {}가 돼 조용히 스킵됐다
    # (2026-11-05 홈 핵심·퀴즈가 전날 그대로) — 한도를 넉넉히 두고 해석 실패 시 1회 재요청.
    data = provider.complete_json(_system_highlights(asked), user, max_tokens=8000)
    if not data.get("highlights"):
        log.warning("글로벌 핵심 — 응답 해석 실패(항목 없음), 1회 재요청")
        data = provider.complete_json(_system_highlights(asked), user, max_tokens=8000)
    items = _validate_highlight_sources(data.get("highlights") or [], rows, asked)
    items = _select_highlights(items, count, config.HIGHLIGHTS_PER_COUNTRY,
                               config.HIGHLIGHTS_MIN_GLOBAL, config.HIGHLIGHTS_MAX_GLOBAL)

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
    return {"written": len(items)}


_CREATE_QUIZ = """
CREATE TABLE IF NOT EXISTS daily_quiz (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    date              TEXT NOT NULL,
    question_ko       TEXT NOT NULL,
    question_en       TEXT NOT NULL,
    choices_ko        TEXT NOT NULL,
    choices_en        TEXT NOT NULL,
    correct_index     INTEGER NOT NULL,
    explanation_ko    TEXT,
    explanation_en    TEXT,
    source_article_id INTEGER,
    model             TEXT,
    generated_at      TEXT,
    UNIQUE(date)
)
"""


def ensure_quiz_table(conn) -> None:
    conn.execute(_CREATE_QUIZ)
    conn.commit()


def _validate_quiz(data: dict, allowed_ids: set[int]) -> dict | None:
    """LLM 퀴즈 응답 검증 — 선택지 2~4개(한/영 동일 개수), correct_index 범위 내,
    출처 article_id가 입력 후보 집합에 있어야 저장(없으면 화면에 가짜 출처로 보일 위험)."""
    if not isinstance(data, dict):
        return None
    q_ko = str(data.get("question_ko") or "").strip()
    q_en = str(data.get("question_en") or "").strip()
    c_ko, c_en = data.get("choices_ko"), data.get("choices_en")
    if not q_ko or not q_en or not isinstance(c_ko, list) or not isinstance(c_en, list):
        return None
    if not (2 <= len(c_ko) <= 4) or len(c_ko) != len(c_en):
        return None
    try:
        idx = int(data.get("correct_index"))
    except (TypeError, ValueError):
        return None
    if not (0 <= idx < len(c_ko)):
        return None
    try:
        sid = int(data.get("source_article_id"))
    except (TypeError, ValueError):
        return None
    if sid not in allowed_ids:
        log.warning("퀴즈 출처 무효 — 스킵: %s", q_ko[:60])
        return None
    return {
        "question_ko": q_ko, "question_en": q_en,
        "choices_ko": [str(c) for c in c_ko], "choices_en": [str(c) for c in c_en],
        "correct_index": idx,
        "explanation_ko": str(data.get("explanation_ko") or "").strip(),
        "explanation_en": str(data.get("explanation_en") or "").strip(),
        "source_article_id": sid,
    }


# 2026-11-20 퀴즈 품질 점검(39문항): 정답이 1·2번에만 몰림(4번 0회), 같은 기사 재출제 5건,
# 오답 보기를 기사 속 다른 숫자에서 단위째 가져와 정답이 드러남("69.72달러 / 5,050.43포인트"),
# 숫자 암기형 80%, 번역 안 된 단위("1.04 trillion 달러")·연도 오류. 아래 규칙은 그 대응이다.
_QUIZ_RECENT_DAYS = 14          # 이 기간에 출제한 기사는 후보에서 뺀다
_QUIZ_NUMERIC_PER_WEEK = 2      # 최근 7일 숫자형 문제가 이만큼이면 오늘은 숫자형 금지
_QUIZ_MAX_ATTEMPTS = 3
_UNTRANSLATED_UNIT = re.compile(r"\b(?:trillion|billion|million|bn|mn)\b", re.I)
_YEAR = re.compile(r"(?<!\d)(20\d\d)(?!\d)")


_UNIT_FAMILIES = [  # (패턴, 계열) — 앞에서부터 매칭. 자릿수(만·억·조·billion)는 계열에 넣지 않는다.
    (r"S\$|SGD|싱가포르\s*달러", "SGD"), (r"HK\$|HKD|홍콩\s*달러", "HKD"),
    (r"US\$|\$|USD|달러(?!당)", "USD"), (r"달러당|per\s+dollar", "FX"),
    (r"루피아|IDR", "IDR"), (r"루피|INR|₹", "INR"), (r"위안|CNY|RMB", "CNY"), (r"엔|JPY|¥", "JPY"),
    (r"바트|THB", "THB"), (r"동(?![가-힣])|VND", "VND"), (r"원(?![가-힣])|KRW", "KRW"), (r"리엘|KHR", "KHR"),
    (r"%p|%|bp|퍼센트", "PCT"), (r"포인트|points?|pt", "PTS"), (r"배럴|barrels?", "BBL"),
    (r"\d\s*(?:년|월|일)|\b(?:19|20)\d\d\b", "DATE"),
    (r"개|명|건|곳|조항|차례|times?", "COUNT"),
]
_UNIT_STRIP = re.compile(r"[\d.,\s~+\-:/()]|약|대략|about|approx\.?|만|억|조|천|trillion|billion|million|bn|mn|주당|per\s+share",
                         re.I)


def _choice_signature(choice: str) -> str:
    """보기의 단위 계열 — 숫자가 없으면 'text', 단위 밖 글자가 많으면 문장형 'desc'."""
    if not re.search(r"\d", choice):
        return "text"
    fams, rest = set(), choice
    for pat, fam in _UNIT_FAMILIES:
        if re.search(pat, rest, re.I):
            fams.add(fam)
            if fam == "FX":   # '달러당 94루피'는 FX+통화 조합 — 뒤 통화도 계열에 넣는다
                continue
            rest = re.sub(pat, "", rest, flags=re.I)
    rest = _UNIT_STRIP.sub("", rest)
    if len(re.findall(r"[가-힣A-Za-z]", rest)) > 4:
        return "desc"
    return "+".join(sorted(fams)) or "num"


def _is_numeric_quiz(choices: list[str]) -> bool:
    return all(_choice_signature(c) not in ("text", "desc") for c in choices)


def _quiz_issues(quiz: dict, source_text: str, source_date: str | None,
                 numeric_allowed: bool = True) -> list[str]:
    """코드로 잡을 수 있는 퀴즈 결함 목록(비어 있으면 통과)."""
    issues = []
    sigs = {_choice_signature(c) for c in quiz["choices_ko"]}
    if "desc" in sigs:   # 문장형 보기('25bp 인상했다' vs '동결했다')는 단위 비교 대상이 아니다
        sigs = set()
    if len(sigs) > 1:
        issues.append(f"보기 단위·종류 불일치 {sorted(sigs)}")
    if len(set(quiz["choices_ko"])) != len(quiz["choices_ko"]):
        issues.append("중복 보기")
    ko_text = " ".join([quiz["question_ko"], *quiz["choices_ko"], quiz["explanation_ko"]])
    if _UNTRANSLATED_UNIT.search(ko_text):
        issues.append("한국어에 번역 안 된 단위")
    if text_quality.KANA_RE.search(ko_text):
        issues.append("한국어에 가나 혼입")
    allowed_years = set(_YEAR.findall(source_text or "")) | ({source_date[:4]} if source_date else set())
    bad_years = set(_YEAR.findall(quiz["question_ko"] + " " + quiz["question_en"])) - allowed_years
    if bad_years:
        issues.append(f"출처에 없는 연도 {sorted(bad_years)}")
    correct = quiz["choices_ko"][quiz["correct_index"]] + " " + quiz["choices_en"][quiz["correct_index"]]
    if numeric_guard.amount_mismatch(source_text or "", correct):
        issues.append("정답 금액이 출처와 불일치")
    if not numeric_allowed and _is_numeric_quiz(quiz["choices_ko"]):
        issues.append("숫자형 문제 제한 초과")
    return issues


def _shuffle_quiz(quiz: dict, seed: str) -> dict:
    """보기 순서를 섞어 정답 위치 편향을 없앤다. 한·영 순서는 함께 움직인다(날짜 고정 시드)."""
    import random
    order = list(range(len(quiz["choices_ko"])))
    random.Random(seed).shuffle(order)
    out = dict(quiz)
    out["choices_ko"] = [quiz["choices_ko"][i] for i in order]
    out["choices_en"] = [quiz["choices_en"][i] for i in order]
    out["correct_index"] = order.index(quiz["correct_index"])
    return out


def _verify_quiz(provider, quiz: dict, source_text: str) -> str | None:
    """정답을 숨긴 채 출처만 보고 풀게 한다. 실패 사유(없으면 None)."""
    choices = "\n".join(f"{i}. {c}" for i, c in enumerate(quiz["choices_en"]))
    user = f"SOURCE:\n{source_text}\n\nQUESTION: {quiz['question_en']}\nCHOICES:\n{choices}"
    try:
        res = provider.complete_json(_SYSTEM_QUIZ_CHECK, user, max_tokens=300)
    except Exception as e:  # 검증 호출 실패는 '검증 못 함'으로 처리해 저장하지 않는다
        return f"검증 호출 실패: {e}"
    if not isinstance(res, dict):
        return "검증 응답 무효"
    if res.get("ambiguous") or res.get("answerable") is False:
        return f"모호/출처로 답 불가: {res.get('reason', '')}"[:200]
    try:
        ans = int(res.get("answer_index"))
    except (TypeError, ValueError):
        return "검증 답 없음"
    if ans != quiz["correct_index"]:
        return f"검증 답({ans})과 정답({quiz['correct_index']}) 불일치: {res.get('reason', '')}"[:200]
    return None


def generate_daily_quiz(
    conn,
    provider: LLMProvider | None = None,
    target_date: str | None = None,
) -> dict:
    """오늘의 글로벌 핵심(daily_highlights) 기사 중 하나로 2~4지선다 퀴즈 1개 생성.
    main.py ai 파이프라인상 generate_daily_highlights 바로 다음 단계 — 그 결과물을 입력으로 쓴다.
    생성 → 코드 검증(_quiz_issues) → 보기 섞기 → 정답 가린 풀이 검증(_verify_quiz) 순서로,
    통과하지 못하면 사유를 알려주며 최대 _QUIZ_MAX_ATTEMPTS번 다시 만든다. 끝내 실패하면
    저장하지 않는다(화면은 퀴즈 블록을 숨김).
    """
    ensure_quiz_table(conn)
    tdate = target_date or date.today().isoformat()

    row = conn.execute("SELECT items FROM daily_highlights WHERE date = ?", (tdate,)).fetchone()
    if not row:
        log.info("퀴즈 — 오늘의 글로벌 핵심 없음, 스킵")
        return {"written": 0}
    try:
        items = json.loads(row["items"])
    except (TypeError, ValueError, json.JSONDecodeError):
        items = []
    source_ids = sorted({
        int(sid) for item in items for sid in (item.get("source_article_ids") or [])
        if isinstance(sid, (int, str)) and str(sid).lstrip("-").isdigit()
    })
    if not source_ids:
        log.info("퀴즈 — 핵심 항목에 출처 없음, 스킵")
        return {"written": 0}

    since = (date.fromisoformat(tdate) - timedelta(days=_QUIZ_RECENT_DAYS)).isoformat()
    recent = conn.execute(
        "SELECT date, question_ko, choices_ko, source_article_id FROM daily_quiz "
        "WHERE date >= ? AND date < ? ORDER BY date DESC", (since, tdate)).fetchall()
    used = {r["source_article_id"] for r in recent}
    week_ago = (date.fromisoformat(tdate) - timedelta(days=7)).isoformat()
    numeric_recent = sum(1 for r in recent if r["date"] >= week_ago
                         and _is_numeric_quiz(json.loads(r["choices_ko"] or "[]")))
    numeric_allowed = numeric_recent < _QUIZ_NUMERIC_PER_WEEK

    placeholders = ",".join("?" for _ in source_ids)
    arts = conn.execute(
        f"SELECT article_id, title, title_en, summary_ko, summary_en, published_at FROM articles_raw "
        f"WHERE article_id IN ({placeholders})",
        source_ids,
    ).fetchall()
    fresh = [r for r in arts if r["article_id"] not in used]
    arts = fresh or arts   # 전부 최근 출제분이면 어쩔 수 없이 전체 사용
    by_id = {r["article_id"]: r for r in arts}
    if not by_id:
        return {"written": 0}

    def src_text(r) -> str:
        return " ".join(x for x in (r["title"], r["title_en"], r["summary_en"], r["summary_ko"]) if x)

    bullets = "\n".join(
        f"- [article_id={r['article_id']}] {r['title']} :: "
        f"{((r['summary_en'] or r['summary_ko']) or '')[:400]}"
        for r in arts
    )
    recent_qs = "\n".join(f"- {r['question_ko']}" for r in recent[:10]) or "- (없음)"
    rule = ("오늘은 숫자를 맞히는 문제 금지 — 국가·기관·조치·의미를 묻는 문제로."
            if not numeric_allowed else "숫자 문제도 가능하나 다른 유형을 우선 고려.")
    base_user = (f"오늘의 글로벌 핵심 기사 목록:\n{bullets}\n\n"
                 f"최근 출제한 문제(같은 사건·주제 재출제 금지):\n{recent_qs}\n\n{rule}")

    provider = provider or get_provider("smart", use_batch=False)
    rejected: list[str] = []
    quiz = None
    for attempt in range(1, _QUIZ_MAX_ATTEMPTS + 1):
        user = base_user + ("\n\n이전 시도 탈락 사유(반복 금지):\n" + "\n".join(rejected) if rejected else "")
        cand = _validate_quiz(provider.complete_json(_SYSTEM_QUIZ, user, max_tokens=900), set(by_id))
        if not cand:
            rejected.append("- 형식 무효")
            continue
        src = by_id[cand["source_article_id"]]
        issues = _quiz_issues(cand, src_text(src), src["published_at"], numeric_allowed)
        if not issues:
            cand = _shuffle_quiz(cand, f"{tdate}:{attempt}")
            fail = _verify_quiz(provider, cand, src_text(src))
            if fail:
                issues = [fail]
        if issues:
            log.info("퀴즈 시도 %d 탈락 — %s: %s", attempt, "; ".join(issues), cand["question_ko"][:50])
            rejected.append(f"- \"{cand['question_ko'][:60]}\" → {'; '.join(issues)}")
            continue
        quiz = cand
        break
    if not quiz:
        log.info("퀴즈 — %d회 시도 모두 탈락, 스킵", _QUIZ_MAX_ATTEMPTS)
        return {"written": 0, "rejected": rejected}

    conn.execute(
        """
        INSERT INTO daily_quiz (date, question_ko, question_en, choices_ko, choices_en,
                                 correct_index, explanation_ko, explanation_en,
                                 source_article_id, model, generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(date) DO UPDATE SET
            question_ko = excluded.question_ko, question_en = excluded.question_en,
            choices_ko = excluded.choices_ko, choices_en = excluded.choices_en,
            correct_index = excluded.correct_index,
            explanation_ko = excluded.explanation_ko, explanation_en = excluded.explanation_en,
            source_article_id = excluded.source_article_id,
            model = excluded.model, generated_at = CURRENT_TIMESTAMP
        """,
        (tdate, quiz["question_ko"], quiz["question_en"],
         json.dumps(quiz["choices_ko"], ensure_ascii=False),
         json.dumps(quiz["choices_en"], ensure_ascii=False),
         quiz["correct_index"], quiz["explanation_ko"], quiz["explanation_en"],
         quiz["source_article_id"], provider.model_id),
    )
    conn.commit()
    log.info("퀴즈 생성 완료(시도 %d) — %s", attempt, quiz["question_ko"][:60])
    return {"written": 1, "rejected": rejected}
