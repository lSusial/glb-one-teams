"""
DB → UI 데이터 계약(JSON) export (export_json.py)

화면 코딩과 무관한 백엔드 export. 설계: 화면분석_개발가이드.md / 데이터_AI_카테고리_설계.md
현재: countries.json(현지언론 Intelligence 화면) 구현. 나머지 화면(subs/topics/brief)은
동일 패턴으로 확장한다.

UI 매핑 (현지언론 기사 카드):
  c=topics→ui키, src=매체, d=날짜, t=제목, q=summary_ko, k=kb_implication, u=link
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import config
import taxonomy

log = logging.getLogger("export_json")

# 현지언론 화면 대상 거점 9개 (자회사 ID·KH 는 subsidiaries 화면으로 분리)
_FLAGS = {
    "GB": "🇬🇧", "US": "🇺🇸", "JP": "🇯🇵", "HK": "🇭🇰", "SG": "🇸🇬",
    "CN": "🇨🇳", "VN": "🇻🇳", "IN": "🇮🇳", "MM": "🇲🇲",
}


def export_countries(conn, active_only: bool = True) -> dict:
    """국가별 기사를 UI 데이터 계약(countries.json)으로 내보낸다.

    active_only=True  (기본, AI 실행 후): ai_score >= AI_SCORE_ACTIVE_THRESHOLD 인 ACTIVE 기사만.
    active_only=False (Phase 1, AI 없이): 키워드 필터 통과(passed) 기사. 요약(q)·시사점(k)은 비어있을 수 있음.
    """
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    if active_only:
        where, params_tail = "a.ai_score >= ? AND a.duplicate_of IS NULL", (config.AI_SCORE_ACTIVE_THRESHOLD,)
        order = "a.ai_score DESC, a.published_at DESC"
    else:
        where, params_tail = "a.filter_decision = 'passed' AND a.duplicate_of IS NULL", ()
        order = "a.published_at DESC"

    countries = []
    total = 0

    for cc, flag in _FLAGS.items():
        rows = conn.execute(
            f"""
            SELECT a.title, a.summary_ko, a.kb_implication, a.topics, a.link,
                   a.published_at, a.ai_score, m.media_name
            FROM articles_raw a
            JOIN media_sources m ON m.source_id = a.source_id
            WHERE m.primary_country_code = ?
              AND {where}
            ORDER BY {order}
            LIMIT 20
            """,
            (cc, *params_tail),
        ).fetchall()

        articles = []
        for a in rows:
            codes = [c for c in (a["topics"] or "").split(",") if c]
            articles.append({
                "c": taxonomy.ui_string(codes),
                "src": a["media_name"],
                "d": (a["published_at"] or "")[:10],
                "t": a["title"],
                "q": a["summary_ko"] or "",
                "k": a["kb_implication"] or "",
                "u": a["link"],
                "score": a["ai_score"],
            })
        total += len(articles)
        countries.append({
            "cc": cc,
            "flag": flag,
            "status": "ACTIVE" if articles else "SOURCE WATCH",
            "count": len(articles),
            "articles": articles,
        })

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "active" if active_only else "passed",
        "active_threshold": config.AI_SCORE_ACTIVE_THRESHOLD if active_only else None,
        "countries": countries,
    }
    path = config.EXPORT_DIR / "countries.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("countries.json 작성 — 국가=%d  ACTIVE 기사=%d  → %s", len(countries), total, path)
    return {"countries": len(countries), "articles": total, "path": str(path)}
