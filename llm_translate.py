"""
번역 (llm_translate.py)

영어 기준본(summary_en·kb_implication_en)을 한국어(summary_ko·kb_implication)로 번역.
원문이 대부분 영어라 rank 는 영어로 분석·요약(canonical)하고, 이 단계가 한국어를 채운다.

원칙:
  - 대상은 '화면에 표시되는' 기사 → 비용 최소화. 단 '표시'의 기준이 화면마다 다르다:
      · 진출국 화면      : ACTIVE(ai_score >= 임계)
      · 미진출국·모니터링 : **임계 없음**(export._compute_non_presence 주석 참조 —
        거점이 없어 점수 루브릭이 낮게 잡히므로 랭킹만 됐으면 노출)
    2026-09-08: 이 둘이 어긋나 미진출국 저점수 기사가 summary_ko 없이 노출됐고,
    UI 폴백(q || q_en)이 걸려 한국어 화면에 영어 요약이 그대로 보였다. 대상을 맞춘다.
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
    "ko": ("You are rewriting financial news into concise Korean for busy bankers. "
           "Rules: ① title_ko — 15자 이내, 핵심 사실만, 신문 헤드라인 스타일 "
           "② summary — 2문장 이내, 단문, '~다' 체. "
           "   중요: title_ko 내용을 절대 반복하지 말 것. title_ko가 다루지 않은 배경·수치·영향만 서술. "
           "③ kb_implication — 1문장, KB 거점 직접 영향만 "
           "Keep KB branch names (예: 뉴욕지점, 프라삭은행). "
           'Output ONLY JSON: {"title_ko": "...", "summary": "...", "kb_implication": "..."}'),
    "en": ("Translate the 'summary' and 'kb_implication' below into natural English "
           "(financial/business register). Keep KB branch/entity names. "
           'Output ONLY JSON: {"summary": "...", "kb_implication": "..."}'),
}


def ensure_columns(conn) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("summary_en",        "ALTER TABLE articles_raw ADD COLUMN summary_en        TEXT"),
        ("kb_implication_en", "ALTER TABLE articles_raw ADD COLUMN kb_implication_en TEXT"),
        ("title_ko",          "ALTER TABLE articles_raw ADD COLUMN title_ko          TEXT"),
    ])


def _user(summary, kb, title=""):
    lines = []
    if title:
        lines.append(f"title: {title[:200]}")
    lines.append(f"summary: {(summary or '')[:1500]}")
    lines.append(f"kb_implication: {(kb or '')[:1000]}")
    return "\n".join(lines)


def run_translate(conn, provider: LLMProvider | None = None,
                  limit: int | None = None, days: int | None = None,
                  use_batch: bool | None = None) -> dict:
    """표시분(ACTIVE) 중 한쪽 언어가 비어있는 기사를 번역해 양 언어를 채운다.

    use_batch: None=배치(50% 할인, 기본) / False=동기 호출(디버깅).
    """
    ensure_columns(conn)
    provider = provider or get_provider("fast", use_batch=use_batch)   # 저비용(Haiku)
    limit = limit or 400   # 미진출국까지 대상에 들어와 한 번에 처리할 물량이 늘었다

    date_clause, params = db.days_clause_now(days, alias="a")   # JOIN 추가로 별칭 명시

    # 노출 대상 = ACTIVE(진출국 기준) 또는 미진출국의 채점 완료분(임계 없이 노출됨).
    np_in = ",".join("?" * len(config.NON_PRESENCE_CODES))

    rows = conn.execute(
        f"""
        SELECT a.article_id, a.title, a.summary_en, a.kb_implication_en,
               a.summary_ko, a.kb_implication, a.title_ko
        FROM articles_raw a
        JOIN media_sources m ON m.source_id = a.source_id
        WHERE a.duplicate_of IS NULL
          AND ( a.ai_score >= ?
             OR (m.primary_country_code IN ({np_in}) AND a.ai_score IS NOT NULL) )
          AND ( (COALESCE(a.summary_en,'')  <> '' AND COALESCE(a.summary_ko,'') = '')
             OR (COALESCE(a.summary_ko,'')  <> '' AND COALESCE(a.summary_en,'') = '')
             OR (COALESCE(a.summary_ko,'')  <> '' AND COALESCE(a.title_ko,'')   = '') )
          {date_clause}
        ORDER BY a.ai_score DESC
        LIMIT ?
        """,
        (config.AI_SCORE_ACTIVE_THRESHOLD, *config.NON_PRESENCE_CODES, *params, limit),
    ).fetchall()

    stats = dict(total=len(rows), ko=0, en=0)

    # 요청 일괄 구성(방향은 custom_id 에 인코딩) → 배치 제출(50% 할인) 또는 동기 폴백
    requests, meta = [], {}
    for i, r in enumerate(rows):
        has_en = bool((r["summary_en"] or "").strip())
        has_ko = bool((r["summary_ko"] or "").strip())
        has_title_ko = bool((r["title_ko"] or "").strip())
        if has_en and (not has_ko or not has_title_ko):   # EN(기준본) → KO + title_ko
            cid, target = str(i), "ko"
            src_s, src_k = r["summary_en"], r["kb_implication_en"]
        elif has_ko and not has_en:                        # KO(레거시) → EN
            cid, target = str(i), "en"
            src_s, src_k = r["summary_ko"], r["kb_implication"]
        else:
            continue
        requests.append((cid, _SYS[target], _user(src_s, src_k, r["title"]), 600))
        meta[cid] = (r["article_id"], target)

    results = provider.complete_json_batch(requests) if requests else {}

    cur = conn.cursor()
    for cid, (article_id, target) in meta.items():
        data = results.get(cid) or {}
        s = str(data.get("summary") or "")[:1500]
        k = str(data.get("kb_implication") or "")[:1000]
        if not (s or k):
            continue
        if target == "ko":
            t_ko = str(data.get("title_ko") or "")[:60]
            cur.execute("UPDATE articles_raw SET summary_ko=?, kb_implication=?, title_ko=? WHERE article_id=?",
                        (s, k, t_ko or None, article_id))
            stats["ko"] += 1
        else:
            cur.execute("UPDATE articles_raw SET summary_en=?, kb_implication_en=? WHERE article_id=?",
                        (s, k, article_id))
            stats["en"] += 1
    conn.commit()

    log.info("번역 완료 — 대상=%d  KO채움=%d  EN채움=%d", stats["total"], stats["ko"], stats["en"])
    return stats
