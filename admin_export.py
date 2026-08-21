"""
관리자 페이지 데이터 export (admin_export.py)

수집 데이터(기사·소스·통과현황)를 관리자가 조회하도록 JSON으로 내보내고,
템플릿(web/admin.html)에 주입해 오프라인에서 바로 열리는 자기완결 HTML을 생성한다.

산출물:
  data/export/admin.json  — 데이터 계약(GitHub Pages fetch용)
  data/export/admin.html  — admin.json을 주입한 자기완결 페이지(더블클릭 열람)

설계: 화면분석_개발가이드.md / STATUS.md (go-forward 정적 배포)
사용: python admin_export.py   (main.py 복원 후 서브커맨드로 편입 가능)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import config
import db

log = logging.getLogger("admin_export")

RECENT_LIMIT = 1000            # 기사 테이블에 담을 최근 표본 수
TEMPLATE = config.ROOT / "web" / "admin.html"


def build_admin(conn) -> dict:
    cur = conn.cursor()

    def one(sql, args=()):
        return cur.execute(sql, args).fetchone()

    total     = one("SELECT COUNT(*) n FROM articles_raw")["n"]
    passed    = one("SELECT COUNT(*) n FROM articles_raw WHERE filter_decision='passed'")["n"]
    rejected  = one("SELECT COUNT(*) n FROM articles_raw WHERE filter_decision='rejected'")["n"]
    pending   = one("SELECT COUNT(*) n FROM articles_raw WHERE filter_decision='pending'")["n"]
    dups      = one("SELECT COUNT(*) n FROM articles_raw WHERE duplicate_of IS NOT NULL")["n"]
    ai_scored = one("SELECT COUNT(*) n FROM articles_raw WHERE ai_score IS NOT NULL")["n"]
    drange    = one("SELECT MIN(published_at) mn, MAX(published_at) mx FROM articles_raw")

    overview = dict(
        total=total, passed=passed, rejected=rejected, pending=pending,
        duplicates=dups, ai_scored=ai_scored,
        date_min=(drange["mn"] or "")[:10], date_max=(drange["mx"] or "")[:10],
    )

    countries = [
        dict(cc=r["cc"], raw=r["raw"], passed=r["passed"] or 0)
        for r in cur.execute("""
            SELECT m.primary_country_code cc, COUNT(*) raw,
                   SUM(a.filter_decision='passed') passed
            FROM articles_raw a JOIN media_sources m ON m.source_id=a.source_id
            GROUP BY 1 ORDER BY raw DESC""")
    ]

    sources = [
        dict(media=r["media"], cc=r["cc"], tier=r["tier"], raw=r["raw"], passed=r["passed"] or 0)
        for r in cur.execute("""
            SELECT m.media_name media, m.primary_country_code cc, m.tier tier,
                   COUNT(*) raw, SUM(a.filter_decision='passed') passed
            FROM articles_raw a JOIN media_sources m ON m.source_id=a.source_id
            GROUP BY m.source_id ORDER BY raw DESC""")
    ]

    articles = [
        dict(
            id=r["article_id"], cc=r["cc"], src=r["media"], tier=r["tier"],
            date=(r["published_at"] or "")[:10], t=r["title"],
            decision=r["filter_decision"], score=r["filter_score"],
            ai=r["ai_score"], topics=(r["topics"] or "")[:60], url=r["link"],
        )
        for r in cur.execute("""
            SELECT a.article_id, a.title, a.link, a.published_at, a.filter_decision,
                   a.filter_score, a.ai_score, a.topics, m.media_name media,
                   m.primary_country_code cc, m.tier tier
            FROM articles_raw a JOIN media_sources m ON m.source_id=a.source_id
            ORDER BY a.fetched_at DESC LIMIT ?""", (RECENT_LIMIT,))
    ]

    return dict(
        generated_at=datetime.now(timezone.utc).isoformat(),
        overview=overview, countries=countries, sources=sources, articles=articles,
    )


def export_admin(conn) -> dict:
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_admin(conn)

    # 1) 데이터 계약 JSON
    json_path = config.EXPORT_DIR / "admin.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    # 2) 템플릿에 데이터 주입한 자기완결 HTML
    html_path = config.EXPORT_DIR / "admin.html"
    if TEMPLATE.exists():
        tpl = TEMPLATE.read_text(encoding="utf-8")
        # '<' 를 이스케이프해 </script> breakout 방지 (JSON 문자열 내에서 유효)
        data_js = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
        placeholder = '<script id="admin-data" type="application/json">null</script>'
        injected = f'<script id="admin-data" type="application/json">{data_js}</script>'
        tpl = tpl.replace(placeholder, injected)
        html_path.write_text(tpl, encoding="utf-8")
    else:
        log.warning("템플릿 없음: %s (JSON만 생성)", TEMPLATE)

    result = dict(
        articles=len(payload["articles"]), sources=len(payload["sources"]),
        countries=len(payload["countries"]),
        json=str(json_path), html=str(html_path) if TEMPLATE.exists() else None,
    )
    log.info("admin export — %s", result)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    conn = db.open_conn()
    r = export_admin(conn)
    print("관리자 페이지 생성:")
    print(f"  기사 {r['articles']} · 소스 {r['sources']} · 국가 {r['countries']}")
    print(f"  JSON: {r['json']}")
    print(f"  HTML: {r['html']}")
