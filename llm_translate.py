"""
번역 (llm_translate.py)

영어 기준본(summary_en·kb_implication_en)을 한국어(summary_ko·kb_implication)로 번역.
원문이 대부분 영어라 rank 는 영어로 분석·요약(canonical)하고, 이 단계가 한국어를 채운다.

원칙:
  - 대상은 '화면에 표시되는' 기사(ACTIVE, ai_score>=임계)만 → 비용 최소화
  - 이미 양 언어가 있으면 스킵(캐시) → 기사당 1회
  - 레거시(한국어만 있고 영어 없음)는 역방향(KO→EN)도 채워 토글이 양쪽 동작
  - 저비용 모델(role='fast', Haiku)

* 향후 현지어(인니어 등)는 이 모듈에 타깃 언어만 추가하면 영어 기준본에서 fan-out.
"""
from __future__ import annotations

import logging

import config
import db
from llm_provider import LLMProvider, get_provider

log = logging.getLogger("llm_translate")

_SYS = {
    "ko": ("Translate the 'summary' and 'kb_implication' below into natural Korean "
           "(financial/business register). Keep KB branch/entity names (예: 뉴욕지점, 프라삭은행). "
           'Output ONLY JSON: {"summary": "...", "kb_implication": "..."}'),
    "en": ("Translate the 'summary' and 'kb_implication' below into natural English "
           "(financial/business register). Keep KB branch/entity names. "
           'Output ONLY JSON: {"summary": "...", "kb_implication": "..."}'),
}


def ensure_columns(conn) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("summary_en",        "ALTER TABLE articles_raw ADD COLUMN summary_en        TEXT"),
        ("kb_implication_en", "ALTER TABLE articles_raw ADD COLUMN kb_implication_en TEXT"),
    ])


def _translate(provider, target, summary, kb):
    user = f"summary: {(summary or '')[:1500]}\nkb_implication: {(kb or '')[:1000]}"
    data = provider.complete_json(_SYS[target], user, max_tokens=600)
    return str(data.get("summary") or "")[:1500], str(data.get("kb_implication") or "")[:1000]


def run_translate(conn, provider: LLMProvider | None = None,
                  limit: int | None = None, days: int | None = None) -> dict:
    """표시분(ACTIVE) 중 한쪽 언어가 비어있는 기사를 번역해 양 언어를 채운다."""
    ensure_columns(conn)
    provider = provider or get_provider("fast")   # 저비용(Haiku)
    limit = limit or 200

    date_clause, params = "", []
    if days:
        date_clause = " AND substr(published_at, 1, 10) >= date('now', ?)"
        params.append(f"-{int(days)} days")

    rows = conn.execute(
        f"""
        SELECT article_id, summary_en, kb_implication_en, summary_ko, kb_implication
        FROM articles_raw
        WHERE ai_score >= ? AND duplicate_of IS NULL
          AND ( (COALESCE(summary_en,'')  <> '' AND COALESCE(summary_ko,'') = '')
             OR (COALESCE(summary_ko,'')  <> '' AND COALESCE(summary_en,'') = '') )
          {date_clause}
        ORDER BY ai_score DESC
        LIMIT ?
        """,
        (config.AI_SCORE_ACTIVE_THRESHOLD, *params, limit),
    ).fetchall()

    stats = dict(total=len(rows), ko=0, en=0)
    cur = conn.cursor()
    for r in rows:
        has_en = bool((r["summary_en"] or "").strip())
        has_ko = bool((r["summary_ko"] or "").strip())
        if has_en and not has_ko:                       # EN(기준본) → KO
            s, k = _translate(provider, "ko", r["summary_en"], r["kb_implication_en"])
            if s or k:
                cur.execute("UPDATE articles_raw SET summary_ko=?, kb_implication=? WHERE article_id=?",
                            (s, k, r["article_id"]))
                stats["ko"] += 1
        elif has_ko and not has_en:                     # KO(레거시) → EN
            s, k = _translate(provider, "en", r["summary_ko"], r["kb_implication"])
            if s or k:
                cur.execute("UPDATE articles_raw SET summary_en=?, kb_implication_en=? WHERE article_id=?",
                            (s, k, r["article_id"]))
                stats["en"] += 1
        conn.commit()   # 증분 커밋

    log.info("번역 완료 — 대상=%d  KO채움=%d  EN채움=%d", stats["total"], stats["ko"], stats["en"])
    return stats
