"""
AI 분석 (llm_ranker.py)

prefilter 를 통과(keep)한 기사에 중요도·요약·주제를 부여한다.

입력: llm_prefilter='keep' AND ai_score IS NULL
순서: filter_score DESC
출력:
  - ai_score        : KB 경영 중요도 (0~100). >= AI_SCORE_ACTIVE_THRESHOLD → UI ACTIVE
  - summary_ko      : 한국어 요약 (UI q)
  - topics          : taxonomy 코드 CSV (UI 카테고리 c, 축 C — 현지언론 필터)
  - event_type      : taxonomy event_types 코드 CSV (모니터링 탭 전용, 축 E)
  - source_links    : 다출처 종합에 실제로 쓰인 소스 목록(JSON, 클러스터<3이면 NULL)
  - ai_model        : 생성 프로바이더:모델 식별자

* 분석 모델(role='smart') 사용. topics/event_type 모두 taxonomy 코드로 검증,
  비면 시드 매칭 폴백. backfill_event_types() — event_type 컬럼 도입 이전
  기사용 1회성 시드 백필(LLM 호출 없음).
* 다출처 종합(2026-08-26): keyword_filter.run_dedup()의 duplicate_of 클러스터가
  SYNTH_MIN_SOURCES(기본 3) 이상이면 해당 소스들을 함께 프롬프트에 넣어 단일 기사
  패러프레이즈가 아닌 "종합" 요약을 생성한다(_cluster_sources 참조).
"""
from __future__ import annotations

import json
import logging
import re

import config
import db
import kb_network
import numeric_guard
import taxonomy
from llm_provider import LLMProvider, get_provider

log = logging.getLogger("llm_ranker")

# 주제 국가(primary_country) 허용 코드 = KB 진출국 + 미진출국 + KR (+ GLOBAL).
# 그 외/불명 -> None 저장(표시 시 매체 국적으로 폴백). 2026-09-11 신설(피드백 7).
_ALLOWED_PRIMARY = set(kb_network.KB_NETWORK.keys()) | set(config.NON_PRESENCE_CODES) | {"KR", "GLOBAL"}
_PRIMARY_COUNTRY_CODES = ", ".join(
    list(kb_network.KB_NETWORK.keys()) + list(config.NON_PRESENCE_CODES) + ["KR"]
)
_ISO2_RE = re.compile(r"[A-Z]{2}")


def _valid_primary_country(v) -> str | None:
    """목록 밖 코드는 두 가지로 갈린다 — 형식만 보고 구분한다(프롬프트 지시만으로는
    모델이 이탈리아 같은 특정국을 GLOBAL 대신 "IT"로 내는 경우가 잦았다, 2026-10-06).

    빈 값/형식이 다른 응답 -> None(모름) -> 표시 시 매체 국적 폴백(원래 설계, 피드백 7).
    ISO2 형식인데 우리 목록에 없는 코드(예: "IT") -> 이미 특정국이 식별된 것이므로
    매체 국적으로 새지 않고 GLOBAL로 — 영국 매체가 쓴 이탈리아 은행 합병 기사가
    GB 국가탭에 뜨던 문제(Intesa/Monte dei Paschi)가 이 경로였다.
    """
    cc = str(v or "").strip().upper()
    if cc in _ALLOWED_PRIMARY:
        return cc
    if _ISO2_RE.fullmatch(cc):
        return "GLOBAL"
    return None


