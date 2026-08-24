"""
AI 분석 (llm_ranker.py)

prefilter 를 통과(keep)한 기사에 중요도·요약·주제·KB 시사점을 부여한다.

입력: llm_prefilter='keep' AND ai_score IS NULL
순서: filter_score DESC
출력:
  - ai_score        : KB 경영 중요도 (0~100). >= AI_SCORE_ACTIVE_THRESHOLD → UI ACTIVE
  - summary_ko      : 한국어 요약 (UI q)
  - topics          : taxonomy 코드 CSV (UI 카테고리 c)
  - kb_implication  : KB 거점 관점 시사점 (UI k) — 신규 컬럼
  - ai_model        : 생성 프로바이더:모델 식별자

* 분석 모델(role='smart') 사용. topics 는 taxonomy 코드로 검증, 비면 시드 매칭 폴백.
"""
from __future__ import annotations

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
    ])


def _system_prompt() -> str:
    # 원문이 영어이므로 영어를 기준본(canonical)으로 생성 → 한국어는 llm_translate 가 번역.
    return (
        "You are a global intelligence analyst at KB Financial Group. "
        "Analyze one overseas news article and output ONLY this JSON:\n"
        '{"ai_score": (KB business importance, integer 0-100), '
        '"title_ko": "15자 이내 신문 헤드라인 스타일 한국어 제목", '
        '"summary_en": "2-3 sentence English summary", '
        '"topics": ["TOPIC_CODE", ...], '
        '"kb_implication_en": "1-2 sentence KB-perspective implication/action, in English"}\n\n'
        "Choose topics ONLY from these codes (multiple allowed, max 3):\n"
        + taxonomy.prompt_reference()
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
        '"summary_en": "2-3 sentence English summary", '
        '"topics": ["TOPIC_CODE", ...]}\n\n'
        "Choose topics ONLY from these codes (multiple allowed, max 3):\n"
        + taxonomy.prompt_reference()
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
    )


def run_rank(conn, provider: LLMProvider | None = None,
             limit: int | None = None, days: int | None = None,
             use_batch: bool | None = None) -> dict:
    """prefilter keep·미분석 기사를 LLM으로 분석.

    days: 지정 시 최근 N일 게시 기사만 처리(전체 백로그 대신 최신치만 — 비용 절감).
    use_batch: None=배치(50% 할인, 기본) / False=동기 호출(디버깅).
    """
    ensure_columns(conn)
    provider = provider or get_provider("smart", use_batch=use_batch)
    limit = limit or config.RANK_LIMIT
    system_full = _system_prompt()
    system_light = _system_prompt_light()

    date_clause, params = db.days_clause_now(days)

    rows = conn.execute(
        f"""
        SELECT a.article_id, a.title, a.summary, a.full_text,
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

    stats = dict(total=len(rows), ranked=0, active=0)

    # 요청 일괄 구성 → 배치 제출(50% 할인) 또는 동기 폴백
    requests, row_by_id = [], {}
    for i, r in enumerate(rows):
        cid = str(i)
        # 본문 추출본이 있으면 본문으로, 없으면 RSS 스니펫으로 (자동 폴백)
        body = (r["full_text"] or "").strip()
        if body:
            body_line = f"본문: {body[:config.RANK_BODY_MAXLEN]}"
        else:
            body_line = f"요약: {(r['summary'] or '')[:1200]}"
        if config.is_presence(r["cc"]):
            ctx = kb_network.context_for(r["cc"])
            system = system_full
            user = (
                f"[거점 맥락: {ctx}]\n"
                f"매체: {r['media_name']}  국가: {r['cc']}\n"
                f"제목: {r['title']}\n"
                f"{body_line}"
            )
        else:
            # KB 미진출국 — 거점 맥락 없이 경량 프롬프트(kb_implication_en 생략)
            system = system_light
            user = (
                f"매체: {r['media_name']}  국가: {r['cc']}\n"
                f"제목: {r['title']}\n"
                f"{body_line}"
            )
        requests.append((cid, system, user, 600))
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
        kb_impl_en = str(data.get("kb_implication_en") or "")[:1000]

        cur.execute(
            """UPDATE articles_raw
               SET ai_score = ?, title_ko = ?, summary_en = ?, topics = ?, kb_implication_en = ?, ai_model = ?
               WHERE article_id = ?""",
            (score, title_ko, summary_en, ",".join(topics), kb_impl_en, provider.model_id, r["article_id"]),
        )
        stats["ranked"] += 1
        if score >= config.AI_SCORE_ACTIVE_THRESHOLD:
            stats["active"] += 1
    conn.commit()

    log.info(
        "랭킹 완료 — 처리=%d  ACTIVE(>=%d)=%d",
        stats["ranked"], config.AI_SCORE_ACTIVE_THRESHOLD, stats["active"],
    )
    return stats
