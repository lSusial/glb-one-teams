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
        # kb_implication: UI 'KB 시사점' 담을 신규 컬럼
        ("kb_implication", "ALTER TABLE articles_raw ADD COLUMN kb_implication TEXT"),
    ])


def _system_prompt() -> str:
    return (
        "당신은 KB금융그룹 글로벌 인텔리전스 애널리스트다. 해외 뉴스 1건을 분석해 "
        "아래 JSON만 출력한다.\n"
        '{"ai_score": (KB 경영 중요도 0~100 정수), '
        '"summary_ko": "한국어 2~3문장 요약", '
        '"topics": ["주제코드", ...], '
        '"kb_implication": "KB 거점 관점의 시사점·액션 1~2문장"}\n\n'
        "topics 는 아래 코드에서만 고른다(복수 가능, 최대 3개):\n"
        + taxonomy.prompt_reference()
        + "\n\nai_score 기준: 거점 여신·리스크·조달에 직접 영향=80+, "
        "간접·배경=40~60, 약함=40미만.\n"
        "kb_implication 은 기사 내용 범위에서만 작성하고 근거 없는 추측은 피한다."
    )


def run_rank(conn, provider: LLMProvider | None = None, limit: int | None = None) -> dict:
    """prefilter keep·미분석 기사를 LLM으로 분석."""
    ensure_columns(conn)
    provider = provider or get_provider("smart")
    limit = limit or config.RANK_LIMIT
    system = _system_prompt()

    rows = conn.execute(
        """
        SELECT a.article_id, a.title, a.summary, m.primary_country_code AS cc, m.media_name
        FROM articles_raw a
        JOIN media_sources m ON m.source_id = a.source_id
        WHERE a.llm_prefilter = 'keep'
          AND a.ai_score IS NULL
        ORDER BY a.filter_score DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    stats = dict(total=len(rows), ranked=0, active=0)
    cur = conn.cursor()
    for r in rows:
        ctx = kb_network.context_for(r["cc"])
        user = (
            f"[거점 맥락: {ctx}]\n"
            f"매체: {r['media_name']}  국가: {r['cc']}\n"
            f"제목: {r['title']}\n"
            f"요약: {(r['summary'] or '')[:1200]}"
        )
        data = provider.complete_json(system, user, max_tokens=600)

        # ── 폴백 포함 파싱 ──
        try:
            score = max(0, min(100, int(data.get("ai_score"))))
        except (TypeError, ValueError):
            score = 50
        summary_ko = str(data.get("summary_ko") or "")[:1500]
        topics = taxonomy.validate(data.get("topics", []))
        if not topics:
            topics = taxonomy.seed_candidates(f"{r['title']} {r['summary'] or ''}")
        kb_impl = str(data.get("kb_implication") or "")[:1000]

        cur.execute(
            """UPDATE articles_raw
               SET ai_score = ?, summary_ko = ?, topics = ?, kb_implication = ?, ai_model = ?
               WHERE article_id = ?""",
            (score, summary_ko, ",".join(topics), kb_impl, provider.model_id, r["article_id"]),
        )
        conn.commit()
        stats["ranked"] += 1
        if score >= config.AI_SCORE_ACTIVE_THRESHOLD:
            stats["active"] += 1

    log.info(
        "랭킹 완료 — 처리=%d  ACTIVE(>=%d)=%d",
        stats["ranked"], config.AI_SCORE_ACTIVE_THRESHOLD, stats["active"],
    )
    return stats