# 카테고리 중복 시 우선순위 판단기준(피드백 첨부 "판단 기준" 표) — full/light 공통.
_TOPIC_DISAMBIG_BLOCK = (
    "\n\nWhen an article fits more than one topic (e.g. a bank building a digital platform), "
    "choose the PRIMARY (first) topic by this priority: (1) the core event/change in the article, "
    "(2) who is primarily affected, (3) secondary detail. Tie-break for MARKETS vs POLICY: a "
    "central-bank/regulator DECISION, announcement, or forward-guidance signal is POLICY (primary); "
    "use MARKETS as primary only when the article is about the market price reaction itself "
    "(yield, FX, equity or credit-spread moves). Guidance: MARKETS = market-price change (rates, FX, bonds, "
    "equities, insurance/securities); ECONOMY = real-economy change (growth, prices, jobs, trade, "
    "consumption); POLICY = regulatory / central-bank / ESG-policy change; GEO = geopolitical or "
    "country risk (war, coup, election, sanctions, sovereign risk); TECH = technology/digital "
    "change (AI, fintech, platforms, semiconductors); SOCIETY = social/cultural change "
    "(population, labor, culture, consumer trends)."
)
# 주제 국가 지시 — full/light 공통.
_PRIMARY_COUNTRY_BLOCK = (
    "\n\nprimary_country — the ISO-3166 alpha-2 code of the country the article is chiefly ABOUT "
    "(its subject), which may differ from the outlet's home country. Example: a Xinhua (Chinese "
    "outlet) story about Indonesia's central bank -> \"ID\". Choose ONE from: "
    + _PRIMARY_COUNTRY_CODES
    + ". These are the ONLY valid codes — never output a country code that is not in this list, "
    "even if you know the article's real subject country precisely. If the subject country is "
    "not in the list (e.g. an Italian bank merger reported by Reuters UK, or a German election "
    "covered by a US outlet), output \"GLOBAL\" — do NOT default to the outlet's own country and "
    "do NOT invent the subject country's own code."
)

_SCORE_FACTORS_BLOCK = (
    "\n\nscore_factors — score each dimension independently as an INTEGER 0-4; do not choose "
    "the same value by default:\n"
    "- directness: 0 unrelated, 1 indirect/global context, 2 relevant to the market, "
    "3 a standalone local-media story squarely about the host market's banking/financial "
    "sector or a market-moving macro event -- e.g. a central bank/regulator grants or revokes "
    "a banking licence, a bank or asset manager's M&A/market expansion, a trade/export/import "
    "data release, a stock-market move, an IPO filing or bond-market development; count these "
    "as 3 even with no KB mention -- 4 names a KB entity or a host authority action directed "
    "at KB specifically.\n"
    "- magnitude: 0 negligible, 1 small, 2 meaningful, 3 market-wide, 4 systemic/sovereign.\n"
    "- urgency: 0 no action horizon, 1 long-term, 2 monitor this week, 3 brief today, "
    "4 immediate response required.\n"
    "- novelty: 0 repeated commentary, 1 routine update, 2 new data/development, "
    "3 new decision/event, 4 unexpected regime change.\n"
    "Python derives the final score from these factors. ai_score is retained only as a fallback, "
    "so assess the four factors carefully and independently."
)

_SCORE_FACTOR_WEIGHTS = {"directness": 8, "magnitude": 6, "urgency": 5, "novelty": 4}


def _checked_title_en(title_en: str, title: str, summary_en: str) -> str:
    """영문 제목의 금액(USD·INR)이 원문 제목·영문 요약과 어긋나면 버린다(export는 원문 제목으로 폴백).
    예: Rp 9.1조(약 $5.7억)를 "$9.1 trillion"으로 통화만 바꿔 쓴 제목."""
    if title_en and numeric_guard.amount_mismatch(" ".join((title, summary_en)), title_en):
        log.warning("영문 제목 금액 불일치 — title_en 버림: %s", title_en)
        return ""
    return title_en


def _checked_title_ko(title_ko: str, title: str, title_en: str, summary_en: str) -> str:
    """한국어 제목의 금액(USD·INR)이 원문 제목·영문 제목·요약과 어긋나면 버린다.
    빈 title_ko는 llm_translate가 금액 검증을 거쳐 다시 채운다(예: Rs 10,000 crore → "1조 루피")."""
    if title_ko and numeric_guard.amount_mismatch(" ".join((title, title_en, summary_en)), title_ko):
        log.warning("제목 금액 불일치 — title_ko 버림: %s", title_ko)
        return ""
    return title_ko


