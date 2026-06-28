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


def export_countries(conn) -> dict:
    """ACTIVE(ai_score>=임계) 기사를 국가별 JSON으로 내보낸다."""
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    countries = []
    total = 0

    for cc, flag in _FLAGS.items():
        rows = conn.execute(
            """
            SELECT a.title, a.summary_ko, a.kb_implication, a.topics, a.link,
                   a.published_at, a.ai_score, m.media_name
            FROM articles_raw a
            JOIN media_sources m ON m.source_id = a.source_id
            WHERE m.primary_country_code = ?
              AND a.ai_score >= ?
              AND a.duplicate_of IS NULL
            ORDER BY a.ai_score DESC, a.published_at DESC
            LIMIT 20
            """,
            (cc, config.AI_SCORE_ACTIVE_THRESHOLD),
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
        "active_threshold": config.AI_SCORE_ACTIVE_THRESHOLD,
        "countries": countries,
    }
    path = config.EXPORT_DIR / "countries.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("countries.json 작성 — 국가=%d  ACTIVE 기사=%d  → %s", len(countries), total, path)
    return {"countries": len(countries), "articles": total, "path": str(path)}
