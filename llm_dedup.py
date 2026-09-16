"""
LLM 근접중복 판정 (llm_dedup.py, 2026-09-16)

토큰 유사도(keyword_filter의 duplicate_of, 제목 0.75)로 못 잡는 '같은 사건 다른 표현'
근접중복을 LLM 의미판단으로 묶는다.
예: "Rupee falls 30 paise to 95.84" vs "Rupee slumps 38 paise to 95.92" (같은 날 루피 하락).

범위: 노출 후보(최근 N일, ai_score 있고 아직 비중복)만 — 국가별로 묶어 국가당 1콜(저렴).
출력: 각 그룹의 최고 ai_score를 대표로, 나머지는 duplicate_of=대표id + dup_by_ai=1.
재실행 안전: dup_by_ai=1 만 리셋 후 다시 판정(키워드 dedup 결과는 건드리지 않음).
표시 피드(국가·모니터링)는 이미 duplicate_of IS NULL 로 거르므로 export 변경 불필요.
"""
from __future__ import annotations

import logging

import config
import db
from llm_provider import LLMProvider, get_provider

log = logging.getLogger("llm_dedup")

_SYSTEM = (
    "당신은 뉴스 근접중복 판정기다. 같은 국가 헤드라인 목록에서 '동일한 하나의 사건·발표를 "
    "보도한' 기사들을 그룹으로 묶는다.\n"
    "같은 사건 = 같은 구체적 사실(같은 날 같은 통화의 하락, 같은 인물의 같은 임명, 같은 거래/"
    "규제/발표). 수치·표현·매체·각도가 조금 달라도 근본 사건이 같으면 한 그룹.\n"
    "★ 다른 사건은 절대 묶지 마라: 다른 날짜·다른 주체·다른 지표·다른 종목이면 별개. "
    "애매하면 묶지 말고 각각 둔다(과합침 금지).\n"
    "JSON만 출력(설명 없이): {\"groups\": [[id, id, ...], ...]}  "
    "— 2건 이상 묶인 그룹만 넣고, 단독 기사는 생략."
)


def ensure_columns(conn) -> None:
    db.ensure_columns(conn, "articles_raw", [
        ("dup_by_ai", "ALTER TABLE articles_raw ADD COLUMN dup_by_ai INTEGER DEFAULT 0"),
    ])


def run_dedup(conn, provider: LLMProvider | None = None,
              days: int | None = 3, use_batch: bool | None = None,
              only_cc: str | None = None) -> dict:
    """노출 후보를 국가별로 LLM 근접중복 판정. only_cc 지정 시 그 국가만(디버그)."""
    ensure_columns(conn)
    provider = provider or get_provider("fast", use_batch=use_batch)

    # 이전 AI중복 리셋(재실행 안전) — 키워드 dedup(dup_by_ai=0)은 보존
    conn.execute("UPDATE articles_raw SET duplicate_of = NULL, dup_by_ai = 0 WHERE dup_by_ai = 1")
    conn.commit()

    date_clause, params = db.days_clause_now(days)
    rows = conn.execute(
        f"""
        SELECT a.article_id, COALESCE(a.title_ko, a.title) AS t, a.ai_score,
               COALESCE(a.primary_country, m.primary_country_code) AS cc
        FROM articles_raw a
        JOIN media_sources m ON m.source_id = a.source_id
        WHERE a.ai_score IS NOT NULL AND a.duplicate_of IS NULL
          AND a.ai_model LIKE '%:%'{date_clause}
        ORDER BY a.ai_score DESC
        """,
        params,
    ).fetchall()

    by_cc: dict[str, list] = {}
    for r in rows:
        cc = r["cc"] or "??"
        if only_cc and cc != only_cc:
            continue
        by_cc.setdefault(cc, []).append(r)

    requests, ctx = [], {}
    for cc, arts in by_cc.items():
        if len(arts) < 2:
            continue
        arts = arts[:40]   # 프롬프트 크기 제한(국가당 상위 40건)
        lines = "\n".join(f"{a['article_id']}: {a['t'][:90]}" for a in arts)
        cid = str(cc)
        requests.append((cid, _SYSTEM, f"[국가:{cc}] 헤드라인 목록:\n{lines}", 600))
        ctx[cid] = {a["article_id"]: (a["ai_score"] or 0) for a in arts}

    results = provider.complete_json_batch(requests) if requests else {}

    cur = conn.cursor()
    marked = 0
    for cid, res in results.items():
        scores = ctx.get(cid, {})
        for group in (res or {}).get("groups", []) or []:
            try:
                ids = [int(x) for x in group if int(x) in scores]
            except (TypeError, ValueError):
                continue
            if len(ids) < 2:
                continue
            rep = max(ids, key=lambda i: scores.get(i, 0))   # 최고 ai_score 대표
            for aid in ids:
                if aid == rep:
                    continue
                cur.execute(
                    "UPDATE articles_raw SET duplicate_of = ?, dup_by_ai = 1 WHERE article_id = ?",
                    (rep, aid),
                )
                marked += 1
    conn.commit()

    log.info("AI 중복판정 완료 — 국가=%d  중복마킹=%d건", len(requests), marked)
    return {"countries": len(requests), "marked": marked}