def _score_from_data(data: dict) -> tuple[int | None, dict | None]:
    """4개 품질 차원을 결정적 점수로 합산한다. 구 응답은 ai_score로 호환한다.

    8 + 8D + 6M + 5U + 4N: 전부 2점인 일반 관심기사는 54점, 직접적·중대한
    당일 조치(3,3,3,3)는 77점, 전 차원 최고는 100점이다. directness 가중치는
    2026-10-06에 7→8로 올렸다 — "KB 자체 영업에 영향"까지 요구하면 현지
    은행업·시장 전체를 뒤흔드는 뉴스조차 directness가 낮게 나와 임계(55점) 아래로
    몰렸다(홍콩 수출입 급증 기사가 directness=1로 46점). directness 3을 "현지
    매체가 독립 기사로 다룬 은행업/금융권 사건"으로 넓히고(KB 언급 불필요),
    가중치를 올려 3점 자체가 ACTIVE 문턱을 안정적으로 넘도록 했다.
    """
    raw = data.get("score_factors")
    if isinstance(raw, dict):
        factors = {}
        for key in _SCORE_FACTOR_WEIGHTS:
            value = raw.get(key)
            if type(value) is not int or not 0 <= value <= 4:
                break
            factors[key] = value
        else:
            score = 8 + sum(_SCORE_FACTOR_WEIGHTS[k] * factors[k] for k in factors)
            return max(0, min(100, score)), factors
    try:
        return max(0, min(100, int(data.get("ai_score")))), None
    except (TypeError, ValueError):
        return None, None


def _score_dimensions(factors: dict | None) -> tuple[int | None, int | None]:
    """4차원 원점수에서 시장 중요도와 KB 관련성을 0~100으로 분리한다."""
    if not factors:
        return None, None
    market = round((factors["magnitude"] * 6 + factors["urgency"] * 5
                    + factors["novelty"] * 4) / 60 * 100)
    kb_relevance = factors["directness"] * 25
    return market, kb_relevance


def ensure_columns(conn) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("ai_score",       "ALTER TABLE articles_raw ADD COLUMN ai_score       INTEGER"),
        ("summary_ko",     "ALTER TABLE articles_raw ADD COLUMN summary_ko     TEXT"),
        ("ai_model",       "ALTER TABLE articles_raw ADD COLUMN ai_model       TEXT"),
        ("topics",         "ALTER TABLE articles_raw ADD COLUMN topics         TEXT"),
        # kb_implication(_en): 2026-09-21 신규 생성 중단(UI에서도 제거) — 컬럼은 과거
        # 데이터 호환을 위해 남겨두되 더 이상 채우지 않는다.
        ("kb_implication", "ALTER TABLE articles_raw ADD COLUMN kb_implication TEXT"),
        # 영어 기준본(canonical) — rank 가 채우고, 한국어는 llm_translate 가 번역
        ("summary_en",        "ALTER TABLE articles_raw ADD COLUMN summary_en        TEXT"),
        ("kb_implication_en", "ALTER TABLE articles_raw ADD COLUMN kb_implication_en TEXT"),
        # 본문 추출본(fulltext.py) — 있으면 rank 가 스니펫 대신 본문으로 분석
        ("full_text",         "ALTER TABLE articles_raw ADD COLUMN full_text         TEXT"),
        ("title_ko",          "ALTER TABLE articles_raw ADD COLUMN title_ko          TEXT"),
        # 영어 헤드라인 — EN 모드에서 제목이 한국어로 남던 문제(2026-09-03) 해결용.
        # 원문 title 은 소스 언어(id/zh 등)라 영어 화면에 그대로 쓸 수 없다.
        ("title_en",          "ALTER TABLE articles_raw ADD COLUMN title_en          TEXT"),
        # 이벤트 유형(축 E, 모니터링 전용) — REG/DEAL/INCIDENT CSV, taxonomy.yaml event_types
        ("event_type",        "ALTER TABLE articles_raw ADD COLUMN event_type        TEXT"),
        # 다출처 종합에 실제로 쓰인 소스 목록(JSON [{"t":제목,"u":URL,"src":매체명}, ...]).
        # 클러스터가 SYNTH_MIN_SOURCES 미만이면 NULL(단일 기사, 기존 방식).
        ("source_links",      "ALTER TABLE articles_raw ADD COLUMN source_links      TEXT"),
        ("source_conflict",   "ALTER TABLE articles_raw ADD COLUMN source_conflict   TEXT"),
        ("publisher_name",    "ALTER TABLE articles_raw ADD COLUMN publisher_name    TEXT"),
        # 주제 국가(기사가 '다루는' 국가, ISO2) — 매체 국적(m.primary_country_code)과 구분.
        # 신화통신의 인니 기사: media=CN, primary_country=ID. 2026-09-11 신설(피드백 7).
        ("primary_country",   "ALTER TABLE articles_raw ADD COLUMN primary_country   TEXT"),
        # 점수 설명가능성·캘리브레이션용 4차원 원점수(JSON). 과거 행은 NULL 허용.
        ("ai_score_factors", "ALTER TABLE articles_raw ADD COLUMN ai_score_factors TEXT"),
        ("market_importance", "ALTER TABLE articles_raw ADD COLUMN market_importance INTEGER"),
        ("kb_relevance", "ALTER TABLE articles_raw ADD COLUMN kb_relevance INTEGER"),
    ])


