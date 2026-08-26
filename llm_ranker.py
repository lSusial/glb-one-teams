"""
AI 분석 (llm_ranker.py)

prefilter 를 통과(keep)한 기사에 중요도·요약·주제·KB 시사점을 부여한다.

입력: llm_prefilter='keep' AND ai_score IS NULL
순서: filter_score DESC
출력:
  - ai_score        : KB 경영 중요도 (0~100). >= AI_SCORE_ACTIVE_THRESHOLD → UI ACTIVE
  - summary_ko      : 한국어 요약 (UI q)
  - topics          : taxonomy 코드 CSV (UI 카테고리 c, 축 C — 현지언론 필터)
  - event_type      : taxonomy event_types 코드 CSV (모니터링 탭 전용, 축 E)
  - kb_implication  : KB 거점 관점 시사점 (UI k) — 신규 컬럼
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

import config
import db
import kb_network
import taxonomy
from llm_provider import LLMProvider, get_provider

log = logging.getLogger("llm_ranker")


def ensure_columns(conn) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("ai_score",       "ALTER TABLE articles_raw ADD COLUMN ai_score       INTEGER"),
        ("summary_ko",     "ALTER TABLE articles_raw ADD COLUMN summary_ko     TEXT"),
        ("ai_model",       "ALTER TABLE articles_raw ADD COLUMN ai_model       TEXT"),
        ("topics",         "ALTER TABLE articles_raw ADD COLUMN topics         TEXT"),
        # kb_implication: UI 'KB 시사점' (한국어, llm_translate 에서 채움)
        ("kb_implication", "ALTER TABLE articles_raw ADD COLUMN kb_implication TEXT"),
        # 영어 기준본(canonical) — rank 가 채우고, 한국어는 llm_translate 가 번역
        ("summary_en",        "ALTER TABLE articles_raw ADD COLUMN summary_en        TEXT"),
        ("kb_implication_en", "ALTER TABLE articles_raw ADD COLUMN kb_implication_en TEXT"),
        # 본문 추출본(fulltext.py) — 있으면 rank 가 스니펫 대신 본문으로 분석
        ("full_text",         "ALTER TABLE articles_raw ADD COLUMN full_text         TEXT"),
        ("title_ko",          "ALTER TABLE articles_raw ADD COLUMN title_ko          TEXT"),
        # 이벤트 유형(축 E, 모니터링 전용) — REG/DEAL/INCIDENT CSV, taxonomy.yaml event_types
        ("event_type",        "ALTER TABLE articles_raw ADD COLUMN event_type        TEXT"),
        # 다출처 종합에 실제로 쓰인 소스 목록(JSON [{"t":제목,"u":URL,"src":매체명}, ...]).
        # 클러스터가 SYNTH_MIN_SOURCES 미만이면 NULL(단일 기사, 기존 방식).
        ("source_links",      "ALTER TABLE articles_raw ADD COLUMN source_links      TEXT"),
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
        '{"ai_score": (KB business importance, integer 0-100), '
        '"title_ko": "15자 이내 신문 헤드라인 스타일 한국어 제목", '
        '"summary_en": "2-4 sentence English summary", '
        '"topics": ["TOPIC_CODE", ...], '
        '"event_type": ["EVENT_CODE", ...] (0-3, empty list if none apply), '
        '"kb_implication_en": "1-2 sentence KB-perspective implication/action, in English"}\n\n'
        "Choose topics ONLY from these codes (multiple allowed, max 3):\n"
        + taxonomy.prompt_reference()
        + "\n\nChoose event_type ONLY from these codes (multiple allowed, max 3; empty if the "
        "article is not about a specific regulatory/deal/incident event):\n"
        + taxonomy.event_prompt_reference()
        + "\n\nai_score rubric — assign the highest tier that applies:\n"
        "75-100  DIRECT · IMMEDIATE: KB branch/subsidiary directly affected today.\n"
        "  Examples: host-country central bank rate decision, capital controls imposed,\n"
        "  KB entity under regulatory action/sanction, sovereign rating downgrade\n"
        "  in a KB-presence market, FX convertibility crisis.\n"
        "50-74   CONTEXTUAL · IMPORTANT: A KB banker at this hub should read this within the day.\n"
        "  Examples: major currency move in a KB country (VND/IDR/MMK/CNY/INR…),\n"
        "  Fed/BoJ/ECB rate path shift, oil shock affecting local inflation,\n"
        "  geopolitical event in KB market (coup, sanctions risk, capital-flow restriction),\n"
        "  banking-sector M&A or stress in KB geography, significant trade/tariff change.\n"
        "25-49   BACKGROUND · CONTEXT: Useful context; no near-term KB action needed.\n"
        "  Examples: global fintech/ESG trends, developed-market macro that only\n"
        "  indirectly reaches KB geographies, general industry research.\n"
        "0-24    NOISE / UNRELATED: Sports, entertainment, stock tips for unrelated sectors,\n"
        "  crime gossip, local events with no macro or financial relevance to KB operations.\n"
        "Write kb_implication_en strictly within the article content; avoid unfounded speculation."
        + _STYLE_AND_SYNTH_BLOCK
    )


# KB 미진출국(config.NON_PRESENCE_COUNTRIES) 전용 경량 프롬프트 — kb_implication_en
# 필드를 아예 요청하지 않는다(거점이 없어 "KB 시사점"이 성립하지 않음 + 토큰 절감).
def _system_prompt_light() -> str:
    return (
        "You are a global intelligence analyst at KB Financial Group. KB has NO branch in "
        "this market — you are scanning it only because Korean competitor banks operate there. "
        "Analyze one news article and output ONLY this JSON:\n"
        '{"ai_score": (importance, integer 0-100), '
        '"title_ko": "15자 이내 신문 헤드라인 스타일 한국어 제목", '
        '"summary_en": "2-4 sentence English summary", '
        '"topics": ["TOPIC_CODE", ...], '
        '"event_type": ["EVENT_CODE", ...] (0-3, empty list if none apply)}\n\n'
        "Choose topics ONLY from these codes (multiple allowed, max 3):\n"
        + taxonomy.prompt_reference()
        + "\n\nChoose event_type ONLY from these codes (multiple allowed, max 3; empty if the "
        "article is not about a specific regulatory/deal/incident event):\n"
        + taxonomy.event_prompt_reference()
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
        """SELECT a.title, a.summary, a.full_text, a.link, m.media_name, m.tier
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
             use_batch: bool | None = None) -> dict:
    """prefilter keep·미분석 기사를 LLM으로 분석.

    days: 지정 시 최근 N일 게시 기사만 처리(전체 백로그 대신 최신치만 — 비용 절감).
    use_batch: None=배치(50% 할인, 기본) / False=동기 호출(디버깅).

    다출처 종합: keyword_filter.run_dedup()이 만든 duplicate_of 클러스터(대표+형제,
    "같은 사건, 다른 매체")가 config.SYNTH_MIN_SOURCES 이상이면 형제 기사들도 함께
    프롬프트에 넣어 여러 출처를 종합한 요약을 생성한다. 미만이면 기존 단일기사 방식.
    """
    ensure_columns(conn)
    provider = provider or get_provider("smart", use_batch=use_batch)
    limit = limit or config.RANK_LIMIT
    system_full = _system_prompt()
    system_light = _system_prompt_light()

    date_clause, params = db.days_clause_now(days)

    rows = conn.execute(
        f"""
        SELECT a.article_id, a.title, a.summary, a.full_text, a.link,
               m.primary_country_code AS cc, m.media_name
        FROM articles_raw a
        JOIN media_sources m ON m.source_id = a.source_id
        WHERE a.llm_prefilter = 'keep'
          AND a.ai_score IS NULL{date_clause}
        ORDER BY a.filter_score DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    stats = dict(total=len(rows), ranked=0, active=0, synthesized=0)

    # 요청 일괄 구성 → 배치 제출(50% 할인) 또는 동기 폴백
    requests, row_by_id, source_links_by_id = [], {}, {}
    for i, r in enumerate(rows):
        cid = str(i)
        siblings = _cluster_sources(conn, r["article_id"], r["media_name"])
        total_n = 1 + len(siblings)   # distinct 매체 수(대표 포함)

        if total_n >= config.SYNTH_MIN_SOURCES:
            blocks, links = [], []
            for n, src in enumerate([r] + list(siblings), start=1):
                blocks.append(
                    f"Source {n} ({src['media_name']}): {src['title']}\n{_source_snippet(src)}"
                )
                links.append({"t": src["title"][:100], "u": src["link"], "src": src["media_name"]})
            content_block = "\n\n".join(blocks)
            source_links_by_id[cid] = links
            stats["synthesized"] += 1
        else:
            # 본문 추출본이 있으면 본문으로, 없으면 RSS 스니펫으로 (자동 폴백)
            body = (r["full_text"] or "").strip()
            body_line = f"본문: {body[:config.RANK_BODY_MAXLEN]}" if body \
                else f"요약: {(r['summary'] or '')[:1200]}"
            content_block = f"제목: {r['title']}\n{body_line}"
            source_links_by_id[cid] = None

        if config.is_presence(r["cc"]):
            ctx = kb_network.context_for(r["cc"])
            system = system_full
            user = f"[거점 맥락: {ctx}]\n매체: {r['media_name']}  국가: {r['cc']}\n{content_block}"
        else:
            # KB 미진출국 — 거점 맥락 없이 경량 프롬프트(kb_implication_en 생략)
            system = system_light
            user = f"매체: {r['media_name']}  국가: {r['cc']}\n{content_block}"
        requests.append((cid, system, user, 700))
        row_by_id[cid] = r

    results = provider.complete_json_batch(requests) if requests else {}

    cur = conn.cursor()
    for cid, r in row_by_id.items():
        data = results.get(cid) or {}
        # ── 폴백 포함 파싱 ──
        try:
            score = max(0, min(100, int(data.get("ai_score"))))
        except (TypeError, ValueError):
            score = 50
        title_ko = str(data.get("title_ko") or "")[:60]
        summary_en = str(data.get("summary_en") or "")[:1500]
        topics = taxonomy.validate(data.get("topics", []))
        if not topics:
            topics = taxonomy.seed_candidates(f"{r['title']} {r['summary'] or ''}")
        event_types = taxonomy.event_validate(data.get("event_type", []))
        if not event_types:
            event_types = taxonomy.event_seed_candidates(f"{r['title']} {r['summary'] or ''}")
        kb_impl_en = str(data.get("kb_implication_en") or "")[:1000]
        links = source_links_by_id.get(cid)
        source_links = json.dumps(links, ensure_ascii=False) if links else None

        cur.execute(
            """UPDATE articles_raw
               SET ai_score = ?, title_ko = ?, summary_en = ?, topics = ?, event_type = ?,
                   kb_implication_en = ?, source_links = ?, ai_model = ?
               WHERE article_id = ?""",
            (score, title_ko, summary_en, ",".join(topics), ",".join(event_types),
             kb_impl_en, source_links, provider.model_id, r["article_id"]),
        )
        stats["ranked"] += 1
        if score >= config.AI_SCORE_ACTIVE_THRESHOLD:
            stats["active"] += 1
    conn.commit()

    log.info(
        "랭킹 완료 — 처리=%d  ACTIVE(>=%d)=%d  다출처종합=%d",
        stats["ranked"], config.AI_SCORE_ACTIVE_THRESHOLD, stats["active"], stats["synthesized"],
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