# 요약 문체·다출처 종합 공통 지시 — system_full/system_light 둘 다 뒤에 붙인다.
# 2026-08-26 회의 결정: AI 특유 문체 배제 + 분량 확대(1줄→2~4문장) + 클러스터가
# 여러 개 소스로 오면(run_rank가 "Source N:" 형식으로 나열) 단일 기사 패러프레이즈가
# 아니라 그것들을 종합하라고 명시.
_STYLE_AND_SYNTH_BLOCK = (
    "\n\nWriting style for summary_en — dry newspaper-desk tone, plain facts first:\n"
    "- Write 2-4 sentences. Lead with the concrete fact (who/what/how much/when), not scene-setting.\n"
    "- Ban AI-ish filler and clichés: no \"in a significant move\", \"marks a pivotal moment\", "
    "\"underscores\", \"in today's fast-paced/evolving landscape\", \"stands as a testament\", "
    "\"navigate\", \"bolster\", \"delve\", \"realm\", \"tapestry\", \"game-changer\", \"in conclusion\", "
    "and no meta-commentary about the article itself.\n"
    "- No stacked hedging qualifiers (\"potentially\", \"could possibly\") and no exclamation marks.\n"
    "- If the user message lists MULTIPLE numbered sources about the same event (\"Source 1:\", "
    "\"Source 2:\", ...), synthesize ONE independent summary combining facts from ALL of them — do "
    "not just paraphrase a single source, and do not copy any one source's exact sentence structure "
    "or wording. If only one source is given, summarize that source normally."
)


def _system_prompt() -> str:
    # 원문이 영어이므로 영어를 기준본(canonical)으로 생성 → 한국어는 llm_translate 가 번역.
    return (
        "You are a global intelligence analyst at KB Financial Group. "
        "Analyze one overseas news article and output ONLY this JSON:\n"
        '{"ai_score": (KB business importance fallback, integer 0-100), '
        '"score_factors": {"directness": 0-4, "magnitude": 0-4, "urgency": 0-4, "novelty": 0-4}, '
        '"title_ko": "15자 이내 신문 헤드라인 스타일 한국어 제목", '
        '"title_en": "one-line dry English newspaper headline (max ~70 chars)", '
        '"summary_en": "2-4 sentence English summary", '
        '"topics": ["TOPIC_CODE", ...], '
        '"event_type": ["EVENT_CODE", ...] (0-3, empty list if none apply), '
        '"primary_country": "ISO2 code of the country the article is ABOUT, or GLOBAL"}\n\n'
        "Choose topics ONLY from these codes. Put the PRIMARY topic FIRST — exactly one main category the article is chiefly about; the first item drives filtering, so be decisive. Add at most 2 SECONDARY topics only if clearly relevant (usually 0-1):\n"
        + taxonomy.prompt_reference()
        + "\n\nChoose event_type ONLY from these codes (multiple allowed, max 3; empty if the "
        "article is not about a specific regulatory/sanctions/deal/incident event; use SANCTION for new or expanded sanctions designations, OFAC/EU/UN actions, embargoes, asset freezes, export controls):\n"
        + taxonomy.event_prompt_reference()
        + _TOPIC_DISAMBIG_BLOCK
        + _PRIMARY_COUNTRY_BLOCK
        + _SCORE_FACTORS_BLOCK
        + "\n\nai_score rubric (fallback only — used if score_factors is missing/invalid; keep it "
        "consistent with the score_factors directions above, do not contradict them). Use the "
        "FULL 0-100 range and DIFFERENTIATE: most routine articles belong below 55, and a typical "
        "day yields only a handful of 80+ items.\n"
        "85-100  a KB entity or host regulator names KB directly, OR a systemic/sovereign shock "
        "hits the host market right now (capital controls, sovereign downgrade, FX convertibility "
        "crisis, a bank run). Rare — only a few per day across all markets.\n"
        "65-84   a standalone local-media story squarely about the host market's banking/financial "
        "sector or a market-wide move — a central bank/regulator grants or revokes a licence, a "
        "bank/asset-manager M&A or expansion, a trade/export/import data release, a large stock-"
        "market move, an IPO filing, a bond-market development — even with NO KB mention.\n"
        "45-64   worth knowing, no near-term action — a foreign central bank's path (Fed/BoJ/ECB) "
        "reaching this market only indirectly, routine currency fluctuation, a general sector/"
        "policy trend.\n"
        "25-44   general or global trends only indirectly relevant to this market; developed-"
        "market macro far away; broad industry research.\n"
        "0-24    sports, entertainment, unrelated crime, non-financial local events.\n"
        "Anti-clustering rule: if you are about to score 60-69, re-check — is it truly a standalone "
        "banking/market story (→65+) or just general context (→45-60)? Avoid defaulting to the middle."
        + _STYLE_AND_SYNTH_BLOCK
    )


# KB 미진출국(config.NON_PRESENCE_COUNTRIES) 전용 경량 프롬프트(거점 맥락 없이 채점).
def _system_prompt_light() -> str:
    return (
        "You are a global intelligence analyst at KB Financial Group. KB has NO branch in "
        "this market — you are scanning it only because Korean competitor banks operate there. "
        "Analyze one news article and output ONLY this JSON:\n"
        '{"ai_score": (importance fallback, integer 0-100), '
        '"score_factors": {"directness": 0-4, "magnitude": 0-4, "urgency": 0-4, "novelty": 0-4}, '
        '"title_ko": "15자 이내 신문 헤드라인 스타일 한국어 제목", '
        '"title_en": "one-line dry English newspaper headline (max ~70 chars)", '
        '"summary_en": "2-4 sentence English summary", '
        '"topics": ["TOPIC_CODE", ...], '
        '"event_type": ["EVENT_CODE", ...] (0-3, empty list if none apply), '
        '"primary_country": "ISO2 code of the country the article is ABOUT, or GLOBAL"}\n\n'
        "Choose topics ONLY from these codes. Put the PRIMARY topic FIRST — exactly one main category the article is chiefly about; the first item drives filtering, so be decisive. Add at most 2 SECONDARY topics only if clearly relevant (usually 0-1):\n"
        + taxonomy.prompt_reference()
        + "\n\nChoose event_type ONLY from these codes (multiple allowed, max 3; empty if the "
        "article is not about a specific regulatory/sanctions/deal/incident event; use SANCTION for new or expanded sanctions designations, OFAC/EU/UN actions, embargoes, asset freezes, export controls):\n"
        + taxonomy.event_prompt_reference()
        + _TOPIC_DISAMBIG_BLOCK
        + _PRIMARY_COUNTRY_BLOCK
        + _SCORE_FACTORS_BLOCK
        + "\n\nai_score rubric — score general macro/financial materiality for a market with "
        "no KB entity (assign the highest tier that applies):\n"
        "75-100  Major sovereign/macro event: central bank decision, currency crisis, "
        "sovereign rating action, major bank failure or large M&A.\n"
        "50-74   Significant market-moving financial/economic news: rate moves, major bank "
        "earnings, regulatory change, large FX swings.\n"
        "25-49   Useful background: routine economic data, minor market moves, general "
        "fintech/ESG news.\n"
        "0-24    Noise: sports, entertainment, crime, local news with no macro/financial "
        "relevance."
        + _STYLE_AND_SYNTH_BLOCK
    )


def _cluster_sources(conn, article_id: int, exclude_media: str) -> list:
    """article_id를 duplicate_of로 가리키는 형제 기사 중 대표(exclude_media)와
    서로 다른 매체만, 매체당 1건(고품질·최신 우선) 최대 SYNTH_MAX_SOURCES-1개.

    같은 매체 기사가 제목만 바뀐 채 여러 건 걸리는 경우(라이브 블로그 업데이트 등)를
    별개 언론사로 잘못 세지 않기 위한 안전장치 — "3개 이상 서로 다른 언론사 종합"이라는
    취지를 실제로 지키려면 클러스터 크기가 아니라 distinct 매체 수로 판단해야 한다."""
    rows = conn.execute(
        f"""SELECT a.title, a.summary, a.full_text, a.link,
                  {db.publisher_expr()} AS media_name, m.tier
           FROM articles_raw a JOIN media_sources m ON m.source_id = a.source_id
           WHERE a.duplicate_of = ?
           ORDER BY m.tier ASC, a.published_at DESC""",
        (article_id,),
    ).fetchall()
    seen = {(exclude_media or "").strip().lower()}
    out = []
    for r in rows:
        key = (r["media_name"] or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= config.SYNTH_MAX_SOURCES - 1:
            break
    return out


def _source_snippet(row) -> str:
    body = (row["full_text"] or "").strip() or (row["summary"] or "").strip()
    return body[:config.SYNTH_SNIPPET_MAXLEN]


def run_rank(conn, provider: LLMProvider | None = None,
             limit: int | None = None, days: int | None = None,
             use_batch: bool | None = None, redo_days: int | None = None,
             article_ids: list[int] | None = None) -> dict:
    """prefilter keep·미분석 기사를 LLM으로 분석.

    days: 지정 시 최근 N일 게시 기사만 처리(전체 백로그 대신 최신치만 — 비용 절감).
    use_batch: None=배치(50% 할인, 기본) / False=동기 호출(디버깅).
    article_ids: 본문을 새로 확보한 기존 채점 기사만 안전하게 재분석할 때 사용한다.

    다출처 종합: keyword_filter.run_dedup()이 만든 duplicate_of 클러스터(대표+형제,
    "같은 사건, 다른 매체")가 config.SYNTH_MIN_SOURCES 이상이면 형제 기사들도 함께
    프롬프트에 넣어 여러 출처를 종합한 요약을 생성한다. 미만이면 기존 단일기사 방식.
    """
    ensure_columns(conn)
    provider = provider or get_provider("smart", use_batch=use_batch)
    limit = limit or config.RANK_LIMIT
    system_full = _system_prompt()
    system_light = _system_prompt_light()

    if article_ids is not None:
        ids = list(dict.fromkeys(int(x) for x in article_ids))
        placeholders = ",".join("?" for _ in ids) or "NULL"
        date_clause, params = "", ids
        rank_cond = f"a.article_id IN ({placeholders}) AND a.duplicate_of IS NULL"
    elif redo_days:
        # 재랭킹(소급): 창 안의 노출(ACTIVE, ai_score>=임계) 기사만 다시 채점한다.
        # 카테고리 재정의(5)·primary_country 신설(7)을 기존분에 반영할 때만 사용.
        # 미채점 백로그(ai_score IS NULL)는 제외되어 대상이 ACTIVE로 한정된다(비용 통제).
        date_clause, params = db.days_clause_now(redo_days)
        rank_cond = (f"a.ai_score >= {int(config.AI_SCORE_ACTIVE_THRESHOLD)} "
                     "AND a.duplicate_of IS NULL")
    else:
        date_clause, params = db.days_clause_now(days)
        rank_cond = "a.ai_score IS NULL"

    rows = conn.execute(
        f"""
        SELECT a.article_id, a.title, a.summary, a.full_text, a.link,
               m.primary_country_code AS cc, {db.publisher_expr()} AS media_name
        FROM articles_raw a
        JOIN media_sources m ON m.source_id = a.source_id
        WHERE a.llm_prefilter = 'keep'
          AND {rank_cond}{date_clause}
        ORDER BY a.filter_score DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    stats = dict(total=len(rows), ranked=0, active=0, synthesized=0, failed=0,
                 source_conflicts=0)

    # 요청 일괄 구성 → 배치 제출(50% 할인) 또는 동기 폴백
    requests, row_by_id, source_links_by_id, source_conflict_by_id = [], {}, {}, {}
    for i, r in enumerate(rows):
        cid = str(i)
        siblings = _cluster_sources(conn, r["article_id"], r["media_name"])
        total_n = 1 + len(siblings)   # distinct 매체 수(대표 포함)

        if total_n >= config.SYNTH_MIN_SOURCES:
            blocks, links = [], []
            sources = [r] + list(siblings)
            conflict = numeric_guard.source_amount_conflicts([
                f"{src['title']}\n{_source_snippet(src)}" for src in sources
            ])
            for n, src in enumerate(sources, start=1):
                blocks.append(
                    f"Source {n} ({src['media_name']}): {src['title']}\n{_source_snippet(src)}"
                )
                links.append({"t": src["title"][:100], "u": src["link"], "src": src["media_name"]})
            if conflict:
                # 서로 다른 단일 금액을 확정적으로 섞지 않는다. 대표 기사만 분석하고
                # 충돌값을 기록해 사람이 확인하거나 후속 출처에서 해소할 수 있게 한다.
                body = (r["full_text"] or "").strip()
                body_line = f"본문: {body[:config.RANK_BODY_MAXLEN]}" if body \
                    else f"요약: {(r['summary'] or '')[:1200]}"
                content_block = f"제목: {r['title']}\n{body_line}"
                source_links_by_id[cid] = None
                source_conflict_by_id[cid] = conflict
                stats["source_conflicts"] += 1
            else:
                content_block = "\n\n".join(blocks)
                source_links_by_id[cid] = links
                source_conflict_by_id[cid] = None
                stats["synthesized"] += 1
        else:
            # 본문 추출본이 있으면 본문으로, 없으면 RSS 스니펫으로 (자동 폴백)
            body = (r["full_text"] or "").strip()
            body_line = f"본문: {body[:config.RANK_BODY_MAXLEN]}" if body \
                else f"요약: {(r['summary'] or '')[:1200]}"
            content_block = f"제목: {r['title']}\n{body_line}"
            source_links_by_id[cid] = None
            source_conflict_by_id[cid] = None

        if config.is_presence(r["cc"]):
            ctx = kb_network.context_for(r["cc"])
            system = system_full
            user = f"[거점 맥락: {ctx}]\n매체: {r['media_name']}  국가: {r['cc']}\n{content_block}"
        else:
            # KB 미진출국 — 거점 맥락 없이 경량 프롬프트
            system = system_light
            user = f"매체: {r['media_name']}  국가: {r['cc']}\n{content_block}"
        requests.append((cid, system, user, 700))
        row_by_id[cid] = r

    results = provider.complete_json_batch(requests) if requests else {}

    cur = conn.cursor()
    for cid, r in row_by_id.items():
        data = results.get(cid) or {}
        # ── 폴백 포함 파싱 ──
        score, score_factors = _score_from_data(data)
        if score is None:
            # 빈/무효 응답을 임의의 중간 점수로 확정하면 재시도 기회를 잃는다.
            # 기존 재랭킹 값도 그대로 보존하고 다음 실행에서 다시 처리한다.
            stats["failed"] += 1
            log.warning("랭커 응답 무효 — 저장 안 함(article_id=%s)", r["article_id"])
            continue
        market_importance, kb_relevance = _score_dimensions(score_factors)
        title_ko = str(data.get("title_ko") or "")[:60]
        summary_en = str(data.get("summary_en") or "")[:1500]
        topics = taxonomy.validate(data.get("topics", []))
        if not topics:
            topics = taxonomy.seed_candidates(f"{r['title']} {r['summary'] or ''}")
        event_types = taxonomy.event_validate(data.get("event_type", []))
        if not event_types:
            event_types = taxonomy.event_seed_candidates(f"{r['title']} {r['summary'] or ''}")
        primary_country = _valid_primary_country(data.get("primary_country"))
        links = source_links_by_id.get(cid)
        source_links = json.dumps(links, ensure_ascii=False) if links else None
        conflict = source_conflict_by_id.get(cid)
        source_conflict = json.dumps(conflict, ensure_ascii=False) if conflict else None

        title_en = _checked_title_en(str(data.get("title_en") or "")[:300], r["title"] or "", summary_en)
        title_ko = _checked_title_ko(title_ko, r["title"] or "", title_en, summary_en)

        cur.execute(
            """UPDATE articles_raw
               SET ai_score = ?, ai_score_factors = ?, market_importance = ?, kb_relevance = ?,
                   title_ko = ?, title_en = ?, summary_en = ?, topics = ?,
                   event_type = ?, source_links = ?, source_conflict = ?,
                   primary_country = ?, ai_model = ?
               WHERE article_id = ?""",
            (score, json.dumps(score_factors, ensure_ascii=False) if score_factors else None,
             market_importance, kb_relevance,
             title_ko, title_en, summary_en, ",".join(topics), ",".join(event_types),
             source_links, source_conflict, primary_country, provider.model_id, r["article_id"]),
        )
        if redo_days or article_ids is not None:
            # 영어 기준본이 바뀌었으므로 한국어 번역·모달요약을 무효화 → translate/expand 재생성
            cur.execute(
                "UPDATE articles_raw SET summary_ko = NULL, kb_implication = NULL, "
                "expanded_summary = NULL, expanded_summary_en = NULL WHERE article_id = ?",
                (r["article_id"],),
            )
        stats["ranked"] += 1
        if score >= config.AI_SCORE_ACTIVE_THRESHOLD:
            stats["active"] += 1
    conn.commit()

    log.info(
        "랭킹 완료 — 처리=%d  실패보존=%d  ACTIVE(>=%d)=%d  다출처종합=%d  출처충돌=%d",
        stats["ranked"], stats["failed"], config.AI_SCORE_ACTIVE_THRESHOLD,
        stats["active"], stats["synthesized"], stats["source_conflicts"],
    )
    return stats


def backfill_event_types(conn) -> dict:
    """event_type 컬럼 도입 이전에 이미 채점된 기사(ai_score 有·event_type 無)를
    시드 키워드 매칭만으로 채운다(LLM 호출 없음 — 1회성 마이그레이션용).
    이후 새로 랭킹되는 기사는 run_rank()가 AI로 직접 채운다."""
    ensure_columns(conn)
    rows = conn.execute(
        """SELECT article_id, title, summary FROM articles_raw
           WHERE ai_score IS NOT NULL AND (event_type IS NULL OR event_type = '')"""
    ).fetchall()

    cur = conn.cursor()
    filled = 0
    for r in rows:
        event_types = taxonomy.event_seed_candidates(f"{r['title']} {r['summary'] or ''}")
        if not event_types:
            continue
        cur.execute(
            "UPDATE articles_raw SET event_type = ? WHERE article_id = ?",
            (",".join(event_types), r["article_id"]),
        )
        filled += 1
    conn.commit()
    log.info("event_type 백필(시드 매칭) — 대상=%d  채움=%d", len(rows), filled)
    return {"total": len(rows), "filled": filled}
